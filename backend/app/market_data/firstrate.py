"""FirstRate Data gold futures (GC) 1-minute bars (#128, PRD #120 slice 1; DAT-01).

The format comes from FirstRate's public pages, read 2026-11-28 (sources and quotes:
`docs/research/2026-11-28-xauusd-data-sources.md` §4). No real file has been seen yet,
so the reader refuses anything that does not fit:
- Comma-separated rows `DateTime,Open,High,Low,Close,Volume`. The pages disagree on a
  header line, so a first line naming those six columns is skipped; any other line must
  be a bar.
- `DateTime` is `yyyy-MM-dd HH:mm:ss`, the bar's **start**, in US Eastern time (EST or
  EDT). It is converted to UTC through the America/New_York zone. Both US clock changes
  fall on a Sunday at 02:00 local time, when CME Globex is closed, so an ambiguous or
  non-existent Eastern time means the file is not what we think: the file is refused.
- Prices are exchange **trades**, not bid quotes, and there is no spread (`spread` is
  None). Volume is contracts traded and may have decimals.
- Minutes with no trade are left out (FirstRate: "a missing minute means there was no
  trading in it"). The importer's no-trade rule (`NO_TRADE_FILL`) handles them.
- Only the ratio-adjusted continuous 1-minute series is read (file name
  `..._1min_continuous_ratio_adjusted...`). Its multiplicative back-adjustment leaves
  returns and other scale-free features unchanged; the unadjusted series jumps at every
  roll and the absolute-adjusted one changes percentage moves. Old prices are rescaled at
  every new roll, so a later download is a new dataset version, never an append.

A malformed row refuses the whole file, as in `CsvM1Source`. Nothing here downloads.
"""

import csv
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from app.market_data.history import open_text
from app.market_data.port import M1Bar

__all__ = ["FirstRateGcSource"]

EASTERN = ZoneInfo("America/New_York")
SERIES = "_1min_continuous_ratio_adjusted"
HEADER = ("open", "high", "low", "close", "volume")
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class FirstRateGcSource:
    def __init__(self, path: Path) -> None:
        if SERIES not in path.name.lower():
            raise ValueError(f"{path.name}: only the ratio_adjusted 1min continuous series is read")
        self.path = path

    def m1_bars(self, start: datetime, end: datetime) -> Iterator[M1Bar]:
        with open_text(self.path) as f:
            for line, row in enumerate(csv.reader(f), start=1):
                if line == 1 and _is_header(row):
                    continue
                bar = self._parse(row, line)
                if start <= bar.open_time < end:
                    yield bar

    def _parse(self, row: list[str], line: int) -> M1Bar:
        try:
            t, o, h, lo, c, v = row
            return M1Bar(_utc(datetime.strptime(t, TIME_FORMAT)), Decimal(o), Decimal(h),
                         Decimal(lo), Decimal(c), Decimal(v), spread=None)  # fmt: skip
        except (ValueError, InvalidOperation) as exc:
            raise ValueError(f"{self.path} line {line}: {exc}") from exc


def _is_header(row: list[str]) -> bool:
    names = tuple(x.strip().lower() for x in row)
    return len(names) == 6 and names[0] in ("timestamp", "datetime") and names[1:] == HEADER


def _utc(local: datetime) -> datetime:
    """An Eastern wall time in UTC; refused if the clock change makes it ambiguous or
    skips it (the two folds then disagree on the offset)."""
    first, second = local.replace(tzinfo=EASTERN), local.replace(tzinfo=EASTERN, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(f"{local} is ambiguous or does not exist in US Eastern time")
    return first.astimezone(UTC)
