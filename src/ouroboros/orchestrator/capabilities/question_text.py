"""How two interview questions are decided to be the same question.

One definition, in one place, because two would drift and the drift would be
invisible: the fan-out digests this to bind an answer to the question it was
asked for, and the echo check compares against it to tell a faithful echo from
a rewritten one. If they disagreed, one side would bind what the other had
already let through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import unicodedata

if TYPE_CHECKING:
    from ouroboros.bigbang.ambiguity import AmbiguityScore


def normalize_question_text(question: str) -> str:
    """Return the form in which two questions are the same question.

    NFKC folds width and compatibility variants; collapsing runs of whitespace
    folds the reflowing text picks up crossing a host. What survives is the
    words, which is what sameness has to mean when the two copies travelled
    different routes.
    """
    return " ".join(unicodedata.normalize("NFKC", question).strip().split())


def format_question_with_ambiguity(question: str, score: AmbiguityScore | None) -> str:
    """Attach the current ambiguity score to a question for display.

    The display form, beside the identity form above, because they are two
    renderings of one question and the difference between them is load-bearing:
    a host echoes back what it saw, and comparing that against what was stored
    read a faithful echo as a rewrite (Q00/ouroboros#1825).

    The text format uses ``(ambiguity: <score>)`` without the milestone
    label to preserve backward compatibility with downstream consumers
    that parse the score via regex.  Milestone data is available in the
    structured ``meta.milestone`` field of the MCP response.
    """
    if score is None:
        return question
    return f"(ambiguity: {score.overall_score:.2f}) {question}"


__all__ = ["format_question_with_ambiguity", "normalize_question_text"]
