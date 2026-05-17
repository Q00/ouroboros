"""Regression tests for the prefix-aware completion-signal gate.

The auto driver's ``_feature_acceptance_answer`` echoes the LLM question text
into its answer. When the LLM emits closing-mode phrasing such as "no remaining
ambiguity", the echoed phrase used to trip ``_is_interview_completion_signal``
inside ``InterviewHandler.handle`` and drive the shortfall branch, which in
turn produced a ``Cannot record answer - the previous round is already
answered`` deadlock in ``ooo auto``.

These tests pin the contract: only ``[from-user]`` and prefix-less answers
represent human intent to close. ``[from-auto]`` / ``[from-code]`` /
``[from-research]`` answers MUST return False regardless of their text.
"""

from __future__ import annotations

import pytest

from ouroboros.mcp.tools.authoring_handlers import _is_interview_completion_signal


@pytest.mark.parametrize(
    "answer",
    [
        "done",
        "DONE",
        "  done  ",
        "complete",
        "no ambiguity remains",
        "ready for seed generation",
        "[from-user] done",
        "[from-user][refined] No remaining ambiguity",
    ],
)
def test_human_typed_completion_intent_still_signals(answer: str) -> None:
    """Raw user input and ``[from-user]`` answers preserve the original heuristic."""
    assert _is_interview_completion_signal(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "[from-auto][conservative_default] Acceptance for r3 must cover the requested "
        "behavior; there is no remaining ambiguity to reduce.",
        "[from-auto][assumption] Assume a single local user; ready for seed generation.",
        "[from-auto] done",
        "[from-code] auth uses JWT; no remaining ambiguity to reduce",
        "[from-code][auto-confirmed] Python 3.12, FastAPI (pyproject.toml)",
        "[from-research] Stripe rate limit is 100 rps; ready for seed generation",
    ],
)
def test_driver_and_factual_prefixes_never_signal_completion(answer: str) -> None:
    """Driver and fact-source prefixes never represent user closure intent.

    Regression for the ``ooo auto`` deadlock where the auto driver's
    acceptance template echoed the LLM question into the answer, accidentally
    matching the human-intent heuristic and shunting the round into the
    shortfall branch (state.rounds left with an unanswered placeholder).
    """
    assert _is_interview_completion_signal(answer) is False


def test_none_answer_returns_false() -> None:
    """A None answer must short-circuit before any string slicing."""
    assert _is_interview_completion_signal(None) is False


def test_empty_string_returns_false() -> None:
    """An empty string has no completion intent."""
    assert _is_interview_completion_signal("") is False


def test_leading_whitespace_does_not_bypass_prefix_guard() -> None:
    """Whitespace before the prefix must not allow ``[from-auto]`` to trip the heuristic."""
    answer = "   [from-auto] no remaining ambiguity"
    assert _is_interview_completion_signal(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "[From-Auto] no remaining ambiguity",
        "[FROM-AUTO] done",
        "[From-Code] done",
        "[FROM-RESEARCH] ready for seed generation",
    ],
)
def test_prefix_guard_is_case_insensitive(answer: str) -> None:
    """Prefix case must not weaken the guard.

    If a future caller (or typo) emits ``[From-Auto]`` instead of the canonical
    lowercase form, the guard must still block — otherwise the same shortfall
    trigger reopens under a trivial case variation.
    """
    assert _is_interview_completion_signal(answer) is False


def test_negation_still_blocks_human_completion_intent() -> None:
    """Negations applied to a [from-user] prefix must keep completion=False.

    Regression for the heuristic itself: the prefix guard widens the surface
    that passes through to the heuristic; ensure the negation rule still wins.
    """
    assert _is_interview_completion_signal("[from-user] not done") is False
    assert _is_interview_completion_signal("[from-user] don't close") is False
