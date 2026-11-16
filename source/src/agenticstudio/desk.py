"""JSON bridge for the macOS desk app: python -m agenticstudio.desk --home DIR <command>.

The app owns no business logic: it calls this module, which runs the same daily
collection locally (owner's network) and reads/writes the same SQLite store.
Secrets arrive only as environment variables (GEMINI_API_KEY, GITHUB_TOKEN).
"""

import argparse
import fcntl
import json
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from agenticstudio import daily
from agenticstudio.decision.prefilter import exclusion_reasons
from agenticstudio.domain import Opportunity, Status
from agenticstudio.evaluation.triage import TriageRecord, approx_usd, consistent, raw_bids
from agenticstudio.lifecycle import InvalidTransitionError, allowed_transitions, transition
from agenticstudio.persistence import SQLiteOpportunityStore

RETRY_EXIT = 75
FeedbackLabel = Literal["review", "maybe", "reject"]


class Settings(BaseModel):
    """Owner-editable run configuration."""

    model_config = ConfigDict(extra="forbid")

    market_a: bool = True
    board: bool = True
    market_b: bool = True
    reddit: bool = True
    forums: bool = True
    hn_latest: bool = True
    hn_threads: list[Annotated[int, Field(ge=1)]] = []
    bountyhub: bool = True
    opire: bool = False
    issuehunt: bool = False
    github_bounty_max_candidates: Annotated[int, Field(ge=1, le=5)] = 5
    github_bounty_max_comment_pages: Annotated[int, Field(ge=1, le=3)] = 1
    minimum_confidence: Annotated[int, Field(ge=1, le=100)] = 60
    triage: bool = True
    gemini_daily_cap: Annotated[int, Field(ge=0, le=500)] = 200
    # Low-bid marketplace leads last minutes, not days: the app re-scans them while open.
    quick_scan: bool = True


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: str
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    exit_code: int | None = None


class Sightings(BaseModel):
    """When each lead was last collected, and when the last full (error-free) scan started."""
    model_config = ConfigDict(extra="forbid")

    full_scan_at: AwareDatetime | None = None
    last_seen: dict[str, AwareDatetime] = {}


class Home:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.database = root / "agenticstudio.sqlite"
        self.reports = root / "reports"
        self.triage = root / "triage"
        # Owner's own verdicts; same item shape as tests/fixtures/labeled_leads.json, so
        # `python -m agenticstudio.evaluation.triage --labels` measures triage against it.
        self.feedback = root / "feedback.json"
        self.shadow = root / "gemini-shadow"
        self.settings = root / "settings.json"
        self.state = root / "last-run.json"
        self.seen = root / "last-scan.json"
        self.sightings = root / "sightings.json"
        self.lock = root / "run.lock"

    def prepare(self) -> None:
        for directory in (self.root, self.reports, self.triage, self.shadow):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def load_settings(self) -> Settings:
        if not self.settings.exists():
            return Settings()
        text = self.settings.read_text()
        try:
            data = json.loads(text)
        except ValueError:
            return Settings.model_validate_json(text)  # raises ValidationError
        # Removed setting; older settings.json files still carry it.
        if isinstance(data, dict):
            data.pop("qualified_shortlist", None)
        return Settings.model_validate(data)

    def load_state(self) -> RunState | None:
        if not self.state.exists():
            return None
        return RunState.model_validate_json(self.state.read_text())

    def load_sightings(self) -> Sightings:
        if not self.sightings.exists():
            return Sightings()
        return Sightings.model_validate_json(self.sightings.read_text())

    def save(self, path: Path, model: BaseModel) -> None:
        daily._write_atomic(path, model.model_dump_json(indent=2))


