"""Transcript contracts for the optional inline interview-material affordance."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


_SKILL_PATHS = (
    Path("skills/interview/SKILL.md"),
    Path(".claude-plugin/skills/interview/SKILL.md"),
)
_FIRST_QUESTION = "Which outcome matters most for the first release?"
_SECOND_QUESTION = "What observable result proves that outcome works?"
_INVITATION = (
    "If you have notes, documents, or a prior discussion, paste them with "
    "your answer — they reduce the number of questions."
)


@dataclass(frozen=True)
class Turn:
    role: str
    text: str


def _ignored_materials_transcript(*, show_invitation: bool) -> tuple[Turn, ...]:
    """Return the same vanilla exchange with or without the inline affordance."""
    first_surface = _FIRST_QUESTION
    if show_invitation:
        first_surface = f"{first_surface}\n\n{_INVITATION}"
    return (
        Turn("assistant", first_surface),
        Turn("user", "A working command-line prototype."),
        Turn("assistant", _SECOND_QUESTION),
    )


def _material_submission_transcript() -> tuple[Turn, ...]:
    """Material synthesis shares the next normal question turn."""
    return (
        Turn("assistant", f"{_FIRST_QUESTION}\n\n{_INVITATION}"),
        Turn(
            "user",
            "A working command-line prototype.\n\n"
            "Meeting notes: MVP must run offline; sync was postponed.",
        ),
        Turn(
            "assistant",
            "Understanding draft\n"
            "- confirmed fact — The supplied meeting notes state that the MVP "
            "must run offline. (source: pasted meeting notes)\n"
            "- inferred assumption — Network sync is outside the MVP. "
            "(unconfirmed; source: pasted meeting notes)\n\n"
            f"{_SECOND_QUESTION}",
        ),
    )


@pytest.mark.parametrize("skill_path", _SKILL_PATHS)
def test_ignored_material_invitation_adds_zero_turns_and_preserves_vanilla_flow(
    skill_path: Path,
) -> None:
    """Ignoring the inline sentence cannot create a request, gate, or MCP round."""
    contract = skill_path.read_text(encoding="utf-8")
    assert "Inline materials contract (zero-turn invariant)" in contract
    assert "never send it through a separate `ask_user` call" in contract
    assert "Ignoring the invitation is a strict no-op" in contract
    assert "Do not add an empty-material marker" in contract

    vanilla = _ignored_materials_transcript(show_invitation=False)
    ignored = _ignored_materials_transcript(show_invitation=True)

    assert [turn.role for turn in ignored] == [turn.role for turn in vanilla]
    assert sum(turn.role == "user" for turn in ignored) == 1
    assert ignored[0].text.removesuffix(f"\n\n{_INVITATION}") == vanilla[0].text
    assert ignored[1:] == vanilla[1:]


@pytest.mark.parametrize("skill_path", _SKILL_PATHS)
def test_submitted_material_enters_next_understanding_draft_without_extra_turn(
    skill_path: Path,
) -> None:
    """The next existing assistant turn carries both ledger and normal question."""
    contract = skill_path.read_text(encoding="utf-8")
    compact_contract = " ".join(contract.split())
    assert "append a concise material digest to the same MCP `answer` payload" in compact_contract
    assert "`confirmed fact`" in contract
    assert "`inferred assumption`" in contract
    assert "next existing assistant turn" in compact_contract
    assert "when #1654 is absent" in contract

    transcript = _material_submission_transcript()

    assert [turn.role for turn in transcript] == ["assistant", "user", "assistant"]
    assert sum(turn.role == "user" for turn in transcript) == 1
    next_surface = transcript[-1].text
    assert "confirmed fact" in next_surface
    assert "must run offline" in next_surface
    assert "inferred assumption" in next_surface
    assert "Network sync is outside the MVP" in next_surface
    assert next_surface.endswith(_SECOND_QUESTION)
