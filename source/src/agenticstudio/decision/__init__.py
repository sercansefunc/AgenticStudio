"""Explainable triage for a human gate; never changes opportunity lifecycle."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field

from agenticstudio.domain import DomainModel, Opportunity, Score
from agenticstudio.evaluation import PredictionRecord
from agenticstudio.execution_profile import (
    HIGH_COMPETITION as HIGH_COMPETITION,
)
from agenticstudio.execution_profile import (
    LOW_COMPETITION as LOW_COMPETITION,
)
from agenticstudio.execution_profile import competition_evidence

Bucket = Literal["needs_investigation", "decision_ready", "terminal_ignored"]
PriorityLevel = Literal["high", "medium", "low"]
HIGH_RECENCY = timedelta(hours=72)
MEDIUM_RECENCY = timedelta(days=7)
MEANINGFUL_SIGNAL = 60
HIGH_POSITIVE_DIMENSIONS = 2
MEDIUM_POSITIVE_DIMENSIONS = 1
DEFAULT_MAX_INVESTIGATION_ENTRIES = 12
SUMMARY_VISIBLE_LIMIT = 200


class DecisionPolicy(DomainModel):
    """Explicit maturity config; advancement requires a separate owner/outcome decision.

    Stage 0 favors bootstrap acquisition. Stage 1 retains stage-agnostic triage;
    it does not assert that the studio has earned advancement.
    """

    stage: Literal[0, 1] = 0


DEFAULT_DECISION_POLICY = DecisionPolicy()


def _proof_first(item: Opportunity) -> bool:
    """A mechanism alone is insufficient to win the bootstrap ordering tie."""
    return (item.commercial.pricing_model == "bounty"
            and bool(item.problem.expected_deliverable
                     and item.problem.expected_deliverable.strip())
            and item.signals.repo_available is True)


class InvestigationPriority(DomainModel):
    """Ordinal investigation attention, never a prediction or human decision."""

    level: PriorityLevel
    reasons: tuple[str, ...]


def investigation_priority(
    item: Opportunity, as_of: datetime, *, policy: DecisionPolicy = DEFAULT_DECISION_POLICY,
) -> InvestigationPriority:
    """Count explicit positive dimensions without scoring money or missing evidence."""
    positive: list[str] = []
    commercial, signals = item.commercial, item.signals
    neutral: list[str] = [competition_evidence(signals.competition_estimate)]
    if policy.stage == 0:
        neutral.append("Stage 0: proof-first/bounded acquisition; owner GO/NO-GO still required.")
        if commercial.pricing_model == "bounty":
            neutral.append("Bounty is a positive proof-first acquisition mechanism, not delivery "
                           "fit, ROI or review readiness.")
            if _proof_first(item):
                neutral.append("Proof-first ordering tie: bounty with explicit deliverable "
                               "and repository evidence.")
        if item.estimate.human_minutes is None:
            neutral.append("estimate.human_minutes missing: effort and ROI remain unknown.")
    high_competition = (signals.competition_estimate is not None
                        and signals.competition_estimate >= HIGH_COMPETITION)
    if signals.competition_estimate is None:
        neutral.append("signals.competition_estimate missing: unknown, neutral.")
    elif high_competition:
        neutral.append(f"Explicit signals.competition_estimate >= {HIGH_COMPETITION}/100.")
    if (commercial.pricing_model in {"fixed", "bounty"}
            and commercial.currency and commercial.currency.strip()
            and commercial.budget_min is not None and commercial.budget_min > 0):
        positive.append("Known positive fixed/bounty lower bound with currency.")
    if item.problem.expected_deliverable and item.problem.expected_deliverable.strip():
        positive.append("Expected deliverable is present.")
    if signals.repo_available is True:
        positive.append("Repository explicitly available.")
    for label, value in (("Acceptance clarity", signals.acceptance_clarity),
                         ("Strategic reuse", signals.strategic_reuse_value),
                         ("Client quality", signals.client_quality)):
        if value is not None and value >= MEANINGFUL_SIGNAL:
            positive.append(f"{label} >= {MEANINGFUL_SIGNAL}/100.")
    if (signals.competition_estimate is not None
            and signals.competition_estimate <= LOW_COMPETITION):
        positive.append(f"Explicit competition <= {LOW_COMPETITION}/100.")
    age = as_of - item.posted_at if item.posted_at is not None else None
    if item.source.risk_class == "experimental":
        rule = "Low rule: experimental source."
    elif age is None:
        rule = "Low rule: posted_at unknown; recency unsubstantiated."
    elif age is not None and age < timedelta(0):
        rule = "Low rule: future posted_at; recency unsubstantiated."
    elif age is not None and age > MEDIUM_RECENCY:
        rule = "Low rule: older than 7 days."
    elif high_competition and len(positive) < HIGH_POSITIVE_DIMENSIONS:
        rule = "Low rule: clearly high competition without countervailing positive dimensions."
    elif (age is not None and age <= HIGH_RECENCY and not high_competition
          and len(positive) >= HIGH_POSITIVE_DIMENSIONS):
        return InvestigationPriority(level="high", reasons=(
            "High rule: age <=72h, core/controlled source, "
            f">={HIGH_POSITIVE_DIMENSIONS} positive dimensions.",
            *positive, *neutral,
        ))
    elif len(positive) >= MEDIUM_POSITIVE_DIMENSIONS:
        return InvestigationPriority(level="medium", reasons=(
            ("Medium rule: clearly high competition limits countervailing evidence to medium."
             if high_competition else
             "Medium rule: age <=7d, core/controlled source, >=1 positive dimension."),
            *positive, *neutral,
        ))
    else:
        rule = "Low rule: no explicit positive evidence dimensions."
    return InvestigationPriority(level="low", reasons=(rule, *positive, *neutral))


TERMINAL_STATUSES = frozenset({"rejected", "lost", "delivered"})


class BriefEntry(DomainModel):
    """Immutable evidence snapshots retain separate budget/prediction currencies."""

    opportunity: Opportunity
    prediction: PredictionRecord
    bucket: Bucket
    reasons: tuple[str, ...]
    investigation_priority: InvestigationPriority | None = None


class TriageBrief(DomainModel):
    entries: tuple[BriefEntry, ...]
    minimum_confidence: Score
    as_of: AwareDatetime
    max_investigation_entries: int = Field(default=DEFAULT_MAX_INVESTIGATION_ENTRIES, ge=0)
    policy: DecisionPolicy = DEFAULT_DECISION_POLICY
    human_gate: str = "Owner must explicitly decide GO/NO-GO; triage changes no lifecycle status."
    ordering: str = (
        "Investigation: priority tier, newest known posting, source, URL, ID; "
        "no monetary ranking or FX."
    )


def build_brief(
    opportunities: Iterable[Opportunity],
    predictions: Iterable[PredictionRecord],
    *,
    minimum_confidence: Score = 60,
    as_of: datetime | None = None,
    max_investigation_entries: int = DEFAULT_MAX_INVESTIGATION_ENTRIES,
    policy: DecisionPolicy = DEFAULT_DECISION_POLICY,
) -> TriageBrief:
    """Require an exact one-to-one join, including terminal items.

    Readiness requires all confidence dimensions to meet the visible threshold,
    substantive rationale in every dimension, and revenue with a stated currency.
    These are review prerequisites, not calibrated GO/NO-GO probabilities.
    Recency uses explicit ``as_of`` or the latest evaluation timestamp, never a
    hidden wall clock. The display cap leaves all joined entries in the brief.
    """
    if isinstance(minimum_confidence, bool) or not 1 <= minimum_confidence <= 100:
        raise ValueError("minimum_confidence must be between 1 and 100")
    if (isinstance(max_investigation_entries, bool)
            or not isinstance(max_investigation_entries, int) or max_investigation_entries < 0):
        raise ValueError("max_investigation_entries must be a nonnegative integer")
    items = tuple(opportunities)
    by_id = {item.id: item for item in items}
    if len(by_id) != len(items):
        raise ValueError("Duplicate opportunity IDs")
    records: dict[object, PredictionRecord] = {}
    for record in predictions:
        if record.opportunity_id in records:
            raise ValueError("Exactly one prediction per opportunity is required")
        records[record.opportunity_id] = record
    if set(records) != set(by_id):
        raise ValueError("Prediction opportunity IDs must match included opportunities exactly")

    # Latest evaluation is a reproducible snapshot clock; callers may override it.
    reference = as_of if as_of is not None else max(
        (record.evaluated_at for record in records.values()),
        default=datetime(1970, 1, 1, tzinfo=UTC),
    )
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    entries: list[BriefEntry] = []
    for item in items:
        record = records[item.id]
        reasons: list[str] = []
        if item.decision.status in TERMINAL_STATUSES:
            bucket: Bucket = "terminal_ignored"
            reasons.append(f"Lifecycle {item.decision.status}: excluded from active candidates.")
        else:
            dimensions = (
                ("overall", record.confidence),
                ("economics", record.economics.confidence),
                ("feasibility", record.feasibility.confidence),
                ("risk", record.risk.confidence),
            )
            for name, confidence in dimensions:
                if confidence < minimum_confidence:
                    reasons.append(
                        f"{name.capitalize()} confidence {confidence}/100 is below "
                        f"the review threshold {minimum_confidence}/100."
                    )
            for name, rationale in (
                ("overall", record.rationale), ("economics", record.economics.rationale),
                ("feasibility", record.feasibility.rationale), ("risk", record.risk.rationale),
            ):
                if not any(text.strip() for text in rationale):
                    reasons.append(f"Missing {name} evidence rationale.")
            if not item.problem.summary.strip():
                reasons.append("Problem summary is missing.")
            if record.economics.expected_revenue is None:
                reasons.append("Expected revenue is unknown; unknown money is not zero.")
            if not record.economics.currency or not record.economics.currency.strip():
                reasons.append("Prediction currency is unknown; no FX is inferred.")
            if (item.commercial.currency and record.economics.currency
                    and item.commercial.currency != record.economics.currency):
                reasons.append("Budget and prediction currencies differ; no comparison or FX.")
            bucket = "needs_investigation" if reasons else "decision_ready"
            if not reasons:
                reasons.append(
                    "Confidence and evidence meet review prerequisites; owner GO/NO-GO required."
                )
        entries.append(BriefEntry(
            opportunity=item, prediction=record, bucket=bucket, reasons=tuple(reasons),
            investigation_priority=(investigation_priority(item, reference, policy=policy)
                                    if bucket == "needs_investigation" else None),
        ))
    bucket_order = {"needs_investigation": 0, "decision_ready": 1, "terminal_ignored": 2}
    priority_order = {"high": 0, "medium": 1, "low": 2}
    entries.sort(key=lambda entry: (
        bucket_order[entry.bucket],
        priority_order[entry.investigation_priority.level] if entry.investigation_priority else 0,
        not (entry.opportunity.signals.competition_estimate is not None
             and entry.opportunity.signals.competition_estimate <= LOW_COMPETITION)
        if policy.stage == 0 and entry.bucket == "needs_investigation" else False,
        not _proof_first(entry.opportunity)
        if policy.stage == 0 and entry.bucket == "needs_investigation" else False,
        entry.opportunity.posted_at is None if entry.bucket == "needs_investigation" else False,
        -entry.opportunity.posted_at.timestamp()
        if entry.bucket == "needs_investigation" and entry.opportunity.posted_at else 0,
        entry.opportunity.source.name,
        entry.opportunity.source.url, str(entry.opportunity.id),
    ))
    return TriageBrief(entries=tuple(entries), minimum_confidence=minimum_confidence,
                       as_of=reference, max_investigation_entries=max_investigation_entries,
                       policy=policy, ordering=(
                           "Investigation: priority tier, observed LOW_COMPETITION, "
                           "proof-first bounty with deliverable/repository, newest known posting, "
                           "source, URL, ID; no monetary ranking or FX."
                           if policy.stage == 0 else (
                               "Investigation: priority tier, newest known posting, "
                               "source, URL, ID; no monetary ranking or FX."
                           )
                       ))


def _text(value: str, limit: int | None = None) -> str:
    """Render external text as one escaped Markdown line."""
    value = " ".join(value.split())
    if limit is not None and len(value) > limit:
        value = value[:limit - 1] + "…"
    for char in ("\\", "`", "*", "_", "{", "}", "[", "]", "<", ">", "#", "|"):
        value = value.replace(char, "\\" + char)
    return value


def _readiness_gaps(entry: BriefEntry, threshold: int) -> tuple[str, ...]:
    """Compact readiness diagnostics; detailed triage reasons remain in the snapshot."""
    record = entry.prediction
    low = [name for name, confidence in (
        ("overall", record.confidence), ("economics", record.economics.confidence),
        ("feasibility", record.feasibility.confidence), ("risk", record.risk.confidence),
    ) if confidence < threshold]
    gaps = [f"Below {threshold}/100 review confidence: {', '.join(low)}."] if low else []
    missing = [name for name, rationale in (
        ("overall", record.rationale), ("economics", record.economics.rationale),
        ("feasibility", record.feasibility.rationale), ("risk", record.risk.rationale),
    ) if not any(text.strip() for text in rationale)]
    if missing:
        gaps.append(f"Missing evidence rationale: {', '.join(missing)}.")
    gaps.extend(reason for reason in entry.reasons
                if " is below the review threshold " not in reason
                and not (reason.startswith("Missing ") and reason.endswith("evidence rationale.")))
    return tuple(gaps)


def render_markdown(brief: TriageBrief) -> str:
    """A deterministic daily brief, with no clock or provider calls."""
    lines = ["# Opportunity triage brief", "", brief.human_gate,
             f"Studio maturity stage: {brief.policy.stage} "
             "(explicit config; no automatic advancement).",
             f"Review confidence threshold: {brief.minimum_confidence}/100.", brief.ordering]
    for bucket in ("needs_investigation", "decision_ready", "terminal_ignored"):
        lines.extend(["", f"## {bucket.replace('_', ' ').capitalize()}", ""])
        selected = [entry for entry in brief.entries if entry.bucket == bucket]
        if bucket == "terminal_ignored" and selected:
            lines.append(f"{len(selected)} terminal opportunities ignored.")
            continue
        if bucket == "needs_investigation":
            omitted = max(0, len(selected) - brief.max_investigation_entries)
            selected = selected[:brief.max_investigation_entries]
            lines.extend([f"{omitted} additional investigation candidates omitted from display.",
                          ""])
        if not selected:
            lines.append("None.")
        for entry in selected:
            item, prediction = entry.opportunity, entry.prediction
            commercial, economics = item.commercial, prediction.economics
            budget_currency = _text(commercial.currency or "currency unknown")
            revenue_currency = _text(economics.currency or "currency unknown")
            minimum = str(commercial.budget_min) if commercial.budget_min is not None else "unknown"
            maximum = str(commercial.budget_max) if commercial.budget_max is not None else "unknown"
            revenue = str(economics.expected_revenue) if economics.expected_revenue is not None \
                else "unknown"
            lines.extend([
                f"### {_text(item.problem.summary, SUMMARY_VISIBLE_LIMIT)}", "",
                f"- Opportunity: {item.id}",
                f"- Source: {_text(item.source.name)}; URL: {_text(item.source.url)}",
                f"- Lifecycle: {item.decision.status}",
                f"- Pricing model: {commercial.pricing_model}",
                f"- Known budget: min {minimum}, max {maximum} ({budget_currency}).",
                f"- Predicted expected revenue: {revenue} ({revenue_currency}).",
                f"- Confidence /100: overall {prediction.confidence}, "
                f"economics {economics.confidence}, feasibility "
                f"{prediction.feasibility.confidence}, risk {prediction.risk.confidence}.",
            ])
            if entry.investigation_priority is not None:
                priority = entry.investigation_priority
                lines.append(f"- Investigation priority: {priority.level}; "
                             + " ".join(priority.reasons))
            if item.posted_at is not None:
                age_hours = (brief.as_of - item.posted_at).total_seconds() / 3600
                lines.append(f"- Posted: {item.posted_at.isoformat()}; "
                             f"age {age_hours:.1f}h as of {brief.as_of.isoformat()}.")
            else:
                lines.append("- Posted: unknown; age unknown.")
            # Readiness gaps are bounded policy text; full evaluator evidence stays in prediction.
            gaps = _readiness_gaps(entry, brief.minimum_confidence)
            if gaps:
                lines.append("- Review prerequisites: " + "; ".join(_text(text) for text in gaps))
            else:
                lines.append("- Review prerequisites: met; owner GO/NO-GO required.")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
