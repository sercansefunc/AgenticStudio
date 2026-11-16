import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agenticstudio import daily
from agenticstudio.domain import Decision
from agenticstudio.ingestion import TransientFetchFailure
from agenticstudio.persistence import SQLiteOpportunityStore
from agenticstudio.sources.bountyhub import BountyHubAdapter
from agenticstudio.sources.market_a import MarketAAdapter
from agenticstudio.sources.hacker_news import HackerNewsAdapter
from agenticstudio.sources.opire import OpireAdapter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    hn: dict[str, Any] = json.loads((FIXTURES / "hn_freelancer.json").read_text())
    opire: dict[str, Any] = json.loads((FIXTURES / "opire.json").read_text())
    bountyhub: dict[str, Any] = json.loads((FIXTURES / "bountyhub.json").read_text())
    market_a = json.loads((FIXTURES / "market_a.json").read_text())
    # Pre-filter excludes jobs older than 7 days: keep fixtures fresh, stable within a UTC day.
    yesterday = int(datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                    .timestamp()) - 86400
    for project in market_a["result"]["projects"]:
        project["submitdate"] = yesterday
    calls: list[str] = []

    def public(url: str, headers: Mapping[str, str]) -> object:
        calls.append(url)
        assert "Authorization" not in headers
        return market_a

    monkeypatch.setattr(daily, "MarketAAdapter", lambda: MarketAAdapter(public))

    def load_hn(identifier: int) -> object:
        calls.append(str(identifier))
        if identifier == 200:
            return {**hn["100"], "id": 200, "kids": [201]}
        if identifier == 201:
            return {**hn["101"], "id": 201, "parent": 200}
        return hn[str(identifier)]

    def github(url: str, headers: Mapping[str, str]) -> object:
        calls.append(url)
        token = os.environ.get("GITHUB_TOKEN")
        assert headers.get("Authorization") == (f"Bearer {token}" if token else None)
        if "/search/issues?" in url:
            return {"incomplete_results": False, "items": [opire["issue"]]}
        if "/comments?" in url:
            return opire["comments"]
        return opire["issue"]

    def github_bountyhub(url: str, headers: Mapping[str, str]) -> object:
        calls.append(url)
        token = os.environ.get("GITHUB_TOKEN")
        assert headers.get("Authorization") == (f"Bearer {token}" if token else None)
        if "/search/issues?" in url:
            return {"incomplete_results": False, "items": [bountyhub["issue"]]}
        if "/comments?" in url:
            return bountyhub["comments"]
        return bountyhub["issue"]

    monkeypatch.setattr(daily, "BountyHubAdapter",
                        lambda *, token: BountyHubAdapter(github_bountyhub, token=token))
    monkeypatch.setattr(daily, "HackerNewsAdapter", lambda: HackerNewsAdapter(load_hn))
    monkeypatch.setattr(daily, "OpireAdapter", lambda *, token: OpireAdapter(github, token=token))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return calls


def arguments(tmp_path: Path, *sources: str) -> list[str]:
    return ["--database", str(tmp_path / "daily.sqlite"), "--run-id", "day-1", *sources]


@pytest.mark.parametrize("sources", [
    ["--hn-thread", "100"], ["--opire"], ["--hn-thread", "100", "--opire"],
    ["--market_a"],
    ["--hn-thread", "100", "--opire", "--bountyhub", "--market_a"],
    ["--bountyhub"], ["--hn-thread", "100", "--opire", "--bountyhub"],
])
def test_sources_and_reopen_replay(
    tmp_path: Path, offline: list[str], capsys: pytest.CaptureFixture[str], sources: list[str],
) -> None:
    args = arguments(tmp_path, *sources)
    assert daily.main([*args, "--migrate", "--minimum-confidence", "70"]) == 0
    first = capsys.readouterr().out
    assert "Review confidence threshold: 70/100" in first
    assert "## Needs investigation" in first
    assert "Below 70/100 review confidence" in first
    assert "retry required: no" in first
    if "--bountyhub" in sources:
        assert "Source: bountyhub" in first
    if "--opire" in sources:
        assert "Source: opire" in first
    if "--hn-thread" in sources:
        assert "Source: hacker\\_news" in first
        assert "rejections: 2" in first
    assert daily.main([*args, "--minimum-confidence", "70"]) == 0
    assert capsys.readouterr().out == first
    assert offline


