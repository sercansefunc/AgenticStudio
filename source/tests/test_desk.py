import fcntl
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agenticstudio import daily, desk
from agenticstudio.sources.market_a import MarketAAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def call(home: Path, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, Any]:
    code = desk.main(["--home", str(home), *args])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.fixture
def market_a_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    market_a = json.loads((FIXTURES / "market_a.json").read_text())
    yesterday = int(datetime.now(UTC).timestamp()) - 86400
    for project in market_a["result"]["projects"]:
        project["submitdate"] = yesterday

    def public(url: str, headers: Mapping[str, str]) -> object:
        return market_a

    monkeypatch.setattr(daily, "MarketAAdapter", lambda: MarketAAdapter(public))
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(desk.Settings(
        market_a=True, board=False, market_b=False, hn_latest=False, bountyhub=False,
        reddit=False, forums=False,
        triage=False).model_dump_json())
    return home


def test_default_arguments(tmp_path: Path) -> None:
    home = desk.Home(tmp_path)
    args = desk.daily_arguments(home, desk.Settings(), "2026-10-16", "2026-10-16_093000")
    for flag in ("--hn-latest", "--bountyhub", "--market_a", "--board", "--market_b",
                 "--reddit", "--forums", "--triage", "--migrate"):
        assert flag in args
    assert "--opire" not in args and "--issuehunt" not in args
    assert args[args.index("--gemini-shadow-daily-cap") + 1] == "200"
    triage_output = args[args.index("--triage-output") + 1]
    assert triage_output == str(tmp_path / "triage/2026-10-16_093000.json")
    assert args[args.index("--output") + 1] == str(tmp_path / "reports/2026-10-16_093000.md")
    quiet = desk.daily_arguments(home, desk.Settings(triage=False, bountyhub=False), "d", "s")
    assert "--gemini-shadow-dir" not in quiet and "--github-bounty-max-candidates" not in quiet


def test_quick_arguments_only_fast_closing_sources(tmp_path: Path) -> None:
    home = desk.Home(tmp_path)
    args = desk.daily_arguments(home, desk.Settings(hn_threads=[1]), "d", "s", quick=True)
    for flag in ("--market_a", "--market_b", "--board", "--quick", "--triage"):
        assert flag in args
    for flag in ("--hn-latest", "--hn-thread", "--bountyhub", "--reddit", "--forums",
                 "--output", "--github-bounty-max-candidates"):
        assert flag not in args
    assert "--quick" not in desk.daily_arguments(home, desk.Settings(), "d", "s")


def test_quick_run_is_partial_and_keeps_day_run_pending(
    market_a_only: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code, result = call(market_a_only, capsys, "run", "--quick")
    assert code == 0 and result["exit_code"] == 0
    _, status = call(market_a_only, capsys, "status")
    assert status["needs_run"] and status["reports"] == []
    _, leads = call(market_a_only, capsys, "leads")
    assert leads["leads"]
    sightings = json.loads((market_a_only / "sightings.json").read_text())
    assert sightings["full_scan_at"] is None


def test_quick_run_needs_a_quick_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "settings.json").write_text(
        desk.Settings(market_a=False, market_b=False, board=False).model_dump_json())
    code, result = call(tmp_path, capsys, "run", "--quick")
    assert code == 1 and not result["ok"]


def test_owner_day_is_istanbul() -> None:
    assert desk.today(datetime(2026, 10, 16, 21, 30, tzinfo=UTC)) == "2026-10-17"
    assert desk.run_stamp(datetime(2026, 10, 16, 21, 30, 5, tzinfo=UTC)) == "2026-10-17_003005"