def today(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(daily.OWNER_TIMEZONE).date().isoformat()


def run_stamp(now: datetime | None = None) -> str:
    """Owner-local scan time; one report per scan. "_" sorts after "." so legacy
    day-only files (2026-10-17.json) stay older than that day's scans."""
    return (now or datetime.now(UTC)).astimezone(daily.OWNER_TIMEZONE).strftime("%Y-%m-%d_%H%M%S")


# Sources whose low-bid/low-reply window closes fast; a quick scan collects only these.
QUICK_SOURCES = ("market_a", "market_b", "board")


def daily_arguments(home: Home, settings: Settings, day: str, stamp: str, *,
                    quick: bool = False) -> list[str]:
    if quick:
        settings = settings.model_copy(update={"hn_threads": [], **{
            name: False for name in ("reddit", "forums", "hn_latest", "bountyhub",
                                     "opire", "issuehunt")}})
    args = ["--database", str(home.database), "--run-id", day, "--migrate",
            "--minimum-confidence", str(settings.minimum_confidence),
            "--seen-output", str(home.seen)]
    if quick:
        args.append("--quick")
    else:  # quick scans leave no report; the next full scan writes one
        args += ["--output", str(home.reports / f"{stamp}.md")]
    for thread in settings.hn_threads:
        args += ["--hn-thread", str(thread)]
    for flag, enabled in (("--hn-latest", settings.hn_latest), ("--opire", settings.opire),
                          ("--bountyhub", settings.bountyhub),
                          ("--market_a", settings.market_a),
                          ("--issuehunt", settings.issuehunt), ("--board", settings.board),
                          ("--market_b", settings.market_b),
                          ("--reddit", settings.reddit), ("--forums", settings.forums)):
        if enabled:
            args.append(flag)
    if settings.opire or settings.bountyhub:
        args += ["--github-bounty-max-candidates", str(settings.github_bounty_max_candidates),
                 "--github-bounty-max-comment-pages",
                 str(settings.github_bounty_max_comment_pages)]
    if settings.triage:
        args += ["--gemini-shadow-dir", str(home.shadow),
                 "--gemini-shadow-daily-cap", str(settings.gemini_daily_cap)]
    if settings.triage:
        args += ["--triage", "--triage-output", str(home.triage / f"{stamp}.json")]
    return args


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def _fail(message: str) -> int:
    _emit({"ok": False, "error": message})
    return 1


def cmd_status(home: Home) -> int:
    state = home.load_state()
    day = today()
    reports = sorted((path.stem for path in home.reports.glob("*.md")), reverse=True)
    _emit({
        "ok": True, "today": day, "last_run": state.model_dump(mode="json") if state else None,
        # A finished run today (even partial) is enough; the owner re-runs by hand.
        "needs_run": state is None or state.day != day or state.finished_at is None,
        "reports": reports,
    })
    return 0


def cmd_run(home: Home, *, quick: bool = False) -> int:
    """A quick scan is always partial: it neither counts as the day's run nor expires leads."""
    try:
        settings = home.load_settings()
    except ValidationError as error:
        return _fail(f"settings.json okunamadı: {error.errors()[0]['msg']}")
    if quick and not any(getattr(settings, name) for name in QUICK_SOURCES):
        return _fail("Hızlı tarama için MarketA, MarketB veya Board açık olmalı.")
    day, stamp = today(), run_stamp()
    with home.lock.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _fail("Başka bir tarama sürüyor.")
        started = RunState(day=day, started_at=datetime.now(UTC))
        if not quick:
            home.save(home.state, started)
        home.seen.unlink(missing_ok=True)  # an early exit must not replay the previous scan
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        root = logging.getLogger("agenticstudio")
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            code = daily.main(daily_arguments(home, settings, day, stamp, quick=quick))
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else 2
        except Exception as error:
            print(f"unexpected {type(error).__name__}: {str(error)[:200]}", flush=True)
            code = 1
        finally:
            root.removeHandler(handler)
        if not quick:
            home.save(home.state, started.model_copy(
                update={"finished_at": datetime.now(UTC), "exit_code": code}))
        if code in (0, RETRY_EXIT):
            _record_scan(home, stamp, started.started_at, full=code == 0 and not quick)
    _emit({"ok": code in (0, RETRY_EXIT), "day": day, "exit_code": code,
           "report": stamp})
    return 0 if code in (0, RETRY_EXIT) else 1


# Owner-tracked leads outlive scans; untouched or rejected ones expire once unseen this long.
DISPOSABLE = frozenset({"new", "rejected"})
RETENTION = timedelta(days=7)


def _record_scan(home: Home, stamp: str, started_at: datetime, *, full: bool) -> None:
    """Note what this scan saw. Only a full scan expires leads and replaces reports/verdicts:
    a partial one cannot tell a closed listing from a source that failed to answer."""
    sightings = home.load_sightings()
    for identifier in json.loads(home.seen.read_text()) if home.seen.exists() else []:
        sightings.last_seen[identifier] = started_at
    if full:
        sightings.full_scan_at = started_at
        cutoff = started_at - RETENTION
        store = _store(home)
        kept: set[str] = set()
        try:
            with store.transaction() as repository:
                for item in repository.list_all():
                    seen = sightings.last_seen.get(str(item.id))
                    if item.decision.status in DISPOSABLE and (seen is None or seen < cutoff):
                        repository.delete(item.id)
                        sightings.last_seen.pop(str(item.id), None)
                    else:
                        kept.add(str(item.id))
        finally:
            store.close()
        # Leads this scan did not re-triage (tracked, rejected, off-page or over the Gemini
        # cap) keep their latest verdict: carry it into this scan's file before pruning.
        verdicts = _triage(home)
        daily._write_atomic(home.triage / f"{stamp}.json", json.dumps(
            [verdicts[key] for key in sorted(kept & verdicts.keys())], ensure_ascii=False))
        for directory, suffix in ((home.reports, ".md"), (home.triage, ".json")):
            for path in directory.glob(f"*{suffix}"):
                if path.stem != stamp:
                    path.unlink()
    home.save(home.sightings, sightings)


def _triage(home: Home) -> dict[str, dict[str, Any]]:
    """Latest verdict per opportunity; day files sort chronologically."""
    verdicts: dict[str, dict[str, Any]] = {}
    for path in sorted(home.triage.glob("*.json")):
        try:
            records = json.loads(path.read_text())
        except ValueError:
            continue
        for record in records if isinstance(records, list) else ():
            try:
                parsed = consistent(TriageRecord.model_validate_json(json.dumps(record)))
            except ValueError:
                continue
            if parsed.verdict != "unknown":
                verdicts[str(parsed.opportunity_id)] = parsed.model_dump(mode="json")
    return verdicts


def _feedback(home: Home) -> dict[str, dict[str, Any]]:
    """Owner verdict per opportunity id."""
    try:
        items = json.loads(home.feedback.read_text())["items"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return {str(item["opportunity_id"]): item for item in items
            if isinstance(item, dict) and "opportunity_id" in item}


def _lead(item: Opportunity, verdict: dict[str, Any] | None,
          feedback: dict[str, Any] | None = None) -> dict[str, Any]:
    commercial = item.commercial
    return {
        "id": str(item.id), "status": item.decision.status, "source": item.source.name,
        "url": item.source.url, "title": " ".join(item.problem.summary.split("\n")[0].split()),
        "summary": item.problem.summary[:4000],
        "posted_at": item.posted_at.isoformat() if item.posted_at else None,
        "pricing_model": commercial.pricing_model, "currency": commercial.currency,
        "budget_min": str(commercial.budget_min) if commercial.budget_min is not None else None,
        "budget_max": str(commercial.budget_max) if commercial.budget_max is not None else None,
        "budget_max_usd": approx_usd(commercial.budget_max, commercial.currency),
        "competition": item.signals.competition_estimate,
        "bids": raw_bids(item)[0],
        "fit": verdict.get("profile_fit") if verdict else None,
        "verdict": verdict.get("verdict") if verdict else None,
        "reasons": verdict.get("reasons", []) if verdict else [],
        "triage_summary": verdict.get("summary") if verdict else None,
        "effort_hours": verdict.get("effort_hours") or None if verdict else None,
        "feedback": ({"label": feedback["label"], "note": feedback.get("note", "")}
                     if feedback else None),
        "transitions": sorted(allowed_transitions(item.decision.status)),
    }


def _store(home: Home) -> SQLiteOpportunityStore:
    store = SQLiteOpportunityStore(home.database)
    store.migrate()
    return store


def cmd_leads(home: Home) -> int:
    verdicts, feedback, sightings = _triage(home), _feedback(home), home.load_sightings()
    now = datetime.now(UTC)

    def current(item: Opportunity) -> bool:
        # Disposable leads show only if the last full scan saw them; pre-filtered never show.
        if item.decision.status not in DISPOSABLE:
            return True
        seen = sightings.last_seen.get(str(item.id))
        if sightings.full_scan_at is not None and (seen is None or seen < sightings.full_scan_at):
            return False
        return item.decision.status != "new" or not exclusion_reasons(item, now)

    store = _store(home)
    try:
        with store.transaction() as repository:
            leads = [_lead(item, verdicts.get(str(item.id)), feedback.get(str(item.id)))
                     for item in repository.list_all() if current(item)]
    finally:
        store.close()
    _emit({"ok": True, "leads": leads})
    return 0


def cmd_mark(home: Home, identifier: UUID, status: Status) -> int:
    store = _store(home)
    try:
        with store.transaction() as repository:
            item = repository.get(identifier)
            if item is None:
                return _fail("Bilinmeyen fırsat.")
            try:
                updated = transition(item, status)
            except InvalidTransitionError as error:
                return _fail(str(error))
            repository.save(updated)
    finally:
        store.close()
    _emit({"ok": True, "lead": _lead(updated, _triage(home).get(str(identifier)),
                                     _feedback(home).get(str(identifier)))})
    return 0


def cmd_feedback(home: Home, identifier: UUID, label: FeedbackLabel | None, note: str) -> int:
    """Record (or clear, without a label) the owner's verdict next to triage's."""
    store = _store(home)
    try:
        with store.transaction() as repository:
            item = repository.get(identifier)
    finally:
        store.close()
    if item is None:
        return _fail("Bilinmeyen fırsat.")
    verdict = _triage(home).get(str(identifier))
    items = _feedback(home)
    if label is None:
        items.pop(str(identifier), None)
    else:
        items[str(identifier)] = {
            "source": item.source.name, "id": item.source.external_id,
            "title": " ".join(item.problem.summary.split("\n")[0].split())[:200],
            "label": label, "note": " ".join(note.split())[:1000],
            "triage": ({key: verdict.get(key) for key in
                        ("verdict", "reasons", "summary", "effort_hours")}
                       if verdict else None),
            "opportunity_id": str(identifier), "url": item.source.url,
            "budget_max_usd": approx_usd(item.commercial.budget_max, item.commercial.currency),
            "competition": item.signals.competition_estimate,
            "bids": raw_bids(item)[0],
            "at": datetime.now(UTC).isoformat(),
        }
    daily._write_atomic(home.feedback, json.dumps({
        "description": "Owner verdicts from the desk app; label = what triage should say.",
        "items": sorted(items.values(), key=lambda entry: entry["at"]),
    }, ensure_ascii=False, indent=1))
    _emit({"ok": True, "lead": _lead(item, verdict, items.get(str(identifier)))})
    return 0


def cmd_settings(home: Home, value: str | None) -> int:
    if value is not None:
        try:
            home.save(home.settings, Settings.model_validate_json(value))
        except ValidationError as error:
            return _fail(f"Geçersiz ayar: {error.errors()[0]['msg']}")
    try:
        settings = home.load_settings()
    except ValidationError as error:
        return _fail(f"settings.json okunamadı: {error.errors()[0]['msg']}")
    _emit({"ok": True, "settings": settings.model_dump()})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    run = commands.add_parser(
        "run", help="collect today's leads; streams log lines, last line JSON")
    run.add_argument("--quick", action="store_true",
                     help=f"only {', '.join(QUICK_SOURCES)}; partial, no report")
    commands.add_parser("leads")
    mark = commands.add_parser("mark")
    mark.add_argument("id", type=UUID)
    mark.add_argument("status", choices=get_args(Status))
    feedback = commands.add_parser("feedback", help="owner verdict; no --label clears it")
    feedback.add_argument("id", type=UUID)
    feedback.add_argument("--label", choices=get_args(FeedbackLabel))
    feedback.add_argument("--note", default="")
    settings = commands.add_parser("settings")
    settings.add_argument("--set", dest="value", help="full settings JSON to validate and store")
    args = parser.parse_args(argv)
    home = Home(args.home)
    home.prepare()
    if args.command == "status":
        return cmd_status(home)
    if args.command == "run":
        return cmd_run(home, quick=args.quick)
    if args.command == "leads":
        return cmd_leads(home)
    if args.command == "mark":
        return cmd_mark(home, args.id, args.status)
    if args.command == "feedback":
        return cmd_feedback(home, args.id, args.label, args.note)
    return cmd_settings(home, args.value)


if __name__ == "__main__":
    raise SystemExit(main())
