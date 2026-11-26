"""The broker port: everything M06 needs from a broker, and nothing more (PRD-06 #13).

The real MT5 adapter (#17, blocked: no MT5 on this machine) and the fake broker
implement the same `BrokerPort`. Every write carries the engine's `request_id`
so an unknown outcome can be reconciled against broker deals later.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.core.enums import Side

BOT_MAGIC = 770001
"""Magic number on every bot order [PROPOSED]. Never sufficient alone to prove ownership."""


class MarginMode(StrEnum):
    HEDGING = "HEDGING"
    NETTING = "NETTING"


class SendStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class AccountInfo:
    login: str
    server: str
    currency: str
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    margin_mode: MarginMode
    connected: bool


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    symbol: str
    contract_size: Decimal
    tick_size: Decimal
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    margin_per_lot: Decimal
    trade_allowed: bool


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    time: datetime


@dataclass(frozen=True, slots=True)
class Position:
    position_id: str
    symbol: str
    side: Side
    volume: Decimal
    open_price: Decimal
    magic: int
    profit: Decimal
    commission: Decimal
    swap: Decimal


@dataclass(frozen=True, slots=True)
class PendingOrder:
    order_id: str
    symbol: str
    side: Side
    volume: Decimal
    price: Decimal
    magic: int


@dataclass(frozen=True, slots=True)
class Deal:
    deal_id: str
    request_id: str | None
    position_id: str | None
    symbol: str | None
    side: Side | None
    volume: Decimal
    price: Decimal | None
    profit: Decimal
    commission: Decimal
    """A cost, >= 0. MT5 reports it negative; the adapter negates it."""
    swap: Decimal
    """Signed: positive is credited."""
    magic: int
    time: datetime
    is_balance: bool
    """A deposit/withdrawal/credit: cash flow, never trading P&L (AT-23)."""


@dataclass(frozen=True, slots=True)
class OpenRequest:
    request_id: str
    symbol: str
    side: Side
    volume: Decimal
    cycle_id: str | None
    intent_id: str | None = None
    emergency_sl: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CloseRequest:
    request_id: str
    position_id: str
    volume: Decimal
    cycle_id: str | None = None


@dataclass(frozen=True, slots=True)
class SendResult:
    status: SendStatus
    position_id: str | None = None
    deal_ids: tuple[str, ...] = ()
    filled_volume: Decimal = Decimal(0)
    price: Decimal | None = None
    error: str | None = None


class BrokerPort(Protocol):
    def account(self) -> AccountInfo: ...
    def symbol_spec(self, symbol: str) -> SymbolSpec: ...
    def quote(self, symbol: str) -> Quote | None: ...
    def positions(self) -> list[Position]: ...
    def pending_orders(self) -> list[PendingOrder]: ...
    def deals_since(self, after_deal_id: str | None) -> list[Deal]: ...
    def find_by_request(self, request_id: str) -> list[Deal]: ...
    def send_open(self, req: OpenRequest) -> SendResult: ...
    def send_close(self, req: CloseRequest) -> SendResult: ...
    def cancel_order(self, order_id: str, request_id: str) -> SendResult: ...
