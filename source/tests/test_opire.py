import json
from collections.abc import Mapping
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import pytest

from agenticstudio.evaluation import BaselineEvaluator
from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionBatch,
    CollectionRequest,
    RecordRejection,
    RetryRequired,
    TransientFetchFailure,
    collect,
)
from agenticstudio.persistence import SQLiteOpportunityStore
from agenticstudio.pipeline import SliceResult, run_slice
from agenticstudio.sources.opire import API, OpireAdapter, github_fetch, normalize

REQUEST = CollectionRequest(source_name="opire", request_id="offline", selection="all")
URL = "https://github.com/example/project/issues/22"
FIXTURE = Path(__file__).parent / "fixtures" / "opire.json"


def fixture() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(FIXTURE.read_text())
    return result


class OfflineGitHub:
    def __init__(self) -> None:
        self.data = fixture()
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str]) -> object:
        self.calls.append(url)
        assert url.startswith(API)
        if "/search/issues?" in url:
            query = parse_qs(urlparse(url).query)
            assert 'is:issue is:open in:comments "How to earn this reward?"' in query["q"][0]
            return {"incomplete_results": False, "items": [self.data["issue"]]}
        if "/comments?" in url:
            return self.data["comments"]
        raise AssertionError(f"unexpected issue endpoint: {url}")


def test_available_exact_money_and_identity() -> None:
    load = OfflineGitHub()
    batch = collect(OpireAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    record = batch.items[0]
    assert isinstance(record, CollectedRecord)
    assert record.identity.kind == "url"
    assert record.identity.value == URL
    assert json.loads(record.raw_text)["rewards"] == ["0.10", "0.20"]
    opportunity = normalize(record)
    assert opportunity.commercial.budget_min == Decimal("0.30")
    assert opportunity.commercial.budget_max == Decimal("0.30")
    assert opportunity.commercial.currency == "USD"
    assert opportunity.commercial.pricing_model == "bounty"
    assert opportunity.source.risk_class == "core"
    load.data["issue"]["title"] = "Changed title"
    changed = collect(OpireAdapter(load), REQUEST)
    assert isinstance(changed, CollectionBatch)
    assert changed.items[0].identity == record.identity


@pytest.mark.parametrize("mode", ["closed", "paid", "malformed", "mismatch", "ambiguous"])
def test_unavailable_and_malformed_rejected(mode: str) -> None:
    load = OfflineGitHub()
    if mode == "closed":
        load.data["issue"]["state"] = "closed"
    elif mode == "paid":
        load.data["comments"].append({"id": 5, "created_at": "2026-01-05T00:00:00Z",
                                      "user": {"login": "opirebot[bot]"},
                                      "body": "claimed all rewards for this issue"})
    elif mode == "malformed":
        load.data["comments"][0]["created_at"] = "bad date"
    elif mode == "mismatch":
        load.data["comments"][3]["body"] = (
            "/reward 10<details><summary>How to earn this $20.00 reward?")
    else:
        load.data["comments"][0]["body"] = "A reward was paid"
    batch = collect(OpireAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert len(batch.items) == 1
    assert isinstance(batch.items[0], RecordRejection)


def test_untrusted_claim_does_not_clear_rewards() -> None:
    load = OfflineGitHub()
    load.data["comments"][2]["user"]["login"] = "imposter"
    batch = collect(OpireAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], CollectedRecord)
    assert normalize(batch.items[0]).commercial.budget_min == Decimal("30.30")


def test_reopen_replay_shared_pipeline(tmp_path: Path) -> None:
    path = tmp_path / "opire.sqlite"
    store = SQLiteOpportunityStore(path)
    store.migrate()
    try:
        result = run_slice(OpireAdapter(OfflineGitHub()), REQUEST, normalize, store,
                           BaselineEvaluator())
        assert isinstance(result, SliceResult)
        assert len(result.opportunities) == len(result.predictions) == 1
        opportunity = result.opportunities[0]
        assert opportunity.id.version == 4
        assert result.predictions[0].opportunity_id == opportunity.id
    finally:
        store.close()
    reopened = SQLiteOpportunityStore(path)
    try:
        replay = run_slice(OpireAdapter(OfflineGitHub()), REQUEST, normalize, reopened,
                           BaselineEvaluator())
        assert isinstance(replay, SliceResult)
        assert replay.opportunities == (opportunity,)
    finally:
        reopened.close()


def test_transient_failure_has_no_pipeline_writes(tmp_path: Path) -> None:
    def fail(url: str, headers: Mapping[str, str]) -> object:
        raise TransientFetchFailure("outage")

    store = SQLiteOpportunityStore(tmp_path / "retry.sqlite")
    store.migrate()
    try:
        result = run_slice(OpireAdapter(fail), REQUEST, normalize, store, BaselineEvaluator())
        assert isinstance(result, RetryRequired)
        assert result.request == REQUEST
    finally:
        store.close()


@pytest.mark.parametrize("code,headers,body", [
    (429, {}, b""), (503, {}, b""), (403, {"X-RateLimit-Remaining": "0"}, b""),
    (403, {"Retry-After": "60"}, b""), (403, {}, b"secondary rate limit"),
])
def test_http_transient(monkeypatch: pytest.MonkeyPatch, code: int,
                        headers: dict[str, str], body: bytes) -> None:
    from email.message import Message

    message = Message()
    for key, value in headers.items():
        message[key] = value

    def fail(request: Request, timeout: int) -> object:
        raise HTTPError(request.full_url, code, "failure", message, BytesIO(body))

    monkeypatch.setattr("agenticstudio.sources.opire.urlopen", fail)
    with pytest.raises(TransientFetchFailure):
        github_fetch(API + "/search/issues", {})


def test_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request: Request, timeout: int) -> object:
        raise URLError("offline")

    monkeypatch.setattr("agenticstudio.sources.opire.urlopen", fail)
    with pytest.raises(TransientFetchFailure):
        github_fetch(API + "/search/issues", {})


