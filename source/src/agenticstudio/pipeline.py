"""Shared collection slice, with source normalization and evaluation injected."""

from collections.abc import Callable
from dataclasses import dataclass

from agenticstudio.dedupe import build_dedupe_key
from agenticstudio.domain import DomainModel, Opportunity
from agenticstudio.evaluation import Evaluator, PredictionRecord
from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionRequest,
    RecordRejection,
    RetryRequired,
    SourceAdapter,
    collect,
)
from agenticstudio.persistence import OpportunityStore


@dataclass(frozen=True)
class _DedupeSource:
    name: str
    url: str
    external_id: str | None


class SliceResult(DomainModel):
    opportunities: tuple[Opportunity, ...]
    predictions: tuple[PredictionRecord, ...]
    rejections: tuple[RecordRejection, ...]


def run_slice(adapter: SourceAdapter, request: CollectionRequest,
              normalize: Callable[[CollectedRecord], Opportunity], store: OpportunityStore,
              evaluator: Evaluator) -> SliceResult | RetryRequired:
    batch = collect(adapter, request)
    if isinstance(batch, RetryRequired):
        return batch
    opportunities: dict[str, Opportunity] = {}
    rejections: list[RecordRejection] = []
    with store.transaction() as repository:
        for item in batch.items:
            if isinstance(item, RecordRejection):
                rejections.append(item)
                continue
            source_identity = _DedupeSource(
                name=item.source_name, url=item.url, external_id=item.external_id,
            )
            key = build_dedupe_key(source_identity).storage_key
            identifier = repository.resolve_identity(key)
            if identifier is None:
                opportunity = normalize(item)
                if build_dedupe_key(opportunity.source).storage_key != key:
                    raise ValueError("normalization changed source identity")
                repository.save(opportunity)
                repository.bind_identity(key, opportunity.id)
            else:
                stored = repository.get(identifier)
                if stored is None:
                    raise RuntimeError("identity mapping references missing Opportunity")
                opportunity = stored
                if stored.decision.status == "new":
                    observed = normalize(item)
                    if build_dedupe_key(observed.source).storage_key != key:
                        raise ValueError("normalization changed source identity")
                    preserved_money = (
                        stored.estimate.execution_cost, stored.estimate.expected_value,
                        stored.outcome.revenue_won, stored.outcome.revenue_collected,
                        stored.outcome.actual_execution_cost,
                    )
                    if (stored.commercial.currency != observed.commercial.currency
                            and any(value is not None for value in preserved_money)):
                        raise ValueError("Source currency conflicts with stored monetary history; "
                                         "human review required")
                    # Refresh source evidence only; retain human/business-owned state.
                    opportunity = stored.model_copy(update={
                        "source": observed.source,
                        "posted_at": observed.posted_at,
                        "problem": observed.problem,
                        "commercial": observed.commercial,
                        "estimate": stored.estimate.model_copy(update={
                            "required_capabilities": observed.estimate.required_capabilities,
                        }),
                        "signals": stored.signals.model_copy(update={
                            "repo_available": observed.signals.repo_available,
                            "competition_estimate": observed.signals.competition_estimate,
                        }),
                    })
                    repository.save(opportunity)
            opportunities[key] = opportunity
    predictions = tuple(evaluator.evaluate(item) for item in opportunities.values())
    for opportunity, prediction in zip(opportunities.values(), predictions, strict=True):
        if prediction.opportunity_id != opportunity.id:
            raise ValueError("evaluator returned prediction for another Opportunity")
    return SliceResult(opportunities=tuple(opportunities.values()), predictions=predictions,
                       rejections=tuple(rejections))
