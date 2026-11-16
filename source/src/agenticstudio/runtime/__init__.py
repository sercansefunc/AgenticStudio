"""Synchronous collection orchestration with caller-owned sources and dependencies."""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from agenticstudio.domain import DomainModel, Opportunity
from agenticstudio.evaluation import Evaluator, PredictionRecord
from agenticstudio.ingestion import CollectedRecord, CollectionRequest, RetryRequired, SourceAdapter
from agenticstudio.persistence import OpportunityStore
from agenticstudio.pipeline import SliceResult, run_slice

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionJob:
    """One logical request; reuse its identity unchanged when retrying or replaying."""

    adapter: SourceAdapter
    request: CollectionRequest
    normalize: Callable[[CollectedRecord], Opportunity]

    @property
    def identity(self) -> tuple[str, str]:
        return self.request.idempotency_key


class CollectionJobResult(DomainModel):
    request: CollectionRequest
    outcome: SliceResult | RetryRequired

    @property
    def identity(self) -> tuple[str, str]:
        return self.request.idempotency_key

    @property
    def result_count(self) -> int:
        return len(self.outcome.opportunities) if isinstance(self.outcome, SliceResult) else 0

    @property
    def retry_count(self) -> int:
        return int(isinstance(self.outcome, RetryRequired))

    @property
    def rejection_count(self) -> int:
        return len(self.outcome.rejections) if isinstance(self.outcome, SliceResult) else 0

    @property
    def predictions(self) -> tuple[PredictionRecord, ...]:
        return self.outcome.predictions if isinstance(self.outcome, SliceResult) else ()


class CollectionRunResult(DomainModel):
    jobs: tuple[CollectionJobResult, ...]


def run_collection(
    jobs: Iterable[CollectionJob], store: OpportunityStore, evaluator: Evaluator,
) -> CollectionRunResult:
    """Run each slice once, in order, preserving its own commit/retry boundary.

    Validate all identities and source bindings before any fetch or write. Retries
    remain explicit outcomes; an unexpected error in one slice (its transaction rolls
    back) is reported as a retry-required outcome so other slices still run. There is
    no encompassing transaction, implicit retry, or external mutation. The caller
    owns store migration/closure and retry timing. Predictions are fresh on replay;
    persisted Opportunity identities are reused by the shared pipeline.
    """
    configured = tuple(jobs)
    identities: set[tuple[str, str]] = set()
    for job in configured:
        if job.identity in identities:
            raise ValueError(f"duplicate collection job identity: {job.identity!r}")
        identities.add(job.identity)
        if job.adapter.source_name != job.request.source_name:
            raise ValueError("adapter source must match request source")
    return CollectionRunResult(jobs=tuple(
        CollectionJobResult(request=job.request, outcome=_run_isolated(job, store, evaluator))
        for job in configured
    ))


def _run_isolated(
    job: CollectionJob, store: OpportunityStore, evaluator: Evaluator,
) -> SliceResult | RetryRequired:
    """One source failing unexpectedly must not discard the other sources' results."""
    name = f"{job.request.source_name}/{job.request.selection}"
    _log.info("%s: collecting", name)
    outcome: SliceResult | RetryRequired
    try:
        outcome = run_slice(job.adapter, job.request, job.normalize, store, evaluator)
    except Exception as error:
        outcome = RetryRequired(request=job.request,
                                reason=f"unexpected {type(error).__name__}: {str(error)[:200]}")
    if isinstance(outcome, SliceResult):
        _log.info("%s: %d opportunities", name, len(outcome.opportunities))
    else:
        _log.warning("%s: retry required (%s)", name, outcome.reason)
    return outcome
