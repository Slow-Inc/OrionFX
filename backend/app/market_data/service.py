"""Ingest: market-data port -> candle engine -> point-in-time store + journal (#32).

Reads ticks after a cursor, builds bars, stores each final bar and publishes it
as `candle.closed`, both in one transaction per tick. The event id is the bar's
store identity and `occurred_at` is when the bar became available, so replaying
the same ticks gives identical events and never duplicates one, and a store
written without its events is repaired by a replay. A bar the engine started
mid-interval (the first after a start or resume) is partial and never stored.
A CandleConflict rolls the whole tick back and propagates: the engine has moved
on, so rebuild it and replay from a bar boundary. Returns the last cursor read.
"""

from app.core.enums import Mode
from app.core.events import make_event
from app.market_data.candles import CandleEngine
from app.market_data.port import MarketDataPort
from app.market_data.store import CandleStore
from app.storage.db import transaction
from app.storage.journal import Journal


def ingest(
    source: MarketDataPort,
    engine: CandleEngine,
    store: CandleStore,
    *,
    journal: Journal,
    cursor: int = 0,
    limit: int = 10_000,
    mode: Mode = Mode.SHADOW,
    account_id: str = "local-account",
    bot_id: str = "bot-1",
) -> int:
    ticks = source.ticks_since(cursor, limit=limit)
    for _, tick in ticks:
        with transaction(store.conn):
            for closed in engine.on_tick(tick):
                if closed.partial:
                    continue
                bar = closed.event
                store.save(bar)
                journal.append(
                    make_event(
                        "candle.closed",
                        source="M02",
                        payload=bar,
                        account_id=account_id,
                        bot_id=bot_id,
                        mode=mode,
                        event_id=store.event_id(bar),
                        occurred_at=bar.available_at,
                    )
                )
    return ticks[-1][0] if ticks else cursor
