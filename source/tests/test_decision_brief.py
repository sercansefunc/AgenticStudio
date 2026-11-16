from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from agenticstudio.decision import (
    SUMMARY_VISIBLE_LIMIT,
    DecisionPolicy,
    TriageBrief,
    build_brief,
    investigation_priority,
    render_markdown,
)
from agenticstudio.domain import (
    Commercial,
    Decision,
    Estimate,
    Opportunity,
    Problem,
    Risk,
    Signals,
    Source,
    Status,
)
from agenticstudio.evaluation import BaselineEvaluator, PredictionRecord


def opportunity(name: str = "hacker_news", status: Status = "new") -> Opportunity:
    return Opportunity(
        source=Source(name=name, url=f"https://example.com/{name}", risk_class="core"),
        problem=Problem(raw_text="Build a tool", summary="Build a tool"),
        signals=Signals(acceptance_clarity=0, strategic_reuse_value=0),
        risk=Risk(technical_uncertainty=100, scope_uncertainty=100, maintenance_burden=100,
                  legal_or_platform_risk=100, abuse_risk=100, payment_or_dispute_risk=100),
        estimate=Estimate(delivery_probability=0.0, win_probability=0.0, confidence=0),
        decision=Decision(status=status),
    )


def baseline(item: Opportunity) -> PredictionRecord:
    return BaselineEvaluator(clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)).evaluate(item)


def ready(item: Opportunity) -> PredictionRecord:
    record = baseline(item)
    return record.model_copy(update={
        "confidence": 80,
        "economics": record.economics.model_copy(update={
            "confidence": 80, "currency": "USD", "expected_revenue": Decimal("100"),
            "rationale": ("Buyer confirmed price",),
        }),
        "feasibility": record.feasibility.model_copy(update={
            "confidence": 80, "rationale": ("Scope verified",),
        }),
        "risk": record.risk.model_copy(update={
            "confidence": 80, "rationale": ("Payment terms verified",),
        }),
        "rationale": ("Evidence reviewed",),
    })


def test_unknown_baseline_is_investigation_and_preserves_human_gate() -> None:
    item = opportunity()
    before = item.model_dump_json()
    brief = build_brief([item], [baseline(item)])
    assert brief.entries[0].bucket == "needs_investigation"
    assert brief.entries[0].prediction.economics.expected_revenue is None
    rendered = render_markdown(brief)
    assert "unknown money is not zero" in rendered
    assert "GO/NO-GO" in rendered
    assert "Below 60/100 review confidence: overall, economics, feasibility, risk" in rendered
    assert item.model_dump_json() == before
    assert TriageBrief.model_validate_json(brief.model_dump_json()) == brief


def test_known_budget_baseline_still_requires_investigation() -> None:
    item = opportunity("opire").model_copy(update={
        "commercial": Commercial(currency="USD", budget_min=Decimal("250")),
    })
    brief = build_brief([item], [baseline(item)])
    assert brief.entries[0].bucket == "needs_investigation"
    rendered = render_markdown(brief)
    assert "Known budget: min 250, max unknown (USD)" in rendered
    assert "expected revenue: 250 (USD)" in rendered


def test_supported_evidence_is_ready_for_owner_review() -> None:
    item = opportunity()
    brief = build_brief([item], [ready(item)])
    assert brief.entries[0].bucket == "decision_ready"
    assert "owner GO/NO-GO required" in brief.entries[0].reasons[0]


def test_currency_mismatch_is_explained_without_conversion() -> None:
    item = opportunity().model_copy(update={
        "commercial": Commercial(currency="EUR", budget_min=Decimal("90")),
    })
    brief = build_brief([item], [ready(item)])
    assert brief.entries[0].bucket == "needs_investigation"
    rendered = render_markdown(brief)
    assert "90, max unknown (EUR)" in rendered
    assert "100 (USD)" in rendered
    assert "currencies differ" in rendered


@pytest.mark.parametrize("status", ["rejected", "lost", "delivered"])
def test_terminal_items_are_grouped_out_of_active_candidates(status: Status) -> None:
    item = opportunity(status=status)
    brief = build_brief([item], [ready(item)])
    assert brief.entries[0].bucket == "terminal_ignored"
    assert "1 terminal opportunities ignored" in render_markdown(brief)
    assert brief.entries[0].investigation_priority is None
    assert item.decision.status == status


