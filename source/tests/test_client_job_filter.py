import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from agenticstudio import opportunities as cli
from agenticstudio.decision import investigation_priority
from agenticstudio.decision.prefilter import exclusion_reasons, render_prefilter
from agenticstudio.domain import (
    Commercial,
    Estimate,
    Opportunity,
    Problem,
    Risk,
    Signals,
    Source,
)
from agenticstudio.persistence import SQLiteOpportunityStore

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def job(*, source: str = "market_a", age_hours: float = 10, competition: int | None = 20,
        pricing: str = "fixed", currency: str | None = "USD", low: int = 100, high: int = 300,
        capabilities: tuple[str, ...] = ("Python",), name: str = "job",
        bids: str | None = None) -> Opportunity:
    return Opportunity(
        source=Source(name=source, url=f"https://example.com/{name}", external_id=name,
                      risk_class="controlled"),
        posted_at=NOW - timedelta(hours=age_hours),
        problem=Problem(raw_text=name if bids is None else json.dumps({"bid_count": bids}),
                        summary=f"{name}\nbody"),
        commercial=Commercial(currency=currency, pricing_model=pricing,  # type: ignore[arg-type]
                              budget_min=Decimal(low), budget_max=Decimal(high)),
        signals=Signals(acceptance_clarity=0, strategic_reuse_value=0,
                        competition_estimate=competition),
        risk=Risk(technical_uncertainty=100, scope_uncertainty=100, maintenance_burden=100,
                  legal_or_platform_risk=100, abuse_risk=100, payment_or_dispute_risk=100),
        estimate=Estimate(required_capabilities=capabilities, delivery_probability=0.0,
                          win_probability=0.0, confidence=0),
    )


def test_fresh_low_competition_job_is_kept() -> None:
    assert exclusion_reasons(job(), NOW) == ()


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"age_hours": 24 * 8}, "older than 7 days"),
    ({"competition": 80}, "high competition"),
    ({"bids": "10", "competition": 40}, "high competition (10 bids)"),
    ({"high": 9, "low": 5}, "fixed budget ceiling below 10 USD"),
])
def test_exclusions(kwargs: dict[str, object], reason: str) -> None:
    (found,) = exclusion_reasons(job(**kwargs), NOW)  # type: ignore[arg-type]
    assert reason in found


def test_market_b_proposals_are_filtered() -> None:
    crowded = job(source="market_b", competition=40)
    crowded = crowded.model_copy(update={"problem": crowded.problem.model_copy(
        update={"raw_text": json.dumps({"proposals": 12})})})
    assert exclusion_reasons(crowded, NOW) == ("high competition (12 bids)",)


def test_raw_bid_count_decides_over_band() -> None:
    assert exclusion_reasons(job(bids="9", competition=40), NOW) == ()
    assert exclusion_reasons(job(bids="bad", competition=40), NOW) == ()


def test_unknown_evidence_and_other_sources_never_excluded() -> None:
    assert exclusion_reasons(job(competition=None, currency="INR", high=5, low=1), NOW) == ()
    assert exclusion_reasons(job(pricing="hourly", high=5, low=1), NOW) == ()
    assert exclusion_reasons(job(source="opire", age_hours=24 * 30, competition=100), NOW) == ()


def test_prefilter_report_counts() -> None:
    old = job(age_hours=24 * 9)
    text = render_prefilter([(old, exclusion_reasons(old, NOW))])
    assert "- older than 7 days: 1" in text
    assert "No opportunities excluded." in render_prefilter([])


def test_high_priority_needs_no_skill_tag_fit() -> None:
    # Fresh, low competition, known fixed budget: high even with unrecognised skills.
    assert investigation_priority(job(capabilities=("PHP", "Docker")), NOW).level == "high"
    assert investigation_priority(job(capabilities=(), competition=80), NOW).level != "high"


def seeded(tmp_path: Path) -> Path:
    path = tmp_path / "db.sqlite"
    store = SQLiteOpportunityStore(path)
    store.migrate()
    with store.transaction() as repository:
        for item in (job(name="a"), job(name="b", capabilities=("PHP",)),
                     job(name="c", source="opire")):
            repository.save(item)
    store.close()
    return path


def test_list_filters_and_mark_hides_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = seeded(tmp_path)
    assert cli.main(["--database", str(path), "list", "--source", "market_a"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "    https://example.com/c" not in lines and "2 of 2 matching shown." in lines
    identifier = lines[lines.index("    https://example.com/a") - 1].split()[0]
    assert cli.main(["--database", str(path), "mark", identifier, "rejected"]) == 0
    assert cli.main(["--database", str(path), "list"]) == 0
    assert "example.com/a" not in capsys.readouterr().out
    assert cli.main(["--database", str(path), "mark", identifier, "pursuing"]) == 1
    assert cli.main(["--database", str(path), "list", "--status", "rejected"]) == 0
    assert "example.com/a" in capsys.readouterr().out
