"""Preserve individually confirmed opening interpretations in a Seed."""

from typing import TYPE_CHECKING

from ouroboros.core.seed import RequesterAnswer, Seed
from ouroboros.interview_adapters.opening_interpretations import confirmed_opening_events

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewState


def anchor_opening_confirmations(seed: Seed, state: "InterviewState") -> Seed:
    """Keep exact server prompt and host-reported user response, not unselected proposals."""
    additions = tuple(
        RequesterAnswer(question=event.presented_prompt, answer=event.user_response)
        for _item, event in confirmed_opening_events(
            state.proposed_opening, state.opening_decisions
        )
    )
    if not additions:
        return seed
    existing = {(entry.question, entry.answer) for entry in seed.requester_answers}
    return seed.model_copy(
        update={
            "requester_answers": seed.requester_answers
            + tuple(entry for entry in additions if (entry.question, entry.answer) not in existing)
        }
    )