def test_comment_pagination_and_truncation() -> None:
    load = OfflineGitHub()
    calls: list[str] = []

    def paged(url: str, headers: Mapping[str, str]) -> object:
        calls.append(url)
        if "/comments?" in url:
            if parse_qs(urlparse(url).query)["page"] == ["1"]:
                return [{"id": 100 + i, "created_at": "2025-01-01T00:00:00Z",
                         "user": {"login": "reader"}, "body": "discussion"}
                        for i in range(100)]
            return load.data["comments"]
        return load(url, headers)

    batch = collect(OpireAdapter(paged, max_comment_pages=2), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], CollectedRecord)
    assert any("page=2" in url for url in calls)
    truncated = collect(OpireAdapter(paged, max_comment_pages=1), REQUEST)
    assert isinstance(truncated, CollectionBatch)
    assert isinstance(truncated.items[0], RecordRejection)


def test_candidate_search_bounds_and_token() -> None:
    calls: list[str] = []
    load = OfflineGitHub()

    def search(url: str, headers: Mapping[str, str]) -> object:
        calls.append(url)
        assert headers["Authorization"] == "Bearer test-token"
        assert headers["X-Test"] == "injected"
        if "/search/issues?" in url:
            page = int(parse_qs(urlparse(url).query)["page"][0])
            return {"incomplete_results": False, "items": [
                dict(load.data["issue"], html_url=f"https://github.com/example/project/issues/{i}",
                     comments_url=f"{API}/repos/example/project/issues/{i}/comments")
                for i in range((page - 1) * 100 + 1, page * 100 + 1)]}
        if "/comments?" in url:
            return load.data["comments"]
        raise AssertionError(f"unexpected issue endpoint: {url}")

    batch = collect(OpireAdapter(search, token="test-token", headers={"X-Test": "injected"},
                                 max_candidates=101), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert len(batch.items) == 101
    assert len(calls) == 103  # Two search pages plus comments per candidate.


def test_incomplete_search_retries() -> None:
    batch = collect(OpireAdapter(lambda url, headers: {
        "incomplete_results": True, "items": []}), REQUEST)
    assert isinstance(batch, RetryRequired)


@pytest.mark.parametrize("comments_url", [
    None,
    "https://evil.example/repos/example/project/issues/22/comments",
    "https://api.github.com.evil/repos/example/project/issues/22/comments",
    "https://api.github.com/repos/other/project/issues/22/comments",
    "https://api.github.com/repos/example/project/issues/999/comments",
    "https://api.github.com/repos/example/project/issues/22/comments?redirect=evil",
])
def test_forged_comments_url_rejected_without_fetch(comments_url: object) -> None:
    load = OfflineGitHub()
    load.data["issue"]["comments_url"] = comments_url
    batch = collect(OpireAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], RecordRejection)
    assert len(load.calls) == 1


@pytest.mark.parametrize("count", [1, 3, 20])
def test_normal_candidate_request_budget(count: int) -> None:
    load = OfflineGitHub()
    calls: list[str] = []

    def bounded(url: str, headers: Mapping[str, str]) -> object:
        calls.append(url)
        if "/search/issues?" in url:
            assert parse_qs(urlparse(url).query)["page"] == ["1"]
            return {"incomplete_results": False, "items": [
                {**load.data["issue"],
                 "html_url": f"https://github.com/example/project/issues/{i}",
                 "comments_url": f"{API}/repos/example/project/issues/{i}/comments"}
                for i in range(1, 101)]}
        assert "/comments?per_page=100&page=1" in url
        return load.data["comments"]

    batch = collect(OpireAdapter(bounded, max_candidates=count), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert len(batch.items) == count
    assert all(isinstance(item, CollectedRecord) for item in batch.items)
    assert len(calls) == 1 + count


@pytest.mark.parametrize("change", [
    {"pull_request": {}}, {"title": 42}, {"body": []},
    {"created_at": "invalid"}, {"created_at": "2026-01-01T00:00:00"},
])
def test_invalid_search_snapshot_rejected(change: dict[str, object]) -> None:
    load = OfflineGitHub()
    load.data["issue"].update(change)
    batch = collect(OpireAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], RecordRejection)
    assert len(load.calls) == 1
