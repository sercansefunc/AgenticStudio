"""Read-only, bounded Opire discovery through public GitHub REST endpoints.

Selection is a GitHub search qualifier suffix (for example ``repo:owner/repo``).
Evidence is a JSON envelope inside CollectedRecord.raw_text, keeping the shared
contracts unchanged. Only complete comment histories can establish availability.
"""

import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from decimal import Decimal, localcontext
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

SOURCE_NAME = "opire"
API = "https://api.github.com"
QUERY = 'is:issue is:open in:comments "How to earn this reward?"'
ISSUE_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)",
)
AMOUNT = r"([0-9]+(?:\.[0-9]{1,2})?)"
COMMAND = re.compile(r"^/reward\s+" + AMOUNT + r"\s*<details><summary>How to earn this \$"
                     + AMOUNT + r" reward\?", re.IGNORECASE)
CREATED = re.compile(r"@[\w-]+ created a \$" + AMOUNT
                     + r" reward using \[Opire\]\(https://(?:[\w-]+\.)?opire\.dev(?:/[^)]*)?\)")


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
    except (URLError, TimeoutError, ConnectionError) as error:
        raise TransientFetchFailure("GitHub transport failure") from error


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return result


def _rewards(comments: list[object]) -> tuple[list[str], list[str]]:
    ordered: list[tuple[datetime, int, str, str]] = []
    seen: set[int] = set()
    for comment in comments:
        if not isinstance(comment, dict):
            raise ValueError("malformed comment")
        identifier, body, user = comment.get("id"), comment.get("body"), comment.get("user")
        if (type(identifier) is not int or identifier <= 0 or identifier in seen
                or not isinstance(body, str) or not isinstance(user, dict)
                or not isinstance(user.get("login"), str)):
            raise ValueError("malformed or duplicate comment")
        seen.add(identifier)
        ordered.append((_timestamp(comment.get("created_at")), identifier, body, user["login"]))
    amounts: list[str] = []
    evidence: list[str] = []
    for _, _, body, author in sorted(ordered):
        if author == "opirebot[bot]" and "claimed all rewards for this issue" in body.lower():
            amounts.clear()
            evidence.append(body)
            continue
        command, created = COMMAND.search(body), CREATED.search(body)
        if command:
            if Decimal(command[1]) != Decimal(command[2]):
                raise ValueError("inconsistent reward amounts")
            amount = command[1]
        elif created and author == "opirebot[bot]":
            amount = created[1]
        else:
            if (author == "opirebot[bot]" and re.search(r"reward|claim|paid", body, re.I)
                    or body.lstrip().startswith("/reward")):
                raise ValueError("ambiguous reward event")
            continue
        if Decimal(amount) <= 0:
            raise ValueError("nonpositive reward")
        amounts.append(amount)
        evidence.append(body)
    return amounts, evidence


class OpireAdapter:
    source_name = SOURCE_NAME

    def __init__(self, load: Callable[[str, Mapping[str, str]], object] = github_fetch,
                 *, token: str | None = None, headers: Mapping[str, str] | None = None,
                 max_candidates: int = 20, max_search_pages: int = 2,
                 max_comment_pages: int = 3) -> None:
        if not (1 <= max_candidates <= 1000 and 1 <= max_search_pages <= 10
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
        self._max_search_pages = max_search_pages
        self._max_comment_pages = max_comment_pages

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        if request.source_name != SOURCE_NAME:
            raise ValueError("request source must be opire")
        seen: set[str] = set()
        for page in range(1, self._max_search_pages + 1):
            query = QUERY if request.selection == "all" else f"{QUERY} {request.selection}"
            result = self._load(f"{API}/search/issues?" + urlencode(
                {"q": query, "per_page": 100, "page": page}), self._headers)
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise ValueError("malformed GitHub search response")
            if result.get("incomplete_results") is not False:
                raise TransientFetchFailure("GitHub search results incomplete")
            for candidate in result["items"]:
                if not isinstance(candidate, dict):
                    raise ValueError("malformed search candidate")
                url_value = candidate.get("html_url")
                if not isinstance(url_value, str):
                    raise ValueError("candidate lacks canonical issue identity")
                url = url_value
                match = ISSUE_URL.fullmatch(url)
                if match is None:
                    raise ValueError("candidate lacks canonical issue identity")
                if url in seen:
                    continue
                if len(seen) >= self._max_candidates:
                    return
                seen.add(url)
                identity = SourceRecordIdentity(source_name=SOURCE_NAME, kind="url", value=url)
                path = f"{API}/repos/{match[1]}/{match[2]}/issues/{match[3]}"
                issue = candidate
                try:
                    if not isinstance(issue, dict):
                        raise ValueError("closed, unavailable, or malformed issue")
                    issue_title = issue.get("title")
                    issue_body = issue.get("body")
                    if (issue.get("html_url") != url or issue.get("state") != "open"
                            or "pull_request" in issue or not isinstance(issue_title, str)
                            or not isinstance(issue_body, (str, type(None)))):
                        raise ValueError("closed, unavailable, or malformed issue")
                    comments_url = issue.get("comments_url")
                    if comments_url != f"{path}/comments":
                        raise ValueError("comments URL does not match canonical issue identity")
                    posted_at = _timestamp(issue.get("created_at"))
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
                    amounts, events = _rewards(comments)
                    if not amounts:
                        raise ValueError("no confidently available rewards")
                except ValueError as error:
                    yield RecordRejection(identity=identity, reason=str(error))
                    continue
                yield CollectedRecord(source_name=SOURCE_NAME, url=url, title=issue_title,
                                      posted_at=posted_at, raw_text=json.dumps({
                                          "title": issue_title, "body": issue_body,
                                          "currency": "USD", "rewards": amounts,
                                          "events": events,
                                      }))
            if len(seen) >= self._max_candidates or len(result["items"]) < 100:
                return


def normalize(record: CollectedRecord) -> Opportunity:
    if record.source_name != SOURCE_NAME:
        raise ValueError("Opire normalizer requires opire records")
    evidence = json.loads(record.raw_text)
    amounts = [Decimal(value) for value in evidence["rewards"]]
    if not amounts or any(not amount.is_finite() or amount <= 0 for amount in amounts):
        raise ValueError("invalid reward evidence")
    # Enough precision even for large amounts and many separate rewards.
    with localcontext() as context:
        context.prec = max(28, max(len(a.as_tuple().digits) for a in amounts)
                           + len(str(len(amounts))) + 2)
        total = sum(amounts, Decimal(0))
    return Opportunity(
        source=Source(name=SOURCE_NAME, url=record.url, external_id=record.external_id,
                      risk_class="core"), posted_at=record.posted_at,
        problem=Problem(raw_text=record.raw_text,
                        summary=f'{evidence["title"]}\n{evidence["body"] or ""}',
                        expected_deliverable="Pull request resolving the GitHub issue"),
        commercial=Commercial(currency="USD", budget_min=total, budget_max=total,
                              pricing_model="bounty"),
        signals=Signals(repo_available=True, acceptance_clarity=0, strategic_reuse_value=0),
        risk=Risk(technical_uncertainty=100, scope_uncertainty=100, maintenance_burden=100,
                  legal_or_platform_risk=100, abuse_risk=100, payment_or_dispute_risk=100),
        estimate=Estimate(delivery_probability=0.0, win_probability=0.0, confidence=0),
    )
