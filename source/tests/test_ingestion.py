from collections.abc import Iterable
from datetime import datetime

import pytest
from pydantic import ValidationError

from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionBatch,
    CollectionRequest,
    RecordRejection,
    RetryRequired,
    SourceAdapter,
    TransientFetchFailure,
    collect,
)


class Adapter:
    source_name = "example"

    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        yield record()
        if self.failure is not None:
            raise self.failure
        yield RecordRejection(identity=record().identity, reason="Unsupported content")


def record(**changes: object) -> CollectedRecord:
    return CollectedRecord.model_validate({
        "source_name": "example", "external_id": "123",
        "url": "https://example.com/123", "raw_text": "Build a report", **changes,
    })


def request() -> CollectionRequest:
    return CollectionRequest(source_name="example", request_id="run-1", selection="window-1")


def test_identity_prefers_external_id_and_is_source_scoped() -> None:
    assert record().identity == record(url="https://example.com/changed").identity
    assert record().identity != record(source_name="other").identity
    assert record().identity != record(external_id=None, url="123").identity


def test_url_fallback_is_exact_and_explicit() -> None:
    item = record(external_id=None, url="https://example.com/123?view=all")
    assert item.identity.kind == "url"
    assert item.identity.value == item.url
    assert item.identity != record(external_id=None).identity


def test_replay_preserves_request_key_and_record_identities() -> None:
    adapter: SourceAdapter = Adapter()
    first = collect(adapter, request())
    second = collect(adapter, request())
    assert first == second
    assert isinstance(first, CollectionBatch)
    assert first.request.idempotency_key == ("example", "run-1")
    next_request = request().model_copy(update={"request_id": "run-2"})
    assert next_request.idempotency_key != first.request.idempotency_key
    assert isinstance(first.items[1], RecordRejection)


def test_transient_iteration_failure_discards_partial_batch_and_can_retry() -> None:
    result = collect(Adapter(TransientFetchFailure("rate limited")), request())
    assert isinstance(result, RetryRequired)
    assert result.request == request()
    assert result.reason == "rate limited"
    assert not hasattr(result, "items")
    assert isinstance(collect(Adapter(), result.request), CollectionBatch)
    with pytest.raises(ValidationError):
        RecordRejection.model_validate(result.model_dump())


def test_immediate_fetch_failure_is_retryable() -> None:
    class FailingAdapter(Adapter):
        def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
            raise TransientFetchFailure("timeout")

    assert isinstance(collect(FailingAdapter(), request()), RetryRequired)


def test_unexpected_failures_propagate() -> None:
    with pytest.raises(RuntimeError, match="configuration"):
        collect(Adapter(RuntimeError("configuration")), request())


def test_source_mismatch_is_an_error() -> None:
    with pytest.raises(ValueError, match="adapter source"):
        collect(Adapter(), request().model_copy(update={"source_name": "other"}))
    with pytest.raises(ValidationError, match="batch item source"):
        CollectionBatch(request=request(), items=(record(source_name="other"),))


def test_empty_success_and_duplicate_records_keep_stable_identity() -> None:
    assert CollectionBatch(request=request(), items=()).items == ()
    batch = CollectionBatch(request=request(), items=(record(), record()))
    assert batch.items[0].identity == batch.items[1].identity


@pytest.mark.parametrize("field", ["source_name", "external_id", "url", "raw_text"])
@pytest.mark.parametrize("value", ["", "   ", 123])
def test_invalid_record_inputs(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        record(**{field: value})


def test_contract_rejects_source_specific_and_business_fields() -> None:
    for field in ("hn_points", "score", "decision", "adapter_private"):
        with pytest.raises(ValidationError):
            record(**{field: 1})
    with pytest.raises(ValidationError):
        record(posted_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(record(), "raw_text", "changed")
