"""Explicit lifecycle policy; transitions return a new validated record."""

from collections.abc import Mapping
from types import MappingProxyType

from agenticstudio.domain import Decision, Opportunity, Status

_TRANSITIONS: Mapping[Status, frozenset[Status]] = MappingProxyType({
    # Owner tracks only what he actually bid on; the middle states are legacy rows only.
    "new": frozenset({"rejected", "proposal_sent"}),
    "shortlisted": frozenset({"rejected", "proposal_sent", "lost"}),
    "investigating": frozenset({"rejected", "proposal_sent", "lost"}),
    "pursuing": frozenset({"rejected", "proposal_sent", "lost"}),
    "proposal_sent": frozenset({"won", "lost"}),
    "won": frozenset({"delivered"}),
    "rejected": frozenset(),
    "lost": frozenset(),
    "delivered": frozenset(),
})


class InvalidTransitionError(ValueError):
    """The requested status change is prohibited by the domain policy."""


def allowed_transitions(status: Status) -> frozenset[Status]:
    return _TRANSITIONS[status]


def can_transition(current: Status, target: Status) -> bool:
    return target in allowed_transitions(current)


def transition(opportunity: Opportunity, target: Status) -> Opportunity:
    current = opportunity.decision.status
    if not can_transition(current, target):
        raise InvalidTransitionError(f"Cannot transition from {current!r} to {target!r}")
    data = dict(opportunity)
    data["decision"] = Decision(status=target, reasons=opportunity.decision.reasons)
    return Opportunity.model_validate(data)
