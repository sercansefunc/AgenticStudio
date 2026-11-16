from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from agenticstudio.domain import (
    Commercial,
    Estimate,
    Opportunity,
    Outcome,
    Problem,
    Risk,
    Signals,
    Source,
)
from agenticstudio.evaluation import (
    BaselineEvaluator,
    EconomicPrediction,
    Evaluator,
    FeasibilityPrediction,
    PredictionRecord,
    RiskPrediction,
    ScreeningEvaluator,
)


class FixedEvaluator:
    """Deterministic implementation demonstrating structural replaceability."""

    def evaluate(self, opportunity: Opportunity) -> PredictionRecord:
        return PredictionRecord(
            opportunity_id=opportunity.id,
            evaluated_at=datetime(2026, 1, 1, tzinfo=UTC),
            economics=EconomicPrediction(
                currency="USD", expected_revenue=Decimal("20"),
                execution_cost=Decimal("25"), expected_value=Decimal("-5"),
                win_probability=0.5, confidence=60, rationale=("Cost exceeds revenue",),
            ),
            feasibility=FeasibilityPrediction(
                required_capabilities=("python",), human_minutes=30.0,
                delivery_probability=0.9, confidence=80, rationale=("Known workflow",),
            ),
            risk=RiskPrediction(
                **opportunity.risk.model_dump(), confidence=70,
                rationale=("Payment is uncertain",),
            ),
            confidence=65, rationale=("Investigate payment terms",),
        )


def opportunity() -> Opportunity:
    return Opportunity(
        source=Source(name="example", url="https://example.com/1", risk_class="core"),
        problem=Problem(raw_text="Build a report", summary="Report"),
        signals=Signals(acceptance_clarity=100, strategic_reuse_value=0),
        risk=Risk(
            technical_uncertainty=10, scope_uncertainty=20, maintenance_burden=30,
            legal_or_platform_risk=40, abuse_risk=50, payment_or_dispute_risk=60,
        ),
        estimate=Estimate(delivery_probability=1.0, win_probability=0.0, confidence=100),
    )


def test_evaluator_preserves_dimensions_and_input() -> None:
    source = opportunity()
    before = source.model_dump_json()
    evaluator: Evaluator = FixedEvaluator()
    result = evaluator.evaluate(source)
    assert source.model_dump_json() == before
    assert result.opportunity_id == source.id
    assert result.economics.expected_value == Decimal("-5")
    assert result.economics.win_probability == 0.5
    assert result.feasibility.delivery_probability == 0.9
    assert result.feasibility.required_capabilities == ("python",)
    assert result.risk.payment_or_dispute_risk == 60
    assert result.risk.technical_uncertainty == 10
    assert (result.economics.confidence, result.feasibility.confidence,
            result.risk.confidence, result.confidence) == (60, 80, 70, 65)
    assert result.economics.rationale == ("Cost exceeds revenue",)
    assert result.feasibility.rationale == ("Known workflow",)
    assert result.risk.rationale == ("Payment is uncertain",)
    assert result.rationale == ("Investigate payment terms",)
    assert PredictionRecord.model_validate_json(result.model_dump_json()) == result
    assert evaluator.evaluate(source).id != result.id


def test_actuals_do_not_overwrite_prediction() -> None:
    source = opportunity()
    prediction = FixedEvaluator().evaluate(source)
    snapshot = prediction.model_dump_json()
    observed = Opportunity.model_validate({
        **source.model_dump(),
        "outcome": Outcome(revenue_collected=Decimal("100"), actual_human_minutes=90.0),
    })
    assert observed.id == prediction.opportunity_id
    assert observed.outcome.actual_human_minutes == 90.0
    assert prediction.feasibility.human_minutes == 30.0
    assert prediction.model_dump_json() == snapshot
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PredictionRecord.model_validate({**prediction.model_dump(), "outcome": observed.outcome})
    with pytest.raises(ValidationError, match="frozen"):
        setattr(prediction.economics, "expected_value", Decimal("100"))


@pytest.mark.parametrize("model,field,value", [
    (EconomicPrediction, "win_probability", 1.1),
    (EconomicPrediction, "win_probability", float("nan")),
    (EconomicPrediction, "expected_revenue", Decimal("-1")),
    (EconomicPrediction, "execution_cost", Decimal("Infinity")),
    (EconomicPrediction, "confidence", 101),
    (FeasibilityPrediction, "human_minutes", -1.0),
    (FeasibilityPrediction, "delivery_probability", -0.1),
    (FeasibilityPrediction, "confidence", "80"),
    (RiskPrediction, "technical_uncertainty", 101),
    (RiskPrediction, "confidence", -1),
    (PredictionRecord, "confidence", True),
    (PredictionRecord, "evaluated_at", datetime(2026, 1, 1)),
])
def test_invalid_prediction_values(model: type[Any], field: str, value: object) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate({field: value})
    assert any(item["loc"] == (field,) for item in error.value.errors())


