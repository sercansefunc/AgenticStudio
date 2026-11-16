"""Offline regression coverage for replay's source/business ownership boundary."""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pytest
from sqlalchemy import create_engine, text

from agenticstudio.dedupe import build_dedupe_key
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
    Status,
)
from agenticstudio.evaluation import BaselineEvaluator, PredictionRecord
from agenticstudio.ingestion import CollectedRecord, CollectionRequest, RecordRejection
from agenticstudio.persistence import OpportunityRepository, SQLiteOpportunityStore
from agenticstudio.pipeline import SliceResult, run_slice

REQUEST = CollectionRequest(source_name="offline", request_id="same-run", selection="fixture")
NOW = datetime(2026, 10, 8, tzinfo=UTC)


class Adapter:
    source_name = "offline"

    def __init__(self, *items: CollectedRecord) -> None:
        self.items = items

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        assert request == REQUEST
        return self.items


def record(identifier: str = "1", summary: str = "updated") -> CollectedRecord:
    return CollectedRecord(source_name="offline", external_id=identifier,
                           url=f"https://example.invalid/{identifier}", raw_text=summary)


def normalize(item: CollectedRecord) -> Opportunity:
    return Opportunity(
        source=Source(name=item.source_name, external_id=item.external_id, url=item.url,
                      risk_class="controlled"),
        posted_at=NOW,
        problem=Problem(raw_text=item.raw_text, summary=item.raw_text,
                        expected_deliverable="updated deliverable"),
        commercial=Commercial(currency="EUR", budget_min=Decimal("200"),
                              budget_max=Decimal("300"), pricing_model="bounty"),
        estimate=Estimate(required_capabilities=("rust",), human_minutes=1.0,
                          execution_cost=Decimal("1"), delivery_probability=0.1,
                          win_probability=0.2, expected_value=Decimal("2"), confidence=5),
        signals=Signals(repo_available=True, competition_estimate=70,
                        acceptance_clarity=1, strategic_reuse_value=2, client_quality=3),
        risk=Risk(technical_uncertainty=1, scope_uncertainty=2, maintenance_burden=3,
                  legal_or_platform_risk=4, abuse_risk=5, payment_or_dispute_risk=6),
        decision=Decision(status="rejected", reasons=("source default",)),
        outcome=Outcome(notes="source default"),
    )


def original(status: Status = "new") -> Opportunity:
    observed = normalize(record(summary="old"))
    return observed.model_copy(update={
        "source": observed.source.model_copy(update={"risk_class": "core"}),
        "posted_at": datetime(2026, 10, 1, tzinfo=UTC),
        "commercial": Commercial(currency="EUR", budget_min=Decimal("100"),
                                 budget_max=Decimal("150"), pricing_model="fixed"),
        "estimate": Estimate(required_capabilities=("python",), human_minutes=120.0,
                             execution_cost=Decimal("15"), delivery_probability=0.8,
                             win_probability=0.7, expected_value=Decimal("42"), confidence=90),
        "signals": Signals(repo_available=False, competition_estimate=10,
                           acceptance_clarity=80, strategic_reuse_value=90, client_quality=60),
        "risk": Risk(technical_uncertainty=20, scope_uncertainty=30, maintenance_burden=40,
                     legal_or_platform_risk=50, abuse_risk=60, payment_or_dispute_risk=70),
        "decision": Decision(status=status, reasons=("human decision",)),
        "outcome": Outcome(revenue_won=Decimal("10"), revenue_collected=Decimal("8"),
                           actual_human_minutes=30.0, actual_execution_cost=Decimal("3"),
                           maintenance_incidents=1, dispute_or_refund=False, notes="human notes"),
    })


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteOpportunityStore]:
    storage = SQLiteOpportunityStore(tmp_path / "freshness.sqlite")
    storage.migrate()
    try:
        yield storage
    finally:
        storage.close()


def seed(store: SQLiteOpportunityStore, snapshot: Opportunity) -> str:
    key = build_dedupe_key(snapshot.source).storage_key
    with store.transaction() as repository:
        repository.save(snapshot)
        repository.bind_identity(key, snapshot.id)
    return key


def assert_persisted(store: SQLiteOpportunityStore, key: str, snapshot: Opportunity) -> None:
    with store.transaction() as repository:
        assert repository.resolve_identity(key) == snapshot.id
        assert repository.get(snapshot.id) == snapshot


def assert_counts(tmp_path: Path, count: int) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'freshness.sqlite'}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM opportunities")) == count
            assert connection.scalar(text("SELECT count(*) FROM identity_mappings")) == count
    finally:
        engine.dispose()


class RecordingEvaluator:
    def __init__(self, fail: bool = False) -> None:
        self.inputs: list[Opportunity] = []
        self.fail = fail

    def evaluate(self, opportunity: Opportunity) -> PredictionRecord:
        self.inputs.append(opportunity)
        if self.fail:
            raise RuntimeError("evaluator failure")
        return BaselineEvaluator(clock=lambda: NOW).evaluate(opportunity)


