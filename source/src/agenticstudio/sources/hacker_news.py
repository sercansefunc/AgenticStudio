"""One selected freelancer thread via the official HN Firebase API."""

import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from agenticstudio.domain import Estimate, Opportunity, Problem, Risk, Signals, Source
from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionRequest,
    RecordRejection,
    SourceRecordIdentity,
    TransientFetchFailure,
)

SOURCE_NAME = "hacker_news"


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "br", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def fetch_item(identifier: int) -> object:
    try:
        with urlopen(
            f"https://hacker-news.firebaseio.com/v0/item/{identifier}.json", timeout=20,
        ) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code in {408, 429} or error.code >= 500:
            raise TransientFetchFailure(str(error)) from error
        raise
    except (URLError, TimeoutError, ConnectionError) as error:
        raise TransientFetchFailure(str(error)) from error


_MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
)
_TITLE = re.compile(
    r"ask hn: freelancer\? seeking freelancer\? \((" + "|".join(_MONTHS)
    + r") ([0-9]{4})\)", re.IGNORECASE,
)


def _thread_month(title: object) -> tuple[int, int] | None:
    if not isinstance(title, str):
        return None
    match = _TITLE.fullmatch(" ".join(title.split()))
    if match is None:
        return None
    return int(match[2]), _MONTHS.index(match[1].lower()) + 1


def fetch_search(page: int) -> object:
    query = urlencode({"query": "Ask HN: Freelancer? Seeking freelancer?",
                       "tags": "story", "hitsPerPage": 100, "page": page})
    try:
        with urlopen(f"https://hn.algolia.com/api/v1/search_by_date?{query}",
                     timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code in {408, 429} or error.code >= 500:
            raise TransientFetchFailure(str(error)) from error
        raise
    except (URLError, TimeoutError, ConnectionError) as error:
        raise TransientFetchFailure(str(error)) from error


def discover_latest_thread(
    load_search: Callable[[int], object] = fetch_search,
    load_item: Callable[[int], object] = fetch_item,
    *, now: datetime | None = None,
) -> int:
    """Verify recent current/previous-month stories; never rely on an author.

    Pagination is bounded to Algolia's public 1,000-hit search limit. Official
    timestamps, then item IDs, determine ordering rather than search ranking.
    """
    now = now or datetime.now(UTC)
    current = (now.year, now.month)
    previous_date = now.replace(day=1) - timedelta(days=1)
    previous = (previous_date.year, previous_date.month)
    candidates: set[int] = set()
    for page in range(10):
        payload = load_search(page)
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise TransientFetchFailure("invalid HN search response")
        for hit in payload["hits"]:
            if not isinstance(hit, dict) or _thread_month(hit.get("title")) not in {
                current, previous,
            }:
                continue
            identifier = hit.get("objectID")
            if isinstance(identifier, str) and re.fullmatch(r"[1-9][0-9]*", identifier):
                candidates.add(int(identifier))
        pages = payload.get("nbPages")
        if type(pages) is not int or pages < 0:
            raise TransientFetchFailure("invalid HN search pagination")
        if page + 1 >= pages:
            break
    verified: list[tuple[bool, int, int]] = []
    for identifier in sorted(candidates, reverse=True):
        item = load_item(identifier)
        if item is None:
            continue
        if (not isinstance(item, dict) or type(item.get("id")) is not int
                or item.get("id") != identifier or item.get("type") != "story"
                or item.get("deleted") or item.get("dead")):
            continue
        month = _thread_month(item.get("title"))
        timestamp = item.get("time")
        if month not in {current, previous} or type(timestamp) is not int:
            continue
        try:
            posted_at = datetime.fromtimestamp(timestamp, tz=UTC)
        except (ValueError, OverflowError, OSError):
            continue
        if not timedelta(0) <= now - posted_at <= timedelta(days=45):
            continue
        verified.append((month == current, timestamp, identifier))
    if not verified:
        raise ValueError("no verified recent HN freelancer thread found")
    return max(verified)[2]


class HackerNewsAdapter:
    source_name = SOURCE_NAME

    def __init__(self, load_item: Callable[[int], object] = fetch_item) -> None:
        self._load_item = load_item

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        if request.source_name != self.source_name:
            raise ValueError("request source must be hacker_news")
        if not re.fullmatch(r"[1-9][0-9]*", request.selection):
            raise ValueError("selection must be a positive HN thread ID")
        thread_id = int(request.selection)
        thread = self._load_item(thread_id)
        if thread is None:
            raise TransientFetchFailure("HN thread is temporarily unavailable")
        if not isinstance(thread, dict):
            raise ValueError("invalid HN thread payload")
        title = thread.get("title")
        if (thread.get("id") != thread_id or thread.get("type") != "story"
                or thread.get("deleted") or thread.get("dead")
                or not isinstance(title, str) or "freelancer" not in title.lower()):
            raise ValueError("selection must be an active freelancer thread")
        kids = thread.get("kids", [])
        if not isinstance(kids, list):
            raise ValueError("invalid HN thread kids")
        for identifier in kids:
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("invalid HN comment ID")
            item = self._load_item(identifier)
            if item is None:
                raise TransientFetchFailure(f"HN item {identifier} is temporarily unavailable")
            identity = SourceRecordIdentity(
                source_name=SOURCE_NAME, kind="external_id", value=str(identifier),
            )
            reason = "unsupported or malformed top-level HN comment"
            if (not isinstance(item, dict) or item.get("id") != identifier
                    or item.get("type") != "comment" or item.get("parent") != thread_id
                    or item.get("deleted") or item.get("dead")
                    or not isinstance(item.get("text"), str)):
                yield RecordRejection(identity=identity, reason=reason)
                continue
            text = plain_text(item["text"])
            if not re.match(r"^SEEKING\s+FREELANCER\b", text, re.IGNORECASE):
                yield RecordRejection(identity=identity, reason="not a seeking-freelancer post")
                continue
            timestamp = item.get("time")
            try:
                if type(timestamp) is not int:
                    raise ValueError("invalid timestamp")
                posted_at = datetime.fromtimestamp(timestamp, tz=UTC)
            except (ValueError, OverflowError, OSError):
                yield RecordRejection(identity=identity, reason="invalid HN timestamp")
                continue
            yield CollectedRecord(
                source_name=SOURCE_NAME, external_id=str(identifier),
                url=f"https://news.ycombinator.com/item?id={identifier}",
                title=plain_text(title), raw_text=item["text"], posted_at=posted_at,
            )


def normalize(record: CollectedRecord) -> Opportunity:
    if record.source_name != SOURCE_NAME:
        raise ValueError("HN normalizer requires hacker_news records")
    text = plain_text(record.raw_text)
    return Opportunity(
        source=Source(name=record.source_name, url=record.url,
                      external_id=record.external_id, risk_class="core"),
        posted_at=record.posted_at,
        problem=Problem(raw_text=record.raw_text, summary=text),
        signals=Signals(acceptance_clarity=0, strategic_reuse_value=0),
        risk=Risk(technical_uncertainty=100, scope_uncertainty=100, maintenance_burden=100,
                  legal_or_platform_risk=100, abuse_risk=100, payment_or_dispute_risk=100),
        estimate=Estimate(delivery_probability=0.0, win_probability=0.0, confidence=0),
    )
