from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

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
from agenticstudio.persistence import (
    IdentityConflictError,
    OpportunityRepository,
    OpportunityStore,
    SQLiteOpportunityStore,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteOpportunityStore]:
    storage = SQLiteOpportunityStore(tmp_path / "opportunities.sqlite")
    storage.migrate()
    try:
        yield storage
    finally:
        storage.close()


@pytest.fixture
def opportunity() -> Opportunity:
    return Opportunity(
        source=Source(name="example", url="https://example.com/1", external_id="1",
                      risk_class="controlled"),
        posted_at=datetime(2026, 10, 7, 12, 34, 56, 123456,
                           tzinfo=timezone(timedelta(hours=5, minutes=30))),
        problem=Problem(raw_text="Build a report — café", summary="Report",
                        expected_deliverable="PDF"),
        commercial=Commercial(currency="USD", budget_min=Decimal("1.2300"),
                              budget_max=Decimal("12345678901234567890.1234567890123456789"),
                              pricing_model="fixed"),
        signals=Signals(repo_available=False, acceptance_clarity=100, competition_estimate=0,
                        client_quality=50, strategic_reuse_value=100),
        risk=Risk(technical_uncertainty=0, scope_uncertainty=10, maintenance_burden=20,
                  legal_or_platform_risk=30, abuse_risk=40, payment_or_dispute_risk=100),
        estimate=Estimate(required_capabilities=("python", "sql"), human_minutes=12.5,
                          execution_cost=Decimal("0.000000000000000001"),
                          delivery_probability=1.0, win_probability=0.5,
                          expected_value=Decimal("-12345678901234567890.123456789"),
                          confidence=80),
        decision=Decision(status="proposal_sent", reasons=("clear scope", "reusable")),
        outcome=Outcome(revenue_won=Decimal("1.00"), revenue_collected=Decimal("0.00"),
                        actual_human_minutes=0.0, actual_execution_cost=Decimal("0.0100"),
                        maintenance_incidents=0, dispute_or_refund=False, notes=""),
    )


def test_empty_database_migration_is_explicit_and_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    storage = SQLiteOpportunityStore(path)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        assert inspect(engine).get_table_names() == []
        with pytest.raises(OperationalError), storage.transaction() as repository:
            repository.get(uuid4())
        storage.migrate()
        storage.migrate()
        assert set(inspect(engine).get_table_names()) == {
            "opportunities", "identity_mappings", "alembic_version",
        }
        assert inspect(engine).get_pk_constraint("opportunities")["constrained_columns"] == ["id"]
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0002_identity_mappings"
            )
    finally:
        storage.close()
        engine.dispose()


def test_roundtrip_after_reopening(store: SQLiteOpportunityStore, opportunity: Opportunity,
                                  tmp_path: Path) -> None:
    boundary: OpportunityStore = store
    with boundary.transaction() as repository:
        assert repository.get(opportunity.id) is None
        repository.save(opportunity)
    store.close()
    reopened = SQLiteOpportunityStore(tmp_path / "opportunities.sqlite")
    try:
        with reopened.transaction() as repository:
            loaded = repository.get(opportunity.id)
        assert loaded == opportunity
        assert loaded is not None
        # Compare serialized forms too: Decimal equality alone ignores trailing zeroes.
        assert loaded.model_dump_json() == opportunity.model_dump_json()
        assert loaded.commercial.budget_min is not None
        assert opportunity.commercial.budget_min is not None
        assert (
            loaded.commercial.budget_min.as_tuple()
            == opportunity.commercial.budget_min.as_tuple()
        )
        assert loaded.posted_at is not None
        assert opportunity.posted_at is not None
        assert loaded.posted_at.isoformat() == opportunity.posted_at.isoformat()
    finally:
        reopened.close()


def test_repeated_saves_replace_one_snapshot(store: SQLiteOpportunityStore,
                                           opportunity: Opportunity, tmp_path: Path) -> None:
    changed = opportunity.model_copy(update={"decision": Decision(status="won")})
    for snapshot in (opportunity, opportunity, changed, changed):
        with store.transaction() as repository:
            repository.save(snapshot)
    with store.transaction() as repository:
        assert repository.get(opportunity.id) == changed
        assert repository.get(uuid4()) is None
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'opportunities.sqlite'}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM opportunities")) == 1
    finally:
        engine.dispose()
    store.migrate()
    with store.transaction() as repository:
        assert repository.get(opportunity.id) == changed


