"""Host-reported opening confirmations preserved independently of model output."""

from dataclasses import dataclass, replace
import re
from typing import TYPE_CHECKING

from ouroboros.interview_adapters.opening_interpretations import (
    InterpretationProposal,
    confirmed_opening_events,
)

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewState
    from ouroboros.bigbang.pm_seed import PMSeed


@dataclass(frozen=True, slots=True)
class PMOpeningConfirmation:
    """Exact confirmation exchange reported by the host, not verified human activity."""

    event_id: str
    item_id: str
    question: str
    answer: str
    source: str = "host_reported_explicit_user_decision"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.event_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", self.event_id)
            or not isinstance(self.item_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", self.item_id)
            or not isinstance(self.question, str)
            or not self.question.startswith(
                "To explicitly confirm this interpretation as your intent, reply exactly: YES\n"
            )
            or len(self.question) > 2000
            or self.answer != "YES"
            or self.source != "host_reported_explicit_user_decision"
        ):
            raise ValueError("invalid host-reported opening confirmation")
        interpretation = self.question.partition("\n")[2]
        parsed = InterpretationProposal(
            interpretation=interpretation, evidence="x", uncertainty="x"
        )
        if parsed.interpretation != interpretation:
            raise ValueError("invalid host-reported opening confirmation")


def anchor_pm_opening(seed: "PMSeed", state: "InterviewState") -> "PMSeed":
    confirmations = tuple(
        PMOpeningConfirmation(
            event_id=event.event_id,
            item_id=item.item_id,
            question=event.presented_prompt,
            answer=event.user_response,
            source=event.source,
        )
        for item, event in confirmed_opening_events(state.proposed_opening, state.opening_decisions)
    )
    return replace(
        seed,
        opening_confirmations=confirmations,
        opening_state_revision=state.persistence_revision if confirmations else "",
    )


def opening_confirmation_markdown(
    confirmations: tuple[PMOpeningConfirmation, ...], revision: str
) -> str:
    if not confirmations:
        return ""
    lines = [
        "## Unverified opening confirmation records",
        "",
        "These records are a snapshot and may have changed since generation. They claim "
        "to be host-reported; the server cannot verify their source or whether the person "
        "saw or answered them.",
        "",
        f"Source state revision: {revision or 'unavailable'}",
        "",
    ]
    for confirmation in confirmations:
        lines.extend(
            [
                "Question:",
                "",
                *(f"    {line}" for line in confirmation.question.splitlines()),
                "",
                "Answer:",
                "",
                *(f"    {line}" for line in confirmation.answer.splitlines()),
                "",
            ]
        )
    return "\n".join(lines)
