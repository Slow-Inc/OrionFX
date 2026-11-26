from datetime import timedelta

from app.core.ids import new_id
from app.core.time import utc_now


def test_new_id_prefix_and_uniqueness() -> None:
    a, b = new_id("cycle"), new_id("cycle")
    assert a.startswith("cycle-") and a != b


def test_utc_now_is_aware_utc() -> None:
    assert utc_now().utcoffset() == timedelta(0)