@pytest.mark.parametrize("model", [
    EconomicPrediction, FeasibilityPrediction, RiskPrediction, PredictionRecord,
])
def test_provider_metadata_is_not_part_of_contract(model: type[Any]) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate({"provider": "example", "model": "example"})
    assert {item["loc"] for item in error.value.errors()
            if item["type"] == "extra_forbidden"} == {("provider",), ("model",)}


def test_unknown_economics_remain_unknown() -> None:
    prediction = EconomicPrediction(win_probability=0.0, confidence=0, rationale=())
    assert prediction.expected_revenue is None
    assert prediction.execution_cost is None
    assert prediction.expected_value is None


@pytest.mark.parametrize("minimum,maximum,expected", [
    (Decimal("100"), Decimal("200"), Decimal("100")),
    (None, Decimal("200"), None),
    (None, None, None),
    (Decimal("0"), Decimal("200"), Decimal("0")),
])
def test_baseline_conservative_revenue(
    minimum: Decimal | None, maximum: Decimal | None, expected: Decimal | None,
) -> None:
    source = opportunity().model_copy(update={
        "commercial": Commercial(currency="USD", budget_min=minimum, budget_max=maximum,
                                 pricing_model="bounty"),
    })
    result = BaselineEvaluator().evaluate(source)
    assert result.economics.currency == "USD"
    assert result.economics.expected_revenue == expected
    assert result.economics.execution_cost is None
    assert result.economics.expected_value is None
    assert result.feasibility.human_minutes is None
    assert result.feasibility.required_capabilities == ()
    assert result.economics.rationale
    assert result.feasibility.rationale


def test_baseline_copies_explicit_estimates_and_is_immutable() -> None:
    source = opportunity().model_copy(update={
        "estimate": Estimate(required_capabilities=("python",), human_minutes=45.0,
                             execution_cost=Decimal("12"), expected_value=Decimal("-3"),
                             delivery_probability=0.7, win_probability=0.2, confidence=15),
    })
    before = source.model_dump_json()
    evaluator: Evaluator = BaselineEvaluator()
    result = evaluator.evaluate(source)
    assert result.opportunity_id == source.id
    assert result.economics.currency is None
    assert result.economics.expected_revenue is None
    assert result.economics.execution_cost == Decimal("12")
    assert result.economics.expected_value == Decimal("-3")
    assert result.economics.win_probability == 0.2
    assert result.feasibility.required_capabilities == ("python",)
    assert result.feasibility.human_minutes == 45.0
    assert result.feasibility.delivery_probability == 0.7
    assert source.model_dump_json() == before
    assert evaluator.evaluate(source).id != result.id
    with pytest.raises(ValidationError, match="frozen"):
        setattr(result, "confidence", 100)
    with pytest.raises(ValidationError, match="frozen"):
        setattr(result.risk, "confidence", 100)


@pytest.mark.parametrize("confidence", [0, 1, 15, 100])
@pytest.mark.parametrize("placeholder", [False, True])
def test_baseline_confidence_and_exact_risks(confidence: int, placeholder: bool) -> None:
    source = opportunity()
    risks = (
        Risk.model_validate({name: 100 for name in source.risk.model_dump()})
        if placeholder else source.risk
    )
    source = source.model_copy(update={
        "risk": risks,
        "estimate": source.estimate.model_copy(update={"confidence": confidence}),
    })
    result = BaselineEvaluator().evaluate(source)
    assert result.economics.confidence == result.feasibility.confidence == confidence
    assert result.risk.confidence == result.confidence == 0
    assert result.confidence <= min(result.economics.confidence, result.feasibility.confidence,
                                    result.risk.confidence)
    assert result.risk.model_dump(exclude={"confidence", "rationale"}) == risks.model_dump()
    assert "independent risk-confidence evidence" in result.risk.rationale[1]


def test_baseline_timestamp_is_injected_and_normalized_to_utc() -> None:
    timestamp = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    evaluator = BaselineEvaluator(clock=lambda: timestamp)
    result = evaluator.evaluate(opportunity())
    assert result.evaluated_at == datetime(2026, 1, 1, 10, tzinfo=UTC)
    assert result.evaluated_at.tzinfo is UTC
    assert evaluator.evaluate(opportunity()).evaluated_at == result.evaluated_at
    with pytest.raises(ValueError, match="timezone-aware"):
        BaselineEvaluator(clock=lambda: datetime(2026, 1, 1)).evaluate(opportunity())


