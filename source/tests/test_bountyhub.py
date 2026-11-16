"""Synthetic public GitHub responses; no live network."""

import json
import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from email.message import Message
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
from agenticstudio.pipeline import run_slice
from agenticstudio.sources.bountyhub import API, QUERY, BountyHubAdapter, github_fetch, normalize

REQUEST = CollectionRequest(source_name="bountyhub", selection="all", request_id="offline")
FIXTURE = Path(__file__).parent / "fixtures/bountyhub.json"


class OfflineGitHub:
    def __init__(self, token: str | None = None) -> None:
        self.data: dict[str, Any] = json.loads(FIXTURE.read_text())
        self.calls: list[str] = []
        self.token = token

    def __call__(self, url: str, headers: Mapping[str, str]) -> object:
        self.calls.append(url)
        assert headers.get("Authorization") == (f"Bearer {self.token}" if self.token else None)
        assert url.startswith(API)
        if "/search/issues?" in url:
            query = parse_qs(urlparse(url).query)
            assert query["q"] == [QUERY]
            assert query["sort"] == ["updated"] and query["order"] == ["desc"]
            assert int(query["per_page"][0]) <= 50
            return {"incomplete_results": False, "items": [self.data["issue"]] * 2}
        if "/comments?" in url:
            return self.data["comments"]
        raise AssertionError(f"unexpected issue endpoint: {url}")


def event(body: str, identifier: int = 2) -> dict[str, object]:
    return {"id": identifier, "created_at": "2026-01-03T00:00:00Z",
            "user": {"login": "bountyhub-bot"}, "body": body}


@pytest.mark.parametrize("token", [None, "offline-secret"])
def test_initial_exact_reward_identity_deduplication(token: str | None) -> None:
    load = OfflineGitHub(token)
    batch = collect(BountyHubAdapter(load, token=token), REQUEST)
    assert isinstance(batch, CollectionBatch) and len(batch.items) == 1
    record = batch.items[0]
    assert isinstance(record, CollectedRecord)
    opportunity = normalize(record)
    assert opportunity.source.external_id == "example/project#37"
    assert record.identity.value == opportunity.source.external_id
    assert opportunity.source.url == load.data["issue"]["html_url"]
    assert opportunity.commercial.currency == "USD"
    assert opportunity.commercial.budget_min == Decimal("100.25")
    assert opportunity.commercial.budget_max == Decimal("100.25")
    assert opportunity.estimate.confidence == 0
    assert "claim_url" in json.loads(record.raw_text)
    assert len(load.calls) == 2
    load.data["issue"]["title"] = "Changed title"
    second = collect(BountyHubAdapter(load, token=token), REQUEST)
    assert isinstance(second, CollectionBatch)
    assert second.items[0].identity == record.identity


def test_explicit_latest_total_and_chronological_order() -> None:
    load = OfflineGitHub()
    update = event(
        """### 🚀 Bounty Update!

💰 **A contribution of** $25.00 **has been made by** [example](https://github.com/example)
**on BountyHub** for this issue.
**The total amount of the bounty is now** $125.25

🔗 **[Claim this bounty by submitting a pull request that solves this issue!]"""
        """(https://www.bountyhub.dev/bounty/view/example)**

Good luck, and happy coding! 💻"""
    )
    load.data["comments"].append(update)
    ignored = event("Bounty cancelled", 3)
    ignored["user"] = {"login": "someone-else"}
    ignored["created_at"] = "invalid ignored timestamp"
    load.data["comments"].append(ignored)
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], CollectedRecord)
    assert normalize(batch.items[0]).commercial.budget_max == Decimal("125.25")


