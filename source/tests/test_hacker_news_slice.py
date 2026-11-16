import json
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from sqlalchemy import create_engine, text

from agenticstudio.dedupe import build_dedupe_key
from agenticstudio.domain import Decision, Opportunity, Source
from agenticstudio.evaluation import BaselineEvaluator
from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionRequest,
    RetryRequired,
    TransientFetchFailure,
    collect,
)
from agenticstudio.persistence import SQLiteOpportunityStore
from agenticstudio.pipeline import SliceResult, run_slice
from agenticstudio.sources.hacker_news import (
    HackerNewsAdapter,
    discover_latest_thread,
    fetch_item,
    fetch_search,
    normalize,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hn_freelancer.json"
REQUEST = CollectionRequest(source_name="hacker_news", request_id="offline-run", selection="100")


def adapter() -> HackerNewsAdapter:
    items = json.loads(FIXTURE.read_text())
    return HackerNewsAdapter(lambda identifier: items[str(identifier)])


def test_offline_slice_reuses_persisted_uuid_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "slice.sqlite"
    store = SQLiteOpportunityStore(path)
    store.migrate()
    try:
        result = run_slice(adapter(), REQUEST, normalize, store, BaselineEvaluator())
        assert isinstance(result, SliceResult)
        assert len(result.opportunities) == len(result.predictions) == 1
        assert len(result.rejections) == 2
        opportunity = result.opportunities[0]
        assert opportunity.id.version == 4
        assert result.predictions[0].opportunity_id == opportunity.id
        assert opportunity.source.external_id == "101"
        assert "&amp;" in opportunity.problem.raw_text
        assert "& export CSV" in opportunity.problem.summary
        assert opportunity.posted_at is not None
        with store.transaction() as repository:
            key = build_dedupe_key(opportunity.source).storage_key
            assert repository.resolve_identity(key) == opportunity.id
            changed = opportunity.model_copy(update={"decision": Decision(status="investigating")})
            repository.save(changed)
    finally:
        store.close()
    reopened = SQLiteOpportunityStore(path)
    try:
        def forbidden_normalize(record: CollectedRecord) -> Opportunity:
            raise AssertionError("replay must reuse the stored Opportunity")

        replay = run_slice(adapter(), REQUEST, forbidden_normalize, reopened,
                           BaselineEvaluator())
        assert isinstance(replay, SliceResult)
        assert replay.opportunities == (changed,)
        assert replay.predictions[0].opportunity_id == opportunity.id
        engine = create_engine(f"sqlite+pysqlite:///{path}")
        try:
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM opportunities")) == 1
                assert connection.scalar(text("SELECT count(*) FROM identity_mappings")) == 1
        finally:
            engine.dispose()
    finally:
        reopened.close()


def test_partial_fetch_failure_is_retryable_without_writes(tmp_path: Path) -> None:
    items = json.loads(FIXTURE.read_text())

    def failing_load(identifier: int) -> object:
        if identifier == 102:
            raise TransientFetchFailure("temporary outage")
        return items[str(identifier)]

    store = SQLiteOpportunityStore(tmp_path / "retry.sqlite")
    store.migrate()
    try:
        failed = run_slice(HackerNewsAdapter(failing_load), REQUEST, normalize, store,
                           BaselineEvaluator())
        assert isinstance(failed, RetryRequired)
        assert failed.request == REQUEST
        with store.transaction() as repository:
            key = build_dedupe_key(Source(name="hacker_news", external_id="101",
                                         url="https://news.ycombinator.com/item?id=101",
                                         risk_class="core")).storage_key
            assert repository.resolve_identity(key) is None
        assert isinstance(run_slice(adapter(), REQUEST, normalize, store, BaselineEvaluator()),
                          SliceResult)
    finally:
        store.close()


def test_normalization_failure_rolls_back_snapshot_and_identity(tmp_path: Path) -> None:
    items = json.loads(FIXTURE.read_text())
    items["100"]["kids"] = [101, 105]
    items["105"] = dict(items["101"], id=105)

    def fail_second(record: CollectedRecord) -> Opportunity:
        if record.external_id == "105":
            raise ValueError("normalization failure")
        return normalize(record)

    store = SQLiteOpportunityStore(tmp_path / "rollback.sqlite")
    store.migrate()
    try:
        with pytest.raises(ValueError, match="normalization failure"):
            run_slice(HackerNewsAdapter(lambda identifier: items[str(identifier)]), REQUEST,
                      fail_second, store, BaselineEvaluator())
        with store.transaction() as repository:
            key = build_dedupe_key(Source(name="hacker_news", external_id="101",
                                         url="https://news.ycombinator.com/item?id=101",
                                         risk_class="core")).storage_key
            assert repository.resolve_identity(key) is None
        retry = run_slice(adapter(), REQUEST, normalize, store, BaselineEvaluator())
        assert isinstance(retry, SliceResult)
        assert len(retry.opportunities) == 1
    finally:
        store.close()


def test_invalid_selection_is_configuration_error() -> None:
    with pytest.raises(ValueError, match="positive HN thread ID"):
        collect(adapter(), REQUEST.model_copy(update={"selection": "not-an-id"}))


NOW = datetime(2026, 10, 7, tzinfo=UTC)


def story(identifier: int, day: str, month: str = "October") -> dict[str, object]:
    return {"id": identifier, "type": "story", "by": "jon_north",
            "title": f"Ask HN: Freelancer? Seeking freelancer? ({month} 2026)",
            "time": int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())}


