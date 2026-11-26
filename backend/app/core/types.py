"""Value types shared by every contract (master plan §8.1 units).

Money, lots, prices and thresholds are Decimal; a float is rejected so that a
binary rounding error can never enter money arithmetic. Unknown values are None,
never zero.
"""

from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, AwareDatetime, BeforeValidator, Field

from app.core.time import require_utc


def _reject_float(value: Any) -> Any:
    if isinstance(value, (bool, float)):
        raise ValueError("float/bool not allowed for decimal values; pass a string or Decimal")
    return value


Dec = Annotated[Decimal, BeforeValidator(_reject_float)]
"""Signed decimal (money in USD or labelled account currency, P&L, EV)."""

Money = Dec
PositiveMoney = Annotated[Dec, Field(gt=0)]
"""E.g. the Profit Target: a positive USD amount (PFT-01)."""
Lot = Annotated[Dec, Field(gt=0)]
Price = Annotated[Dec, Field(gt=0)]
NonNegative = Annotated[Dec, Field(ge=0)]
Percent = Annotated[Dec, Field(ge=0, le=100)]
"""Drawdown and similar percentages, 0-100."""
Probability = Annotated[Dec, Field(ge=0, le=1)]
"""0-1. Carried inside `contracts.Confidence` together with what it means."""

UtcDatetime = Annotated[AwareDatetime, AfterValidator(require_utc)]
