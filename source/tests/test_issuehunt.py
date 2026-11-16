"""Offline, sanitized rendered HTML; never use client-side page data."""

import json
import sqlite3
from decimal import Decimal
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from agenticstudio import daily
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
from agenticstudio.sources.issuehunt import (
    BASE,
    IssueHuntAdapter,
    IssueHuntBlocked,
    normalize,
    public_fetch,
)

REQUEST = CollectionRequest(source_name="issuehunt", selection="all", request_id="offline")
FIXTURES = Path(__file__).parent / "fixtures"


class Offline:
    def __init__(self) -> None:
        self.listing = (FIXTURES / "issuehunt_listing.html").read_text()
        self.detail = (FIXTURES / "issuehunt_detail.html").read_text()
        self.calls: list[str] = []
        self.pauses: list[float] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        assert url.startswith(BASE)
        if "/issues?page=" in url:
            return self.listing
        assert url == BASE + "/r/example/project/issues/37"
        return self.detail

    def adapter(self, *, max_pages: int = 1, max_results: int = 10) -> IssueHuntAdapter:
        return IssueHuntAdapter(self, pause=self.pauses.append,
                                max_pages=max_pages, max_results=max_results)


def test_funded_identity_exact_money_competition_and_unknowns() -> None:
    offline = Offline()
    batch = collect(offline.adapter(), REQUEST)
    assert isinstance(batch, CollectionBatch) and len(batch.items) == 1
    record = batch.items[0]
    assert isinstance(record, CollectedRecord)
    assert record.external_id == "example/project#37"
    evidence = json.loads(record.raw_text)
    assert evidence["state"] == "open"
    assert evidence["repository"] == "example/project"
    assert evidence["issue_number"] == 37
    assert evidence["pull_request_count"] == 1
    assert evidence["bounty_request_count"] == 2
    assert evidence["currency"] == "USD"
    assert "currency_evidence_url" not in evidence
    opportunity = normalize(record)
    assert opportunity.commercial.budget_min == Decimal("100.25")
    assert opportunity.commercial.budget_max == Decimal("100.25")
    assert opportunity.commercial.currency == "USD"
    assert opportunity.commercial.pricing_model == "bounty"
    assert opportunity.signals.repo_available is True
    assert opportunity.estimate.confidence == 0
    assert "claim and payment unknown" in opportunity.problem.summary
    assert offline.pauses == [2.0]
    assert len(offline.calls) == 2


def test_duplicates_across_pages_and_title_changes_keep_identity() -> None:
    offline = Offline()
    offline.listing *= 2
    batch = collect(offline.adapter(max_pages=3), REQUEST)
    assert isinstance(batch, CollectionBatch) and len(batch.items) == 1
    assert len(offline.calls) == 4
    offline.listing = offline.listing.replace("Add issue tags", "Renamed issue")
    second = collect(offline.adapter(), REQUEST)
    assert isinstance(second, CollectionBatch)
    assert second.items[0].identity == batch.items[0].identity


def test_missing_competition_is_unknown() -> None:
    offline = Offline()
    offline.listing = offline.listing.replace("1 pull request", "")
    offline.listing = offline.listing.replace("2 bounty requests", "")
    batch = collect(offline.adapter(), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], CollectedRecord)
    evidence = json.loads(batch.items[0].raw_text)
    assert evidence["pull_request_count"] is None
    assert evidence["bounty_request_count"] is None


def test_title_cannot_supply_competition_or_currency_evidence() -> None:
    offline = Offline()
    offline.listing = offline.listing.replace("Add issue tags", "42 pull requests CAD $500")
    batch = collect(offline.adapter(), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], CollectedRecord)
    evidence = json.loads(batch.items[0].raw_text)
    assert evidence["pull_request_count"] == 1
    assert evidence["reward"] == "100.25"


@pytest.mark.parametrize("old,new", [
    ("100.25", "0.00"), ("100.25", "10.123"), ("100.25", "NaN"),
    ("<span>$</span>", "<span>CAD $</span>"),
    (">Funded<", ">Rewarded<"), (" open ", " closed "),
    (">example/project<", ">other/project<"),
])
def test_reject_bad_listing_without_detail_fetch(old: str, new: str) -> None:
    offline = Offline()
    assert old in offline.listing
    offline.listing = offline.listing.replace(old, new)
    batch = collect(offline.adapter(), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], RecordRejection)
    assert len(offline.calls) == 1


@pytest.mark.parametrize("old,new", [
    ("100.25", "100.26"), (" open ", " closed "),
    ("issueStatus ready", "issueStatus rewarded"),
    ("https://github.com/example/project/issues/37",
     "https://github.com/example/project/issues/38"),
])
def test_reject_detail_mismatch_or_unavailability(old: str, new: str) -> None:
    offline = Offline()
    assert old in offline.detail
    offline.detail = offline.detail.replace(old, new)
    batch = collect(offline.adapter(), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], RecordRejection)