def test_refreshes_evaluation_input_and_preserves_business_state(
    store: SQLiteOpportunityStore, tmp_path: Path,
) -> None:
    stored = original()
    key = seed(store, stored)
    item = record().model_copy(update={"url": "https://example.invalid/new-url"})
    observed = normalize(item)
    evaluator = RecordingEvaluator()
    result = run_slice(Adapter(item), REQUEST, normalize, store, evaluator)
    assert isinstance(result, SliceResult)
    refreshed, = result.opportunities
    assert evaluator.inputs == [refreshed]
    assert refreshed.id == stored.id
    assert refreshed.id.version == 4
    for field in ("source", "posted_at", "problem", "commercial"):
        assert getattr(refreshed, field) == getattr(observed, field)
    assert refreshed.estimate.required_capabilities == ("rust",)
    assert refreshed.estimate.model_dump(exclude={"required_capabilities"}) == (
        stored.estimate.model_dump(exclude={"required_capabilities"})
    )
    assert refreshed.signals.repo_available is True
    assert refreshed.signals.competition_estimate == 70
    assert refreshed.signals.model_dump(exclude={"repo_available", "competition_estimate"}) == (
        stored.signals.model_dump(exclude={"repo_available", "competition_estimate"})
    )
    assert refreshed.decision == stored.decision
    assert refreshed.outcome == stored.outcome
    assert refreshed.risk == stored.risk
    assert result.predictions[0].economics.expected_revenue == Decimal("200")
    assert result.predictions[0].economics.currency == "EUR"
    assert result.predictions[0].feasibility.required_capabilities == ("rust",)
    assert_persisted(store, key, refreshed)
    assert_counts(tmp_path, 1)


def test_unknown_source_values_replace_stale_known_facts(store: SQLiteOpportunityStore) -> None:
    stored = original()
    stored = stored.model_copy(update={
        "estimate": stored.estimate.model_copy(update={"execution_cost": None,
                                                       "expected_value": None}),
        "outcome": stored.outcome.model_copy(update={"revenue_won": None,
            "revenue_collected": None, "actual_execution_cost": None}),
    })
    key = seed(store, stored)

    def unknown(item: CollectedRecord) -> Opportunity:
        observed = normalize(item)
        return observed.model_copy(update={
            "commercial": Commercial(), "posted_at": None,
            "estimate": observed.estimate.model_copy(update={"required_capabilities": ()}),
            "signals": observed.signals.model_copy(update={
                "repo_available": None, "competition_estimate": None,
            }),
        })

    result = run_slice(Adapter(record()), REQUEST, unknown, store, RecordingEvaluator())
    assert isinstance(result, SliceResult)
    refreshed, = result.opportunities
    assert refreshed.commercial == Commercial()
    assert refreshed.posted_at is None
    assert refreshed.estimate.required_capabilities == ()
    assert refreshed.signals.repo_available is None
    assert refreshed.signals.competition_estimate is None
    assert result.predictions[0].economics.expected_revenue is None
    assert result.predictions[0].economics.currency is None
    assert_persisted(store, key, refreshed)


@pytest.mark.parametrize("status", [status for status in get_args(Status) if status != "new"])
def test_non_new_snapshot_is_unchanged(store: SQLiteOpportunityStore, status: Status) -> None:
    stored = original(status)
    key = seed(store, stored)

    def forbidden(item: CollectedRecord) -> Opportunity:
        raise AssertionError("committed snapshot must not be normalized")

    evaluator = RecordingEvaluator()
    result = run_slice(Adapter(record()), REQUEST, forbidden, store, evaluator)
    assert isinstance(result, SliceResult)
    assert result.opportunities == (stored,)
    assert evaluator.inputs == [stored]
    assert_persisted(store, key, stored)


@pytest.mark.parametrize("preexisting", [False, True])
def test_duplicate_identity_and_same_run_replay_are_last_observation_wins(
    store: SQLiteOpportunityStore, tmp_path: Path, preexisting: bool,
) -> None:
    if preexisting:
        seed(store, original())
    adapter = Adapter(record(summary="first"), record(summary="last"))
    evaluator = RecordingEvaluator()
    # Source normalization normally defaults to new; source lifecycle defaults
    # must not overwrite stored lifecycle on refresh.
    def new(item: CollectedRecord) -> Opportunity:
        return normalize(item).model_copy(update={"decision": Decision()})

    first = run_slice(adapter, REQUEST, new, store, evaluator)
    second = run_slice(adapter, REQUEST, new, store, evaluator)
    assert isinstance(first, SliceResult)
    assert isinstance(second, SliceResult)
    assert len(first.opportunities) == 1
    assert first.opportunities[0].problem.summary == "last"
    assert first.opportunities == second.opportunities
    assert len(evaluator.inputs) == 2
    assert evaluator.inputs[0] == evaluator.inputs[1]
    assert_counts(tmp_path, 1)