def test_baseline_source_name_does_not_affect_policy() -> None:
    source = opportunity()
    evaluator = BaselineEvaluator(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    expected = evaluator.evaluate(source).model_dump(exclude={"id"})
    for name in ("hacker_news", "opire", "Board"):
        renamed = source.model_copy(update={
            "source": source.source.model_copy(update={"name": name}),
        })
        assert evaluator.evaluate(renamed).model_dump(exclude={"id"}) == expected


def screening_opportunity(
    capabilities: tuple[str, ...] = ("Python", "API integration"),
) -> Opportunity:
    return opportunity().model_copy(update={
        "commercial": Commercial(currency="USD", budget_min=Decimal("100"),
                                 pricing_model="fixed"),
        "estimate": Estimate(required_capabilities=capabilities,
                             delivery_probability=1.0, win_probability=1.0, confidence=100),
    })


def test_screening_useful_evidence_and_human_gate() -> None:
    from agenticstudio.decision import build_brief
    source = screening_opportunity()
    before = source.model_dump_json()
    result = ScreeningEvaluator().evaluate(source)
    assert result.economics.expected_revenue == Decimal("100")
    assert result.feasibility.delivery_probability == 0.0
    assert result.feasibility.confidence == result.confidence == 0
    assert all(0 < value < 60 for value in (
        result.economics.confidence, result.risk.confidence,
    ))
    assert all(dimension.rationale for dimension in (
        result.economics, result.feasibility, result.risk,
    ))
    assert build_brief([source], [result]).entries[0].bucket == "needs_investigation"
    assert source.model_dump_json() == before


@pytest.mark.parametrize("pricing,minimum,expected", [
    ("fixed", Decimal("100"), Decimal("100")),
    ("bounty", Decimal("100"), Decimal("100")),
    ("hourly", Decimal("100"), None),
    ("unknown", Decimal("100"), None),
    ("fixed", Decimal("0"), None),
    ("bounty", None, None),
])
@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY", None])
def test_screening_money_semantics(
    pricing: str, minimum: Decimal | None, expected: Decimal | None, currency: str | None,
) -> None:
    commercial = Commercial.model_validate({
        "pricing_model": pricing, "currency": currency,
        "budget_min": minimum, "budget_max": Decimal("200"),
    })
    result = ScreeningEvaluator().evaluate(
        screening_opportunity().model_copy(update={"commercial": commercial}),
    )
    assert result.economics.currency == currency
    assert result.economics.expected_revenue == expected
    assert result.economics.expected_value is None
    assert result.economics.execution_cost is None


def test_screening_feasibility_ignores_skill_tags() -> None:
    evaluator = ScreeningEvaluator(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    results = [evaluator.evaluate(screening_opportunity(labels)).feasibility
               for labels in (("Python",), (), ("graphic design",))]
    assert results[0].model_dump(exclude={"required_capabilities"}) == \
        results[1].model_dump(exclude={"required_capabilities"}) == \
        results[2].model_dump(exclude={"required_capabilities"})
    assert results[0].delivery_probability == 0.0 and results[0].confidence == 0
    assert "judged by Gemini triage" in results[0].rationale[0]


@pytest.mark.parametrize("competition", [None, 0, 50, 100])
def test_screening_competition_is_not_win_probability(competition: int | None) -> None:
    source = screening_opportunity()
    source = source.model_copy(update={
        "signals": source.signals.model_copy(update={"competition_estimate": competition}),
    })
    result = ScreeningEvaluator().evaluate(source)
    assert result.economics.win_probability == 0.0
    if competition is None:
        assert "competition remains unassessed" in result.economics.rationale[3]
    else:
        assert f"competition_estimate={competition}/100" in result.economics.rationale[3]
        assert "not a calibrated win probability" in result.economics.rationale[3]


@pytest.mark.parametrize("risk_class,floor", [("core", 0), ("controlled", 25),
                                              ("experimental", 50)])
@pytest.mark.parametrize("canonical_score", [0, 40, 100])
def test_screening_risk_class_is_explainable_and_preserves_placeholders(
    risk_class: str, floor: int, canonical_score: int,
) -> None:
    source = screening_opportunity()
    source = source.model_copy(update={
        "source": Source.model_validate({**source.source.model_dump(), "risk_class": risk_class}),
        "risk": Risk.model_validate({name: canonical_score for name in source.risk.model_dump()}),
    })
    before = source.model_dump_json()
    result = ScreeningEvaluator().evaluate(source)
    assert result.risk.legal_or_platform_risk == max(canonical_score, floor)
    for name in source.risk.model_dump():
        if name != "legal_or_platform_risk":
            assert getattr(result.risk, name) == canonical_score
    assert f"source.risk_class={risk_class}" in result.risk.rationale[0]
    assert result.risk.confidence == 15
    assert source.model_dump_json() == before
    core = source.model_copy(update={
        "source": source.source.model_copy(update={"risk_class": "core"}),
    })
    reference = ScreeningEvaluator().evaluate(core)
    assert result.feasibility == reference.feasibility
    assert result.economics == reference.economics


def test_screening_deterministic_and_source_neutral() -> None:
    source = screening_opportunity()
    evaluator = ScreeningEvaluator(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC))
    first = evaluator.evaluate(source)
    # Snapshot identity is intentionally fresh, as in BaselineEvaluator.
    assert (evaluator.evaluate(source).model_dump(exclude={"id"})
            == first.model_dump(exclude={"id"}))
    renamed = source.model_copy(update={
        "source": source.source.model_copy(update={"name": "unrelated-provider"}),
    })
    assert (evaluator.evaluate(renamed).model_dump(exclude={"id"})
            == first.model_dump(exclude={"id"}))
    with pytest.raises(ValueError, match="timezone-aware"):
        ScreeningEvaluator(clock=lambda: datetime(2026, 1, 1)).evaluate(source)
