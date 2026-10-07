"""Shared scenario settings and stable application version."""

from dataclasses import dataclass

VERSION = "1.0.1"


@dataclass(frozen=True)
class Scenario:
    """USD budget is available for new commitments in the selected month."""

    budget_cents: int = 5_000_000
    lead_buffer_days: int = 0
    service_level: float = 0.95
    transfer_lead_days: int = 2
    transfer_cost_cents_per_unit: int = 75
    target_days: int = 30
    store_target_days: int = 7
    policy: str = "proposed"

    def __post_init__(self) -> None:
        if self.budget_cents < 0 or not -15 <= self.lead_buffer_days <= 30:
            raise ValueError("Budget or lead buffer is outside the supported range")
        if not 0.85 <= self.service_level <= 0.98:
            raise ValueError("Service level must be between 0.85 and 0.98")
        if self.policy not in {"proposed", "baseline"}:
            raise ValueError("Unknown purchasing policy")
        if self.transfer_cost_cents_per_unit < 0:
            raise ValueError("Transfer unit cost must be nonnegative")
