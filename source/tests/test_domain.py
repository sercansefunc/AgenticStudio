from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid1, uuid4

import pytest
from pydantic import ValidationError

from agenticstudio.domain import (
    Commercial,
    Decision,
    Estimate,
    Opportunity,
    Outcome,
    Problem,
    Risk,
    Signals,
    Source,
)


def minimal_data() -> dict[str, Any]:
    return {
        "source": Source(name="example", url="https://example.com/1", risk_class="core"),
        "problem": Problem(raw_text="Build a report", summary="Report"),
        "signals": Signals(acceptance_clarity=100, strategic_reuse_value=0),
        "risk": Risk(
            technical_uncertainty=0, scope_uncertainty=0, maintenance_burden=0,
            legal_or_platform_risk=0, abuse_risk=0, payment_or_dispute_risk=0,
        ),
        "estimate": Estimate(delivery_probability=1.0, win_probability=0.0, confidence=100),
    }


def test_minimal_and_generated_ids() -> None:
    first = Opportunity.model_validate(minimal_data())
    second = Opportunity.model_validate(minimal_data())
    assert first.id.version == 4
    assert first.id != second.id
    assert first.source == second.source
    assert first.decision.status == "new"
    assert first.commercial.pricing_model == "unknown"
    assert first.outcome is not second.outcome
    assert first.estimate.required_capabilities == ()


def test_full_record_and_json_roundtrip() -> None:
    identifier = uuid4()
    data = minimal_data()
    data.update(
        id=identifier,
        posted_at=datetime(2026, 1, 1, tzinfo=UTC),
        source=Source(name="example", url="https://example.com/1", external_id="1",
                      risk_class="controlled"),
        problem=Problem(raw_text="Build a report", summary="Report",
                        expected_deliverable="PDF"),
        commercial=Commercial(currency="USD", budget_min=Decimal("1.25"),
                              budget_max=Decimal("100"), pricing_model="fixed"),
        signals=Signals(repo_available=True, acceptance_clarity=50, competition_estimate=20,
                        client_quality=80, strategic_reuse_value=100),
        estimate=Estimate(required_capabilities=("python",), human_minutes=30.0,
                          execution_cost=Decimal("2"), delivery_probability=0.9,
                          win_probability=0.5, expected_value=Decimal("-1"), confidence=80),
        decision=Decision(status="investigating", reasons=("clear scope",)),
        outcome=Outcome(revenue_won=Decimal("100"), revenue_collected=Decimal("90"),
                        actual_human_minutes=40.0, actual_execution_cost=Decimal("3"),
                        maintenance_incidents=0, dispute_or_refund=False, notes="done"),
    )
    opportunity = Opportunity.model_validate(data)
    assert opportunity.id == identifier
    assert Opportunity.model_validate_json(opportunity.model_dump_json()) == opportunity


@pytest.mark.parametrize("identifier", [uuid1(), "not-a-uuid", str(uuid4())])
def test_invalid_python_ids(identifier: object) -> None:
    with pytest.raises(ValidationError):
        Opportunity.model_validate({**minimal_data(), "id": identifier})


@pytest.mark.parametrize("model", [Source, Problem, Commercial, Signals, Risk,
                                   Estimate, Decision, Outcome, Opportunity])
def test_extra_fields_rejected(model: type[Any]) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate({"adapter_private": "unexpected"})
    assert any(item["type"] == "extra_forbidden" for item in error.value.errors())


@pytest.mark.parametrize("model,field", [
    (Signals, "acceptance_clarity"), (Signals, "competition_estimate"),
    (Signals, "client_quality"), (Signals, "strategic_reuse_value"),
    (Risk, "technical_uncertainty"), (Risk, "scope_uncertainty"),
    (Risk, "maintenance_burden"), (Risk, "legal_or_platform_risk"),
    (Risk, "abuse_risk"), (Risk, "payment_or_dispute_risk"), (Estimate, "confidence"),
])
@pytest.mark.parametrize("value", [-1, 101, "50", 1.5, True])
def test_score_bounds_and_strict_types(model: type[Any], field: str, value: object) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate({field: value})
    assert any(item["loc"] == (field,) for item in error.value.errors())


@pytest.mark.parametrize("field", ["delivery_probability", "win_probability"])
@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf"), "0.5"])
def test_probability_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as error:
        Estimate.model_validate({field: value})
    assert any(item["loc"] == (field,) for item in error.value.errors())


@pytest.mark.parametrize("model,field,value", [
    (Commercial, "budget_min", Decimal("-1")),
    (Commercial, "budget_max", Decimal("-1")),
    (Estimate, "human_minutes", -1.0), (Estimate, "execution_cost", Decimal("-1")),
    (Outcome, "revenue_won", Decimal("-1")),
    (Outcome, "revenue_collected", Decimal("-1")),
    (Outcome, "actual_human_minutes", -1.0),
    (Outcome, "actual_execution_cost", Decimal("-1")),
    (Outcome, "maintenance_incidents", -1),
])
def test_nonnegative_fields(model: type[Any], field: str, value: object) -> None:
    with pytest.raises(ValidationError) as error:
        model.model_validate({field: value})
    assert any(item["loc"] == (field,) for item in error.value.errors())


def test_budget_ordering() -> None:
    with pytest.raises(ValidationError, match="budget_min must not exceed budget_max"):
        Commercial(budget_min=Decimal("2"), budget_max=Decimal("1"))
    assert Commercial(budget_min=Decimal("1"), budget_max=Decimal("1"))
    assert Commercial(budget_min=Decimal("1"))


def test_timezone_required() -> None:
    with pytest.raises(ValidationError):
        Opportunity.model_validate({**minimal_data(), "posted_at": datetime(2026, 1, 1)})
    assert Opportunity.model_validate({
        **minimal_data(), "posted_at": datetime(2026, 1, 1, tzinfo=UTC),
    }).posted_at is not None


def test_models_and_collections_are_immutable() -> None:
    opportunity = Opportunity.model_validate(minimal_data())
    with pytest.raises(ValidationError, match="frozen"):
        setattr(opportunity, "id", uuid4())
    with pytest.raises(ValidationError, match="frozen"):
        setattr(opportunity.decision, "status", "won")
    assert isinstance(opportunity.decision.reasons, tuple)
    assert isinstance(opportunity.estimate.required_capabilities, tuple)
