"""Re-present the pending question when an explicit close is refused.

An explicit close ("done", or a ``[from-user][refined][closure]`` answer) that
the closure gate refuses keeps the pending round. The refusal is an explanation,
not a question, yet hosts take a response's text as the next question and echo
it back as ``last_question``; the console does so through
``HandlerInterviewBackend``. For an unanswered round without a structured
presentation the answer path lets ``last_question`` replace the stored question
(placeholder repair), so the next answer was recorded against the refusal and
the issued question dropped out of the transcript.

The refusal therefore ends with the pending question, and an echo of that shape
resolves to the question the server issued.
"""

from typing import TYPE_CHECKING, Any

from ouroboros.mcp.tools.interview_opening_decisions import render_opening

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewState


def with_pending_question(text: str, state: "InterviewState | None") -> str:
    """Return refusal *text* followed by the round still awaiting an answer.

    The question is rendered as resume renders it, minus the ambiguity prefix
    the refusal already states. *text* is returned unchanged when no round is
    pending.
    """
    if state is None or not state.rounds or state.rounds[-1].user_response is not None:
        return text
    pending = state.rounds[-1]
    presentation = pending.first_question_presentation
    shown = presentation.render() if presentation else pending.question.strip()
    return f"{text}\n\n{render_opening(state, pending.question, shown)}"


def echo_carries_pending(echoed: Any, issued: str) -> bool:
    """Return whether *echoed* re-presents the *issued* pending question.

    True when the issued question closes the echo as its own paragraph after
    other text, which is the shape :func:`with_pending_question` writes. As with
    ``echo_carries_dispatch``, recognition licenses only a preference for the
    question the server already holds for the unanswered round; nothing is cut
    from the caller's text. A false positive keeps the issued question, and a
    repair that still ends with the text it would replace is no repair. A
    question alone, or one carrying the ambiguity prefix, is not this shape and
    keeps its existing handling.
    """
    question = issued.strip()
    return isinstance(echoed, str) and bool(question) and echoed.strip().endswith(f"\n\n{question}")


__all__ = ["echo_carries_pending", "with_pending_question"]
