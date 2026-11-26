"""Deterministic in-memory broker for tests, replay and Shadow runs (PRD-06 #13).

Not a market simulator: fills happen at the current quote (+ configurable
slippage). Scripted outcomes reproduce the failure modes M06 must survive:
`reject`, `partial:<fraction>`, `timeout_filled` (the order went through but the
reply was lost), `timeout_lost` (nothing happened, reply lost).
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from app.core.enums import Side
from app.execution.broker import (
    BOT_MAGIC,
    AccountInfo,
    CloseRequest,
    Deal,
    MarginMode,
    OpenRequest,
    PendingOrder,
    Position,
    Quote,
    SendResult,
    SendStatus,
    SymbolSpec,
)

CENT = Decimal("0.01")


@dataclass
class _Pos:
    position_id: str
    symbol: str
    side: Side
    volume: Decimal
    open_price: Decimal
    magic: int
    commission: Decimal


class FakeBroker:
    def __init__(
        self,
        *,
        balance: Decimal,
        now: datetime,
        margin_mode: MarginMode = MarginMode.HEDGING,
        commission_per_lot: Decimal = Decimal(0),
        slippage: Decimal = Decimal(0),
    ) -> None:
        self.balance = balance
        self.now = now
        self.margin_mode = margin_mode
        self.commission_per_lot = commission_per_lot
        self.slippage = slippage
        self.market_open = True
        self.connected = True
        self.on_send: Callable[[str], None] | None = None
        self.specs: dict[str, SymbolSpec] = {
            "XAUUSD": SymbolSpec(
                symbol="XAUUSD",
                contract_size=Decimal(100),
                tick_size=Decimal("0.01"),
                volume_min=Decimal("0.01"),
                volume_max=Decimal(100),
                volume_step=Decimal("0.01"),
                margin_per_lot=Decimal(400),
                trade_allowed=True,
            )
        }
        self._quotes: dict[str, Quote] = {}
        self._positions: dict[str, _Pos] = {}
        self._pending: dict[str, PendingOrder] = {}
        self._deals: list[Deal] = []
        self._script: deque[str] = deque()
        self._seq = 0

    # --- test controls ---------------------------------------------------------------

    def script(self, *outcomes: str) -> None:
        self._script.extend(outcomes)

    def set_quote(self, symbol: str, *, bid: Decimal, ask: Decimal) -> None:
        self._quotes[symbol] = Quote(symbol, bid, ask, self.now)

    def add_manual_position(
        self, symbol: str, side: Side, volume: Decimal, *, magic: int = 0
    ) -> Position:
        q = self._require_quote(symbol)
        pid = self._next("pos")
        self._positions[pid] = _Pos(
            pid, symbol, side, volume, self._fill_price(q, side), magic, Decimal(0)
        )
        return self._view(self._positions[pid])

    def add_pending(self, symbol: str, side: Side, volume: Decimal, price: Decimal) -> PendingOrder:
        order = PendingOrder(self._next("ord"), symbol, side, volume, price, BOT_MAGIC)
        self._pending[order.order_id] = order
        return order

    def deposit(self, amount: Decimal) -> None:
        self._cash(amount)

    def withdraw(self, amount: Decimal) -> None:
        self._cash(-amount)

    # --- BrokerPort ----------------------------------------------------------------------

    def account(self) -> AccountInfo:
        positions = self.positions()
        floating = sum((p.profit - p.commission + p.swap for p in positions), Decimal(0))
        equity = self.balance + floating
        margin = sum(
            (p.volume * self.specs[p.symbol].margin_per_lot for p in positions), Decimal(0)
        )
        return AccountInfo(
            login="demo-1",
            server="FakeBroker-Demo",
            currency="USD",
            balance=self.balance,
            equity=equity,
            margin=margin,
            free_margin=equity - margin,
            margin_mode=self.margin_mode,
            connected=self.connected,
        )

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        spec = self.specs[symbol]
        return replace(spec, trade_allowed=spec.trade_allowed and self.market_open)

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol)

    def positions(self) -> list[Position]:
        return [self._view(p) for p in self._positions.values()]

    def pending_orders(self) -> list[PendingOrder]:
        return list(self._pending.values())

    def deals_since(self, after_deal: int) -> list[Deal]:
        return self._deals[after_deal:]

    def find_by_request(self, request_id: str) -> list[Deal]:
        return [d for d in self._deals if d.request_id == request_id]

    def send_open(self, req: OpenRequest) -> SendResult:
        if self.on_send:
            self.on_send(req.request_id)
        outcome, early = self._begin()
        if early:
            return early
        volume = self._scaled(req.volume, outcome)
        q = self._require_quote(req.symbol)
        price = self._fill_price(q, req.side)
        commission = (volume * self.commission_per_lot).quantize(CENT)
        pid = self._merge_or_open(req.symbol, req.side, volume, price, commission)
        deal = self._deal(
            req.request_id, pid, req.symbol, req.side, volume, price, Decimal(0), commission
        )
        return self._finish(
            outcome, SendResult(SendStatus.ACCEPTED, pid, (deal.deal_id,), volume, price)
        )

    def send_close(self, req: CloseRequest) -> SendResult:
        if self.on_send:
            self.on_send(req.request_id)
        pos = self._positions.get(req.position_id)
        if pos is None:
            return SendResult(SendStatus.REJECTED, error="position not found")
        outcome, early = self._begin()
        if early:
            return early
        volume = min(self._scaled(req.volume, outcome), pos.volume)
        q = self._require_quote(pos.symbol)
        closing_side = Side.SELL if pos.side is Side.BUY else Side.BUY
        price = self._fill_price(q, closing_side)
        profit = self._profit(pos.side, pos.open_price, price, volume, pos.symbol)
        commission = (volume * self.commission_per_lot).quantize(CENT)
        self.balance += profit - commission
        pos.volume -= volume
        if pos.volume == 0:
            del self._positions[pos.position_id]
        deal = self._deal(
            req.request_id,
            pos.position_id,
            pos.symbol,
            closing_side,
            volume,
            price,
            profit,
            commission,
        )
        return self._finish(
            outcome,
            SendResult(SendStatus.ACCEPTED, pos.position_id, (deal.deal_id,), volume, price),
        )

    def cancel_order(self, order_id: str, request_id: str) -> SendResult:
        if self._pending.pop(order_id, None) is None:
            return SendResult(SendStatus.REJECTED, error="order not found")
        return SendResult(SendStatus.ACCEPTED)

    # --- internals -------------------------------------------------------------------------

    def _begin(self) -> tuple[str, SendResult | None]:
        outcome = self._script.popleft() if self._script else "fill"
        if not self.connected:
            return outcome, SendResult(SendStatus.UNKNOWN, error="disconnected")
        if not self.market_open:
            return outcome, SendResult(SendStatus.REJECTED, error="market closed")
        if outcome == "reject":
            return outcome, SendResult(SendStatus.REJECTED, error="rejected by broker")
        if outcome == "timeout_lost":
            return outcome, SendResult(SendStatus.UNKNOWN, error="timeout")
        return outcome, None

    @staticmethod
    def _finish(outcome: str, result: SendResult) -> SendResult:
        if outcome == "timeout_filled":  # it happened, but the reply never arrived
            return SendResult(SendStatus.UNKNOWN, error="timeout")
        return result

    @staticmethod
    def _scaled(volume: Decimal, outcome: str) -> Decimal:
        if outcome.startswith("partial:"):
            return (volume * Decimal(outcome.split(":", 1)[1])).quantize(Decimal("0.01"))
        return volume

    def _require_quote(self, symbol: str) -> Quote:
        q = self._quotes.get(symbol)
        if q is None:
            raise LookupError(f"no quote for {symbol}")
        return q

    def _fill_price(self, q: Quote, side: Side) -> Decimal:
        return q.ask + self.slippage if side is Side.BUY else q.bid - self.slippage

    def _profit(
        self, side: Side, open_price: Decimal, price: Decimal, volume: Decimal, symbol: str
    ) -> Decimal:
        move = price - open_price if side is Side.BUY else open_price - price
        return (move * volume * self.specs[symbol].contract_size).quantize(CENT)

    def _merge_or_open(
        self, symbol: str, side: Side, volume: Decimal, price: Decimal, commission: Decimal
    ) -> str:
        if self.margin_mode is MarginMode.NETTING:
            for pos in self._positions.values():
                if pos.symbol == symbol and pos.side is side:
                    total = pos.volume + volume
                    pos.open_price = (pos.open_price * pos.volume + price * volume) / total
                    pos.volume = total
                    pos.commission += commission
                    return pos.position_id
        pid = self._next("pos")
        self._positions[pid] = _Pos(pid, symbol, side, volume, price, BOT_MAGIC, commission)
        return pid

    def _view(self, p: _Pos) -> Position:
        q = self._require_quote(p.symbol)
        mark = q.bid if p.side is Side.BUY else q.ask
        return Position(
            position_id=p.position_id,
            symbol=p.symbol,
            side=p.side,
            volume=p.volume,
            open_price=p.open_price,
            magic=p.magic,
            profit=self._profit(p.side, p.open_price, mark, p.volume, p.symbol),
            commission=p.commission,
            swap=Decimal(0),
        )

    def _deal(
        self,
        request_id: str | None,
        position_id: str | None,
        symbol: str | None,
        side: Side | None,
        volume: Decimal,
        price: Decimal | None,
        profit: Decimal,
        commission: Decimal,
        *,
        is_balance: bool = False,
    ) -> Deal:
        deal = Deal(
            deal_id=self._next("deal"),
            request_id=request_id,
            position_id=position_id,
            symbol=symbol,
            side=side,
            volume=volume,
            price=price,
            profit=profit,
            commission=commission,
            swap=Decimal(0),
            magic=0 if is_balance else BOT_MAGIC,
            time=self.now,
            is_balance=is_balance,
        )
        self._deals.append(deal)
        return deal

    def _cash(self, amount: Decimal) -> None:
        self.balance += amount
        self._deal(None, None, None, None, Decimal(0), None, amount, Decimal(0), is_balance=True)

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"
