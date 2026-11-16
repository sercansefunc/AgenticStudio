from typing import get_args

import pytest
from test_domain import minimal_data

from agenticstudio.domain import Decision, Opportunity, Status
from agenticstudio.lifecycle import (
    InvalidTransitionError,
    allowed_transitions,
    can_transition,
    transition,
)

EXPECTED: dict[Status, frozenset[Status]] = {
    "new": frozenset({"rejected", "proposal_sent"}),
    "shortlisted": frozenset({"rejected", "proposal_sent", "lost"}),
    "investigating": frozenset({"rejected", "proposal_sent", "lost"}),
    "pursuing": frozenset({"rejected", "proposal_sent", "lost"}),
    "proposal_sent": frozenset({"won", "lost"}),
    "won": frozenset({"delivered"}),
    "rejected": frozenset(), "lost": frozenset(), "delivered": frozenset(),
}


@pytest.mark.parametrize("current", get_args(Status))
@pytest.mark.parametrize("target", get_args(Status))
def test_entire_transition_policy(current: Status, target: Status) -> None:
    original = Opportunity.model_validate({
        **minimal_data(), "decision": Decision(status=current, reasons=("reason",)),
    })
    snapshot = original.model_dump_json()
    assert allowed_transitions(current) == EXPECTED[current]
    assert can_transition(current, target) == (target in EXPECTED[current])
    if target in EXPECTED[current]:
        result = transition(original, target)
        assert result is not original
        assert result.decision is not original.decision
        assert result.decision.status == target
        assert result.decision.reasons == original.decision.reasons
        assert result.id == original.id
        assert result.model_dump(exclude={"decision"}) == original.model_dump(exclude={"decision"})
    else:
        with pytest.raises(InvalidTransitionError):
            transition(original, target)
    assert original.model_dump_json() == snapshot
