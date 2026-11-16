"""Provider-neutral evaluation contracts; all returned values are predictions.

Actual observations belong to ``domain.Outcome`` and are never fields of a
prediction record. Evaluators must not update an Opportunity or its outcome.
Ranking policy and persistence are separate consumers of these contracts.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import UUID4, AwareDatetime, Field

from agenticstudio.domain import (
    DomainModel,
    Minutes,
    Money,
    NonNegativeMoney,
    Opportunity,
    OpportunityId,
    Probability,
    Risk,
    Score,
)


class EconomicPrediction(DomainModel):
    """Money estimates share the stated currency; None means unknown, not zero.

    Expected value may be negative. Its calculation is explained by rationale,
    rather than imposed by the evaluation interface.
    """

    currency: str | None = None
    expected_revenue: NonNegativeMoney | None = None
    execution_cost: NonNegativeMoney | None = None
    expected_value: Money | None = None
    win_probability: Probability
    confidence: Score
    rationale: tuple[str, ...]


class FeasibilityPrediction(DomainModel):
    """Delivery estimates and the capabilities needed to achieve them."""

    required_capabilities: tuple[str, ...] = ()
    human_minutes: Minutes | None = None
    delivery_probability: Probability
    confidence: Score
    rationale: tuple[str, ...]


class RiskPrediction(Risk):
    """Individual risk scores: 0 is lowest risk, 100 is highest risk."""

    confidence: Score
    rationale: tuple[str, ...]


class PredictionRecord(DomainModel):
    """Immutable evaluation snapshot, independently identifiable from outcomes.

    Confidence scores range from 0 (lowest) to 100 (highest). ``evaluated_at``
    is supplied by the evaluator and must include a timezone. Retain this record
    when later actuals are collected instead of overwriting its predictions.
    No provider, model, or ranking policy is required by this contract.
    """

    id: UUID4 = Field(default_factory=uuid4)
    opportunity_id: OpportunityId
    evaluated_at: AwareDatetime
    economics: EconomicPrediction
    feasibility: FeasibilityPrediction
    risk: RiskPrediction
    confidence: Score
    rationale: tuple[str, ...]


class Evaluator(Protocol):
    """Replaceable evaluation boundary consuming the canonical Opportunity.

    Return a new prediction linked to ``opportunity.id`` without mutating the
    input. Concrete implementations own their configuration and dependencies.
    """

    def evaluate(self, opportunity: Opportunity) -> PredictionRecord:
        """Predict feasibility, economics and risk for the given opportunity."""
        ...


class BaselineEvaluator:
    """Copy canonical evidence without deriving probabilities or expected value.

    Economics and feasibility retain estimate confidence. Risk confidence is
    zero: the canonical contract has no independent evidence of risk confidence,
    and estimate confidence alone does not substantiate individual risk scores.
    Overall confidence is the minimum dimension confidence, consequently zero
    until risk confidence can be substantiated by a future evaluation policy.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    def evaluate(self, opportunity: Opportunity) -> PredictionRecord:
        evaluated_at = self._clock()
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("Evaluation clock must return a timezone-aware datetime")
        commercial = opportunity.commercial
        estimate = opportunity.estimate
        economics = EconomicPrediction(
            currency=commercial.currency,
            expected_revenue=commercial.budget_min,
            execution_cost=estimate.execution_cost,
            expected_value=estimate.expected_value,
            win_probability=estimate.win_probability,
            confidence=estimate.confidence,
            rationale=(
                "Expected revenue copies budget_min."
                if commercial.budget_min is not None else
                "Expected revenue is unknown: budget_min is absent; a ceiling is not revenue.",
                "Currency copies commercial.currency."
                if commercial.currency is not None else "Currency is unknown.",
                "Execution cost copies the canonical estimate."
                if estimate.execution_cost is not None else "Execution cost is unknown.",
                "Expected value copies the explicit canonical estimate."
                if estimate.expected_value is not None else
                "Expected value is unknown; no formula is inferred.",
                "Win probability and confidence copy the canonical estimate; "
                "zero confidence denotes unsubstantiated estimates.",
            ),
        )
        feasibility = FeasibilityPrediction(
            required_capabilities=estimate.required_capabilities,
            human_minutes=estimate.human_minutes,
            delivery_probability=estimate.delivery_probability,
            confidence=estimate.confidence,
            rationale=(
                "Required capabilities copy the canonical estimate."
                if estimate.required_capabilities else
                "No required capabilities are recorded; this does not establish none are needed.",
                "Human minutes copy the canonical estimate."
                if estimate.human_minutes is not None else "Human minutes are unknown.",
                "Delivery probability and confidence copy the canonical estimate; "
                "zero confidence denotes unsubstantiated estimates.",
            ),
        )
        risk = RiskPrediction(
            **opportunity.risk.model_dump(),
            confidence=0,
            rationale=(
                "All six risk scores copy the canonical values without reinterpretation.",
                "Risk confidence is zero because independent risk-confidence evidence "
                "is absent; scores may be conservative placeholders.",
            ),
        )
        return PredictionRecord(
            opportunity_id=opportunity.id,
            evaluated_at=evaluated_at.astimezone(UTC),
            economics=economics,
            feasibility=feasibility,
            risk=risk,
            confidence=min(economics.confidence, feasibility.confidence, risk.confidence),
            rationale=(
                "Canonical estimates are preserved; missing monetary values remain unknown.",
                "Overall confidence is the weakest dimension confidence; "
                "risk confidence is unsubstantiated.",
            ),
        )


# Public implementation import follows contract definitions to avoid a cycle.
from agenticstudio.evaluation.screening import (  # noqa: E402
    ScreeningEvaluator as ScreeningEvaluator,
)