def test_exception_rolls_back_all_writes(store: SQLiteOpportunityStore,
                                        opportunity: Opportunity) -> None:
    with store.transaction() as repository:
        repository.save(opportunity)
    other = opportunity.model_copy(update={"id": uuid4()})
    with pytest.raises(RuntimeError, match="abort"), store.transaction() as repository:
        repository.save(opportunity.model_copy(update={"decision": Decision(status="lost")}))
        repository.save(other)
        assert repository.get(other.id) == other
        raise RuntimeError("abort")
    with store.transaction() as repository:
        assert repository.get(opportunity.id) == opportunity
        assert repository.get(other.id) is None


def test_repository_cannot_escape_transaction(store: SQLiteOpportunityStore,
                                            opportunity: Opportunity) -> None:
    escaped: OpportunityRepository
    with store.transaction() as escaped:
        escaped.save(opportunity)
    with pytest.raises(RuntimeError, match="transaction has ended"):
        escaped.get(opportunity.id)
    with pytest.raises(RuntimeError, match="transaction has ended"):
        escaped.save(opportunity)


def test_optional_values_and_defaults(store: SQLiteOpportunityStore,
                                     opportunity: Opportunity) -> None:
    minimal = Opportunity(source=opportunity.source, problem=opportunity.problem,
                          signals=opportunity.signals, risk=opportunity.risk,
                          estimate=Estimate(delivery_probability=0.0, win_probability=1.0,
                                            confidence=0))
    with store.transaction() as repository:
        repository.save(minimal)
    with store.transaction() as repository:
        assert repository.get(minimal.id) == minimal


def test_identity_bindings(store: SQLiteOpportunityStore, opportunity: Opportunity) -> None:
    key = "dedupe:v1:example"
    with store.transaction() as repository:
        assert repository.resolve_identity(key) is None
        repository.save(opportunity)
        repository.bind_identity(key, opportunity.id)
        repository.bind_identity(key, opportunity.id)
        repository.bind_identity("dedupe:v2:example", opportunity.id)
        with pytest.raises(IdentityConflictError):
            repository.bind_identity(key, uuid4())
        assert repository.resolve_identity(key) == opportunity.id
        assert repository.resolve_identity("dedupe:v2:example") == opportunity.id
    with store.transaction() as repository:
        assert repository.resolve_identity(key) == opportunity.id
    assert opportunity.id.version == 4
    independent = Opportunity.model_validate(opportunity.model_dump(exclude={"id"}))
    assert independent.id.version == 4
    assert independent.id != opportunity.id


def test_identity_and_snapshot_rollback(store: SQLiteOpportunityStore,
                                        opportunity: Opportunity) -> None:
    with pytest.raises(RuntimeError, match="abort"), store.transaction() as repository:
        repository.save(opportunity)
        repository.bind_identity("dedupe:v1:rollback", opportunity.id)
        assert repository.resolve_identity("dedupe:v1:rollback") == opportunity.id
        raise RuntimeError("abort")
    with store.transaction() as repository:
        assert repository.get(opportunity.id) is None
        assert repository.resolve_identity("dedupe:v1:rollback") is None


def test_identity_survives_reopen(store: SQLiteOpportunityStore, opportunity: Opportunity,
                                  tmp_path: Path) -> None:
    with store.transaction() as repository:
        repository.save(opportunity)
        repository.bind_identity("dedupe:v1:reopen", opportunity.id)
    store.close()
    reopened = SQLiteOpportunityStore(tmp_path / "opportunities.sqlite")
    try:
        with reopened.transaction() as repository:
            assert repository.resolve_identity("dedupe:v1:reopen") == opportunity.id
            assert repository.get(opportunity.id) == opportunity
    finally:
        reopened.close()


def test_identity_repository_cannot_escape(store: SQLiteOpportunityStore) -> None:
    with store.transaction() as repository:
        pass
    with pytest.raises(RuntimeError, match="transaction has ended"):
        repository.resolve_identity("dedupe:v1:escaped")
    with pytest.raises(RuntimeError, match="transaction has ended"):
        repository.bind_identity("dedupe:v1:escaped", uuid4())


def test_delete_removes_snapshot_and_bindings(store: SQLiteOpportunityStore,
                                              opportunity: Opportunity) -> None:
    with store.transaction() as repository:
        repository.save(opportunity)
        repository.bind_identity("dedupe:v1:deleted", opportunity.id)
    with store.transaction() as repository:
        repository.delete(opportunity.id)
        repository.delete(uuid4())
    with store.transaction() as repository:
        assert repository.get(opportunity.id) is None
        assert repository.resolve_identity("dedupe:v1:deleted") is None
