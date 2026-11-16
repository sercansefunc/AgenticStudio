import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from agenticstudio.dedupe import build_dedupe_key
from agenticstudio.domain import Opportunity, Source
from agenticstudio.evaluation import BaselineEvaluator
from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionRequest,
    RecordRejection,
    RetryRequired,
    TransientFetchFailure,
)
from agenticstudio.persistence import SQLiteOpportunityStore
from agenticstudio.pipeline import SliceResult
from agenticstudio.runtime import CollectionJob, run_collection
from agenticstudio.sources.hacker_news import HackerNewsAdapter
from agenticstudio.sources.hacker_news import normalize as normalize_hn
from agenticstudio.sources.opire import OpireAdapter
from agenticstudio.sources.opire import normalize as normalize_opire

FIXTURES = Path(__file__).parent / "fixtures"


def configured_jobs() -> tuple[CollectionJob, ...]:
    hn: dict[str, Any] = json.loads((FIXTURES / "hn_freelancer.json").read_text())
    opire: dict[str, Any] = json.loads((FIXTURES / "opire.json").read_text())

    def github(url: str, headers: Mapping[str, str]) -> object:
        if "/search/issues?" in url:
            return {"incomplete_results": False, "items": [opire["issue"]]}
        if "/comments?" in url:
            return opire["comments"]
        return opire["issue"]

    return (
        CollectionJob(HackerNewsAdapter(lambda identifier: hn[str(identifier)]),
                      CollectionRequest(source_name="hacker_news", request_id="shared-run",
                                        selection="100"), normalize_hn),
        CollectionJob(OpireAdapter(github),
                      CollectionRequest(source_name="opire", request_id="shared-run",
                                        selection="all"), normalize_opire),
    )


def forbidden_normalize(record: CollectedRecord) -> Opportunity:
    raise AssertionError("persisted mappings must prevent normalization on replay")


def test_two_sources_replay_after_store_reopen(tmp_path: Path) -> None:
    path = tmp_path / "runtime.sqlite"
    store = SQLiteOpportunityStore(path)
    store.migrate()
    jobs = configured_jobs()
    try:
        result = run_collection(iter(jobs), store, BaselineEvaluator())
        assert [job.identity for job in result.jobs] == [job.identity for job in jobs]
        assert [job.result_count for job in result.jobs] == [1, 1]
        assert [job.rejection_count for job in result.jobs] == [2, 0]
        assert [job.retry_count for job in result.jobs] == [0, 0]
        for job in result.jobs:
            assert isinstance(job.outcome, SliceResult)
            assert job.predictions[0].opportunity_id == job.outcome.opportunities[0].id
    finally:
        store.close()

    reopened = SQLiteOpportunityStore(path)
    try:
        replay = run_collection(
            (CollectionJob(job.adapter, job.request, job.normalize) for job in jobs),
            reopened, BaselineEvaluator(),
        )
        for original, repeated in zip(result.jobs, replay.jobs, strict=True):
            assert isinstance(original.outcome, SliceResult)
            assert isinstance(repeated.outcome, SliceResult)
            assert original.outcome.opportunities == repeated.outcome.opportunities
            with reopened.transaction() as repository:
                opportunity = repeated.outcome.opportunities[0]
                assert repository.resolve_identity(
                    build_dedupe_key(opportunity.source).storage_key,
                ) == opportunity.id
        engine = create_engine(f"sqlite+pysqlite:///{path}")
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM opportunities")) == 2
                assert connection.scalar(text("SELECT count(*) FROM identity_mappings")) == 2
        finally:
            engine.dispose()
    finally:
        reopened.close()


class PartialFailure:
    source_name = "opire"

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        yield CollectedRecord(source_name=self.source_name, url="https://example.com/partial",
                              raw_text="partial evidence")
        raise TransientFetchFailure("temporary outage")


@pytest.mark.parametrize("retry_first", [False, True])
def test_retry_does_not_poison_successful_source(tmp_path: Path, retry_first: bool) -> None:
    hn, opire = configured_jobs()
    failed = CollectionJob(PartialFailure(), opire.request, forbidden_normalize)
    jobs = (failed, hn) if retry_first else (hn, failed)
    store = SQLiteOpportunityStore(tmp_path / "retry.sqlite")
    store.migrate()
    try:
        result = run_collection(jobs, store, BaselineEvaluator())
        success, retry = (result.jobs[1], result.jobs[0]) if retry_first else result.jobs
        assert isinstance(success.outcome, SliceResult)
        assert success.result_count == 1
        assert success.rejection_count == 2
        assert isinstance(retry.outcome, RetryRequired)
        assert retry.outcome.request == opire.request
        assert retry.outcome.reason == "temporary outage"
        assert retry.retry_count == 1
        assert retry.result_count == retry.rejection_count == 0
        assert retry.predictions == ()
        with store.transaction() as repository:
            partial_key = build_dedupe_key(Source(
                name="opire", url="https://example.com/partial", risk_class="core",
            )).storage_key
            assert repository.resolve_identity(partial_key) is None
        replay = run_collection(
            (CollectionJob(hn.adapter, hn.request, hn.normalize), opire),
            store, BaselineEvaluator(),
        )
        assert isinstance(replay.jobs[0].outcome, SliceResult)
        assert replay.jobs[0].outcome.opportunities == success.outcome.opportunities
        assert [job.result_count for job in replay.jobs] == [1, 1]
    finally:
        store.close()


@pytest.mark.parametrize("invalid", ["duplicate", "source_mismatch"])
def test_preflight_rejects_invalid_jobs_before_fetch(tmp_path: Path, invalid: str) -> None:
    class NeverFetch:
        source_name = "hacker_news"

        def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
            raise AssertionError("invalid run must not fetch any source")

    hn, opire = configured_jobs()
    first = CollectionJob(NeverFetch(), hn.request, forbidden_normalize)
    second = (CollectionJob(hn.adapter, hn.request.model_copy(update={"selection": "200"}),
                            normalize_hn) if invalid == "duplicate" else
              CollectionJob(NeverFetch(), opire.request, forbidden_normalize))
    store = SQLiteOpportunityStore(tmp_path / "invalid.sqlite")
    try:
        with pytest.raises(ValueError, match="duplicate collection job|adapter source"):
            run_collection((first, second), store, BaselineEvaluator())
        assert run_collection((), store, BaselineEvaluator()).jobs == ()
    finally:
        store.close()
