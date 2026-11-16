"""One read-only daily discovery run: python -m agenticstudio.daily."""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from agenticstudio.decision import build_brief, render_markdown
from agenticstudio.decision.prefilter import exclusion_reasons, render_prefilter
from agenticstudio.evaluation import PredictionRecord, ScreeningEvaluator
from agenticstudio.ingestion import CollectionRequest, RetryRequired, TransientFetchFailure
from agenticstudio.persistence import SQLiteOpportunityStore
from agenticstudio.pipeline import SliceResult
from agenticstudio.runtime import CollectionJob, CollectionRunResult, run_collection
from agenticstudio.sources.bountyhub import SOURCE_NAME as BOUNTYHUB_SOURCE
from agenticstudio.sources.bountyhub import BountyHubAdapter
from agenticstudio.sources.bountyhub import normalize as normalize_bountyhub
from agenticstudio.sources.discourse import FORUMS, DiscourseAdapter
from agenticstudio.sources.discourse import normalize as normalize_discourse
from agenticstudio.sources.market_a import SOURCE_NAME as MARKET_A_SOURCE
from agenticstudio.sources.market_a import MarketAAdapter
from agenticstudio.sources.market_a import normalize as normalize_market_a
from agenticstudio.sources.hacker_news import SOURCE_NAME as HN_SOURCE
from agenticstudio.sources.hacker_news import HackerNewsAdapter, discover_latest_thread
from agenticstudio.sources.hacker_news import normalize as normalize_hn
from agenticstudio.sources.issuehunt import SOURCE_NAME as ISSUEHUNT_SOURCE
from agenticstudio.sources.issuehunt import IssueHuntAdapter
from agenticstudio.sources.issuehunt import normalize as normalize_issuehunt
from agenticstudio.sources.opire import SOURCE_NAME as OPIRE_SOURCE
from agenticstudio.sources.opire import OpireAdapter
from agenticstudio.sources.opire import normalize as normalize_opire
from agenticstudio.sources.market_b import MarketBAdapter
from agenticstudio.sources.market_b import normalize as normalize_market_b
from agenticstudio.sources.board import SECTIONS as BOARD_SECTIONS
from agenticstudio.sources.board import BoardAdapter
from agenticstudio.sources.board import normalize as normalize_board
from agenticstudio.sources.reddit import SOURCE_NAME as REDDIT_SOURCE
from agenticstudio.sources.reddit import RedditAdapter
from agenticstudio.sources.reddit import normalize as normalize_reddit

OWNER_TIMEZONE = ZoneInfo("Europe/Istanbul")
_log = logging.getLogger(__name__)


def _brief_as_of(run_id: str, predictions: Iterable[PredictionRecord]) -> datetime:
    """Return a stable report reference for retries of one logical daily run."""
    try:
        owner_day = date.fromisoformat(run_id)
    except ValueError:
        latest = max(
            (record.evaluated_at.astimezone(UTC) for record in predictions),
            default=datetime(1970, 1, 1, tzinfo=UTC),
        )
        return datetime.combine(latest.date(), time.max, tzinfo=UTC)
    end_of_day = datetime.combine(owner_day, time.max, tzinfo=OWNER_TIMEZONE).astimezone(UTC)
    # A morning run must not age postings against a reference hours in the future.
    return min(end_of_day, datetime.now(UTC))


def _request(run_id: str, source: str, selection: str) -> CollectionRequest:
    # Structured encoding avoids delimiter ambiguity and separates source namespaces.
    identity = json.dumps([run_id, source, selection], ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return CollectionRequest(source_name=source, selection=selection,
                             request_id=f"daily-v1-{digest}")


def _text(value: str, token: str | None) -> str:
    if token:
        value = value.replace(token, "[REDACTED]")
    value = " ".join(value.split())
    for char in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "|"):
        value = value.replace(char, "\\" + char)
    return value


def _status(result: CollectionRunResult, token: str | None) -> str:
    lines = ["## Collection status", ""]
    for job in result.jobs:
        request = job.request
        retry = isinstance(job.outcome, RetryRequired)
        reason = "none"
        if isinstance(job.outcome, RetryRequired):
            reason = _text(job.outcome.reason, token)
        lines.append(
            f"- Source: {_text(request.source_name, token)}; "
            f"selection: {_text(request.selection, token)}; "
            f"request id: {_text(request.request_id, token)}; "
            f"opportunities: {job.result_count}; rejections: {job.rejection_count}; "
            f"retry required: {'yes' if retry else 'no'}; reason: {reason}."
        )
    return "\n".join(lines) + "\n"


