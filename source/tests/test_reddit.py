from datetime import UTC, datetime
from pathlib import Path

import pytest

from agenticstudio.ingestion import CollectedRecord, CollectionRequest, RecordRejection
from agenticstudio.sources import reddit

FEED = (Path(__file__).parent / "fixtures" / "reddit_hiring.xml").read_text()
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
REQUEST = CollectionRequest(source_name="reddit", selection="hiring", request_id="offline")


def collect(feed: str = FEED, now: datetime = NOW,
            ) -> tuple[list[str], list[CollectedRecord | RecordRejection]]:
    calls: list[str] = []

    def load(url: str) -> str:
        calls.append(url)
        return feed

    return calls, list(reddit.RedditAdapter(load, clock=lambda: now).fetch(REQUEST))


def test_buyer_posts_only_one_request() -> None:
    calls, out = collect()
    assert calls == [reddit.FEED]
    records = [r for r in out if isinstance(r, CollectedRecord)]
    titles = [r.title or "" for r in records]
    assert len(records) == 4 and not any("OFFER" in t.upper() for t in titles)
    spreadsheet = next(r for r in records if "spreadsheet" in (r.title or ""))
    assert spreadsheet.external_id == "1x7nlbd"
    assert spreadsheet.url.startswith("https://www.reddit.com/r/DoneDirtCheap/comments/1x7nlbd/")
    assert spreadsheet.raw_text.startswith("r/DoneDirtCheap\n")
    assert "Google Sheets" in spreadsheet.raw_text and "submitted by" not in spreadsheet.raw_text
    item = reddit.normalize(spreadsheet)
    assert item.source.risk_class == "experimental" and item.posted_at == spreadsheet.posted_at
    assert item.problem.summary.startswith("[task] please make a spreadsheet")


def test_stale_posts_are_rejected() -> None:
    _, out = collect(now=datetime(2026, 11, 30, tzinfo=UTC))
    assert out and all(isinstance(r, RecordRejection) and r.reason == "stale post" for r in out)


@pytest.mark.parametrize("feed", ["<html>blocked</html>", "not xml <"])
def test_unexpected_feed_fails_closed(feed: str) -> None:
    with pytest.raises(reddit.RedditBlocked):
        collect(feed)


def test_only_the_fixed_feed_is_fetched() -> None:
    with pytest.raises(ValueError):
        reddit.public_fetch("https://www.reddit.com/r/all/.rss")