def test_real_shaped_five_thousand_plus_five_thousand_update() -> None:
    load = OfflineGitHub()
    load.data["comments"][0]["body"] = """### 🚀 Bounty Alert!

💰 **A bounty of** $5000.00 **has been created by** [owner](https://github.com/owner)
**on BountyHub** for this issue.

🔗 **[Claim this bounty by submitting a pull request that solves this issue!]"""
    load.data["comments"][0]["body"] += (
        "(https://www.bountyhub.dev/bounty/view/real-shaped)**\n"
    )
    load.data["comments"].append(event(
        """### 🚀 Bounty Update!

💰 **A contribution of** $5000.00 **has been made by** [other](https://github.com/other)
**on BountyHub** for this issue.
**The total amount of the bounty is now** $10000

🔗 **[Claim this bounty by submitting a pull request that solves this issue!]"""
        """(https://www.bountyhub.dev/bounty/view/real-shaped)**"""
    ))
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], CollectedRecord)
    opportunity = normalize(batch.items[0])
    assert opportunity.commercial.budget_min == Decimal("10000")
    assert opportunity.commercial.budget_max == Decimal("10000")


@pytest.mark.parametrize("body", [
    "Bounty Alert! $100 USD", "Bounty Alert! $0 https://www.bountyhub.dev/bounty/view/x",
    "Bounty Alert! $10.123 https://www.bountyhub.dev/bounty/view/x",
    "Bounty Alert! $10 https://www.bountyhub.dev.evil/bounty/view/x",
    "Bounty Alert! $10 and $20 https://www.bountyhub.dev/bounty/view/x",
    "Bounty Alert! no amount https://www.bountyhub.dev/bounty/view/x",
])
def test_malformed_initial_rejected(body: str) -> None:
    load = OfflineGitHub()
    load.data["comments"][0]["body"] = body
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch) and isinstance(batch.items[0], RecordRejection)


@pytest.mark.parametrize("body", [
    "Bounty cancelled", "Bounty rewarded", "Bounty closed", "Bounty withdrawn",
    "Bounty Update! contributed $20", "Unrecognized bounty event",
    "Bounty Update! The total amount of the bounty is now $0",
])
def test_unavailable_or_ambiguous_later_evidence_rejected(body: str) -> None:
    load = OfflineGitHub()
    load.data["comments"].append(event(body))
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch) and isinstance(batch.items[0], RecordRejection)


@pytest.mark.parametrize("change", [{"state": "closed"}, {"pull_request": {}},
                                    {"title": 42}, {"body": []}])
def test_invalid_issue_rejected(change: dict[str, object]) -> None:
    load = OfflineGitHub()
    load.data["issue"].update(change)
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch) and isinstance(batch.items[0], RecordRejection)


@pytest.mark.parametrize("code,headers,body", [
    (408, {}, b""), (429, {}, b""), (503, {}, b""),
    (403, {"X-RateLimit-Remaining": "0"}, b""),
    (403, {"Retry-After": "60"}, b""), (403, {}, b"secondary rate limit"),
])
def test_http_retry(monkeypatch: pytest.MonkeyPatch, code: int,
                    headers: dict[str, str], body: bytes) -> None:
    message = Message()
    for key, value in headers.items():
        message[key] = value

    def fail(request: Request, timeout: int) -> object:
        raise HTTPError(request.full_url, code, "failure", message, BytesIO(body))

    monkeypatch.setattr("agenticstudio.sources.bountyhub.urlopen", fail)
    with pytest.raises(TransientFetchFailure):
        github_fetch(API + "/search/issues", {})


def test_transport_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(request: Request, timeout: int) -> object:
        raise URLError("offline")

    monkeypatch.setattr("agenticstudio.sources.bountyhub.urlopen", fail)
    with pytest.raises(TransientFetchFailure):
        github_fetch(API + "/search/issues", {})


def test_transient_after_record_has_no_partial_persistence(tmp_path: Path) -> None:
    load = OfflineGitHub()

    def fail_late(url: str, headers: Mapping[str, str]) -> object:
        if "/search/issues?" in url:
            other = {**load.data["issue"],
                     "html_url": "https://github.com/example/project/issues/38",
                     "comments_url": API + "/repos/example/project/issues/38/comments"}
            return {"incomplete_results": False, "items": [load.data["issue"], other]}
        if "/38/comments?" in url:
            raise TransientFetchFailure("temporary outage")
        return load(url, headers)

    store = SQLiteOpportunityStore(tmp_path / "retry.sqlite")
    store.migrate()
    try:
        outcome = run_slice(BountyHubAdapter(fail_late), REQUEST, normalize,
                            store, BaselineEvaluator())
        assert isinstance(outcome, RetryRequired)
        with sqlite3.connect(tmp_path / "retry.sqlite") as connection:
            assert connection.execute("SELECT count(*) FROM opportunities").fetchone() == (0,)
            assert connection.execute("SELECT count(*) FROM identity_mappings").fetchone() == (0,)
    finally:
        store.close()