def _write_atomic(path: Path, report: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(report)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--run-id", required=True, help="reuse unchanged for logical run retries")
    parser.add_argument("--hn-thread", action="append", default=[])
    parser.add_argument("--hn-latest", action="store_true")
    parser.add_argument("--opire", action="store_true")
    parser.add_argument("--bountyhub", action="store_true")
    parser.add_argument("--github-bounty-max-candidates", type=int)
    parser.add_argument("--github-bounty-max-comment-pages", type=int)
    parser.add_argument("--market_a", action="store_true")
    parser.add_argument("--issuehunt", action="store_true")
    parser.add_argument("--board", action="store_true")
    parser.add_argument("--quick", action="store_true",
                        help="frequent partial scan: only the newest Board topics")
    parser.add_argument("--market_b", action="store_true")
    parser.add_argument("--reddit", action="store_true",
                        help="hiring/task posts from job subreddits (one public feed request)")
    parser.add_argument("--forums", action="store_true",
                        help="niche Discourse job categories (n8n, Make)")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--minimum-confidence", type=int, default=60,
                        help="review threshold; screening enforces a minimum of 60")
    parser.add_argument("--gemini-shadow-dir", type=Path,
                        help="opt-in separate semantic sidecars; never changes decisions")
    parser.add_argument("--triage", action="store_true",
                        help="prepend advisory semantic triage of all candidates; "
                             "requires --gemini-shadow-dir")
    parser.add_argument("--triage-output", type=Path,
                        help="also write triage verdicts as JSON; requires --triage")
    parser.add_argument("--gemini-shadow-model", default="gemini-3.5-flash-lite")
    parser.add_argument("--gemini-shadow-daily-cap", type=int, default=60)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seen-output", type=Path,
                        help="write IDs of every opportunity collected in this run as JSON")
    args = parser.parse_args(argv)
    if not any((args.hn_thread, args.hn_latest, args.opire, args.bountyhub,
                args.market_a, args.issuehunt, args.board, args.market_b,
                args.reddit, args.forums)):
        parser.error("configure at least one --hn-thread, --hn-latest, --opire, "
                     "--bountyhub, --market_a, --issuehunt, --board, --market_b, "
                     "--reddit or --forums")
    if not args.run_id.strip():
        parser.error("--run-id must not be blank")
    if not 1 <= args.minimum_confidence <= 100:
        parser.error("--minimum-confidence must be between 1 and 100")
    if any(not re.fullmatch(r"[1-9][0-9]*", thread) for thread in args.hn_thread):
        parser.error("--hn-thread must be a positive HN thread ID")
    if len(set(args.hn_thread)) != len(args.hn_thread):
        parser.error("duplicate --hn-thread")
    if args.output and args.output.resolve() == args.database.resolve():
        parser.error("--output must differ from --database")

    if args.triage and not args.gemini_shadow_dir:
        parser.error("--triage requires --gemini-shadow-dir")
    if args.triage_output and not args.triage:
        parser.error("--triage-output requires --triage")

    if not 0 <= args.gemini_shadow_daily_cap <= 500:
        parser.error("--gemini-shadow-daily-cap must be between 0 and 500")

    bounty_bounds: dict[str, int] = {}
    if ((args.github_bounty_max_candidates is None)
            != (args.github_bounty_max_comment_pages is None)):
        parser.error("GitHub bounty limits must be supplied together")
    if args.github_bounty_max_candidates is not None:
        if not 1 <= args.github_bounty_max_candidates <= 5:
            parser.error("GitHub bounty candidates must be between 1 and 5")
        if not 1 <= args.github_bounty_max_comment_pages <= 3:
            parser.error("GitHub bounty comment pages must be between 1 and 3")
        bounty_bounds = {"max_candidates": args.github_bounty_max_candidates,
                         "max_comment_pages": args.github_bounty_max_comment_pages}

    if args.hn_latest:
        try:
            latest = str(discover_latest_thread())
        except TransientFetchFailure:
            print("HN discovery temporarily unavailable; retry required", file=sys.stderr)
            return 75
        except ValueError as error:
            parser.error(str(error))
        if latest not in args.hn_thread:
            args.hn_thread.append(latest)

    token = os.environ.get("GITHUB_TOKEN")
    jobs = [CollectionJob(HackerNewsAdapter(), _request(args.run_id, HN_SOURCE, thread),
                          normalize_hn) for thread in args.hn_thread]
    if args.opire:
        opire_adapter = (OpireAdapter(
            token=token, max_candidates=args.github_bounty_max_candidates,
            max_comment_pages=args.github_bounty_max_comment_pages, max_search_pages=1,
        ) if bounty_bounds else OpireAdapter(token=token))
        jobs.append(CollectionJob(opire_adapter,
                                  _request(args.run_id, OPIRE_SOURCE, "all"), normalize_opire))
    if args.bountyhub:
        bountyhub_adapter = (BountyHubAdapter(
            token=token, max_candidates=args.github_bounty_max_candidates,
            max_comment_pages=args.github_bounty_max_comment_pages,
        ) if bounty_bounds else BountyHubAdapter(token=token))
        jobs.append(CollectionJob(bountyhub_adapter,
                                  _request(args.run_id, BOUNTYHUB_SOURCE, "all"),
                                  normalize_bountyhub))
    if args.market_a:
        jobs.append(CollectionJob(MarketAAdapter(),
                                  _request(args.run_id, MARKET_A_SOURCE, "technical"),
                                  normalize_market_a))
    if args.issuehunt:
        jobs.append(CollectionJob(IssueHuntAdapter(),
                                  _request(args.run_id, ISSUEHUNT_SOURCE, "all"),
                                  normalize_issuehunt))
    if args.board:
        # Per section, 2 s apart: a quick scan reads 1 listing + 3 newest topics; a full scan
        # pages newest-first until a week-old first post (at most 5 listings + 30 topics).
        for section in BOARD_SECTIONS:
            adapter = (BoardAdapter(section=section, max_topics=3, max_results=3, max_age_days=1)
                       if args.quick else
                       BoardAdapter(section=section, max_pages=5, max_topics=30, max_results=30,
                                  max_age_days=7))
            jobs.append(CollectionJob(adapter, _request(args.run_id, "board", section),
                                      normalize_board))
    if args.market_b:
        # Three listing pages, 2 s apart; descriptions are on the listing.
        jobs.append(CollectionJob(MarketBAdapter(),
                                  _request(args.run_id, "market_b", "technology"),
                                  normalize_market_b))
    if args.reddit:
        jobs.append(CollectionJob(RedditAdapter(), _request(args.run_id, REDDIT_SOURCE, "hiring"),
                                  normalize_reddit))
    if args.forums:
        for forum in FORUMS:
            jobs.append(CollectionJob(DiscourseAdapter(forum), _request(args.run_id, forum, "jobs"),
                                      normalize_discourse))
    store = SQLiteOpportunityStore(args.database)
    try:
        if args.migrate:
            store.migrate()
        result = run_collection(jobs, store, ScreeningEvaluator())
        # Shared persistence can resolve overlapping slices to the same Opportunity.
        # Keep one matching prediction per ID, in deterministic job order.
        opportunities = {}
        predictions = {}
        for job in result.jobs:
            if isinstance(job.outcome, SliceResult):
                for item in job.outcome.opportunities:
                    opportunities[item.id] = item
                for prediction in job.outcome.predictions:
                    predictions[prediction.opportunity_id] = prediction
        if args.seen_output:
            _write_atomic(args.seen_output, json.dumps(sorted(str(key) for key in opportunities)))
        as_of = _brief_as_of(args.run_id, predictions.values())
        excluded = []
        for key, item in list(opportunities.items()):
            reasons = (exclusion_reasons(item, as_of)
                       if item.decision.status == "new" else ())
            if reasons:
                excluded.append((item, reasons))
                del opportunities[key]
                del predictions[item.id]
        brief = build_brief(
            opportunities.values(), predictions.values(),
            # Heuristics must remain human-gated even with a lower CLI threshold.
            minimum_confidence=max(60, args.minimum_confidence),
            as_of=as_of,
        )
        report = (render_markdown(brief) + "\n" + render_prefilter(excluded) + "\n"
                  + _status(result, token))
        report += "\n## Screening evidence\n\n"
        candidates = [entry for entry in brief.entries
                      if entry.bucket == "needs_investigation"]
        for entry in candidates[:brief.max_investigation_entries]:
            prediction = entry.prediction
            report += f"- Screening for {prediction.opportunity_id}; delivery "
            report += f"{prediction.feasibility.delivery_probability}; "
            report += " ".join(_text(reason, token) for reason in (
                *prediction.rationale, *prediction.economics.rationale,
                *prediction.feasibility.rationale, *prediction.risk.rationale,
            )) + "\n"
        if args.triage:
            from agenticstudio.evaluation.triage import (
                GeminiTriager,
                TriageRecord,
                render_triage,
                triage_shadow,
            )

            # Spend the shared cap on every candidate in brief order.
            items = [entry.opportunity for entry in candidates]
            _log.info("triage: %d candidates", len(items))
            try:
                verdicts = triage_shadow(
                    items, triager=GeminiTriager(model=args.gemini_shadow_model),
                    directory=args.gemini_shadow_dir, as_of=brief.as_of,
                    clock=lambda: datetime.now(UTC), daily_cap=args.gemini_shadow_daily_cap)
            except Exception:
                verdicts = [TriageRecord(opportunity_id=item.id, evaluated_at=brief.as_of)
                            for item in items]
            triaged = list(zip(items, verdicts, strict=True))
            if args.triage_output:
                _write_atomic(args.triage_output, json.dumps(
                    [record.model_dump(mode="json") for record in verdicts], ensure_ascii=False))
            report = render_triage(triaged) + "\n" + report
        if token:
            report = report.replace(token, "[REDACTED]")
        if args.output:
            _write_atomic(args.output, report)
        else:
            print(report, end="")
        return 75 if any(job.retry_count for job in result.jobs) else 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
