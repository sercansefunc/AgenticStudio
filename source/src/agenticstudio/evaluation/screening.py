"""Offline investigation heuristics, deliberately below review confidence.

Probabilities required by the contract use 0.0 with zero confidence when
unassessed. That sentinel is not evidence of failure or inability to win.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from agenticstudio.domain import Opportunity
from agenticstudio.evaluation import (
    EconomicPrediction,
    FeasibilityPrediction,
    PredictionRecord,
    RiskPrediction,
)


class ScreeningEvaluator:
    """Conservative version-2 policy; confidence is evidence coverage, not calibration.

    Delivery fit is left unassessed here; Gemini triage judges profile fit. Source risk class
    imposes only a legal/platform caution floor, without lowering existing risks.
    Competition is recorded as evidence, never translated to win probability.
    """

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    def evaluate(self, opportunity: Opportunity) -> PredictionRecord:
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Evaluation clock must return a timezone-aware datetime")
        commercial, estimate = opportunity.commercial, opportunity.estimate
        revenue = (
            commercial.budget_min
            if commercial.pricing_model in {"fixed", "bounty"}
            and commercial.budget_min is not None and commercial.budget_min > 0
            else None
        )
        competition = opportunity.signals.competition_estimate
        economics = EconomicPrediction(
            currency=commercial.currency,
            expected_revenue=revenue,
            execution_cost=estimate.execution_cost,
            expected_value=estimate.expected_value,
            win_probability=0.0,
            confidence=20 if revenue is not None else 0,
            rationale=(
                f"commercial.pricing_model={commercial.pricing_model}; "
                + ("positive commercial.budget_min supplies the revenue lower bound."
                   if revenue is not None else
                   "total revenue is unknown; hourly rates, ceilings and zero bounds "
                   "do not establish total revenue."),
                "Currency copies commercial.currency; no FX or cross-currency comparison.",
                "Execution cost and expected value copy estimate fields; absent money stays "
                "unknown and no profit formula is inferred.",
                ("signals.competition_estimate is absent: competition remains unassessed."
                 if competition is None else
                 f"signals.competition_estimate={competition}/100 is explicit competition "
                 "evidence, not a calibrated win probability."),
                "Win probability is unassessed: 0.0 is a contract sentinel, not a forecast. "
                "Economics confidence covers only stated revenue, not probability or profit.",
            ),
        )
        feasibility = FeasibilityPrediction(
            required_capabilities=estimate.required_capabilities,
            human_minutes=estimate.human_minutes,
            delivery_probability=0.0,
            confidence=0,
            rationale=(
                "Delivery is unassessed: 0.0 is a contract sentinel, not failure evidence; "
                "profile fit is judged by Gemini triage, not skill labels.",
                "Human minutes copy estimate.human_minutes; no effort is inferred.",
            ),
        )
        risk_class = opportunity.source.risk_class
        floor = {"core": 0, "controlled": 25, "experimental": 50}[risk_class]
        risk = RiskPrediction(
            **{**opportunity.risk.model_dump(), "legal_or_platform_risk": max(
                opportunity.risk.legal_or_platform_risk, floor)},
            confidence=15,
            rationale=(
                f"source.risk_class={risk_class} supplies legal/platform caution floor "
                f"{floor}/100; max(canonical legal_or_platform_risk, floor) is used.",
                "Other risk fields copy canonical risk unchanged. Scores may be placeholders; "
                "class evidence alone gives confidence 15, not validation of all risk scores.",
            ),
        )
        return PredictionRecord(
            opportunity_id=opportunity.id,
            evaluated_at=timestamp.astimezone(UTC),
            economics=economics, feasibility=feasibility, risk=risk,
            confidence=min(economics.confidence, feasibility.confidence, risk.confidence),
            rationale=(
                "Screening v2 uses only commercial and explicit signals/source.risk_class "
                "evidence; human investigation is required.",
                "Overall confidence is the weakest dimension; all heuristic confidence is "
                "below 60/100 and no lifecycle or outcome is changed.",
            ),
        )
