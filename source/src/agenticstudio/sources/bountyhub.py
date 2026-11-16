"""Bounded read-only BountyHub discovery using public GitHub bot evidence."""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agenticstudio.domain import Commercial, Estimate, Opportunity, Problem, Risk, Signals, Source
from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionRequest,
    RecordRejection,
    SourceRecordIdentity,
    TransientFetchFailure,
)

SOURCE_NAME = "bountyhub"
API = "https://api.github.com"
QUERY = "is:issue is:open commenter:bountyhub-bot"
ISSUE_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)",
)
AMOUNT = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)"
MONEY = re.compile(r"\$" + AMOUNT + r"(?![0-9,]|\.[0-9])")
TOTAL = re.compile(r"The total amount of the bounty is now(?:\*\*)?\s+\$" + AMOUNT
                   + r"(?![0-9,]|\.[0-9])", re.I)
CLAIM = re.compile(r"https://(?:www\.)?bountyhub\.dev/bounty/view/[A-Za-z0-9_-]+"
                   r"(?=$|[\s)\]<>.,!])")
# Closed/rewarded issues are rejected after search; look wider so they do not eat the budget.
SEARCH_FACTOR = 5
UNAVAILABLE = re.compile(r"\b(?:rewarded|closed|cancelled|canceled|withdrawn)\b", re.I)


