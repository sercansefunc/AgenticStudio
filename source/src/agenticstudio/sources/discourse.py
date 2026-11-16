"""Niche Discourse community job categories via the public JSON API; no account.

2026-10-17 check: n8n `Jobs` and Make `Hire a Pro` categories are anonymous JSON.
Replies are bidders, so the reply count becomes the competition signal. Category
pages also hold sellers' offers; triage, not this adapter, judges them.
"""

import json
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agenticstudio.domain import Estimate, Opportunity, Problem, Risk, Signals, Source
from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionRequest,
    RecordRejection,
    SourceRecordIdentity,
    TransientFetchFailure,
)
from agenticstudio.sources.market_a import _competition_estimate
from agenticstudio.sources.board import _HTML

# source name -> (forum origin, job category JSON path)
FORUMS = {
    "n8n-forum": ("https://community.n8n.io", "/c/jobs/13.json"),
    "make-forum": ("https://community.make.com", "/c/hire-a-pro/50.json"),
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
           "Accept": "application/json"}
MAX_BYTES = 3_000_000


class DiscourseBlocked(ValueError):
    """Public category or supported JSON unavailable; fail closed."""


def public_fetch(url: str) -> Any:
    if not any(url == origin + path or re.fullmatch(re.escape(origin) + r"/t/[1-9][0-9]*\.json",
                                                    url)
               for origin, path in FORUMS.values()):
        raise ValueError("unsupported Discourse URL")
    # 2026-10-17: n8n's host intermittently drops a TCP connect; a fresh short attempt
    # succeeds, so retry the connection (never an HTTP answer) up to three times.
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=10) as response:
                data: bytes = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise DiscourseBlocked("BLOCKED: response exceeds bound")
                return json.loads(data)
        except HTTPError as error:
            if error.code in {408, 429} or error.code >= 500:
                raise TransientFetchFailure(f"Discourse HTTP {error.code}") from error
            raise DiscourseBlocked(f"BLOCKED: Discourse HTTP {error.code}") from error
        except (URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
            if attempt == 2:
                raise TransientFetchFailure("Discourse transport failure") from error
        except ValueError as error:
            raise DiscourseBlocked("BLOCKED: unparseable JSON") from error
    raise AssertionError("unreachable")


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(UTC)


class DiscourseAdapter:
    def __init__(self, source_name: str, load: Callable[[str], Any] = public_fetch, *,
                 max_topics: int = 10, max_age_days: int = 14, interval_seconds: float = 2.0,
                 pause: Callable[[float], None] = sleep,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if (source_name not in FORUMS or type(max_topics) is not int or not 1 <= max_topics <= 20
                or type(max_age_days) is not int or not 1 <= max_age_days <= 30
                or not 2 <= interval_seconds <= 60):
            raise ValueError("invalid collection bounds")
        self.source_name = source_name
        self._origin, self._path = FORUMS[source_name]
        self._load, self._pause, self._clock = load, pause, clock
        self._topics, self._max_age = max_topics, timedelta(days=max_age_days)
        self._interval = interval_seconds

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        if request.source_name != self.source_name or request.selection != "jobs":
            raise ValueError("Discourse requires <forum>/jobs selection")
        reference = self._clock()
        listing = self._load(self._origin + self._path)
        try:
            topics = listing["topic_list"]["topics"]
            if not isinstance(topics, list):
                raise TypeError
        except (KeyError, TypeError) as error:
            raise DiscourseBlocked("BLOCKED: unsupported category JSON") from error
        fetched = 0
        for topic in topics:
            if fetched >= self._topics:
                return
            if not isinstance(topic, dict) or type(topic.get("id")) is not int:
                continue
            if topic.get("pinned") or topic.get("closed") or topic.get("archived"):
                continue
            identifier = str(topic["id"])
            identity = SourceRecordIdentity(source_name=self.source_name, kind="external_id",
                                            value=identifier)
            try:
                created = _time(topic.get("created_at"))
            except ValueError as error:
                yield RecordRejection(identity=identity, reason=f"unsupported topic: {error}")
                continue
            if reference - created > self._max_age:
                continue
            if fetched:
                self._pause(self._interval)
            fetched += 1
            detail = self._load(f"{self._origin}/t/{identifier}.json")
            try:
                first = detail["post_stream"]["posts"][0]
                if first.get("post_number") != 1 or str(first.get("topic_id")) != identifier:
                    raise ValueError("first post mismatch")
                body = "\n".join(" ".join(line.split()) for line in
                                 _HTML(str(first.get("cooked", ""))).root.text().splitlines()
                                 if line.strip())
                title = " ".join(str(detail.get("title", "")).split())
                if not body or not title:
                    raise ValueError("empty topic")
            except (KeyError, IndexError, TypeError, AttributeError, ValueError) as error:
                yield RecordRejection(identity=identity, reason=f"unsupported topic: {error}")
                continue
            replies = max(0, int(topic.get("posts_count") or 1) - 1)
            yield CollectedRecord(
                source_name=self.source_name, external_id=identifier,
                url=f"{self._origin}/t/{topic.get('slug') or 'topic'}/{identifier}",
                title=title, posted_at=created,
                raw_text=json.dumps({"title": title, "body": body[:30000], "replies": replies},
                                    ensure_ascii=False))


def normalize(record: CollectedRecord) -> Opportunity:
    if record.source_name not in FORUMS:
        raise ValueError("Discourse normalizer requires a known forum record")
    evidence = json.loads(record.raw_text)
    return Opportunity(
        source=Source(name=record.source_name, url=record.url, external_id=record.external_id,
                      risk_class="experimental"), posted_at=record.posted_at,
        problem=Problem(raw_text=record.raw_text,
                        summary=f"{evidence['title']}\n{evidence['body']}\n"
                                f"Replies so far: {evidence['replies']}"),
        signals=Signals(acceptance_clarity=0, strategic_reuse_value=0,
                        competition_estimate=_competition_estimate(evidence["replies"])),
        risk=Risk(technical_uncertainty=100, scope_uncertainty=100, maintenance_burden=100,
                  legal_or_platform_risk=100, abuse_risk=100, payment_or_dispute_risk=100),
        estimate=Estimate(delivery_probability=0.0, win_probability=0.0, confidence=0),
    )
