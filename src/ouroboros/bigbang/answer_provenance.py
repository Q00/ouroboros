"""Provenance of an interview answer, decided once where the answer enters.

The interview advertises ``[from-*]`` prefixes to the caller on every turn (see
``bigbang/interview.py``).  Two of the advertised sources — ``[from-code]`` /
``[from-repo]`` and ``[from-research]`` — mark a fact the caller *adopted* from
somewhere else rather than a decision the caller *made*.  An adopted fact informs
the user's judgment and must never become a requirement on its own (#1755).

Two properties this module exists to hold:

**Decided once, carried as a type.**  Provenance is settled at the single point
where an answer is attached to the interview (``InterviewState.record_answer``)
and read as a field afterwards.  Consumers do not re-read the marker.
Re-reading per surface is what drifted: ``_classify_interview_answer_source`` in
``mcp/tools/authoring_handlers.py`` classifies ``[from-research]`` — one of the
three prefixes advertised every turn — as ``human``.

**Withholding governs authority, not content.**  An observation's content is
absent from the *answer* slot, because that is the slot from which requirements
are read.  It is left untouched in the *question* slot, because sharpening the
next question is what the observation was collected for in the first place.  The
same text plays two roles, and the rule is per-role rather than per-string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.bigbang.interview import InterviewState

AnswerProvenance = Literal["user", "observation"]

#: Prefixes marking an answer as an adopted fact rather than a decision.
#: Compound and refined forms (``[from-code][auto-confirmed]``,
#: ``[from-user][refined]``) extend a base prefix, so prefix matching covers
#: them.  Note that per the answer contract in
#: ``orchestrator/capabilities/interview_schemas.py`` bare ``[from-code]`` is
#: the *user-confirmed* form and ``[from-code][auto-confirmed]`` is the one that
#: skipped confirmation — the longer prefix is the less endorsed one.  Both are
#: still facts, so both classify the same way here.
OBSERVATION_PREFIXES: tuple[str, ...] = (
    "[from-code]",
    "[from-repo]",
    "[from-research]",
    # ``[from-data]`` has no forwarding path: the data lane's numbers are shown
    # beside the question, the user reads them, and the answer is the user's own
    # words on the ordinary ``[from-user]`` path (#1754).
    #
    # That held when the lane could only propose a read, and it is what still
    # holds now that the lane runs one and carries the aggregate back
    # (Q00/ouroboros#1825) -- the absence of a value field was never what kept a
    # measurement out of the Seed; this is. So the entry matters more than it
    # did, not less: a measurement is the least durable fact of the three, and
    # if one arrives in an answer slot out of contract it must be withheld like
    # the rest rather than treated as a decision because no rule named it.
    "[from-data]",
)

#: Rendered in place of a withheld answer.  It names why the content is gone and
#: where the observation did its work, so the extractor meets a deliberate
#: placeholder rather than an unexplained gap.
WITHHELD_ANSWER_NOTE = (
    "[observation withheld — an adopted fact, not a decision. "
    "It informed the questions that follow.]"
)


def classify_answer_provenance(answer: str | None) -> AnswerProvenance:
    """Return the provenance of ``answer`` from its advertised prefix.

    Everything that is not an adopted fact classifies as ``"user"``, including
    the generated sources ``[from-auto]`` / ``[from-safe-default]``.  Those are
    not *human* decisions, but they are decisions — the auto driver commits to
    them on the user's behalf — and the split this field encodes is decision vs.
    adopted fact, not human vs. machine.  Classifying them as observations would
    withhold the whole of ``ooo auto``'s contribution from extraction.
    """
    if not answer:
        return "user"
    return "observation" if answer.lstrip().casefold().startswith(OBSERVATION_PREFIXES) else "user"


@dataclass(frozen=True, slots=True)
class ExtractionRound:
    """One interview round as a requirement-producing consumer should read it."""

    round_number: int
    question: str
    answer: str | None
    withheld: bool


def extraction_rounds(state: InterviewState) -> list[ExtractionRound]:
    """Project ``state.rounds`` for every consumer that produces requirements.

    An observation's answer renders as :data:`WITHHELD_ANSWER_NOTE`; its content
    is absent from the projection, so a paraphrase of it is impossible by
    construction rather than something a later check has to detect.  Once an
    extractor has rewritten ``[from-code] 3 attempts, 2s/4s/8s backoff`` into
    "retry three times with exponential backoff", the marker is gone and the
    sentence is indistinguishable from a decision — which is why this has to
    happen at the input.

    Question text is projected unchanged, deliberately.  See the module
    docstring: the question is where an observation is supposed to arrive.

    Rounds still awaiting an answer are projected as-is; they carry no answer to
    withhold and consumers already render a bare question line for them.
    """
    projected: list[ExtractionRound] = []
    for round_data in state.rounds:
        withheld = bool(round_data.user_response) and round_data.provenance == "observation"
        projected.append(
            ExtractionRound(
                round_number=round_data.round_number,
                question=round_data.question,
                answer=WITHHELD_ANSWER_NOTE if withheld else round_data.user_response,
                withheld=withheld,
            )
        )
    return projected
