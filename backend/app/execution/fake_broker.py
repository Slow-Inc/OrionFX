"""Deterministic in-memory broker for tests, replay and Shadow runs (PRD-06 #13).

Not a market simulator: fills happen at the current quote (+ configurable
slippage). Scripted outcomes reproduce the failure modes M06 must survive:
REJECT, PARTIAL(fraction), TIMEOUT_FILLED (it happened, the reply was lost),
TIMEOUT_LOST (nothing happened, the reply was lost).

Accounting follows MT5: commission is charged to balance on each deal (open and
close); a netting account holds at most one position per symbol, and an
opposite order reduces, closes or flips it.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

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


class Outcome(StrEnum):
    FILL = "FILL"
    REJECT = "REJECT"
    TIMEOUT_FILLED = "TIMEOUT_FILLED"
    TIMEOUT_LOST = "TIMEOUT_LOST"


@dataclass(frozen=True, slots=True)
class Partial:
    fraction: Decimal


Scripted = Outcome | Partial


@dataclass
class _Pos:
    position_id: str
    symbol: str
    side: Side
    volume: Decimal
    open_price: Decimal
    magic: int


def _opposite(side: Side) -> Side:
    return Side.SELL if side is Side.BUY else Side.BUY


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
        self._script: deque[Scripted] = deque()
        self._seq = 0

    # --- test controls ---------------------------------------------------------------

    def script(self, *outcomes: Scripted) -> None:
        for o in outcomes:
            if not isinstance(o, Outcome | Partial):
                raise TypeError(f"unknown scripted outcome {o!r}")
        self._script.extend(outcomes)

    def set_quote(self, symbol: str, *, bid: Decimal, ask: Decimal) -> None:
        self._quotes[symbol] = Quote(symbol, bid, ask, self.now)

    def add_manual_position(
        self, symbol: str, side: Side, volume: Decimal, *, magic: int = 0
    ) -> Position:
        q = self._require_quote(symbol)
        pid = self._next("pos")
        self._positions[pid] = _Pos(pid, symbol, side, volume, self._fill_price(q, side), magic)
        return self._view(self._positions[pid])

    def add_pending(
        self, symbol: str, side: Side, volume: Decimal, price: Decimal, *, magic: int = 0
    ) -> PendingOrder:
        order = PendingOrder(self._next("ord"), symbol, side, volume, price, magic)
        self._pending[order.order_id] = order
        return order

    def deposit(self, amount: Decimal) -> None:
        self._cash(amount)

    def withdraw(self, amount: Decimal) -> None:
        self._cash(-amount)

    # --- BrokerPort ----------------------------------------------------------------------

    def account(self) -> AccountInfo:
        positions = self.positions()
        floating = sum((p.profit + p.swap for p in positions), Decimal(0))
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

    def deals_since(self, after_deal_id: str | None) -> list[Deal]:
        """Deals after the given deal id (None = all). The cursor is a broker deal id."""
        if after_deal_id is None:
            return list(self._deals)
        ids = [d.deal_id for d in self._deals]
        return self._deals[ids.index(after_deal_id) + 1 :] if after_deal_id in ids else []

    def find_by_request(self, request_id: str) -> list[Deal]:
        return [d for d in self._deals if d.request_id == request_id]

    def send_open(self, req: OpenRequest) -> SendResult:
        if self.on_send:
            self.on_send(req.request_id)
        q = self._require_quote(req.symbol)
        outcome, early = self._begin()
        if early:
            return early
        volume = self._scaled(req.volume, outcome, req.symbol)
        price = self._fill_price(q, req.side)
        pid, realized = self._apply(req.symbol, req.side, volume, price)
        deal = self._trade_deal(req.request_id, pid, req.symbol, req.side, volume, price, realized)
        return self._finish(
            outcome, SendResult(SendStatus.ACCEPTED, pid, (deal.deal_id,), volume, price)
        )

    def send_close(self, req: CloseRequest) -> SendResult:
        if self.on_send:
            self.on_send(req.request_id)
        pos = self._positions.get(req.position_id)
        if pos is None:
            return SendResult(SendStatus.REJECTED, error="position not found")
        q = self._require_quote(pos.symbol)
        outcome, early = self._begin()
        if early:
            return early
        volume = min(self._scaled(req.volume, outcome, pos.symbol), pos.volume)
        side = _opposite(pos.side)
        price = self._fill_price(q, side)
        realized = self._reduce(pos, volume, price)
        deal = self._trade_deal(
            req.request_id, pos.position_id, pos.symbol, side, volume, price, realized
        )
        return self._finish(
            outcome,
            SendResult(SendStatus.ACCEPTED, pos.position_id, (deal.deal_id,), volume, price),
        )

    def cancel_order(self, order_id: str, request_id: str) -> SendResult:
        if self.on_send:
            self.on_send(request_id)
        outcome, early = self._begin()
        if early:
            return early
        if self._pending.pop(order_id, None) is None:
            return SendResult(SendStatus.REJECTED, error="order not found")
        return self._finish(outcome, SendResult(SendStatus.ACCEPTED))

    # --- internals -------------------------------------------------------------------------

    def _begin(self) -> tuple[Scripted, SendResult | None]:
        outcome = self._script.popleft() if self._script else Outcome.FILL
        if not self.connected:
            return outcome, SendResult(SendStatus.UNKNOWN, error="disconnected")
        if not self.market_open:
            return outcome, SendResult(SendStatus.REJECTED, error="market closed")
        if outcome is Outcome.REJECT:
            return outcome, SendResult(SendStatus.REJECTED, error="rejected by broker")
        if outcome is Outcome.TIMEOUT_LOST:
            return outcome, SendResult(SendStatus.UNKNOWN, error="timeout")
        return outcome, None

    @staticmethod
    def _finish(outcome: Scripted, result: SendResult) -> SendResult:
        if outcome is Outcome.TIMEOUT_FILLED:
            return SendResult(SendStatus.UNKNOWN, error="timeout")
        return result

    def _scaled(self, volume: Decimal, outcome: Scripted, symbol: str) -> Decimal:
        if isinstance(outcome, Partial):
            step = self.specs[symbol].volume_step
            return (volume * outcome.fraction).quantize(step, rounding=ROUND_DOWN)
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

    def _reduce(self, pos: _Pos, volume: Decimal, price: Decimal) -> Decimal:
        realized = self._profit(pos.side, pos.open_price, price, volume, pos.symbol)
        pos.volume -= volume
        if pos.volume == 0:
            del self._positions[pos.position_id]
        return realized

    def _apply(
        self, symbol: str, side: Side, volume: Decimal, price: Decimal
    ) -> tuple[str, Decimal]:
        """Open (hedging) or net against the symbol's single position (netting).
        Returns (position id, realized P&L of any reduced volume)."""
        if self.margin_mode is MarginMode.NETTING:
            pos = next((p for p in self._positions.values() if p.symbol == symbol), None)
            if pos is not None:
                if pos.side is side:
                    total = pos.volume + volume
                    pos.open_price = (pos.open_price * pos.volume + price * volume) / total
                    pos.volume = total
                    return pos.position_id, Decimal(0)
                reduce = min(volume, pos.volume)
                pid = pos.position_id
                realized = self._reduce(pos, reduce, price)
                left = volume - reduce
                if left > 0:  # flip: the same netting position id continues on the other side
                    self._positions[pid] = _Pos(pid, symbol, side, left, price, BOT_MAGIC)
                return pid, realized
        pid = self._next("pos")
        self._positions[pid] = _Pos(pid, symbol, side, volume, price, BOT_MAGIC)
        return pid, Decimal(0)

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
            commission=Decimal(0),
            swap=Decimal(0),
        )

    def _trade_deal(
        self,
        request_id: str,
        position_id: str,
        symbol: str,
        side: Side,
        volume: Decimal,
        price: Decimal,
        realized: Decimal,
    ) -> Deal:
        commission = (volume * self.commission_per_lot).quantize(CENT)
        self.balance += realized - commission
        deal = Deal(
            deal_id=self._next("deal"),
            request_id=request_id,
            position_id=position_id,
            symbol=symbol,
            side=side,
            volume=volume,
            price=price,
            profit=realized,
            commission=commission,
            swap=Decimal(0),
            magic=BOT_MAGIC,
            time=self.now,
            is_balance=False,
        )
        self._deals.append(deal)
        return deal

    def _cash(self, amount: Decimal) -> None:
        self.balance += amount
        self._deals.append(
            Deal(
                deal_id=self._next("deal"),
                request_id=None,
                position_id=None,
                symbol=None,
                side=None,
                volume=Decimal(0),
                price=None,
                profit=amount,
                commission=Decimal(0),
                swap=Decimal(0),
                magic=0,
                time=self.now,
                is_balance=True,
            )
        )

    def _next(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"
