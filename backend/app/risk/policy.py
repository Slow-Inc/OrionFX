"""Protected risk policy (PRD-06 #14; RSK-03, RSK-09; master plan OD-04, OD-05).

Every value here is PROPOSED and UNCERTIFIED: it exists so Shadow and Demo can
run. None of it is a live qualification threshold. Neither owner settings nor
any AI can change it; a change is a reviewed code/policy change with a new
`policy_version`.
"""

from decimal import Decimal

from pydantic import Field

from app.core.contracts import ContractModel
from app.core.enums import Mode, RiskProfile
from app.core.types import Dec, Lot, NonNegative, Percent


class ProfileCaps(ContractModel):
    max_open_lots: Lot
    max_positions: int = Field(gt=0)


class RiskPolicy(ContractModel):
    policy_version: str
    certified: bool = False
    lifetime_ceiling_factor: Dec
    """Lifetime DD ceiling = min(owner max DD x factor, max_lifetime_ceiling_percent)."""
    max_lifetime_ceiling_percent: Percent
    profiles: dict[RiskProfile, ProfileCaps]
    max_spread: NonNegative
    """In quote currency (XAUUSD: USD per ounce)."""
    max_quote_age_seconds: int = Field(gt=0)
    min_free_margin_percent: Percent
    """Free margin left after the trade, as a percent of equity."""
    approval_ttl_seconds: int = Field(gt=0)
    martingale_modes: frozenset[Mode]
    """Loss-chasing size-ups (behaviour): Shadow/Demo only (RSK-06, CAT-X02-04)."""
    mode_restricted_strategies: dict[str, frozenset[Mode]]
    """Strategy ids allowed only in the listed modes (by name; behaviour is checked too)."""
    trailing_min_activation_fraction: Dec
    """Basket trailing may not activate below this fraction of the Profit Target."""
    trailing_max_activation_fraction: Dec
    """Nor at or above the target (< 1): activation there would switch trailing off."""
    trailing_min_distance_fraction: Dec
    trailing_max_distance_fraction: Dec
    trailing_default_activation_fraction: Dec
    trailing_default_distance_fraction: Dec

    def caps_for(self, profile: RiskProfile) -> ProfileCaps:
        return self.profiles[profile]

    def lifetime_ceiling(self, owner_max_dd: Decimal) -> Decimal:
        return min(owner_max_dd * self.lifetime_ceiling_factor, self.max_lifetime_ceiling_percent)


DEFAULT_POLICY = RiskPolicy(
    policy_version="risk-0.1-uncertified",
    lifetime_ceiling_factor=Decimal("1.5"),
    max_lifetime_ceiling_percent=Decimal(50),
    profiles={
        RiskProfile.LOW: ProfileCaps(max_open_lots=Decimal("0.05"), max_positions=3),
        RiskProfile.NORMAL: ProfileCaps(max_open_lots=Decimal("0.20"), max_positions=6),
        RiskProfile.HIGH: ProfileCaps(max_open_lots=Decimal("0.50"), max_positions=10),
    },
    max_spread=Decimal("0.80"),
    max_quote_age_seconds=5,
    min_free_margin_percent=Decimal(50),
    approval_ttl_seconds=5,
    martingale_modes=frozenset({Mode.SHADOW, Mode.DEMO}),
    mode_restricted_strategies={"martingale": frozenset({Mode.SHADOW, Mode.DEMO})},
    trailing_min_activation_fraction=Decimal("0.5"),
    trailing_max_activation_fraction=Decimal("0.9"),
    trailing_min_distance_fraction=Decimal("0.05"),
    trailing_max_distance_fraction=Decimal("0.5"),
    trailing_default_activation_fraction=Decimal("0.6"),
    trailing_default_distance_fraction=Decimal("0.2"),
)
