"""How two interview questions are decided to be the same question.

Two renderings of one question: the form its identity is digested from, and the
form a host is shown. They sit together because a change to either is a change
to what "the same question" means, and the two were far enough apart once that a
comparison against the stored form did not know the shown form existed.

`normalize_question_text` has one consumer today, the fan-out identity digest.
It is here rather than beside that digest because this is where the question's
forms live, and because `capabilities/__init__.py` is where it would otherwise
grow.
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

    The display form. It lives here rather than in the handler that renders it
    because `authoring_handlers.py` is at its module-size budget, and because
    the difference between this form and the stored one is what an echo of a
    question actually carries — which cost a round to learn
    (Q00/ouroboros#1825).

    The text format uses ``(ambiguity: <score>)`` without the milestone
    label to preserve backward compatibility with downstream consumers
    that parse the score via regex.  Milestone data is available in the
    structured ``meta.milestone`` field of the MCP response.
    """
    if score is None:
        return question
    return f"(ambiguity: {score.overall_score:.2f}) {question}"


__all__ = ["format_question_with_ambiguity", "normalize_question_text"]