def test_exact_one_to_one_prediction_join() -> None:
    item = opportunity()
    record = baseline(item)
    with pytest.raises(ValueError, match="match included"):
        build_brief([item], [record.model_copy(update={"opportunity_id": uuid4()})])
    with pytest.raises(ValueError, match="match included"):
        build_brief([item], [])
    with pytest.raises(ValueError, match="one prediction"):
        build_brief([item], [record, record])
    with pytest.raises(ValueError, match="Duplicate opportunity"):
        build_brief([item, item], [record])
    with pytest.raises(ValueError, match="match included"):
        build_brief([], [record])


def test_deterministic_order_has_no_cross_currency_money_ranking() -> None:
    first, second = opportunity("a"), opportunity("b")
    small = ready(first)
    large = ready(second).model_copy(update={
        "economics": ready(second).economics.model_copy(update={
            "currency": "EUR", "expected_revenue": Decimal("100000"),
        }),
    })
    forward = build_brief([first, second], [small, large])
    reverse = build_brief([second, first], [large, small])
    assert forward == reverse
    assert render_markdown(forward) == render_markdown(reverse)
    assert [entry.opportunity.id for entry in forward.entries] == [first.id, second.id]
    assert "no monetary ranking or FX" in render_markdown(forward)


@pytest.mark.parametrize("dimension", ["risk", "economics", "feasibility"])
def test_low_dimension_confidence_cannot_be_hidden_by_overall(dimension: str) -> None:
    item = opportunity()
    record = ready(item)
    component = getattr(record, dimension).model_copy(update={"confidence": 0})
    assert build_brief([item], [record.model_copy(update={dimension: component})]) \
        .entries[0].bucket == "needs_investigation"


def test_missing_evidence_and_unknown_currency_require_investigation() -> None:
    item = opportunity()
    record = ready(item).model_copy(update={"rationale": (" ",)})
    assert build_brief([item], [record]).entries[0].bucket == "needs_investigation"
    record = ready(item)
    record = record.model_copy(update={
        "economics": record.economics.model_copy(update={"currency": None}),
    })
    assert build_brief([item], [record]).entries[0].bucket == "needs_investigation"


def test_empty_brief_and_invalid_threshold() -> None:
    assert render_markdown(build_brief([], [])).count("None.") == 3
    for threshold in (0, 101):
        with pytest.raises(ValueError, match="minimum_confidence"):
            build_brief([], [], minimum_confidence=threshold)


def test_source_name_does_not_change_bucket_and_zero_money_is_known() -> None:
    item = opportunity()
    record = ready(item)
    record = record.model_copy(update={
        "economics": record.economics.model_copy(update={"expected_revenue": Decimal("0")}),
    })
    for name in ("hacker_news", "opire", "other"):
        renamed = item.model_copy(update={
            "source": item.source.model_copy(update={"name": name}),
        })
        brief = build_brief([renamed], [record])
        assert brief.entries[0].bucket == "decision_ready"
        assert "expected revenue: 0 (USD)" in render_markdown(brief)


def test_external_text_cannot_inject_markdown_sections() -> None:
    item = opportunity().model_copy(update={
        "problem": Problem(raw_text="text", summary="Tool\n## GO *now*"),
    })
    rendered = render_markdown(build_brief([item], [baseline(item)]))
    assert "### Tool \\#\\# GO \\*now\\*" in rendered
    assert "\n## GO" not in rendered


AS_OF = datetime(2026, 1, 1, tzinfo=UTC)


def candidate(hours: int = 24, /, **signal_values: object) -> Opportunity:
    item = opportunity()
    capabilities = signal_values.pop("capabilities", ())
    assert isinstance(capabilities, tuple)
    return item.model_copy(update={
        "posted_at": AS_OF - timedelta(hours=hours),
        "estimate": item.estimate.model_copy(update={"required_capabilities": capabilities}),
        "signals": item.signals.model_copy(update=signal_values),
    })


def priority(item: Opportunity) -> tuple[str, tuple[str, ...]]:
    result = build_brief([item], [baseline(item)]).entries[0].investigation_priority
    assert result is not None
    return result.level, result.reasons


