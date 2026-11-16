"""Offline competition-band ordering regressions, without source/provider requests."""

from datetime import timedelta
from uuid import UUID

from test_decision_brief import AS_OF, candidate

from agenticstudio.decision import build_brief
from agenticstudio.domain import Opportunity
from agenticstudio.evaluation import ScreeningEvaluator


def job(number: int, capabilities: tuple[str, ...], competition: int | None = 40) -> Opportunity:
    return candidate(capabilities=capabilities, repo_available=True,
                     competition_estimate=competition).model_copy(update={
                         "id": UUID(int=number, version=4), "posted_at": AS_OF,
                     })


def test_low_band_first_and_unknown_is_not_zero() -> None:
    many = job(1, ("Python", "API", "SQL", "Linux"), 40)
    low = job(2, ("Python",), 20)
    unknown = job(3, ("Python",), None)
    zero = job(4, ("Python",), 0)
    items = [many, low, unknown, zero]
    predictions = [ScreeningEvaluator(clock=lambda: AS_OF).evaluate(item) for item in items]
    brief = build_brief(items, predictions, as_of=AS_OF)
    ids = [entry.opportunity.id for entry in brief.entries]
    assert ids.index(low.id) < ids.index(many.id)
    assert ids.index(zero.id) < ids.index(unknown.id)


def test_zero_competition_does_not_override_freshness_risk_or_terminal_status() -> None:
    useful = job(1, ("Python",), 20)
    zero = job(2, ("Python",), 0)
    variants = [
        zero.model_copy(update={"posted_at": AS_OF - timedelta(days=8)}),
        zero.model_copy(update={"posted_at": None}),
        zero.model_copy(update={
            "source": zero.source.model_copy(update={"risk_class": "experimental"}),
        }),
        zero.model_copy(update={
            "decision": zero.decision.model_copy(update={"status": "rejected"}),
        }),
    ]
    for other in variants:
        predictions = [ScreeningEvaluator(clock=lambda: AS_OF).evaluate(item)
                       for item in (useful, other)]
        brief = build_brief([useful, other], predictions, as_of=AS_OF)
        assert brief.entries[0].opportunity.id == useful.id