def test_two_threads_have_distinct_stable_requests(
    tmp_path: Path, offline: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    args = arguments(tmp_path, "--hn-thread", "100", "--hn-thread", "200")
    assert daily.main([*args, "--migrate"]) == 0
    report = capsys.readouterr().out
    requests = re.findall(r"request id: ([^;]+)", report)
    assert len(set(requests)) == 2
    assert "selection: 100" in report and "selection: 200" in report
    assert report.count("- Opportunity:") == 2
    assert daily.main(args) == 0
    assert capsys.readouterr().out == report


@pytest.mark.parametrize("sources", [[], ["--hn-thread", "100", "--hn-thread", "100"],
                                      ["--hn-thread", "0"]])
def test_invalid_configuration_before_fetch(
    tmp_path: Path, offline: list[str], sources: list[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        daily.main(arguments(tmp_path, *sources))
    assert error.value.code == 2
    assert offline == []
    assert not (tmp_path / "daily.sqlite").exists()


def test_retry_preserves_success_and_redacts_token(
    tmp_path: Path, offline: list[str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "secret-github-credential"
    monkeypatch.setenv("GITHUB_TOKEN", token)

    def failure(url: str, headers: Mapping[str, str]) -> object:
        assert headers["Authorization"] == f"Bearer {token}"
        raise TransientFetchFailure(f"temporary outage {token}")

    original_factory = getattr(daily, "OpireAdapter")
    monkeypatch.setattr(
        "agenticstudio.daily.OpireAdapter",
        lambda *, token: OpireAdapter(failure, token=token),
    )
    args = arguments(tmp_path, "--hn-thread", "100", "--opire")
    assert daily.main([*args, "--migrate"]) == 75
    report = capsys.readouterr().out
    assert token not in report
    assert "temporary outage" in report and "REDACTED" in report
    assert "retry required: yes" in report
    ids = re.findall(r"- Opportunity: (.+)", report)
    assert len(ids) == 1
    monkeypatch.setattr("agenticstudio.daily.OpireAdapter", original_factory)
    assert daily.main(args) == 0
    replay = capsys.readouterr().out
    assert ids[0] in replay
    assert replay.count("- Opportunity:") == 2
    assert token not in replay


def test_terminal_state_obeys_existing_brief(
    tmp_path: Path, offline: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    args = arguments(tmp_path, "--hn-thread", "100")
    daily.main([*args, "--migrate"])
    report = capsys.readouterr().out
    identifier = UUID(re.findall(r"- Opportunity: (.+)", report)[0])
    store = SQLiteOpportunityStore(tmp_path / "daily.sqlite")
    try:
        with store.transaction() as repository:
            item = repository.get(identifier)
            assert item is not None
            repository.save(item.model_copy(update={"decision": Decision(status="rejected")}))
    finally:
        store.close()
    assert daily.main(args) == 0
    report = capsys.readouterr().out
    assert "## Terminal ignored\n\n1 terminal opportunities ignored." in report


def test_output_atomic_replace_and_cleanup(
    tmp_path: Path, offline: list[str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "report.md"
    output.write_text("old report", encoding="utf-8")
    original = os.replace
    replacements: list[Path] = []

    def replace(source: Path, destination: Path) -> None:
        assert destination == output
        assert source.parent == output.parent
        assert output.read_text() == "old report"
        assert source.read_text().startswith("# Opportunity triage brief")
        replacements.append(source)
        original(source, destination)

    monkeypatch.setattr("agenticstudio.daily.os.replace", replace)
    args = arguments(tmp_path, "--opire", "--output", str(output))
    assert daily.main([*args, "--migrate"]) == 0
    assert capsys.readouterr().out == ""
    assert len(replacements) == 1 and not replacements[0].exists()
    saved = output.read_text()

    def failed_replace(source: Path, destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("agenticstudio.daily.os.replace", failed_replace)
    with pytest.raises(OSError, match="replace failed"):
        daily.main(args)
    assert output.read_text() == saved
    assert list(tmp_path.glob(".report.md.*")) == []


def test_unexpected_source_error_is_isolated_and_reported(
    tmp_path: Path, offline: list[str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(identifier: int) -> object:
        raise ValueError("invalid source configuration")

    monkeypatch.setattr(daily, "HackerNewsAdapter", lambda: HackerNewsAdapter(broken))
    assert daily.main(arguments(tmp_path, "--hn-thread", "100", "--migrate")) == 75
    assert "unexpected ValueError: invalid source configuration" in capsys.readouterr().out


def test_request_encoding_is_unambiguous() -> None:
    identities = [daily._request(*values).request_id for values in (
        ("run", "hacker_news", "100"), ("run", "hacker_news", "200"),
        ("run", "opire", "all"), ("run-2", "hacker_news", "100"),
        ("a:b", "c", "d"), ("a", "b:c", "d"),
    )]
    assert len(set(identities)) == len(identities)


@pytest.mark.parametrize("explicit", ["100", "200"])
def test_latest_combines_and_replays(
    tmp_path: Path, offline: list[str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], explicit: str,
) -> None:
    monkeypatch.setattr(daily, "discover_latest_thread", lambda: 100)
    args = arguments(tmp_path, "--hn-latest", "--hn-thread", explicit)
    assert daily.main([*args, "--migrate"]) == 0
    report = capsys.readouterr().out
    assert report.count("selection: 100;") == 1
    assert offline.count("100") == 1
    assert daily.main(args) == 0
    assert capsys.readouterr().out == report
    # Discovery and an explicit selection resolve to identical request identities.
    explicit_args = arguments(tmp_path, "--hn-thread", explicit)
    if explicit != "100":
        explicit_args.extend(["--hn-thread", "100"])
    assert daily.main(explicit_args) == 0
    assert capsys.readouterr().out == report


@pytest.mark.parametrize("transient", [True, False])
def test_discovery_failure_before_persistence(
    tmp_path: Path, offline: list[str], monkeypatch: pytest.MonkeyPatch, transient: bool,
) -> None:
    def fail() -> int:
        if transient:
            raise TransientFetchFailure("outage")
        raise ValueError("no verified recent HN freelancer thread found")

    monkeypatch.setattr(daily, "discover_latest_thread", fail)
    args = arguments(tmp_path, "--hn-latest", "--opire", "--migrate")
    if transient:
        assert daily.main(args) == 75
    else:
        with pytest.raises(SystemExit) as error:
            daily.main(args)
        assert error.value.code == 2
    assert offline == []
    assert not (tmp_path / "daily.sqlite").exists()


def test_bountyhub_retry_preserves_other_sources(
    tmp_path: Path, offline: list[str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = "bountyhub-offline-secret"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    original = getattr(daily, "BountyHubAdapter")

    def failure(url: str, headers: Mapping[str, str]) -> object:
        assert headers["Authorization"] == f"Bearer {token}"
        raise TransientFetchFailure(f"temporary outage {token}")

    monkeypatch.setattr(daily, "BountyHubAdapter",
                        lambda *, token: BountyHubAdapter(failure, token=token))
    args = arguments(tmp_path, "--hn-thread", "100", "--opire", "--bountyhub")
    assert daily.main([*args, "--migrate"]) == 75
    report = capsys.readouterr().out
    identifiers = re.findall(r"- Opportunity: (.+)", report)
    assert len(identifiers) == 2 and token not in report
    monkeypatch.setattr(daily, "BountyHubAdapter", original)
    assert daily.main(args) == 0
    replay = capsys.readouterr().out
    assert all(identifier in replay for identifier in identifiers)
    assert replay.count("- Opportunity:") == 3 and token not in replay
    assert daily.main(args) == 0
    assert capsys.readouterr().out == replay


def test_market_a_retry_preserves_other_sources(
    tmp_path: Path, offline: list[str], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failure(url: str, headers: Mapping[str, str]) -> object:
        raise TransientFetchFailure("MarketA outage")

    monkeypatch.setattr(daily, "MarketAAdapter", lambda: MarketAAdapter(failure))
    args = arguments(tmp_path, "--hn-thread", "100", "--opire", "--bountyhub", "--market_a")
    assert daily.main([*args, "--migrate"]) == 75
    report = capsys.readouterr().out
    assert report.count("- Opportunity:") == 3
    assert "Source: market_a; selection: technical;" in report
    assert "retry required: yes; reason: MarketA outage" in report


def test_screening_daily_evidence_and_minimum_human_gate(
    tmp_path: Path, offline: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    args = arguments(tmp_path, "--opire", "--market_a", "--minimum-confidence", "1")
    assert daily.main([*args, "--migrate"]) == 0
    report = capsys.readouterr().out
    assert "Review confidence threshold: 60/100" in report
    assert "## Decision ready\n\nNone." in report
    assert "## Screening evidence" in report
    assert "judged by Gemini triage" in report
    assert "source.risk" in report
    assert "human investigation is required" in report
    assert daily.main(args) == 0
    assert capsys.readouterr().out == report


def test_shadow_dir_without_triage_makes_no_gemini_calls(
    tmp_path: Path, offline: list[str], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = arguments(tmp_path, "--market_a")
    assert daily.main([*args, "--migrate"]) == 0
    baseline = capsys.readouterr().out
    monkeypatch.setenv("GEMINI_API_KEY", "shadow-secret")
    sidecars = tmp_path / "shadow"
    assert daily.main([*args, "--gemini-shadow-dir", str(sidecars)]) == 0
    assert capsys.readouterr().out == baseline
    assert not sidecars.exists()


@pytest.mark.parametrize("mode", ["success", "failure"])
def test_triage_prepends_all_candidates(
    tmp_path: Path, offline: list[str], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    from agenticstudio.evaluation.triage import TriageRecord

    args = arguments(tmp_path, "--market_a")
    assert daily.main([*args, "--migrate"]) == 0
    baseline = capsys.readouterr().out
    seen: list[str] = []

    def capture(opportunities: Any, **kwargs: Any) -> list[TriageRecord]:
        seen.extend(str(item.id) for item in opportunities)
        if mode == "failure":
            raise ValueError("Provider unavailable")
        return [TriageRecord(opportunity_id=item.id, evaluated_at=kwargs["as_of"],
                             verdict="review", summary="Uygun.", confidence=80)
                for item in opportunities]

    monkeypatch.setattr("agenticstudio.evaluation.triage.triage_shadow", capture)
    assert daily.main([*args, "--triage", "--gemini-shadow-dir",
                       str(tmp_path / "shadow")]) == 0
    report = capsys.readouterr().out
    assert report.startswith("# Triage") and report.endswith(baseline)
    assert seen and f"Evaluated {len(seen)}:" in report
    assert len(set(seen)) == len(seen)
    assert ("review " + str(len(seen))) in report if mode == "success" else (
        f"unknown {len(seen)}" in report)


def test_triage_requires_shadow_dir(tmp_path: Path, offline: list[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        daily.main([*arguments(tmp_path, "--market_a"), "--triage"])
    assert not offline


@pytest.mark.parametrize("limits", [
    ["--github-bounty-max-candidates", "3"],
    ["--github-bounty-max-comment-pages", "1"],
    ["--github-bounty-max-candidates", "0", "--github-bounty-max-comment-pages", "1"],
    ["--github-bounty-max-candidates", "6", "--github-bounty-max-comment-pages", "1"],
    ["--github-bounty-max-candidates", "3", "--github-bounty-max-comment-pages", "0"],
    ["--github-bounty-max-candidates", "3", "--github-bounty-max-comment-pages", "4"],
    ["--github-bounty-max-candidates", "bad", "--github-bounty-max-comment-pages", "1"],
])
def test_invalid_bounty_bounds_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limits: list[str],
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid bounds reached discovery, adapter or database")
    for name in ("discover_latest_thread", "OpireAdapter", "BountyHubAdapter",
                 "SQLiteOpportunityStore"):
        monkeypatch.setattr(daily, name, forbidden)
    output = tmp_path / "report.md"
    output.write_text("existing report")
    with pytest.raises(SystemExit) as error:
        daily.main(arguments(tmp_path, "--hn-latest", "--opire", "--bountyhub",
                             "--migrate", "--output", str(output), *limits))
    assert error.value.code == 2
    assert not (tmp_path / "daily.sqlite").exists()
    assert output.read_text() == "existing report"


@pytest.mark.parametrize("sources", [("--opire",), ("--bountyhub",),
                                      ("--opire", "--bountyhub")])
@pytest.mark.parametrize("bounds", [None, (1, 1), (3, 1), (5, 3)])
def test_bounty_adapter_bounds(
    tmp_path: Path, offline: list[str], monkeypatch: pytest.MonkeyPatch,
    sources: tuple[str, ...], bounds: tuple[int, int] | None,
) -> None:
    captured: dict[str, dict[str, Any]] = {}
    for name in ("OpireAdapter", "BountyHubAdapter"):
        factory = getattr(daily, name)
        def capture(*, token: str | None, _name: str = name,
                    _factory: Any = factory, **kwargs: Any) -> Any:
            captured[_name] = kwargs
            adapter = _factory(token=token)
            for key, value in kwargs.items():
                setattr(adapter, "_" + key, value)
            return adapter
        monkeypatch.setattr(daily, name, capture)
    limits = ([] if bounds is None else
              ["--github-bounty-max-candidates", str(bounds[0]),
               "--github-bounty-max-comment-pages", str(bounds[1])])
    assert daily.main(arguments(tmp_path, *sources, "--migrate", *limits)) == 0
    expected = ({} if bounds is None else
                {"max_candidates": bounds[0], "max_comment_pages": bounds[1]})
    assert set(captured) == {name for flag, name in
                            (("--opire", "OpireAdapter"), ("--bountyhub", "BountyHubAdapter"))
                            if flag in sources}
    for name, settings in captured.items():
        assert settings == {**expected, **({"max_search_pages": 1}
                                           if bounds and name == "OpireAdapter" else {})}


def test_prefilter_removes_excluded_from_list_but_reports_counts(
    tmp_path: Path, offline: list[str], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daily, "exclusion_reasons", lambda item, as_of: ("older than 7 days",))
    assert daily.main([*arguments(tmp_path, "--market_a"), "--migrate"]) == 0
    report = capsys.readouterr().out
    assert "- older than 7 days: 3" in report
    assert "- Opportunity:" not in report


def test_triage_output_writes_verdicts(
    tmp_path: Path, offline: list[str], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agenticstudio.evaluation.triage import TriageRecord

    def review(opportunities: Any, **kwargs: Any) -> list[TriageRecord]:
        return [TriageRecord(opportunity_id=item.id, evaluated_at=kwargs["as_of"],
                             verdict="review", summary="Uygun.", confidence=80)
                for item in opportunities]

    monkeypatch.setattr("agenticstudio.evaluation.triage.triage_shadow", review)
    output = tmp_path / "triage.json"
    assert daily.main([*arguments(tmp_path, "--market_a"), "--migrate", "--triage",
                       "--gemini-shadow-dir", str(tmp_path / "shadow"),
                       "--triage-output", str(output)]) == 0
    records = json.loads(output.read_text())
    assert records and {record["verdict"] for record in records} == {"review"}
    assert f"Evaluated {len(records)}:" in capsys.readouterr().out


def test_triage_output_requires_triage(tmp_path: Path, offline: list[str]) -> None:
    with pytest.raises(SystemExit, match="2"):
        daily.main([*arguments(tmp_path, "--market_a"), "--triage-output",
                    str(tmp_path / "t.json")])
    assert not offline