def test_priority_rules_and_recency_boundaries() -> None:
    assert priority(candidate(capabilities=("Python",), repo_available=True,
                              acceptance_clarity=60))[0] == "high"
    assert priority(candidate(72, capabilities=("Python",), repo_available=True,
                              acceptance_clarity=60))[0] == "high"
    assert priority(candidate(73, repo_available=True, acceptance_clarity=60))[0] == "medium"
    assert priority(candidate(168, repo_available=True))[0] == "medium"
    assert priority(candidate(169, repo_available=True))[0] == "low"
    assert priority(candidate(-1, repo_available=True))[0] == "low"
    for item, rule in ((candidate(repo_available=True, acceptance_clarity=60), "High rule"),
                       (candidate(repo_available=True), "Medium rule"),
                       (candidate(), "Low rule")):
        assert priority(item)[1][0].startswith(rule)
    unknown = candidate(repo_available=True, client_quality=100).model_copy(
        update={"posted_at": None})
    assert priority(unknown)[0] == "low"
    assert "posted_at unknown" in priority(unknown)[1][0]
    experimental = candidate(repo_available=True, client_quality=100)
    experimental = experimental.model_copy(update={
        "source": experimental.source.model_copy(update={"risk_class": "experimental"}),
    })
    assert priority(experimental)[0] == "low"
    assert "experimental" in priority(experimental)[1][0]


@pytest.mark.parametrize("signals, expected", [
    ({}, "low"), ({"repo_available": False}, "low"),
    ({"competition_estimate": None, "client_quality": None}, "low"),
    ({"competition_estimate": 0}, "medium"), ({"client_quality": 60}, "medium"),
    ({"competition_estimate": 31, "client_quality": 59}, "low"),
    ({"repo_available": True}, "medium"),
    ({"acceptance_clarity": 60, "strategic_reuse_value": 60}, "high"),
])
def test_explicit_signal_evidence(signals: dict[str, object], expected: str) -> None:
    assert priority(candidate(**signals))[0] == expected


def test_commercial_evidence_is_presence_not_currency_amount_ranking() -> None:
    items = []
    for name, currency, amount in (("a", "JPY", "1"), ("b", "USD", "999999")):
        item = candidate(repo_available=True)
        item = item.model_copy(update={
            "source": item.source.model_copy(update={"name": name}),
            "commercial": Commercial(pricing_model="fixed", currency=currency,
                                     budget_min=Decimal(amount)),
        })
        assert priority(item)[0] == "high"
        assert any("positive fixed/bounty" in reason for reason in priority(item)[1])
        items.append(item)
    records = [baseline(item) for item in items]
    brief = build_brief(reversed(items), reversed(records))
    assert [entry.opportunity.id for entry in brief.entries] == [item.id for item in items]
    hourly = candidate().model_copy(update={
        "commercial": Commercial(pricing_model="hourly", currency="USD"),
    })
    assert priority(hourly)[0] == "low"
    rendered = render_markdown(build_brief([hourly], [baseline(hourly)]))
    assert "Pricing model: hourly" in rendered
    assert "expected revenue: unknown" in rendered
    for commercial in (Commercial(pricing_model="fixed", budget_min=Decimal("5")),
                       Commercial(pricing_model="fixed", currency="USD",
                                  budget_min=Decimal("0"))):
        assert priority(candidate().model_copy(update={"commercial": commercial}))[0] == "low"


def test_high_volume_cap_preserves_records_and_ready_entries() -> None:
    items = [candidate(hours=index, repo_available=True) for index in range(48)]
    terminal = opportunity(status="rejected")
    review = opportunity("review")
    items.extend([terminal, review])
    records = [baseline(item) for item in items[:-1]] + [ready(review)]
    snapshots = [item.model_dump_json() for item in items]
    prediction_snapshots = [record.model_dump_json() for record in records]
    for cap in (0, 5, 12):
        brief = build_brief(items, records, max_investigation_entries=cap)
        rendered = render_markdown(brief)
        assert rendered.count("- Investigation priority:") == cap
        assert rendered.count("- Opportunity:") == cap + 1
        assert f"{48 - cap} additional investigation candidates omitted" in rendered
        assert str(review.id) in rendered
        assert str(terminal.id) not in rendered
        assert len(brief.entries) == 50
        assert brief == build_brief(reversed(items), reversed(records),
                                    max_investigation_entries=cap)
    assert render_markdown(build_brief(items, records)).count("- Investigation priority:") == 12
    assert snapshots == [item.model_dump_json() for item in items]
    assert prediction_snapshots == [record.model_dump_json() for record in records]


def test_bounded_summary_and_no_full_rationale_or_raw_text() -> None:
    item = candidate().model_copy(update={
        "problem": Problem(raw_text="secret high priority repo fixed USD 1000",
                           summary="*Tool*\n" + "x" * 500),
    })
    record = baseline(item).model_copy(update={"rationale": ("DEEP EVIDENCE " * 500,)})
    rendered = render_markdown(build_brief([item], [record]))
    heading = next(line for line in rendered.splitlines() if line.startswith("### "))
    assert len(heading.removeprefix("### ").replace("\\", "")) == SUMMARY_VISIBLE_LIMIT
    assert "DEEP EVIDENCE" not in rendered
    assert "secret high priority" not in rendered
    assert priority(item)[0] == "low"
    for name in ("market_a", "opire", "anything"):
        renamed = item.model_copy(update={
            "source": item.source.model_copy(update={"name": name}),
        })
        assert priority(renamed) == priority(item)


