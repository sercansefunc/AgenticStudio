import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agenticstudio.ingestion import CollectedRecord, CollectionRequest, RecordRejection
from agenticstudio.sources import discourse

FIXTURES = Path(__file__).parent / "fixtures"
CATEGORY = json.loads((FIXTURES / "discourse_category.json").read_text())
TOPIC = json.loads((FIXTURES / "discourse_topic.json").read_text())
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
REQUEST = CollectionRequest(source_name="n8n-forum", selection="jobs", request_id="offline")
ORIGIN = "https://community.n8n.io"


def collect(topic: Any = TOPIC, category: Any = CATEGORY, now: datetime = NOW,
            ) -> tuple[list[str], list[CollectedRecord | RecordRejection], list[float]]:
    calls: list[str] = []
    pauses: list[float] = []

    def load(url: str) -> Any:
        calls.append(url)
        if url.endswith("/c/jobs/13.json"):
            return category
        identifier = int(url.rsplit("/", 1)[1].removesuffix(".json"))
        if identifier == topic["id"]:
            return topic
        return {**topic, "id": identifier, "post_stream": {"posts": [
            {**topic["post_stream"]["posts"][0], "topic_id": identifier}]}}

    adapter = discourse.DiscourseAdapter("n8n-forum", load, pause=pauses.append,
                                         clock=lambda: now)
    return calls, list(adapter.fetch(REQUEST)), pauses


def test_fresh_open_topics_with_reply_competition() -> None:
    calls, out, pauses = collect()
    # Pinned, closed and older-than-14-day topics are never opened.
    assert calls == [ORIGIN + "/c/jobs/13.json", ORIGIN + "/t/320146.json",
                     ORIGIN + "/t/322016.json"]
    assert pauses == [2.0]
    records = [r for r in out if isinstance(r, CollectedRecord)]
    assert [r.external_id for r in records] == ["320146", "322016"]
    busy, fresh = (discourse.normalize(r) for r in records)
    assert busy.signals.competition_estimate == 80 and fresh.signals.competition_estimate == 20
    assert fresh.source.name == "n8n-forum" and fresh.source.risk_class == "experimental"
    assert fresh.source.url == (
        ORIGIN + "/t/n8n-mirrorfly-developer-for-video-conferencing-automation/322016")
    assert "automation" in fresh.problem.summary.lower()
    assert "Replies so far: 2" in fresh.problem.summary


def test_mismatched_first_post_is_rejected() -> None:
    broken = {**TOPIC, "post_stream": {"posts": [{"post_number": 2}]}}
    _, out, _ = collect(topic={**broken, "id": 320146})
    assert any(isinstance(r, RecordRejection) and r.identity.value == "320146" for r in out)


def test_unexpected_category_fails_closed() -> None:
    with pytest.raises(discourse.DiscourseBlocked):
        collect(category={"errors": ["blocked"]})


def test_only_known_forum_urls_are_fetched() -> None:
    with pytest.raises(ValueError):
        discourse.public_fetch("https://example.com/t/1.json")
    with pytest.raises(ValueError):
        discourse.DiscourseAdapter("unknown-forum")
