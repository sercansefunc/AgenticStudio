"""Canonical, storage-independent Opportunity contract (version 1)."""

from decimal import Decimal
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import UUID4, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

OpportunityId = UUID4
Score = Annotated[int, Field(ge=0, le=100)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(allow_inf_nan=False)]
NonNegativeMoney = Annotated[Money, Field(ge=0)]
Minutes = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Status = Literal[
    "new", "rejected", "shortlisted", "investigating", "pursuing",
    "proposal_sent", "won", "lost", "delivered",
]


class DomainModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Source(DomainModel):
    name: str
    url: str
    external_id: str | None = None
    risk_class: Literal["core", "controlled", "experimental"]


class Problem(DomainModel):
    raw_text: str
    summary: str
    expected_deliverable: str | None = None


class Commercial(DomainModel):
    currency: str | None = None
    budget_min: NonNegativeMoney | None = None
    budget_max: NonNegativeMoney | None = None
    pricing_model: Literal["fixed", "hourly", "bounty", "unknown"] = "unknown"

    @model_validator(mode="after")
    def validate_budget_range(self) -> Self:
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_min > self.budget_max
        ):
            raise ValueError("budget_min must not exceed budget_max")
        return self


class Signals(DomainModel):
    repo_available: bool | None = None
    acceptance_clarity: Score
    competition_estimate: Score | None = None
    client_quality: Score | None = None
    strategic_reuse_value: Score


class Risk(DomainModel):
    technical_uncertainty: Score
    scope_uncertainty: Score
    maintenance_burden: Score
    legal_or_platform_risk: Score
    abuse_risk: Score
    payment_or_dispute_risk: Score


class Estimate(DomainModel):
    required_capabilities: tuple[str, ...] = ()
    human_minutes: Minutes | None = None
    execution_cost: NonNegativeMoney | None = None
    delivery_probability: Probability
    win_probability: Probability
    expected_value: Money | None = None
    confidence: Score


class Decision(DomainModel):
    status: Status = "new"
    reasons: tuple[str, ...] = ()


class Outcome(DomainModel):
    revenue_won: NonNegativeMoney | None = None
    revenue_collected: NonNegativeMoney | None = None
    actual_human_minutes: Minutes | None = None
    actual_execution_cost: NonNegativeMoney | None = None
    maintenance_incidents: Annotated[int, Field(ge=0)] | None = None
    dispute_or_refund: bool | None = None
    notes: str | None = None


class Opportunity(DomainModel):
    id: OpportunityId = Field(default_factory=uuid4)
    source: Source
    posted_at: AwareDatetime | None = None
    problem: Problem
    commercial: Commercial = Field(default_factory=Commercial)
    signals: Signals
    risk: Risk
    estimate: Estimate
    decision: Decision = Field(default_factory=Decision)
    outcome: Outcome = Field(default_factory=Outcome)
