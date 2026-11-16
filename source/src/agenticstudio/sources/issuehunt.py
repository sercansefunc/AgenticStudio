"""Bounded public HTML discovery; no scripts, private data, or account actions.

Feed re-check on 2026-10-07 found no confirmed official API/RSS. IssueHunt's
public repository pages label platform funding as ``USD raised``; rendered
``$`` rewards are therefore normalized as USD while claim/payment stay unknown.
"""

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from html.parser import HTMLParser
from http.client import IncompleteRead
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from agenticstudio.domain import Commercial, Estimate, Opportunity, Problem, Risk, Signals, Source
from agenticstudio.ingestion import (
    CollectedRecord,
    CollectionRequest,
    RecordRejection,
    SourceRecordIdentity,
    TransientFetchFailure,
)

SOURCE_NAME = "issuehunt"
BASE = "https://oss.issuehunt.io"
ISSUE_PATH = re.compile(r"/r/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)")
MONEY = re.compile(r"\$([0-9]+(?:\.[0-9]{2})?)")
MAX_BYTES = 2_000_000


class IssueHuntBlocked(ValueError):
    """Public markup no longer supplies the supported evidence contract."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: object, code: int, msg: str,
                         headers: object, newurl: str) -> None:
        return None


def public_fetch(url: str) -> str:
    """Bounded anonymous GET, with no cookies, redirects, or automatic retries."""
    if not (url.startswith(BASE + "/issues?page=") or
            ISSUE_PATH.fullmatch(url.removeprefix(BASE))):
        raise ValueError("unsupported public IssueHunt URL")
    try:
        with build_opener(_NoRedirect()).open(Request(url, headers={
            "User-Agent": "AgenticStudio", "Accept": "text/html",
        }), timeout=20) as response:
            if response.headers.get_content_type() != "text/html":
                raise IssueHuntBlocked("BLOCKED: expected public HTML")
            data: bytes = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise IssueHuntBlocked("BLOCKED: HTML exceeds response bound")
            return data.decode("utf-8")
    except HTTPError as error:
        if error.code in {408, 429} or error.code >= 500:
            raise TransientFetchFailure(f"IssueHunt HTTP {error.code}") from error
        raise
    except (URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
        raise TransientFetchFailure("IssueHunt transport failure") from error


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node | str"] = field(default_factory=list)

    def has(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def nodes(self) -> Iterable["_Node"]:
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.nodes()

    def text(self) -> str:
        return "".join(child.text() if isinstance(child, _Node) else child
                       for child in self.children)


class _HTML(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.feed(html)
        self.close()
        if len(self.stack) != 1:
            raise IssueHuntBlocked("BLOCKED: incomplete public HTML")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                       "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        if len(self.stack) < 2 or self.stack[-1].tag != tag:
            raise IssueHuntBlocked("BLOCKED: unsupported public HTML structure")
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.stack[-1].tag not in {"script", "style"}:
            self.stack[-1].children.append(data)


def _one(nodes: Iterable[_Node], description: str) -> _Node:
    items = list(nodes)
    if len(items) != 1:
        raise ValueError(f"missing or ambiguous {description}")
    return items[0]


def _amount(node: _Node) -> str:
    match = MONEY.fullmatch(node.text().strip())
    if match is None or Decimal(match[1]) <= 0:
        raise ValueError("malformed, ambiguous-currency or nonpositive reward")
    return match[1]


def _open(node: _Node) -> None:
    icon = _one((n for n in node.nodes() if n.has("issueStateIcon")), "issue state")
    if not icon.has("open") or icon.has("closed"):
        raise ValueError("closed or unavailable issue")


def _count(node: _Node, pattern: str) -> int | None:
    matches = [match[1] for n in node.nodes()
               if ((n.tag == "span" and n.has("text-muted"))
                   or (n.tag == "strong" and n.has("small")))
               if (match := re.fullmatch(pattern, " ".join(n.text().split()))) is not None]
    if len(matches) > 1:
        raise ValueError("ambiguous competition count")
    return int(matches[0]) if matches else None


class IssueHuntAdapter:
    source_name = SOURCE_NAME

    def __init__(self, load: Callable[[str], str] = public_fetch, *, max_pages: int = 1,
                 max_results: int = 10, interval_seconds: float = 2.0,
                 pause: Callable[[float], None] = sleep) -> None:
        if (type(max_pages) is not int or not 1 <= max_pages <= 3
                or type(max_results) is not int or not 1 <= max_results <= 30
                or not 2 <= interval_seconds <= 60):
            raise ValueError("invalid collection bounds")
        self._load, self._pause = load, pause
        self._pages, self._results, self._interval = max_pages, max_results, interval_seconds

    def fetch(self, request: CollectionRequest) -> Iterable[CollectedRecord | RecordRejection]:
        if request.source_name != SOURCE_NAME or request.selection != "all":
            raise ValueError("IssueHunt requires issuehunt/all selection")
        seen: set[str] = set()
        fetched = False

        def get(url: str) -> _Node:
            nonlocal fetched
            if fetched:
                self._pause(self._interval)
            fetched = True
            return _HTML(self._load(url)).root

        for page in range(1, self._pages + 1):
            if len(seen) >= self._results:
                return
            listing = get(f"{BASE}/issues?page={page}")
            rows = [n for n in listing.nodes() if n.has("list-group-item")]
            if not rows:
                raise IssueHuntBlocked("BLOCKED: public funded-issue listing unavailable")
            for row in rows:
                if len(seen) >= self._results:
                    return
                links = [n for n in row.nodes() if n.tag == "a" and n.has("text-body")]
                try:
                    title_link = _one(links, "issue title link")
                except ValueError as error:
                    raise IssueHuntBlocked("BLOCKED: unsupported public listing") from error
                path = title_link.attrs.get("href", "")
                match = ISSUE_PATH.fullmatch(path)
                if match is None:
                    if path.startswith("/r/") and "?" not in path and "#" not in path:
                        yield RecordRejection(identity=SourceRecordIdentity(
                            source_name=SOURCE_NAME, kind="url", value=BASE + path),
                            reason="malformed or incomplete issue identity")
                        continue
                    raise IssueHuntBlocked("BLOCKED: listing lacks stable issue identity")
                external_id = f"{match[1]}/{match[2]}#{match[3]}".lower()
                if external_id in seen:
                    continue
                seen.add(external_id)
                identity = SourceRecordIdentity(source_name=SOURCE_NAME, kind="external_id",
                                                value=external_id)
                try:
                    _open(title_link)
                    badge = _one((n for n in row.nodes() if n.has("badge")), "funded badge")
                    if badge.text().strip() != "Funded":
                        raise ValueError("non-target or unavailable funding")
                    repo = _one((n for n in row.nodes() if n.tag == "a" and
                                 n.attrs.get("href") == f"/r/{match[1]}/{match[2]}"),
                                "explicit repository")
                    if repo.text().strip() != f"{match[1]}/{match[2]}":
                        raise ValueError("repository identity mismatch")
                    amount = _amount(_one((n for n in row.nodes() if n.has("strong")),
                                          "listing reward"))
                    title = title_link.text().strip()
                    if not title:
                        raise ValueError("empty issue title")
                    prs = _count(row, r"\b([0-9]+) pull requests?\b")
                    requests = _count(row, r"\b([0-9]+) bounty requests?\b")
                    try:
                        detail = get(BASE + path)
                    except HTTPError as error:
                        if error.code not in {404, 410}:
                            raise
                        raise ValueError("public issue unavailable") from error
                    heading = _one((n for n in detail.nodes() if n.tag == "h1"), "issue heading")
                    _open(heading)
                    number = _one((n for n in heading.nodes() if n.tag == "small"),
                                  "detail issue number")
                    if number.text().strip() != f"#{match[3]}":
                        raise ValueError("detail issue number mismatch")
                    github = f"https://github.com/{match[1]}/{match[2]}/issues/{match[3]}"
                    post = _one((n for n in detail.nodes() if n.has("githubPost")), "GitHub post")
                    _one((n for n in post.nodes() if n.tag == "a" and
                          n.attrs.get("href") == github), "canonical GitHub issue link")
                    block = _one((n for n in detail.nodes() if n.has("fundedBlock")),
                                 "detail funding block")
                    status = _one((n for n in block.nodes() if n.has("issueStatus")),
                                  "funding status")
                    if status.text().strip() != "Funded" or not status.has("ready"):
                        raise ValueError("funding unavailable")
                    if _amount(_one((n for n in block.nodes() if n.has("h3")),
                                    "detail reward")) != amount:
                        raise ValueError("listing/detail reward mismatch")
                except IssueHuntBlocked:
                    raise
                except ValueError as error:
                    yield RecordRejection(identity=identity, reason=str(error))
                    continue
                yield CollectedRecord(source_name=SOURCE_NAME, external_id=external_id,
                                      url=github, title=title, raw_text=json.dumps({
                                          "title": title, "repository": repo.text().strip(),
                                          "issue_number": int(match[3]), "state": "open",
                                          "reward": amount, "currency": "USD",
                                          "issuehunt_url": BASE + path, "github_url": github,
                                          "pull_request_count": prs,
                                          "bounty_request_count": requests,
                                      }))


def normalize(record: CollectedRecord) -> Opportunity:
    if record.source_name != SOURCE_NAME:
        raise ValueError("IssueHunt normalizer requires issuehunt records")
    evidence = json.loads(record.raw_text)
    total = Decimal(evidence["reward"])
    if evidence["currency"] != "USD" or not total.is_finite() or total <= 0:
        raise ValueError("invalid reward evidence")
    return Opportunity(
        source=Source(name=SOURCE_NAME, url=record.url, external_id=record.external_id,
                      risk_class="core"), posted_at=record.posted_at,
        problem=Problem(raw_text=record.raw_text,
                        summary=f'{evidence["title"]}\nUSD {total}; claim and payment unknown.',
                        expected_deliverable="Pull request resolving the GitHub issue"),
        commercial=Commercial(currency="USD", budget_min=total, budget_max=total,
                              pricing_model="bounty"),
        signals=Signals(repo_available=True, acceptance_clarity=0, strategic_reuse_value=0),
        risk=Risk(technical_uncertainty=100, scope_uncertainty=100, maintenance_burden=100,
                  legal_or_platform_risk=100, abuse_risk=100, payment_or_dispute_risk=100),
        estimate=Estimate(delivery_probability=0.0, win_probability=0.0, confidence=0),
    )
