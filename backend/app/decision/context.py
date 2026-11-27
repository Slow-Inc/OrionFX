"""What a strategy may see when it proposes (#52; DEC-01, UST-03).

Built once at `now` by the decision loop; strategies are pure functions of it.
`price` and `spread` come from the current market snapshot; `initial_lot` is the
owner's (TRD-02): a strategy never sizes the opening trade.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.contracts import FeatureSnapshot
from app.core.enums import Mode


@dataclass(frozen=True, slots=True)
class DecisionContext:
    now: datetime
    mode: Mode
    snapshot: FeatureSnapshot
    price: Decimal | None
    spread: Decimal | None
    initial_lot: Decimal
    allowed: frozenset[str] = frozenset()
    """Strategy ids that passed the gate; composites vote only with these."""

    def value(self, key: str) -> Decimal | None:
        return self.snapshot.values.get(key)
