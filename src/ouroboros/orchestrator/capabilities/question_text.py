"""How two interview questions are decided to be the same question.

One definition, in one place, because two would drift and the drift would be
invisible: the fan-out digests this to bind an answer to the question it was
asked for, and the echo check compares against it to tell a faithful echo from
a rewritten one. If they disagreed, one side would bind what the other had
already let through.
"""

from __future__ import annotations

import unicodedata


def normalize_question_text(question: str) -> str:
    """Return the form in which two questions are the same question.

    NFKC folds width and compatibility variants; collapsing runs of whitespace
    folds the reflowing text picks up crossing a host. What survives is the
    words, which is what sameness has to mean when the two copies travelled
    different routes.
    """
    return " ".join(unicodedata.normalize("NFKC", question).strip().split())


__all__ = ["normalize_question_text"]