def test_candidate_bound() -> None:
    load = OfflineGitHub()

    def many(url: str, headers: Mapping[str, str]) -> object:
        load.calls.append(url)
        if "/search/issues?" in url:
            return {"incomplete_results": False, "items": [
                {**load.data["issue"],
                 "html_url": f"https://github.com/example/project/issues/{i}",
                 "comments_url": f"{API}/repos/example/project/issues/{i}/comments"}
                for i in range(1, 61)]}
        assert "/comments?" in url
        return load.data["comments"]

    batch = collect(BountyHubAdapter(many), REQUEST)
    assert isinstance(batch, CollectionBatch) and len(batch.items) == 30
    assert all(isinstance(item, CollectedRecord) for item in batch.items)
    assert len(load.calls) == 31
    with pytest.raises(ValueError):
        BountyHubAdapter(many, max_candidates=51)


def test_complete_comment_history_required() -> None:
    load = OfflineGitHub()

    def paged(url: str, headers: Mapping[str, str]) -> object:
        if "/comments?" in url:
            if parse_qs(urlparse(url).query)["page"] == ["1"]:
                return [load.data["comments"][0], *[
                    {"user": {"login": "reader"}} for _ in range(99)]]
            return [event("Bounty closed")]
        return load(url, headers)

    for pages in (1, 2):
        batch = collect(BountyHubAdapter(paged, max_comment_pages=pages), REQUEST)
        assert isinstance(batch, CollectionBatch) and isinstance(batch.items[0], RecordRejection)


def test_incomplete_search_retry() -> None:
    batch = collect(BountyHubAdapter(lambda url, headers: {
        "items": [], "incomplete_results": True}), REQUEST)
    assert isinstance(batch, RetryRequired)


def test_malformed_bot_timestamp_rejected() -> None:
    load = OfflineGitHub()
    load.data["comments"][0]["created_at"] = "invalid"
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch) and isinstance(batch.items[0], RecordRejection)


@pytest.mark.parametrize("comments_url", [
    None,
    "https://evil.example/repos/example/project/issues/37/comments",
    "https://api.github.com.evil/repos/example/project/issues/37/comments",
    "https://api.github.com/repos/other/project/issues/37/comments",
    "https://api.github.com/repos/example/project/issues/999/comments",
    "https://api.github.com/repos/example/project/issues/37/comments?redirect=evil",
])
def test_forged_comments_url_rejected_without_fetch(comments_url: object) -> None:
    load = OfflineGitHub()
    load.data["issue"]["comments_url"] = comments_url
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], RecordRejection)
    assert len(load.calls) == 1


@pytest.mark.parametrize("count", [1, 3, 30])
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

    batch = collect(BountyHubAdapter(bounded, max_candidates=count), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert len(batch.items) == count
    assert all(isinstance(item, CollectedRecord) for item in batch.items)
    assert len(calls) == 1 + count


@pytest.mark.parametrize("created_at", [None, "invalid", "2026-01-01T00:00:00"])
def test_search_timestamp_conservative_fallback(created_at: object) -> None:
    load = OfflineGitHub()
    load.data["issue"]["created_at"] = created_at
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], CollectedRecord)
    assert batch.items[0].posted_at is None
    assert len(load.calls) == 2


def test_labels_become_capabilities_unfiltered() -> None:
    from agenticstudio.sources.bountyhub import normalize

    load = OfflineGitHub()
    load.data["issue"]["labels"] = [{"name": "Python"}, {"name": "help wanted"}, {"name": "bug"}]
    batch = collect(BountyHubAdapter(load), REQUEST)
    assert isinstance(batch, CollectionBatch)
    record = batch.items[0]
    assert isinstance(record, CollectedRecord)
    assert normalize(record).estimate.required_capabilities == ("Python", "help wanted", "bug")
