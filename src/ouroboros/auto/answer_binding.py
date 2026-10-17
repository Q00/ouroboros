"""Which ``ooo auto`` interview answers may bind a Seed, declared at the source.

The auto driver answers the interview on the requester's behalf. Most of those
answers are guesses: conservative defaults and assumptions, including a draft
kept because the requester gave no answer. A guess must never become a Seed
constraint or acceptance criterion; it becomes a fact only when the requester
confirms it.

The driver knows which kind each answer is when it creates it, so it declares
that with the answer as a structured ``answer_provenance`` argument. The
interview records it on the round (``bigbang/answer_provenance.py``) and Seed
extraction withholds the answer. Nothing downstream reads the ``[from-auto]``
text prefix to decide. The guesses still reach the worker: the auto pipeline
carries the unconfirmed ledger drafts into ``Seed.assumptions``
(``auto/ledger_seed.py``).
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource
from ouroboros.bigbang.answer_provenance import AnswerProvenance

if TYPE_CHECKING:
    from ouroboros.auto.interview_driver import InterviewBackend, InterviewTurn

# Answer sources whose text is what the user stated, the requester answered,
# or the repository shows. Any other source binds only through a ledger entry
# that binds on its own (``LedgerEntry.binds_seed``).
_BINDING_ANSWER_SOURCES = frozenset(
    {
        AutoAnswerSource.USER_GOAL,
        AutoAnswerSource.REPO_FACT,
        AutoAnswerSource.USER_PREFERENCE,
        AutoAnswerSource.REQUESTER,
    }
)


# Sources that are guesses by definition. Such an answer is an assumption
# whatever its ledger updates carry, so no entry attached to it can lift it.
_GUESS_ANSWER_SOURCES = frozenset(
    {AutoAnswerSource.CONSERVATIVE_DEFAULT, AutoAnswerSource.ASSUMPTION}
)


def answer_provenance(answer: AutoAnswer) -> AnswerProvenance:
    """Return ``"user"`` for an answer that may bind, else ``"assumption"``."""
    if answer.source in _GUESS_ANSWER_SOURCES:
        return "assumption"
    if answer.source in _BINDING_ANSWER_SOURCES:
        return "user"
    if any(entry.binds_seed for _, entry in answer.ledger_updates):
        return "user"
    return "assumption"


class _DeclaringBackend:
    """``backend.answer`` with a fixed provenance declared on every call."""

    __slots__ = ("_backend", "_provenance")

    def __init__(self, backend: InterviewBackend, provenance: AnswerProvenance) -> None:
        self._backend = backend
        self._provenance = provenance

    async def answer(
        self, session_id: str, answer: str, *, last_question: str | None = None
    ) -> InterviewTurn:
        """Record ``answer``, declaring the provenance when the backend takes it.

        Backends without a ``provenance`` keyword (test doubles and
        plain-callable backends) receive the original call unchanged; a
        declaration only lowers authority, so omitting it never widens what
        binds.
        """
        answer_method: Any = self._backend.answer
        if _accepts_keyword(answer_method, "provenance"):
            return await answer_method(
                session_id, answer, last_question=last_question, provenance=self._provenance
            )
        return await self._backend.answer(session_id, answer, last_question=last_question)


def with_provenance(backend: InterviewBackend, provenance: AnswerProvenance) -> _DeclaringBackend:
    """Return ``backend`` whose ``answer`` declares ``provenance``."""
    return _DeclaringBackend(backend, provenance)


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    # Only an explicitly named keyword counts: a ``**kwargs`` double (such as a
    # bare mock) keeps receiving the call it was written against.
    return name in parameters


__all__ = ["answer_provenance", "with_provenance"]
