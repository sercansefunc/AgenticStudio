"""Synchronous ingestion contracts, independent of storage and business decisions.

Retry the same CollectionRequest after a transient failure. A batch is exposed only
once fetching/parsing finishes, including lazy iteration. Consumers must upsert by
SourceRecordIdentity and commit the batch with its request key atomically before
advancing their checkpoint. Replaying a committed request must have no additional
side effects. This module supplies identity inputs, not dedupe or storage logic.
"""

from collections.abc import Iterable
from typing import Annotated, Literal, Protocol, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class IngestionModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class SourceRecordIdentity(IngestionModel):
    """Exact, case-sensitive source namespace plus external ID or URL fallback.

    External IDs take precedence. Without one, use the exact stable URL supplied
    by the adapter: no query stripping, case folding, or URL normalization occurs.
    Adapters must consistently supply canonical source URLs. A changed fallback
    URL or later introduction of an external ID creates a different identity;
    reconciliation belongs to downstream dedupe, not this contract.
    """

    source_name: NonBlank
    kind: Literal["external_id", "url"]
    value: NonBlank


class CollectedRecord(IngestionModel):
    """Parsed source evidence for normalization; no scores or business status."""

    source_name: NonBlank
    url: NonBlank
    external_id: NonBlank | None = None
    title: str | None = None
    raw_text: NonBlank
    posted_at: AwareDatetime | None = None

    @property
    def identity(self) -> SourceRecordIdentity:
        return SourceRecordIdentity(
            source_name=self.source_name,
            kind="external_id" if self.external_id is not None else "url",
            value=self.external_id if self.external_id is not None else self.url,
        )


class CollectionRequest(IngestionModel):
    """Caller-assigned stable selection, reused unchanged across retry attempts.

    selection is an opaque cursor/window understood by the adapter. It must not
    depend on the current clock. request_id identifies this logical run (not an
    attempt); a subsequent polling run gets a new ID even for the same selection.
    """

    source_name: NonBlank
    request_id: NonBlank
    selection: NonBlank

    @property
    def idempotency_key(self) -> tuple[str, str]:
        return (self.source_name, self.request_id)


class RecordRejection(IngestionModel):
    """Permanent input rejection, e.g. unsupported/malformed source content.

    Never use for network errors, rate limits, authentication/configuration errors,
    or economic/business rejection of an Opportunity. Requires a stable identity
    so a downstream consumer can record the same rejection idempotently.
    """

    identity: SourceRecordIdentity
    reason: NonBlank


class TransientFetchFailure(Exception):
    """Retryable transport/source failure; never converted to RecordRejection."""


class SourceAdapter(Protocol):
    """Owns fetching and parsing only, with no scoring or persistence methods.

    Yield records or permanent input rejections for the requested selection.
    Raise TransientFetchFailure on retryable failure, even during iteration.
    Other exceptions propagate: programming/configuration failures must not be
    silently retried or recorded as permanent input/business rejections.
    """

    @property
    def source_name(self) -> str: ...

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]: ...


class CollectionBatch(IngestionModel):
    request: CollectionRequest
    items: tuple[CollectedRecord | RecordRejection, ...]

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        for item in self.items:
            if item.identity.source_name != self.request.source_name:
                raise ValueError("batch item source must match request source")
        return self


class RetryRequired(IngestionModel):
    """No partial batch or checkpoint may be committed; retry this request."""

    request: CollectionRequest
    reason: str


def collect(adapter: SourceAdapter, request: CollectionRequest) -> CollectionBatch | RetryRequired:
    """Materialize one attempt without side effects, sleeping, or implicit retries.

    Repeated records retain their identities; downstream upserts handle repeats.
    Transient failure discards the entire partial batch. The caller controls retry
    timing and attempt limits, and only successful batches may advance checkpoints.
    """
    if adapter.source_name != request.source_name:
        raise ValueError("adapter source must match request source")
    try:
        items = tuple(adapter.fetch(request))
    except TransientFetchFailure as error:
        return RetryRequired(request=request, reason=str(error))
    return CollectionBatch(request=request, items=items)
