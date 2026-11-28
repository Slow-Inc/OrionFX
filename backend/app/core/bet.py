"""The bet M05's expected value prices and the M07 triple-barrier label measures (#129).

One place for what both sides must agree on, so they cannot drift:
- TARGET_ATR: the barrier distance in ATR_M15, the same for TP and SL;
- SLIPPAGE: what every fill is worse by;
- LABEL_VERSION: the label a forecast's class probabilities must be calibrated on;
- the round-trip `UnitCost` and the hurdle `cost_over_atr`.

A PROXY, not the real trade: the system has no per-position SL/TP (RSK-05). Real exits
are the Emergency SL, the USD cycle Profit Target and basket trailing. Only the system
backtest that replays those exits (#131) can say whether a model is usable.
"""

from dataclasses import dataclass
from decimal import Decimal

__all__ = [
    "LABEL_VERSION",
    "SLIPPAGE",
    "STRESSED_COST",
    "TARGET_ATR",
    "UnitCost",
    "cost_over_atr",
]

D = Decimal
TARGET_ATR = D(1)
LABEL_VERSION = "m07.labels.tb.v1"
SLIPPAGE = D("0.05")
"""Price units every fill is worse by [PROPOSED]."""


@dataclass(frozen=True, slots=True)
class UnitCost:
    """The round-trip cost of one unit (USD per oz)."""

    spread: Decimal
    """Paid once: a long buys at ask, a short buys back at ask."""
    slippage: Decimal
    """Per fill; a round trip has two."""
    commission: Decimal
    """Round trip, per unit: 2 x commission per lot / contract size."""

    @property
    def fills(self) -> Decimal:
        """What the fills cost: inside the label's TP and SL payoffs, paid by a FLAT."""
        return self.spread + 2 * self.slippage


STRESSED_COST = UnitCost(spread=D("0.80"), slippage=D("0.10"), commission=D("0.07"))
"""A pre-declared stressed cost for the claim protocol: the widest spread M06 accepts
(`max_spread`), twice the label's slippage, and 3.5 USD per lot per side on 100 oz
[PROPOSED]."""


ALL_FLAT = D(1)


def cost_over_atr(cost: UnitCost, atr: Decimal, *, flat: Decimal = ALL_FLAT) -> Decimal:
    """The edge `p_side - p_opposite` must beat for a positive EV, in barrier units.

    The label's TP and SL fills already pay `cost.fills`, so only a FLAT (probability
    `flat`) pays them on top of its drift, which EV takes as 0; the commission is always
    paid. The default `flat=1` charges every cost on every outcome: the conservative
    break-even a protocol can use per era without the FLAT probability."""
    return (flat * cost.fills + cost.commission) / (TARGET_ATR * atr)