def test_run_then_leads_then_mark(
    market_a_only: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code, status = call(market_a_only, capsys, "status")
    assert code == 0 and status["needs_run"] and status["last_run"] is None
    code = desk.main(["--home", str(market_a_only), "run"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 0 and any("market_a/technical: collecting" in line for line in lines)
    result = json.loads(lines[-1])
    assert result["exit_code"] == 0
    assert (market_a_only / "reports" / f"{result['report']}.md").exists()
    _, status = call(market_a_only, capsys, "status")
    assert not status["needs_run"] and status["reports"] == [result["report"]]
    _, leads = call(market_a_only, capsys, "leads")
    lead = leads["leads"][0]
    assert lead["status"] == "new" and lead["verdict"] is None and lead["url"].startswith("http")
    assert "budget_max_usd" in lead and "prefilter" not in lead
    assert isinstance(lead["bids"], int)  # raw count, not the 20/40/60/80 band
    assert "rejected" in lead["transitions"]
    code, marked = call(market_a_only, capsys, "mark", lead["id"], "proposal_sent")
    assert code == 0 and marked["lead"]["status"] == "proposal_sent"
    code, invalid = call(market_a_only, capsys, "mark", lead["id"], "delivered")
    assert code == 1 and not invalid["ok"]


def test_latest_full_scan_replaces_older_leads_reports_and_hides_prefiltered(
    market_a_only: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    market_a = json.loads((FIXTURES / "market_a.json").read_text())
    projects = market_a["result"]["projects"]
    for project in projects:
        project["submitdate"] = int(datetime.now(UTC).timestamp()) - 86400
    monkeypatch.setattr(daily, "MarketAAdapter",
                        lambda: MarketAAdapter(lambda url, headers: market_a))
    monkeypatch.setattr(desk, "run_stamp", lambda: "2026-10-17_090000")
    desk.main(["--home", str(market_a_only), "run"])
    capsys.readouterr()
    _, leads = call(market_a_only, capsys, "leads")
    first = {lead["id"]: lead for lead in leads["leads"]}
    tracked, dropped = sorted(first)[:2]
    call(market_a_only, capsys, "mark", tracked, "proposal_sent")

    # Second scan: one listing gets crowded (pre-filtered), the rest are closed.
    store = desk._store(desk.Home(market_a_only))
    with store.transaction() as repository:
        crowded_id = next(item.source.external_id for item in repository.list_all()
                          if str(item.id) not in (tracked, dropped))
    store.close()
    crowded = next(p for p in projects if str(p["id"]) == crowded_id)
    crowded["bid_stats"]["bid_count"] = 30
    market_a["result"]["projects"] = [crowded]
    monkeypatch.setattr(desk, "run_stamp", lambda: "2026-10-17_120000")
    desk.main(["--home", str(market_a_only), "run"])
    capsys.readouterr()

    _, leads = call(market_a_only, capsys, "leads")
    assert [lead["id"] for lead in leads["leads"]] == [tracked]
    _, status = call(market_a_only, capsys, "status")
    assert status["reports"] == ["2026-10-17_120000"]

    # Hidden, not deleted: the dropped lead keeps its ID (and triage cache) for 7 days.
    home = desk.Home(market_a_only)
    store = desk._store(home)
    with store.transaction() as repository:
        assert repository.get(UUID(dropped)) is not None
    store.close()
    sightings = home.load_sightings()
    sightings.last_seen[dropped] -= timedelta(days=8)
    home.save(home.sightings, sightings)
    desk.main(["--home", str(market_a_only), "run"])
    capsys.readouterr()
    store = desk._store(home)
    with store.transaction() as repository:
        assert repository.get(UUID(dropped)) is None
        assert repository.get(UUID(tracked)) is not None
    store.close()


def test_partial_scan_prunes_nothing(
    market_a_only: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desk, "run_stamp", lambda: "2026-10-17_090000")
    desk.main(["--home", str(market_a_only), "run"])
    capsys.readouterr()
    _, before = call(market_a_only, capsys, "leads")
    monkeypatch.setattr(daily, "main", lambda argv: desk.RETRY_EXIT)
    monkeypatch.setattr(desk, "run_stamp", lambda: "2026-10-17_120000")
    desk.main(["--home", str(market_a_only), "run"])
    capsys.readouterr()
    _, after = call(market_a_only, capsys, "leads")
    assert after == before and before["leads"]


def test_full_scan_keeps_verdicts_it_did_not_recompute(
    market_a_only: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desk, "run_stamp", lambda: "2026-10-17_090000")
    desk.main(["--home", str(market_a_only), "run"])
    capsys.readouterr()
    _, leads = call(market_a_only, capsys, "leads")
    lead = leads["leads"][0]["id"]
    call(market_a_only, capsys, "mark", lead, "proposal_sent")
    (market_a_only / "triage" / "2026-10-17_090000.json").write_text(json.dumps([
        {"opportunity_id": lead, "evaluated_at": "2026-10-17T06:00:00Z", "verdict": "review",
         "summary": "iyi iş", "confidence": 80, "profile_fit": "full"}]))
    # The next full scan triages nothing (triage off / cap spent); the verdict must survive.
    monkeypatch.setattr(desk, "run_stamp", lambda: "2026-10-17_120000")
    desk.main(["--home", str(market_a_only), "run"])
    capsys.readouterr()
    assert [p.name for p in (market_a_only / "triage").iterdir()] == ["2026-10-17_120000.json"]
    _, leads = call(market_a_only, capsys, "leads")
    kept = next(item for item in leads["leads"] if item["id"] == lead)
    assert kept["verdict"] == "review" and kept["triage_summary"] == "iyi iş"
    assert kept["fit"] == "full"


def test_feedback_is_recorded_as_labels_and_cleared(
    market_a_only: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    desk.main(["--home", str(market_a_only), "run"])
    capsys.readouterr()
    _, leads = call(market_a_only, capsys, "leads")
    lead = leads["leads"][0]
    assert lead["feedback"] is None
    code, rated = call(market_a_only, capsys, "feedback", lead["id"], "--label", "reject",
                       "--note", " çok zor \n iş ")
    assert code == 0 and rated["lead"]["feedback"] == {"label": "reject", "note": "çok zor iş"}
    _, leads = call(market_a_only, capsys, "leads")
    assert next(item for item in leads["leads"] if item["id"] == lead["id"])["feedback"][
        "label"] == "reject"
    # Same shape as tests/fixtures/labeled_leads.json, so the triage evaluator can read it.
    [saved] = json.loads((market_a_only / "feedback.json").read_text())["items"]
    assert saved["source"] == "market_a" and saved["label"] == "reject"
    assert saved["id"] == lead["url"].rsplit("/", 1)[1] and saved["triage"] is None
    code, cleared = call(market_a_only, capsys, "feedback", lead["id"])
    assert code == 0 and cleared["lead"]["feedback"] is None
    assert json.loads((market_a_only / "feedback.json").read_text())["items"] == []
    code, unknown = call(market_a_only, capsys, "feedback",
                         "11111111-1111-4111-8111-111111111111", "--label", "review")
    assert code == 1 and not unknown["ok"]


def test_failed_run_is_recorded_without_auto_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    def crash(argv: Sequence[str]) -> int:
        raise RuntimeError("boom")

    monkeypatch.setattr(daily, "main", crash)
    code, result = call(tmp_path, capsys, "run")
    assert code == 1 and result["exit_code"] == 1
    _, status = call(tmp_path, capsys, "status")
    assert status["last_run"]["exit_code"] == 1 and not status["needs_run"]


def test_partial_run_is_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily, "main", lambda argv: desk.RETRY_EXIT)
    code, result = call(tmp_path, capsys, "run")
    assert code == 0 and result["ok"] and result["exit_code"] == desk.RETRY_EXIT


def test_busy_run_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily, "main", lambda argv: pytest.fail("must not run"))
    desk.Home(tmp_path).prepare()
    with (tmp_path / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        code, result = call(tmp_path, capsys, "run")
    assert code == 1 and "sürüyor" in result["error"]
    assert not (tmp_path / "last-run.json").exists()


def test_settings_roundtrip_and_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code, current = call(tmp_path, capsys, "settings")
    assert code == 0 and current["settings"] == desk.Settings().model_dump()
    changed = {**current["settings"], "board": False, "gemini_daily_cap": 50}
    code, saved = call(tmp_path, capsys, "settings", "--set", json.dumps(changed))
    assert code == 0 and saved["settings"] == changed
    code, rejected = call(tmp_path, capsys, "settings", "--set",
                          json.dumps({**changed, "gemini_daily_cap": 501}))
    assert code == 1 and not rejected["ok"]
    code, unknown = call(tmp_path, capsys, "settings", "--set", json.dumps({"bogus": 1}))
    assert code == 1
    _, kept = call(tmp_path, capsys, "settings")
    assert kept["settings"] == changed


def test_legacy_settings_key_is_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    desk.Home(tmp_path).prepare()
    legacy = {**desk.Settings().model_dump(), "board": False, "qualified_shortlist": True}
    (tmp_path / "settings.json").write_text(json.dumps(legacy))
    code, result = call(tmp_path, capsys, "settings")
    assert code == 0 and result["settings"]["board"] is False
    assert "qualified_shortlist" not in result["settings"]
    (tmp_path / "settings.json").write_text("not json")
    code, broken = call(tmp_path, capsys, "settings")
    assert code == 1 and "okunamadı" in broken["error"]


def test_latest_known_triage_verdict_wins(tmp_path: Path) -> None:
    home = desk.Home(tmp_path)
    home.prepare()
    ids = {"a": "11111111-1111-4111-8111-111111111111", "b": "22222222-2222-4222-8222-222222222222"}
    record = {"opportunity_id": ids["a"], "verdict": "maybe", "reasons": [], "summary": "x",
              "evaluated_at": "2026-10-16T00:00:00+00:00", "confidence": 50}
    (home.triage / "2026-10-15.json").write_text(json.dumps([record]))
    (home.triage / "2026-10-16.json").write_text(json.dumps(
        [{**record, "verdict": "review"}, {**record, "opportunity_id": ids["b"],
                                             "verdict": "reject", "reasons": ["too_cheap"]}]))
    (home.triage / "2026-10-17.json").write_text(json.dumps(
        [{**record, "verdict": "unknown"}]))
    verdicts = desk._triage(home)
    # Old files are read through the same reason->tier rule as new runs.
    assert verdicts[ids["a"]]["verdict"] == "review" and verdicts[ids["b"]]["verdict"] == "maybe"
