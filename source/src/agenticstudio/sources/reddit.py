"""Public Reddit hiring posts from one combined-subreddit Atom feed; no account or API.

2026-10-17 check: anonymous `.rss` works from a residential IP; the unauthenticated
limit is about one request per window (`x-ratelimit-remaining: 0` after one call), so
all subreddits share a single request of the newest 100 posts.
"""

import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
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
from agenticstudio.sources.board import _HTML

SOURCE_NAME = "reddit"
SUBREDDITS = ("slavelabour", "forhire", "DoneDirtCheap", "jobbit", "hireaprogrammer",
              "freelance_forhire", "Jobs4Bitcoins")
FEED = f"https://www.reddit.com/r/{'+'.join(SUBREDDITS)}/new/.rss?limit=100"
HEADERS = {"User-Agent": "macos:agenticstudio-desk:0.1 (personal job discovery)",
           "Accept": "application/atom+xml"}
MAX_BYTES = 3_000_000
ATOM = "{http://www.w3.org/2005/Atom}"
# Only buyer posts; [For Hire]/[Offer] are other sellers.
BUYER = re.compile(r"\s*[\[(]\s*(hiring|task)\s*[\])]", re.IGNORECASE)
POST = re.compile(r"https://www\.reddit\.com/r/([A-Za-z0-9_]+)/comments/([a-z0-9]+)/[^\s]*")


class RedditBlocked(ValueError):
    """Public feed unavailable or unsupported; fail closed."""


def public_fetch(url: str) -> str:
    if url != FEED:
        raise ValueError("unsupported Reddit URL")
    try:
        with urlopen(Request(url, headers=HEADERS), timeout=20) as response:
            data: bytes = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise RedditBlocked("BLOCKED: response exceeds bound")
            return data.decode("utf-8", errors="replace")
    except HTTPError as error:
        if error.code in {408, 429} or error.code >= 500:
            raise TransientFetchFailure(f"Reddit HTTP {error.code}") from error
        raise RedditBlocked(f"BLOCKED: Reddit HTTP {error.code}") from error
    except (URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
        raise TransientFetchFailure("Reddit transport failure") from error


def _body(content: str) -> str:
    """Post HTML -> text, without Reddit's 'submitted by ... [link] [comments]' footer."""
    text = _HTML(content).root.text()
    text = text.split("submitted by", 1)[0]
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


class RedditAdapter:
    source_name = SOURCE_NAME

    def __init__(self, load: Callable[[str], str] = public_fetch, *, max_age_days: int = 7,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if type(max_age_days) is not int or not 1 <= max_age_days <= 30:
            raise ValueError("invalid collection bounds")
        self._load, self._clock, self._max_age = load, clock, timedelta(days=max_age_days)

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        if request.source_name != SOURCE_NAME or request.selection != "hiring":
            raise ValueError("Reddit requires reddit/hiring selection")
        reference = self._clock()
        try:
            root = ElementTree.fromstring(self._load(FEED))
        except ElementTree.ParseError as error:
            raise RedditBlocked("BLOCKED: unparseable feed") from error
        if root.tag != ATOM + "feed":
            raise RedditBlocked("BLOCKED: not an Atom feed")
        seen: set[str] = set()
        for entry in root.iter(ATOM + "entry"):
            title = " ".join((entry.findtext(ATOM + "title") or "").split())
            if not BUYER.match(title):
                continue
            link = entry.find(ATOM + "link")
            match = POST.fullmatch(link.get("href", "") if link is not None else "")
            if match is None or match[2] in seen:
                continue
            seen.add(match[2])
            identity = SourceRecordIdentity(source_name=SOURCE_NAME, kind="external_id",
                                            value=match[2])
            try:
                posted_at = datetime.fromisoformat(entry.findtext(ATOM + "published") or "")
                if posted_at.tzinfo is None:
                    raise ValueError("naive timestamp")
                body = _body(entry.findtext(ATOM + "content") or "")
            except ValueError as error:
                yield RecordRejection(identity=identity, reason=f"unsupported post: {error}")
                continue
            if reference - posted_at > self._max_age:
                yield RecordRejection(identity=identity, reason="stale post")
                continue
            yield CollectedRecord(
                source_name=SOURCE_NAME, external_id=match[2], url=match[0], title=title,
                posted_at=posted_at.astimezone(UTC),
                raw_text=f"r/{match[1]}\n{body or title}"[:30000])


def normalize(record: CollectedRecord) -> Opportunity:
    if record.source_name != SOURCE_NAME:
        raise ValueError("Reddit normalizer requires reddit records")
    return Opportunity(
        source=Source(name=SOURCE_NAME, url=record.url, external_id=record.external_id,
                      risk_class="experimental"), posted_at=record.posted_at,
        problem=Problem(raw_text=record.raw_text,
                        summary=f"{record.title}\n{record.raw_text}"),
        signals=Signals(acceptance_clarity=0, strategic_reuse_value=0),
        risk=Risk(technical_uncertainty=100, scope_uncertainty=100, maintenance_burden=100,
                  legal_or_platform_risk=100, abuse_risk=100, payment_or_dispute_risk=100),
        estimate=Estimate(delivery_probability=0.0, win_probability=0.0, confidence=0),
    )
