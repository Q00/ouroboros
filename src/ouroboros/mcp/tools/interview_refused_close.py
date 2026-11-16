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
resolves to the question the server issued. A session with opening
interpretations rejects any echo that differs from the issued question, so the
refusal itself is recognised there by :func:`echo_is_refused_close`.
"""

from typing import TYPE_CHECKING, Any

from ouroboros.mcp.tools.interview_opening_decisions import render_opening

if TYPE_CHECKING:
    from ouroboros.bigbang.interview import InterviewState


#: How the two refused-close explanations begin: the ambiguity gate and the
#: stability check in ``authoring_handlers``.
REFUSED_CLOSE_OPENERS = ("Cannot complete yet — ", "Ambiguity looks low (score=")


def _pending_block(state: "InterviewState | None") -> str | None:
    """Render the unanswered round as resume does, or None when none is pending."""
    if state is None or not state.rounds or state.rounds[-1].user_response is not None:
        return None
    pending = state.rounds[-1]
    presentation = pending.first_question_presentation
    shown = presentation.render() if presentation else pending.question.strip()
    return render_opening(state, pending.question, shown)


def with_pending_question(text: str, state: "InterviewState | None") -> str:
    """Return refusal *text* followed by the round still awaiting an answer.

    The question is rendered as resume renders it, minus the ambiguity prefix
    the refusal already states. *text* is returned unchanged when no round is
    pending.
    """
    block = _pending_block(state)
    return text if block is None else f"{text}\n\n{block}"


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


def echo_is_refused_close(echoed: Any, state: "InterviewState") -> bool:
    """Return whether *echoed* is this server's refused-close reply for the pending round.

    That reply is one refusal explanation (a single paragraph starting with a
    :data:`REFUSED_CLOSE_OPENERS` entry) followed by the pending round exactly
    as :func:`with_pending_question` renders it, opening interpretations
    included. The match is narrower than :func:`echo_carries_pending` because
    the opening guard otherwise rejects every echo that differs from the issued
    question: the rendered opening alone, another question, or other text in
    front of the rendered round still fail there.
    """
    block = _pending_block(state)
    if block is None or not isinstance(echoed, str):
        return False
    text, tail = echoed.strip(), f"\n\n{block.strip()}"
    head = text[: -len(tail)]
    return text.endswith(tail) and head.startswith(REFUSED_CLOSE_OPENERS) and "\n\n" not in head


def echo_differs_from_pending(echoed: Any, issued: str, state: "InterviewState") -> bool:
    """Return whether *echoed* names a question other than the pending round.

    No echo, the *issued* question itself, or this server's refused-close reply
    for that round does not differ. The opening guard rejects every other echo.
    """
    return echoed is not None and echoed != issued and not echo_is_refused_close(echoed, state)


__all__ = [
    "REFUSED_CLOSE_OPENERS",
    "echo_carries_pending",
    "echo_differs_from_pending",
    "echo_is_refused_close",
    "with_pending_question",
]