def test_priority_order_precedes_recency_and_ties_use_source_url_id() -> None:
    high = candidate(72, capabilities=("Python",), repo_available=True, client_quality=60)
    medium = candidate(1, repo_available=True)
    low = candidate(0)
    tied = medium.model_copy(update={"id": uuid4()})
    items = [low, tied, high, medium]
    records = [baseline(item) for item in items]
    brief = build_brief(items, records)
    assert [entry.investigation_priority.level for entry in brief.entries
            if entry.investigation_priority] == ["high", "medium", "medium", "low"]
    assert brief == build_brief(reversed(items), reversed(records))
    assert [str(entry.opportunity.id) for entry in brief.entries[1:3]] == sorted(
        [str(medium.id), str(tied.id)])


def test_policy_validation_and_deliverable_evidence() -> None:
    item = candidate().model_copy(update={
        "problem": Problem(raw_text="irrelevant", summary="Tool", expected_deliverable="API"),
    })
    assert priority(item)[0] == "medium"
    for cap in (-1, True):
        with pytest.raises(ValueError, match="max_investigation_entries"):
            build_brief([], [], max_investigation_entries=cap)
    with pytest.raises(ValueError, match="timezone"):
        build_brief([], [], as_of=datetime(2026, 1, 1))


def test_explicit_reference_clock_and_controlled_source() -> None:
    item = candidate(capabilities=("Python",), repo_available=True, client_quality=60)
    item = item.model_copy(update={
        "source": item.source.model_copy(update={"risk_class": "controlled"}),
    })
    record = baseline(item)
    current = build_brief([item], [record], as_of=AS_OF)
    later = build_brief([item], [record], as_of=AS_OF + timedelta(days=8))
    assert current.entries[0].investigation_priority is not None
    assert current.entries[0].investigation_priority.level == "high"
    assert current.entries[0].bucket == "needs_investigation"
    assert later.entries[0].investigation_priority is not None
    assert later.entries[0].investigation_priority.level == "low"
    assert "age 24.0h" in render_markdown(current)


def test_decision_renderer_does_not_reinterpret_hourly_prediction() -> None:
    item = candidate().model_copy(update={
        "commercial": Commercial(
            pricing_model="hourly", currency="USD", budget_min=Decimal("80"),
        ),
    })
    record = baseline(item)
    assert record.economics.expected_revenue == Decimal("80")
    rendered = render_markdown(build_brief([item], [record]))
    assert "Predicted expected revenue: 80 (USD)." in rendered
    assert "hourly total unsubstantiated" not in rendered


def test_competition_changes_attention_without_changing_predictions() -> None:
    budget = Commercial(pricing_model="fixed", currency="USD", budget_min=Decimal("100"))
    budget_only = candidate().model_copy(update={"commercial": budget})
    low_budget = candidate(competition_estimate=30).model_copy(update={"commercial": budget})
    assert priority(budget_only)[0] == "medium"
    assert priority(low_budget)[0] == "high"
    assert priority(candidate(competition_estimate=70, repo_available=True))[0] == "low"
    assert priority(candidate(competition_estimate=70, repo_available=True,
                              client_quality=60))[0] == "medium"
    # Skill tags are neutral: Gemini triage judges profile fit.
    assert priority(candidate(capabilities=("React",)).model_copy(
        update={"commercial": budget})) == priority(budget_only)
    unknown = priority(candidate())
    assert any("competition_estimate missing: unknown, neutral" in reason for reason in unknown[1])
    assert unknown != priority(candidate(competition_estimate=0))
    items = [budget_only, low_budget] + [candidate() for _ in range(40)]
    records = [baseline(item) for item in items]
    before = [record.model_dump_json() for record in records]
    brief = build_brief(items, records)
    assert {entry.investigation_priority.level for entry in brief.entries
            if entry.investigation_priority} == {"high", "medium", "low"}
    assert brief.entries[0].opportunity.id == low_budget.id
    assert "30 additional investigation candidates omitted" in render_markdown(brief)
    assert before == [record.model_dump_json() for record in records]
    for name in ("market_a", "renamed"):
        changed = low_budget.model_copy(update={
            "source": low_budget.source.model_copy(update={"name": name}),
            "problem": low_budget.problem.model_copy(
                update={"raw_text": "React only high competition"}),
        })
        assert priority(changed) == priority(low_budget)


