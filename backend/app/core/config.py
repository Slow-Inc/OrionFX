"""Owner configuration and protected policy (PRD-08 FR-004..006, master plan §8.5).

No ready-to-trade default: the three trading inputs stay None until the owner
sets them. Live permission is protected policy, never owner configuration.
"""

from typing import Literal

from pydantic import Field

from app.core.contracts import ContractModel
from app.core.enums import RiskProfile
from app.core.secrets import SecretRef
from app.core.types import Lot, Percent, PositiveMoney


class TradingConfig(ContractModel):
    symbol: str = "XAUUSD"
    initial_lot: Lot | None = None
    profit_target_usd: PositiveMoney | None = None
    max_drawdown_percent: Percent | None = Field(default=None, gt=0)
    order_scope: Literal["BOT_ONLY"] = "BOT_ONLY"
    drawdown_scope: Literal["ACCOUNT_EQUITY"] = "ACCOUNT_EQUITY"
    account_mode: Literal["SHADOW", "DEMO"] = "DEMO"
    risk_profile: RiskProfile = RiskProfile.LOW
    strategies_enabled: list[str] = Field(default_factory=list)


class LlmConfig(ContractModel):
    provider: str | None = None
    model: str | None = None
    api_key_ref: SecretRef | None = None
    news_handling: Literal["adaptive", "off"] = "adaptive"


class SystemConfig(ContractModel):
    deployment: Literal["local", "vps", "hybrid"] = "local"
    storage: Literal["local", "cloud", "hybrid"] = "local"
    bind_host: str = "127.0.0.1"
    dashboard_origin: str = "http://127.0.0.1:5173"


class UserConfig(ContractModel):
    trading: TradingConfig = Field(default_factory=TradingConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)


class ProtectedPolicy(ContractModel):
    """Not editable from settings (VAL-06). Changing it is a reviewed code/policy change."""

    policy_version: str = "policy-0.1"
    live_allowed: bool = False


_REQUIRED_TO_START = ("initial_lot", "profit_target_usd", "max_drawdown_percent")


def missing_for_start(cfg: UserConfig) -> list[str]:
    """Owner inputs that must be set before Start may be accepted (§8.5)."""
    return [f"trading.{name}" for name in _REQUIRED_TO_START if getattr(cfg.trading, name) is None]