def discover(items: dict[int, object], titles: dict[int, str] | None = None) -> int:
    hits: list[object] = [
        {"objectID": str(identifier),
         "title": (titles or {}).get(identifier, item.get("title"))}
        for identifier, item in items.items() if isinstance(item, dict)
    ]
    first_title = next(
        hit["title"] for hit in hits if isinstance(hit, dict) and "title" in hit
    )
    hits.extend([None, {"objectID": "bad", "title": first_title}])
    return discover_latest_thread(lambda page: {"hits": hits, "nbPages": 1},
                                  items.__getitem__, now=NOW)


def test_latest_verified_timestamp_and_author_independence() -> None:
    older = story(300, "2026-10-01")
    older["by"] = "different_author"
    newer = story(200, "2026-10-02")
    assert discover({300: older, 200: newer}) == 200
    assert discover({300: older, 200: story(200, "2026-10-01")}) == 300


@pytest.mark.parametrize("changes", [
    {"dead": True}, {"deleted": True}, {"id": 999}, {"type": "comment"},
    {"title": "Ask HN: Who is hiring? (October 2026)"}, {"time": "bad"},
])
def test_unverified_newest_hit_is_skipped(changes: dict[str, object]) -> None:
    bad = {**story(300, "2026-10-03"), **changes}
    assert discover({300: bad, 200: story(200, "2026-10-01")},
                    {300: str(story(300, "2026-10-03")["title"])}) == 200


def test_missing_official_candidate_is_skipped() -> None:
    valid = story(200, "2026-10-01")
    hits = [
        {"objectID": "300", "title": valid["title"]},
        {"objectID": "200", "title": valid["title"]},
    ]

    def load_item(identifier: int) -> object:
        return None if identifier == 300 else valid

    assert discover_latest_thread(
        lambda page: {"hits": hits, "nbPages": 1}, load_item, now=NOW,
    ) == 200


def test_previous_month_fallback_and_stale_rejection() -> None:
    assert discover({200: story(200, "2026-09-01", "September")}) == 200
    with pytest.raises(ValueError, match="no verified recent"):
        discover({200: story(200, "2026-08-01", "September")})
    with pytest.raises(ValueError, match="no verified recent"):
        discover({200: story(200, "2026-08-31", "August")})


def test_current_month_preferred_to_previous_month() -> None:
    assert discover({100: story(100, "2026-10-01"),
                     200: story(200, "2026-10-02", "September")}) == 100


@pytest.mark.parametrize("title,accepted", [
    ("  ASK hn: Freelancer?   Seeking freelancer? (october 2026)  ", True),
    ("Ask HN: Freelancer Seeking freelancer (October 2026)", False),
    ("Ask HN: Freelancer? Seeking freelancer? (October 2026) extra", False),
    ("Ask HN: Freelancer? Seeking freelancer? (Smarch 2026)", False),
])
def test_strict_title_normalization(title: str, accepted: bool) -> None:
    item = {**story(200, "2026-10-01"), "title": title}
    if accepted:
        assert discover({200: item}) == 200
    else:
        with pytest.raises(ValueError):
            discover({200: item})


@pytest.mark.parametrize("stage", ["search", "official"])
def test_discovery_transient_failure_propagates(stage: str) -> None:
    def fail(value: int) -> object:
        raise TransientFetchFailure("outage")

    item = story(200, "2026-10-01")
    with pytest.raises(TransientFetchFailure):
        discover_latest_thread(
            fail if stage == "search" else lambda page: {
                "hits": [{"objectID": "200", "title": item["title"]}], "nbPages": 1,
            }, fail, now=NOW,
        )


def test_discovery_paginates_and_skips_malformed_payloads() -> None:
    item = story(200, "2026-10-01")
    pages: list[int] = []

    def search(page: int) -> object:
        pages.append(page)
        return {"nbPages": 2, "hits": [None] if page == 0 else [
            {"objectID": "200", "title": item["title"]},
        ]}

    assert discover_latest_thread(search, lambda identifier: item, now=NOW) == 200
    assert pages == [0, 1]


@pytest.mark.parametrize("loader", [fetch_search, fetch_item])
@pytest.mark.parametrize("failure", [
    URLError("network unavailable"), TimeoutError("timeout"),
    HTTPError("https://example.invalid", 429, "rate limit", Message(), None),
    HTTPError("https://example.invalid", 503, "unavailable", Message(), None),
])
def test_discovery_http_failures_are_transient(
    monkeypatch: pytest.MonkeyPatch, loader: Callable[[int], object], failure: Exception,
) -> None:
    def unavailable(*args: object, **kwargs: object) -> object:
        raise failure

    monkeypatch.setattr("agenticstudio.sources.hacker_news.urlopen", unavailable)
    with pytest.raises(TransientFetchFailure):
        loader(0)