def test_stage_zero_default_and_later_stage_preserve_snapshots() -> None:
    proposal = candidate(capabilities=("Python",), repo_available=True).model_copy(update={
        "source": Source(name="a", url="https://example.com/a", risk_class="core"),
        "problem": Problem(raw_text="irrelevant", summary="Tool", expected_deliverable="API"),
        "commercial": Commercial(pricing_model="fixed"),
    })
    bounty = proposal.model_copy(update={
        "id": uuid4(),
        "source": Source(name="z", url="https://example.com/z", risk_class="core"),
        "commercial": Commercial(pricing_model="bounty"),
    })
    items = [proposal, bounty]
    records = [baseline(item) for item in items]
    snapshots = [item.model_dump_json() for item in items]
    predictions = [record.model_dump_json() for record in records]
    bootstrap = build_brief(items, records)
    later = build_brief(items, records, policy=DecisionPolicy(stage=1))
    assert DecisionPolicy().stage == bootstrap.policy.stage == 0
    assert [entry.opportunity.id for entry in bootstrap.entries] == [bounty.id, proposal.id]
    assert [entry.opportunity.id for entry in later.entries] == [proposal.id, bounty.id]
    assert "Studio maturity stage: 0" in render_markdown(bootstrap)
    assert "Bounty is a positive proof-first" in render_markdown(bootstrap)
    assert "Proof-first ordering tie" in render_markdown(bootstrap)
    assert "Bounty is a positive proof-first" not in render_markdown(later)
    assert bootstrap == build_brief(reversed(items), reversed(records))
    assert snapshots == [item.model_dump_json() for item in items]
    assert predictions == [record.model_dump_json() for record in records]


def test_stage_zero_proof_first_unknown_recency_stays_low() -> None:
    item = opportunity().model_copy(update={
        "problem": Problem(raw_text="", summary="Small fix",
                           expected_deliverable="Pull request resolving the issue"),
        "commercial": Commercial(pricing_model="bounty", currency="USD",
                                 budget_min=Decimal("50")),
        "signals": Signals(repo_available=True, acceptance_clarity=0, strategic_reuse_value=0),
    })
    result = investigation_priority(item, AS_OF)
    assert result.level == "low"
    assert result.reasons[0].startswith("Low rule: posted_at unknown")
    ordinary = item.model_copy(update={
        "commercial": Commercial(pricing_model="fixed", currency="USD",
                                 budget_min=Decimal("50")),
    })
    assert investigation_priority(ordinary, AS_OF).level == "low"


def test_bounty_cannot_create_readiness() -> None:
    alone = candidate().model_copy(update={"commercial": Commercial(pricing_model="bounty")})
    entry = build_brief([alone], [baseline(alone)]).entries[0]
    assert entry.bucket == "needs_investigation"
    assert entry.investigation_priority is not None
    assert entry.investigation_priority.level == "low"
    assert alone.decision.status == "new"


def test_bootstrap_money_prose_and_source_are_neutral_and_effort_unknown() -> None:
    item = candidate(repo_available=True).model_copy(update={
        "problem": Problem(raw_text="junior small team cheap", summary="Tool",
                           expected_deliverable="API"),
        "commercial": Commercial(pricing_model="bounty", currency="JPY",
                                 budget_min=Decimal("0.01")),
    })
    changed = item.model_copy(update={
        "source": item.source.model_copy(update={"name": "renamed"}),
        "problem": item.problem.model_copy(update={
            "raw_text": "senior large team impossible", "summary": "Another summary",
        }),
        "commercial": Commercial(pricing_model="bounty", currency="EUR",
                                 budget_min=Decimal("999999")),
    })
    assert investigation_priority(item, AS_OF) == investigation_priority(changed, AS_OF)
    assert priority(item) == priority(changed)
    reasons = priority(item)[1]
    assert any("effort and ROI remain unknown" in reason for reason in reasons)
    assert not any("cheap" in reason or "small payout" in reason for reason in reasons)
    record = baseline(item)
    entry = build_brief([item], [record]).entries[0]
    assert entry.prediction.feasibility.human_minutes is None
    assert entry.prediction.economics.expected_value is None
    assert "no monetary ranking or FX" in render_markdown(build_brief([item], [record]))
