"""Ingest: market-data port -> candle engine -> point-in-time store + journal (#32).

Reads ticks after a cursor, builds bars, stores each newly final bar and
publishes it as `candle.closed`. The event id is derived from the bar's identity,
so replaying the same ticks never duplicates an event, a crash between storing
and publishing is repaired by the replay, and a stored bar is never rewritten.
Returns the last cursor read, to resume from.
"""

from app.core.enums import Mode
from app.core.events import make_event
from app.market_data.candles import CandleEngine
from app.market_data.port import MarketDataPort
from app.market_data.store import CandleStore
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
    last = cursor
    for last, tick in source.ticks_since(cursor, limit=limit):  # noqa: B007
        for closed in engine.on_tick(tick):
            bar = closed.event
            store.save(bar)  # idempotent; refuses a different bar for the same key
            # Always publish: the id is the bar's identity, so the journal drops a
            # repeat, and a crash between save and publish never loses the event.
            env = make_event(
                "candle.closed",
                source="M02",
                payload=bar,
                account_id=account_id,
                bot_id=bot_id,
                mode=mode,
            )
            journal.append(
                env.model_copy(
                    update={
                        "event_id": f"candle:{store.dataset_version}:{bar.symbol}:"
                        f"{bar.timeframe}:{bar.open_time.isoformat()}"
                    }
                )
            )
    return last
