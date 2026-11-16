"""Hard exclusions applied before the capped investigation list; never changes lifecycle."""

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal

from agenticstudio.domain import Opportunity
from agenticstudio.evaluation.triage import raw_bids
from agenticstudio.execution_profile import HIGH_COMPETITION

# Bidding marketplaces with raw bid/proposal counts.
FILTERED_SOURCES = frozenset({"market_a", "market_b"})
# Owner 2026-10-17: a zero-review account cannot win crowded leads; only <10 bids are shown.
MAX_BIDS = 10
MAX_AGE = timedelta(days=7)
# Original currency only (no FX): unknown currencies are never excluded on budget.
MIN_FIXED_BUDGET_MAX = {"USD": Decimal(10), "EUR": Decimal(10), "GBP": Decimal(8)}


def exclusion_reasons(item: Opportunity, as_of: datetime) -> tuple[str, ...]:
    """Reasons to drop a client job from the owner list; unknown evidence never excludes."""
    if item.source.name not in FILTERED_SOURCES:
        return ()
    reasons: list[str] = []
    if item.posted_at is not None and as_of - item.posted_at > MAX_AGE:
        reasons.append("older than 7 days")
    bids, competition = raw_bids(item)[0], item.signals.competition_estimate
    if bids is not None:
        if bids >= MAX_BIDS:
            reasons.append(f"high competition ({bids} bids)")
    elif competition is not None and competition >= HIGH_COMPETITION:
        reasons.append(f"high competition ({competition}/100)")
    commercial = item.commercial
    floor = MIN_FIXED_BUDGET_MAX.get(commercial.currency or "")
    if (commercial.pricing_model == "fixed" and floor is not None
            and commercial.budget_max is not None and commercial.budget_max < floor):
        reasons.append(f"fixed budget ceiling below {floor} {commercial.currency}")
    return tuple(reasons)


def render_prefilter(excluded: Iterable[tuple[Opportunity, tuple[str, ...]]]) -> str:
    counts = Counter(reason.split(" (")[0] for _, reasons in excluded for reason in reasons)
    lines = ["## Pre-filter", ""]
    if not counts:
        return "\n".join([*lines, "No opportunities excluded.", ""])
    lines.append("Excluded before the investigation list; stored state is unchanged.")
    lines.extend(f"- {reason}: {count}" for reason, count in sorted(counts.items()))
    return "\n".join([*lines, ""])