def github_fetch(url: str, headers: Mapping[str, str]) -> object:
    """GET only; callers control retry scheduling and credentials."""
    try:
        with urlopen(Request(url, headers=dict(headers)), timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        if (error.code in {408, 429} or error.code >= 500
                or (error.code == 403 and (error.headers.get("X-RateLimit-Remaining") == "0"
                                          or error.headers.get("Retry-After") is not None))):
            raise TransientFetchFailure(f"GitHub HTTP {error.code}") from error
        # Secondary rate limits can lack rate-limit headers.
        if error.code == 403 and b"rate limit" in error.read().lower():
            raise TransientFetchFailure("GitHub secondary rate limit") from error
        raise
    except (URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
        raise TransientFetchFailure("GitHub transport failure") from error


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result


def _rewards(comments: list[object]) -> tuple[str, str, list[str]]:
    ordered: list[tuple[datetime, int, str]] = []
    seen: set[int] = set()
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get("user"), dict):
            raise ValueError("malformed comment author")
        if comment["user"].get("login") != "bountyhub-bot":
            continue
        identifier, body = comment.get("id"), comment.get("body")
        if (type(identifier) is not int or identifier <= 0 or identifier in seen
                or not isinstance(body, str)):
            raise ValueError("malformed or duplicate bot comment")
        seen.add(identifier)
        ordered.append((_timestamp(comment.get("created_at")), identifier, body))
    amount: str | None = None
    claim: str | None = None
    evidence: list[str] = []
    for _, _, body in sorted(ordered):
        if UNAVAILABLE.search(body):
            raise ValueError("bot indicates unavailable bounty")
        if re.search(r"\b(?:EUR|GBP|CAD|AUD|NZD)\b", body):
            raise ValueError("ambiguous reward currency")
        urls = set(CLAIM.findall(body))
        if len(urls) > 1 or (claim is not None and urls and urls != {claim}):
            raise ValueError("ambiguous claim URL")
        if urls:
            claim = next(iter(urls))
        totals = TOTAL.findall(body)
        amounts = MONEY.findall(body)
        if re.search(r"Bounty Alert!?", body, re.I):
            if amount is not None or len(amounts) != 1 or not urls:
                raise ValueError("ambiguous initial bounty")
            amount = amounts[0].replace(",", "")
        elif re.search(r"Bounty Update!?", body, re.I) or totals:
            if amount is None or len(totals) != 1:
                raise ValueError("bounty update lacks explicit total")
            amount = totals[0].replace(",", "")
        else:
            raise ValueError("unrecognized bot bounty evidence")
        if Decimal(amount) <= 0:
            raise ValueError("nonpositive reward")
        evidence.append(body)
    if amount is None or claim is None:
        raise ValueError("missing verified bounty or claim URL")
    return amount, claim, evidence


class BountyHubAdapter:
    source_name = SOURCE_NAME

    def __init__(self, load: Callable[[str, Mapping[str, str]], object] = github_fetch,
                 *, token: str | None = None, headers: Mapping[str, str] | None = None,
                 max_candidates: int = 30,
                 max_comment_pages: int = 3) -> None:
        if not (1 <= max_candidates <= 50
                and 1 <= max_comment_pages <= 100):
            raise ValueError("invalid collection bounds")
        self._load = load
        self._headers = {"Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "AgenticStudio"}
        if headers:
            self._headers.update(headers)
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        self._max_candidates = max_candidates
        self._max_comment_pages = max_comment_pages

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        if request.source_name != SOURCE_NAME:
            raise ValueError("request source must be bountyhub")
        if request.selection != "all":
            raise ValueError("BountyHub selection must be all")
        seen: set[str] = set()
        query = QUERY
        result = self._load(f"{API}/search/issues?" + urlencode(
            {"q": query, "sort": "updated", "order": "desc",
             "per_page": min(50, self._max_candidates * SEARCH_FACTOR), "page": 1}), self._headers)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise ValueError("malformed GitHub search response")
        if result.get("incomplete_results") is not False:
            raise TransientFetchFailure("GitHub search results incomplete")
        accepted = 0
        for candidate in result["items"][:self._max_candidates * SEARCH_FACTOR]:
            if not isinstance(candidate, dict):
                raise ValueError("malformed search candidate")
            url_value = candidate.get("html_url")
            if not isinstance(url_value, str):
                raise ValueError("candidate lacks canonical issue identity")
            url = url_value
            match = ISSUE_URL.fullmatch(url)
            if match is None:
                yield RecordRejection(
                    identity=SourceRecordIdentity(source_name=SOURCE_NAME, kind="url",
                                                  value=url),
                    reason="candidate is not a canonical GitHub issue",
                )
                continue
            if url in seen:
                continue
            if accepted >= self._max_candidates:
                return
            seen.add(url)
            external_id = f"{match[1]}/{match[2]}#{match[3]}"
            identity = SourceRecordIdentity(source_name=SOURCE_NAME, kind="external_id",
                                            value=external_id)
            path = f"{API}/repos/{match[1]}/{match[2]}/issues/{match[3]}"
            try:
                issue = candidate
                issue_title = issue.get("title")
                issue_body = issue.get("body")
                if (issue.get("html_url") != url or issue.get("state") != "open"
                        or "pull_request" in issue or not isinstance(issue_title, str)
                        or not isinstance(issue_body, (str, type(None)))):
                    raise ValueError("closed, unavailable, or malformed issue")
                comments_url = issue.get("comments_url")
                if comments_url != f"{path}/comments":
                    raise ValueError("comments URL does not match canonical issue identity")
                comments: list[object] = []
                for comment_page in range(1, self._max_comment_pages + 1):
                    payload = self._load(f"{comments_url}?per_page=100&page={comment_page}",
                                         self._headers)
                    if not isinstance(payload, list) or len(payload) > 100:
                        raise ValueError("malformed comments response")
                    comments.extend(payload)
                    if len(payload) < 100:
                        break
                else:
                    raise ValueError("comment history exceeds collection bound")
                amount, claim, events = _rewards(comments)
                try:
                    posted_at = _timestamp(issue.get("created_at"))
                except ValueError:
                    posted_at = None
            except ValueError as error:
                yield RecordRejection(identity=identity, reason=str(error))
                continue
            accepted += 1
            labels = [label["name"] for label in (candidate.get("labels") or [])[:30]
                      if isinstance(label, dict) and isinstance(label.get("name"), str)
                      and len(label["name"]) <= 100]
            yield CollectedRecord(source_name=SOURCE_NAME, url=url, external_id=external_id,
                                  title=issue_title,
                                  posted_at=posted_at, raw_text=json.dumps({
                                      "title": issue_title, "body": issue_body,
                                      "currency": "USD", "reward": amount, "claim_url": claim,
                                      "events": events, "labels": labels,
                                  }))


def normalize(record: CollectedRecord) -> Opportunity:
    if record.source_name != SOURCE_NAME:
        raise ValueError("BountyHub normalizer requires bountyhub records")
    evidence = json.loads(record.raw_text)
    total = Decimal(evidence["reward"])
    if not total.is_finite() or total <= 0:
        raise ValueError("invalid reward evidence")
    return Opportunity(
        source=Source(name=SOURCE_NAME, url=record.url, external_id=record.external_id,
                      risk_class="core"), posted_at=record.posted_at,
        problem=Problem(raw_text=record.raw_text,
                        summary=(f'{evidence["title"]}\n{evidence["body"] or ""}\n'
                                 f"BountyHub bot reports USD {total}; payment and claim "
                                 "status unverified."),
                        expected_deliverable="Pull request resolving the GitHub issue"),
        commercial=Commercial(currency="USD", budget_min=total, budget_max=total,
                              pricing_model="bounty"),
        signals=Signals(repo_available=True, acceptance_clarity=0, strategic_reuse_value=0),
        risk=Risk(technical_uncertainty=100, scope_uncertainty=100, maintenance_burden=100,
                  legal_or_platform_risk=100, abuse_risk=100, payment_or_dispute_risk=100),
        # Raw issue labels; Gemini triage judges fit. Absence is unknown, not "no requirements".
        estimate=Estimate(delivery_probability=0.0, win_probability=0.0, confidence=0,
                          required_capabilities=tuple(evidence.get("labels", ()))),
    )