@pytest.mark.parametrize("failure", ["normalization", "identity", "persistence"])
def test_late_failure_rolls_back_refreshes_and_new_records(
    store: SQLiteOpportunityStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    stored = original()
    key = seed(store, stored)
    original_transaction = store.transaction

    @contextmanager
    def failing_transaction() -> Iterator[OpportunityRepository]:
        with original_transaction() as repository:
            save = repository.save

            def fail_save(snapshot: Opportunity) -> None:
                save(snapshot)
                if snapshot.source.external_id == "3":
                    raise RuntimeError("late persistence failure")

            monkeypatch.setattr(repository, "save", fail_save)
            yield repository

    if failure == "persistence":
        monkeypatch.setattr(store, "transaction", failing_transaction)

    def fail_last(item: CollectedRecord) -> Opportunity:
        if item.external_id == "3":
            if failure == "normalization":
                raise RuntimeError("late normalization failure")
            if failure == "identity":
                return normalize(record("different"))
        return normalize(item)

    evaluator = RecordingEvaluator()
    with pytest.raises((RuntimeError, ValueError), match="failure|changed source identity"):
        run_slice(Adapter(record(), record("2"), record("3")), REQUEST,
                  fail_last, store, evaluator)
    monkeypatch.undo()
    assert evaluator.inputs == []
    assert_persisted(store, key, stored)
    assert_counts(tmp_path, 1)


def test_evaluator_failure_leaves_valid_state_and_retry_refreshes_again(
    store: SQLiteOpportunityStore, tmp_path: Path,
) -> None:
    stored = original()
    key = seed(store, stored)
    evaluator = RecordingEvaluator(fail=True)
    with pytest.raises(RuntimeError, match="evaluator failure"):
        run_slice(Adapter(record(summary="first refresh")), REQUEST, normalize, store, evaluator)
    assert_persisted(store, key, evaluator.inputs[0])
    retry = RecordingEvaluator()
    result = run_slice(Adapter(record(summary="retry refresh")), REQUEST, normalize, store, retry)
    assert isinstance(result, SliceResult)
    assert result.opportunities[0].id == stored.id
    assert retry.inputs[0].problem.summary == "retry refresh"
    assert_persisted(store, key, retry.inputs[0])
    assert_counts(tmp_path, 1)


def test_currency_refresh_without_preserved_monetary_history(
    store: SQLiteOpportunityStore,
) -> None:
    # A source may legitimately change currency when no business-owned amounts
    # survive the refresh. This is distinct from the guarded monetary-history case.
    previous = original().model_copy(update={
        "commercial": Commercial(currency="USD", budget_min=Decimal("100"),
                                 budget_max=Decimal("150"), pricing_model="fixed"),
    })
    previous = previous.model_copy(update={
        "estimate": previous.estimate.model_copy(update={
            "execution_cost": None, "expected_value": None,
        }),
        "outcome": previous.outcome.model_copy(update={
            "revenue_won": None, "revenue_collected": None,
            "actual_execution_cost": None,
        }),
    })
    key = seed(store, previous)
    evaluator = RecordingEvaluator()
    result = run_slice(Adapter(record()), REQUEST, normalize, store, evaluator)
    assert isinstance(result, SliceResult)
    updated, = result.opportunities
    assert updated.id == previous.id
    assert updated.commercial.currency == "EUR"
    assert updated.commercial.budget_min == Decimal("200")
    assert evaluator.inputs == [updated]
    assert updated.outcome == previous.outcome
    assert_persisted(store, key, updated)


@pytest.mark.parametrize("old_currency,new_currency", [
    ("USD", "EUR"), ("USD", None), (None, "EUR"),
])
@pytest.mark.parametrize("owner,field", [
    ("estimate", "execution_cost"), ("estimate", "expected_value"),
    ("outcome", "revenue_won"), ("outcome", "revenue_collected"),
    ("outcome", "actual_execution_cost"),
])
def test_currency_change_cannot_relabel_preserved_money(
    store: SQLiteOpportunityStore, tmp_path: Path, old_currency: str | None,
    new_currency: str | None, owner: str, field: str,
) -> None:
    stored = original().model_copy(update={
        "commercial": original().commercial.model_copy(update={"currency": old_currency}),
        "estimate": original().estimate.model_copy(update={"execution_cost": None,
                                                          "expected_value": None}),
        "outcome": original().outcome.model_copy(update={"revenue_won": None,
            "revenue_collected": None, "actual_execution_cost": None}),
    })
    preserved = getattr(stored, owner).model_copy(update={field: Decimal("0")})
    stored = stored.model_copy(update={owner: preserved})
    key = seed(store, stored)

    def changed(item: CollectedRecord) -> Opportunity:
        observed = normalize(item)
        return observed.model_copy(update={
            "commercial": observed.commercial.model_copy(update={"currency": new_currency}),
        })

    evaluator = RecordingEvaluator()
    with pytest.raises(ValueError, match="stored monetary history"):
        run_slice(Adapter(record()), REQUEST, changed, store, evaluator)
    assert evaluator.inputs == []
    assert_persisted(store, key, stored)
    assert_counts(tmp_path, 1)
