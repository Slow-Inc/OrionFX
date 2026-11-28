"""Protocol v1's sealed windows (#132): fixed by the length rule on 2026-11-28 before any
statistic read the data. A real source's window may end after the seal only once the
holdout was opened, and then never reads the unread slice or past the holdout."""

from datetime import UTC, datetime

import pytest
from tests.support.seal import Opened

from app.market_data.sources import HOLDOUT, UNREAD_SLICE, Sealed, require_unsealed


def _t(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def test_the_windows_follow_the_30_month_length_rule() -> None:
    assert (_t(2024, 5, 28), _t(2026, 11, 28)) == HOLDOUT
    assert (_t(2023, 11, 28), _t(2024, 5, 28)) == UNREAD_SLICE


def test_development_data_is_free_and_unsealed_sources_are_not_checked() -> None:
    require_unsealed("dukascopy-xauusd", _t(2023, 11, 28), start=_t(2008, 1, 1))
    require_unsealed("dukascopy", _t(2030, 1, 1))  # synthetic-test key: not sealed
    require_unsealed(None, _t(2030, 1, 1))  # an unbound (live) version


def test_past_the_seal_needs_an_opened_holdout() -> None:
    with pytest.raises(Sealed, match="sealed from 2023-11-28"):
        require_unsealed("dukascopy-xauusd", _t(2023, 11, 29))
    require_unsealed("dukascopy-xauusd", HOLDOUT[1], start=HOLDOUT[0], opening=Opened())
    require_unsealed("dukascopy-xauusd", HOLDOUT[1], opening=Opened())  # a recorder: no start


def test_fresh_forward_data_after_both_windows_is_free() -> None:
    require_unsealed("dukascopy-xauusd", _t(2027, 6, 1), start=HOLDOUT[1])
    with pytest.raises(Sealed):
        require_unsealed("dukascopy-xauusd", _t(2027, 6, 1), start=_t(2026, 11, 27))


def test_an_opened_holdout_still_never_reads_the_unread_slice_or_past_the_holdout() -> None:
    with pytest.raises(Sealed, match="unread slice"):
        require_unsealed("dukascopy-xauusd", HOLDOUT[1], start=_t(2008, 1, 1), opening=Opened())
    with pytest.raises(Sealed, match="after the holdout"):
        require_unsealed("dukascopy-xauusd", _t(2026, 11, 29), start=HOLDOUT[0],
                         opening=Opened())  # fmt: skip