@pytest.mark.parametrize("listing", [
    '<html><body><script>privateData = {"reward":100}</script></body></html>',
    '<html><body><div>',
])
def test_unsupported_or_incomplete_html_is_blocked(listing: str) -> None:
    offline = Offline()
    offline.listing = listing
    with pytest.raises(IssueHuntBlocked, match="BLOCKED"):
        collect(offline.adapter(), REQUEST)


def test_malformed_identity_is_rejected_without_detail_fetch() -> None:
    offline = Offline()
    offline.listing = offline.listing.replace("issues/37", "issues/NaN")
    batch = collect(offline.adapter(), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], RecordRejection)
    assert len(offline.calls) == 1


def test_bounds_stop_detail_fetches_and_validate_configuration() -> None:
    offline = Offline()
    offline.listing *= 3
    batch = collect(offline.adapter(max_pages=3, max_results=1), REQUEST)
    assert isinstance(batch, CollectionBatch) and len(batch.items) == 1
    assert len(offline.calls) == 2
    for bounds in ({"max_pages": 4}, {"max_results": 31}, {"max_results": 0}):
        with pytest.raises(ValueError):
            offline.adapter(**bounds)
    with pytest.raises(ValueError):
        IssueHuntAdapter(interval_seconds=0)


def test_replay_persists_one_opportunity(tmp_path: Path) -> None:
    store = SQLiteOpportunityStore(tmp_path / "replay.sqlite")
    store.migrate()
    offline = Offline()
    try:
        first = run_slice(offline.adapter(), REQUEST, normalize, store, BaselineEvaluator())
        second = run_slice(offline.adapter(), REQUEST, normalize, store, BaselineEvaluator())
        assert not isinstance(first, RetryRequired) and not isinstance(second, RetryRequired)
        assert first.opportunities[0].id == second.opportunities[0].id
        with sqlite3.connect(tmp_path / "replay.sqlite") as connection:
            assert connection.execute("SELECT count(*) FROM opportunities").fetchone() == (1,)
    finally:
        store.close()


def test_late_transient_failure_discards_batch(tmp_path: Path) -> None:
    offline = Offline()

    def fail(url: str) -> str:
        if "page=2" in url:
            raise TransientFetchFailure("outage")
        return offline(url)

    store = SQLiteOpportunityStore(tmp_path / "retry.sqlite")
    store.migrate()
    try:
        adapter = IssueHuntAdapter(fail, max_pages=2, pause=offline.pauses.append)
        outcome = run_slice(adapter, REQUEST, normalize, store, BaselineEvaluator())
        assert isinstance(outcome, RetryRequired)
        with sqlite3.connect(tmp_path / "retry.sqlite") as connection:
            assert connection.execute("SELECT count(*) FROM opportunities").fetchone() == (0,)
    finally:
        store.close()


def test_detail_not_found_rejected() -> None:
    offline = Offline()

    def missing(url: str) -> str:
        if "?page=" not in url:
            raise HTTPError(url, 404, "missing", Message(), BytesIO())
        return offline(url)

    batch = collect(IssueHuntAdapter(missing, pause=offline.pauses.append), REQUEST)
    assert isinstance(batch, CollectionBatch)
    assert isinstance(batch.items[0], RecordRejection)


def test_public_fetch_rejects_other_hosts_and_private_paths() -> None:
    for url in ["https://oss.issuehunt.io.evil/issues?page=1", BASE + "/api/issues",
                BASE + "/login", "https://github.com/example/project/issues/37"]:
        with pytest.raises(ValueError):
            public_fetch(url)


@pytest.mark.parametrize("code", [408, 429, 503])
def test_public_http_transient(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    class Opener:
        def open(self, request: Request, timeout: int) -> object:
            assert "Cookie" not in request.headers and "Authorization" not in request.headers
            raise HTTPError(request.full_url, code, "outage", Message(), BytesIO())

    monkeypatch.setattr("agenticstudio.sources.issuehunt.build_opener", lambda *args: Opener())
    with pytest.raises(TransientFetchFailure):
        public_fetch(BASE + "/issues?page=1")


def test_public_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Opener:
        def open(self, request: Request, timeout: int) -> object:
            raise URLError("offline")

    monkeypatch.setattr("agenticstudio.sources.issuehunt.build_opener", lambda *args: Opener())
    with pytest.raises(TransientFetchFailure):
        public_fetch(BASE + "/issues?page=1")


def test_daily_opt_in_and_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    offline = Offline()
    monkeypatch.setattr(daily, "IssueHuntAdapter", offline.adapter)
    args = ["--database", str(tmp_path / "daily.sqlite"), "--run-id", "offline", "--issuehunt"]
    assert daily.main([*args, "--migrate"]) == 0
    first = capsys.readouterr().out
    assert "Source: issuehunt" in first and "opportunities: 1" in first
    assert daily.main(args) == 0
    assert "opportunities: 1" in capsys.readouterr().out
