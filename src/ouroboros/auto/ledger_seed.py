"""Deterministic Seed synthesis from an auto Seed Draft Ledger."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ouroboros.auto.grading import deterministic_floor
from ouroboros.auto.ledger import (
    REPO_FACT_KEY_INFIX,
    REQUESTER_KEY_SUFFIX,
    USER_GOAL_KEY,
    LedgerEntry,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.core.seed import (
    BrownfieldContext,
    ContextReference,
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    RepoFact,
    RequesterAnswer,
    Seed,
    SeedMetadata,
    SourcedScope,
)

if TYPE_CHECKING:
    from ouroboros.auto.expectations import ExpectedExperience

_INACTIVE_STATUSES: frozenset[LedgerStatus] = frozenset(
    {
        LedgerStatus.WEAK,
        LedgerStatus.CONFLICTING,
        LedgerStatus.BLOCKED,
        LedgerStatus.MISSING,
    }
)

PARTIAL_SEED_GENERATION_MODE = "partial_seed_from_evidence"
"""``SeedMetadata.generation_mode`` value for degraded-recovery Seeds.

Single source of truth shared by :func:`partial_seed_from_evidence` and the
grade/run gates that suppress high-ambiguity-only blockers for degraded seeds
(#1257 PR-C).
"""

DEADLINE_LEDGER_SEED_GENERATION_MODE = "interview_deadline_complete_ledger"
"""``SeedMetadata.generation_mode`` for a *complete* ledger that reached closure
via the interview-phase deadline recovery path (#1302).

Distinct from :data:`PARTIAL_SEED_GENERATION_MODE`: the ledger is fully resolved
(no ``unresolved_slots``), but the per-phase interview deadline cut the Socratic
loop short before the backend could confirm low ambiguity (``<= 0.20``). The
Seed therefore carries the full ledger content yet is flagged ``degraded`` so
the grade/run gates surface it as a typed partial-product terminal instead of
auto-running a Seed whose low ambiguity was never backend-confirmed.
"""

_PARTIAL_DEFAULT_ACCEPTANCE = (
    "Synthesized Seed runs without raising and the recorded goal is "
    "surfaced in execution output (conservative smoke).",
)

_PARTIAL_DEFAULT_VERIFICATION = (
    "Execute the smallest available smoke check for the goal; capture "
    "stdout/stderr and exit code; confirm no unhandled exception."
)

_PARTIAL_DEFAULT_FAILURE_MODES = (
    "Unresolved ledger slots may produce incomplete or surprising behavior; "
    "treat as next-step requirements rather than terminal blockers."
)

_PARTIAL_PLACEHOLDER_FIELD = "(unresolved at deadline; see unresolved_slots)"

# Criterion used when no confirmed criterion exists. It is the seed repairer's
# goal-level placeholder, which ``execution_acceptance`` already treats as
# generic, so it neither narrows the goal nor reads as a specific requirement.
_GOAL_PROOF_CRITERION = (
    "A command/API check returns stable observable output or artifacts proving the task goal."
)

# The constraint used when nothing confirmed one. It restates the binding
# rule itself rather than guessing a limit on the task, so the Seed keeps a
# hard constraint without narrowing what the user asked for.
CONFIRMED_SCOPE_CONSTRAINT = (
    "Scope comes only from the goal, the requester's answers, and the repository; "
    "anything listed under Assumptions may be overridden."
)

# Stand-ins for a ledger section with no binding entry. They describe the
# section as open instead of repeating a guess the worker would have to keep.
_UNCONFIRMED_FIELD = "Not confirmed; see Assumptions."
_UNCONFIRMED_VERIFICATION = "Each acceptance criterion passes its own verification."
_UNCONFIRMED_FAILURE_MODES = "none confirmed"

# Every ledger section except the goal. Only binding entries fill the Seed's
# binding fields (constraints, criteria, ontology, principles, exit
# conditions); the rest are carried as Seed ``assumptions``. A label names the
# section an assumption fills; the value after it is kept verbatim, in the
# language it was written in.
_ASSUMPTION_SECTIONS = (
    "constraints",
    "non_goals",
    "acceptance_criteria",
    "actors",
    "inputs",
    "outputs",
    "verification_plan",
    "failure_modes",
    "runtime_context",
)
_ASSUMPTION_PREFIXES = {
    "constraints": "",
    "non_goals": "Non-goal: ",
    "acceptance_criteria": "",
}
# Sections whose auto-chosen answers stay in the record but never reach the
# worker (``SourcedScope.assumptions``). Decided by section only, never by the
# text: an answer about what counts as failure or how to check the work is a
# guess that measured to narrow what the worker builds (#3061).
_RECORD_ONLY_ASSUMPTION_SECTIONS = frozenset({"failure_modes", "verification_plan"})


def brownfield_context_from_cwd(cwd: str | Path | None) -> BrownfieldContext:
    """Detect brownfield state deterministically from a working directory.

    ``ooo auto`` has no interview-driven brownfield extraction, so a synthesized
    Seed would otherwise always default to greenfield even when it runs against
    an existing repository. This mirrors the interview's :func:`detect_brownfield`
    check and, when it fires, records the cwd as a primary context reference so
    the run resolves against the real project directory. Best-effort: any failure
    yields the greenfield default.
    """
    if not cwd:
        return BrownfieldContext()
    try:
        from ouroboros.bigbang.explore import detect_brownfield

        root = Path(cwd).expanduser()
        if not root.is_dir() or not detect_brownfield(root):
            return BrownfieldContext()
        return BrownfieldContext(
            project_type="brownfield",
            context_references=(ContextReference(path=str(root), role="primary", summary=""),),
        )
    except Exception:
        return BrownfieldContext()


def workspace_has_existing_code(cwd: str | Path | None) -> bool:
    """Whether the auto workspace already has code, by the same signal as the Seed.

    ``False`` only when ``cwd`` is a directory that
    :func:`brownfield_context_from_cwd` calls greenfield. A missing ``cwd``, a
    path that is not a directory, or a failed check is ``True``: a change to
    existing code is the safe reading when the workspace is unknown.
    """
    if not cwd:
        return True
    try:
        if not Path(cwd).expanduser().is_dir():
            return True
    except (OSError, RuntimeError, ValueError):
        return True
    return brownfield_context_from_cwd(cwd).project_type == "brownfield"


def synthesize_seed_from_ledger(
    ledger: SeedDraftLedger,
    *,
    interview_id: str | None = None,
    recovery_reason: str | None = None,
    brownfield_context: BrownfieldContext | None = None,
) -> Seed:
    """Build a valid Seed directly from a complete auto ledger.

    This is the fail-soft path for ``ooo auto`` when the authoring backend is
    unavailable after the deterministic ledger has enough evidence to proceed.
    The ledger remains the source of truth; this function performs no model or
    external IO.

    Args:
        ledger: The complete ledger to synthesize from.
        interview_id: Reference to the source interview, mirrored on
            :attr:`~ouroboros.core.seed.SeedMetadata.interview_id`.
        recovery_reason: When set (e.g. ``"interview_phase_deadline"``), the
            ledger is complete but closure was reached via a recovery path that
            never obtained backend-confirmed low ambiguity (#1302). The Seed
            keeps its full ledger content but is stamped ``degraded`` with
            :data:`DEADLINE_LEDGER_SEED_GENERATION_MODE` so the grade/run gates
            route it to the typed partial-product terminal instead of auto-RUN.
            ``None`` (default) yields the legacy fully-runnable ``"normal"`` Seed.
        brownfield_context: Deterministic brownfield context detected from the
            auto session's cwd (see :func:`brownfield_context_from_cwd`). ``None``
            keeps the greenfield default so the ledger path is unchanged when the
            session targets a fresh directory.
    """
    if not ledger.is_seed_ready():
        msg = f"cannot synthesize Seed from incomplete ledger: {', '.join(ledger.open_gaps())}"
        raise ValueError(msg)

    goal = _latest_value(ledger, "goal")
    constraints = _lines_from_section(ledger, "constraints", binding=True)
    non_goals = _lines_from_section(ledger, "non_goals", binding=True)
    if non_goals:
        constraints = (*constraints, *[f"Non-goal: {item}" for item in non_goals])
    constraints = constraints or (CONFIRMED_SCOPE_CONSTRAINT,)

    # With no confirmed criterion, the criterion is proof of the goal itself: a
    # guessed criterion would narrow it.
    acceptance_criteria = _lines_from_section(
        ledger, "acceptance_criteria", split_clauses=False, binding=True
    ) or (_GOAL_PROOF_CRITERION,)
    verification_plan = _binding_joined(ledger, "verification_plan") or _UNCONFIRMED_VERIFICATION
    failure_modes = _binding_joined(ledger, "failure_modes") or _UNCONFIRMED_FAILURE_MODES

    return Seed(
        goal=goal,
        brownfield_context=brownfield_context or BrownfieldContext(),
        constraints=constraints,
        acceptance_criteria=acceptance_criteria,
        assumptions=ledger_assumptions(ledger),
        requester_answers=ledger_requester_answers(ledger),
        repo_facts=ledger_repo_facts(ledger),
        open_questions=ledger_open_questions(ledger),
        ontology_schema=OntologySchema(
            name=_ontology_name(goal),
            description="Auto-synthesized ontology from the completed Seed Draft Ledger.",
            fields=_ontology_fields(ledger, placeholder=_UNCONFIRMED_FIELD),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="ledger_completeness",
                description="All required Seed Draft Ledger sections are resolved before execution.",
                weight=0.5,
            ),
            EvaluationPrinciple(
                name="observable_verification",
                description=verification_plan,
                weight=0.5,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="acceptance_verified",
                description="All acceptance criteria pass under the verification plan.",
                evaluation_criteria=verification_plan,
            ),
            ExitCondition(
                name="failure_modes_absent",
                description=f"Known failure modes are absent: {failure_modes}",
                evaluation_criteria="No listed failure mode is observed in verification evidence.",
            ),
        ),
        metadata=SeedMetadata(
            **_synthesized_metadata_kwargs(ledger, interview_id, recovery_reason)
        ),
    )


def _synthesized_metadata_kwargs(
    ledger: SeedDraftLedger,
    interview_id: str | None,
    recovery_reason: str | None,
) -> dict[str, Any]:
    """Build the ``SeedMetadata`` kwargs for :func:`synthesize_seed_from_ledger`.

    A plain ``recovery_reason is None`` keeps the legacy ``"normal"`` provenance.
    When a recovery reason is supplied the complete-ledger Seed is flagged
    ``degraded`` (with no ``unresolved_slots`` — the ledger is fully resolved)
    so the deadline-recovery contract (#1302) routes it away from auto-RUN.
    """
    kwargs: dict[str, Any] = {
        "seed_id": f"seed_{uuid4().hex[:12]}",
        "ambiguity_score": max(0.05, deterministic_floor(ledger)),
        "interview_id": interview_id,
        "decision_provenance": ledger.provenance_histogram(),
    }
    if recovery_reason is not None:
        kwargs["generation_mode"] = DEADLINE_LEDGER_SEED_GENERATION_MODE
        kwargs["degraded"] = True
        kwargs["recovery_reason"] = recovery_reason
    return kwargs


def partial_seed_from_evidence(
    ledger: SeedDraftLedger,
    *,
    reason: str,
    interview_id: str | None = None,
) -> Seed:
    """Synthesize a degraded but executable Seed from an incomplete ledger.

    The primary use case is the interview-deadline closure ladder (#1257 PR-B):
    when ``ledger.is_seed_ready()`` is False but the deadline has fired, we
    still want to produce *some* product surface rather than terminating in
    ``interview_phase_deadline`` BLOCKED. The resulting Seed:

    * is a valid Pydantic :class:`~ouroboros.core.seed.Seed`,
    * preserves every active ledger entry (so partial work is not discarded),
    * records the unresolved sections in
      :attr:`~ouroboros.core.seed.SeedMetadata.unresolved_slots` for downstream
      gates to convert into next-step hints,
    * fills missing acceptance/verification slots with conservative smoke
      defaults, and
    * lists the unresolved slots in ``constraints`` so executors cannot
      silently overrun the deadline-driven recovery contract.

    ``goal`` remains a hard prerequisite: a *missing* goal (no active value at
    all) is a structural failure — no product can exist without one — and
    raises ``ValueError``. This is the only divergence from "fail-soft": if
    even the goal is unresolved, the deadline has nothing to recover into. A
    goal that is present but only WEAK / CONFLICTING / BLOCKED at the
    aggregate level (e.g. a confirmed goal followed by a later blocked or
    conflicting goal entry) is NOT a structural failure — the active value is
    used, but ``"goal"`` is surfaced in ``unresolved_slots`` and
    ``constraints`` so downstream gates see the contested provenance.

    The function performs no model or external IO; it is safe to call from
    deadline handlers without re-introducing latency that was the original
    timeout source.

    Args:
        ledger: The incomplete (or complete) ledger to build a Seed from.
        reason: Free-form provenance string recorded on
            :attr:`~ouroboros.core.seed.SeedMetadata.recovery_reason` (e.g.
            ``"interview_phase_deadline"``).
        interview_id: Reference to the source interview, mirrored on
            :attr:`~ouroboros.core.seed.SeedMetadata.interview_id`.

    Returns:
        A valid degraded :class:`~ouroboros.core.seed.Seed`.

    Raises:
        ValueError: If the ledger has no active goal entry.
    """
    if not reason or not reason.strip():
        msg = "partial_seed_from_evidence requires a non-empty reason"
        raise ValueError(msg)

    try:
        goal = _latest_value(ledger, "goal")
    except ValueError as exc:
        msg = (
            "cannot synthesize partial Seed without an active goal entry; "
            "goal missing is a structural defect, not a deadline recovery case"
        )
        raise ValueError(msg) from exc

    # ``_latest_value`` above guarantees the ledger has at least one active goal
    # value, but the aggregate goal-section status can still be WEAK /
    # CONFLICTING / BLOCKED (e.g. a confirmed goal followed by a blocked or
    # conflicting later entry), in which case ``open_gaps`` still includes
    # ``"goal"``. The recovery contract requires us to surface that fact
    # verbatim in both ``unresolved_slots`` and ``constraints`` so PR-C gates
    # can convert it into a next-step hint instead of treating the degraded
    # Seed as goal-resolved. We therefore preserve every open gap — including
    # ``goal`` — in the provenance surface.
    unresolved_slots = tuple(ledger.open_gaps())
    degraded = bool(unresolved_slots)

    constraints = list(_lines_from_section(ledger, "constraints", binding=True))
    non_goals = _lines_from_section(ledger, "non_goals", binding=True)
    for item in non_goals:
        constraints.append(f"Non-goal: {item}")
    if not constraints:
        constraints.append(CONFIRMED_SCOPE_CONSTRAINT)
    for slot in unresolved_slots:
        constraints.append(
            f"Known unresolved slot ({slot}): treat as next-step requirement, not implicit success."
        )

    acceptance = _lines_from_section(
        ledger, "acceptance_criteria", split_clauses=False, binding=True
    )
    if not acceptance:
        acceptance = _PARTIAL_DEFAULT_ACCEPTANCE

    verification_plan = (
        _binding_joined(ledger, "verification_plan") or _PARTIAL_DEFAULT_VERIFICATION
    )
    failure_modes = _binding_joined(ledger, "failure_modes") or _PARTIAL_DEFAULT_FAILURE_MODES

    ontology_schema = OntologySchema(
        name=_ontology_name(goal),
        description=(
            "Partial ontology synthesized from incomplete Seed Draft Ledger "
            f"under degraded-recovery reason: {reason}."
        ),
        fields=_ontology_fields(ledger, placeholder=_PARTIAL_PLACEHOLDER_FIELD),
    )

    # Degraded seeds carry a deliberately elevated ambiguity floor so that
    # downstream observers can see the deadline-driven uncertainty in the
    # Seed's ``ambiguity_score``.
    floor = deterministic_floor(ledger)
    ambiguity_score = max(0.05, floor)
    if degraded:
        ambiguity_score = max(ambiguity_score, 0.6)

    return Seed(
        goal=goal,
        constraints=tuple(dict.fromkeys(constraints)),
        acceptance_criteria=acceptance,
        assumptions=ledger_assumptions(ledger),
        requester_answers=ledger_requester_answers(ledger),
        repo_facts=ledger_repo_facts(ledger),
        open_questions=ledger_open_questions(ledger),
        ontology_schema=ontology_schema,
        evaluation_principles=(
            EvaluationPrinciple(
                name="partial_recovery_evidence",
                description=(
                    "Degraded Seed preserves ledger evidence collected before the "
                    "deadline; unresolved slots are surfaced as next-step hints."
                ),
                weight=0.5,
            ),
            EvaluationPrinciple(
                name="observable_verification",
                description=verification_plan,
                weight=0.5,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="partial_acceptance_smoke",
                description=(
                    "Conservative smoke verification passes for the recorded goal "
                    "without raising or violating known constraints."
                ),
                evaluation_criteria=verification_plan,
            ),
            ExitCondition(
                name="failure_modes_absent",
                description=f"Known failure modes are absent: {failure_modes}",
                evaluation_criteria="No listed failure mode is observed in verification evidence.",
            ),
        ),
        metadata=SeedMetadata(
            seed_id=f"seed_{uuid4().hex[:12]}",
            ambiguity_score=ambiguity_score,
            interview_id=interview_id,
            generation_mode=PARTIAL_SEED_GENERATION_MODE,
            degraded=degraded,
            unresolved_slots=unresolved_slots,
            recovery_reason=reason,
            decision_provenance=ledger.provenance_histogram(),
        ),
    )


def _ontology_fields(ledger: SeedDraftLedger, *, placeholder: str) -> tuple[OntologyField, ...]:
    """Ontology fields from binding entries; an unconfirmed field is optional."""
    fields: list[OntologyField] = []
    for name in ("actors", "inputs", "outputs", "runtime_context"):
        confirmed = _binding_joined(ledger, name)
        fields.append(
            OntologyField(
                name=name,
                field_type="string",
                description=confirmed or placeholder,
                required=bool(confirmed),
            )
        )
    return tuple(fields)


def _binding_joined(ledger: SeedDraftLedger, section_name: str) -> str:
    return "; ".join(
        entry.value.strip()
        for entry in _active_entries(ledger, section_name)
        if entry.binds_seed and not _is_requester_answer(entry) and not _is_repo_fact(entry)
    )


def _is_requester_answer(entry: LedgerEntry) -> bool:
    """Whether ``entry`` holds a requester's answer (``requester.requester_answer``).

    Such an entry is a confirmed fact, but it reaches the worker verbatim as a
    quoted question and answer (``Seed.requester_answers``), never rewritten
    into a constraint, criterion or ontology field.
    """
    return entry.key.endswith(REQUESTER_KEY_SUFFIX)


def _is_repo_fact(entry: LedgerEntry) -> bool:
    """Whether ``entry`` holds a checked repository fact (``auto/investigator.py``).

    It settles its section, but it describes the current code, not a
    requirement: it reaches the worker verbatim in ``Seed.repo_facts``, never
    as a constraint, criterion or ontology field.
    """
    return REPO_FACT_KEY_INFIX in entry.key


def _is_assumed_answer(entry: LedgerEntry) -> bool:
    """Whether ``entry`` holds the answer auto chose for an interviewer question.

    Decided by provenance alone: the entry carries the question
    (``LedgerEntry.question``), the answer refiner wrote its value for this
    goal (``LedgerEntry.refined``), and the requester did not decline the
    question (``LedgerEntry.requester_unanswered``). It does not bind (callers
    pass non-binding entries only): it reaches the worker as a labelled
    assumption (``SourcedScope.assumptions``), and its question is not open.
    A question auto decided nothing for keeps no such entry: the
    investigator's not-settled reply, the answerer's fixed templates and the
    refiner's fixed fallback are never marked refined.
    """
    return bool(entry.question.strip()) and entry.refined and not entry.requester_unanswered


def _latest_value(ledger: SeedDraftLedger, section_name: str) -> str:
    values = _active_values(ledger, section_name)
    if not values:
        msg = f"ledger section has no active value: {section_name}"
        raise ValueError(msg)
    return values[-1]


def ledger_assumptions(ledger: SeedDraftLedger) -> tuple[str, ...]:
    """Return the ledger's unconfirmed requirement drafts as Seed assumptions.

    These are the active entries of every section but the goal that do not
    bind (see :attr:`LedgerEntry.binds_seed`) and that either no interviewer
    question produced (auto's conservative defaults and assumptions) or hold
    the answer auto chose for an interviewer question (``_is_assumed_answer``,
    kept verbatim as in :func:`ledger_assumed_answers`). The generic
    safe-default closure block is interview bookkeeping, not a statement about
    the task, so it is left out.

    Any other entry that answers an interviewer question renders nothing
    here, neither its draft nor its question: auto decided nothing for it, so
    the question goes to ``Seed.open_questions`` (``ledger_open_questions``),
    which no execution prompt renders. A requester's answer renders in
    ``Seed.requester_answers`` instead (``ledger_requester_answers``).
    """
    return _assumption_lines(ledger, include_unasked=True)


def ledger_assumed_answers(ledger: SeedDraftLedger) -> tuple[str, ...]:
    """The answers auto chose for its interviewer questions, as labelled assumption lines.

    Each line is the section label (``Non-goal: ``, ``Outputs: ``, none for
    constraints and criteria) followed by the entry's value exactly as the
    refiner wrote it, outer whitespace aside: no punctuation is stripped and
    nothing is split. In section and ledger order. They never bind; ``ooo
    auto`` gives them to the worker as ``SourcedScope.assumptions``, under a
    heading that says nobody confirmed them. Answers in the failure-mode and
    verification-plan sections are left out (``_RECORD_ONLY_ASSUMPTION_SECTIONS``):
    they stay in ``Seed.assumptions`` and still decide their question.
    """
    return _assumption_lines(
        ledger, include_unasked=False, skip_sections=_RECORD_ONLY_ASSUMPTION_SECTIONS
    )


def _assumption_lines(
    ledger: SeedDraftLedger,
    *,
    include_unasked: bool,
    skip_sections: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Labelled assumption lines: auto's assumed answers, and optionally its unasked drafts.

    An unasked draft is cleaned and split like every ledger line
    (``_lines_from_section``); an assumed answer is kept verbatim. One value
    written for several sections is listed once, under the first section's
    label, the way the interview answer states it once. ``skip_sections`` are
    left out entirely.
    """
    lines: dict[str, str] = {}
    for section_name in _ASSUMPTION_SECTIONS:
        if section_name in skip_sections:
            continue
        prefix = _ASSUMPTION_PREFIXES.get(
            section_name, f"{section_name.replace('_', ' ').capitalize()}: "
        )
        values: list[str] = []
        if include_unasked:
            values.extend(
                _lines_from_section(
                    ledger,
                    section_name,
                    split_clauses=False,
                    binding=False,
                    closure=False,
                    keep=lambda entry: not entry.question.strip(),
                )
            )
        values.extend(entry.value.strip() for entry in _assumed_entries(ledger, section_name))
        for value in values:
            lines.setdefault(value, f"{prefix}{value}")
    return tuple(lines.values())


def _assumed_entries(ledger: SeedDraftLedger, section_name: str) -> tuple[LedgerEntry, ...]:
    """The active, non-binding entries of a section that hold an answer auto chose."""
    return tuple(
        entry
        for entry in _active_entries(ledger, section_name)
        if not entry.binds_seed
        and _is_assumed_answer(entry)
        and not _is_requester_answer(entry)
        and not _is_repo_fact(entry)
    )


def _decided_questions(ledger: SeedDraftLedger) -> frozenset[str]:
    """Interviewer questions auto chose an answer for in at least one section.

    The refiner writes each section of an answer in its own call, and a call
    that fails leaves that section's template in place. The question is still
    decided: its chosen answer reaches the worker, so it is not open.
    """
    return frozenset(
        entry.question.strip()
        for section_name in _ASSUMPTION_SECTIONS
        for entry in _assumed_entries(ledger, section_name)
    )


def ledger_open_questions(ledger: SeedDraftLedger) -> tuple[str, ...]:
    """Every interviewer question nobody answered, verbatim and in ledger order.

    The split is by entry provenance, never by text: an active entry that does
    not bind and carries the interviewer's question (``LedgerEntry.question``,
    stamped where the entry is created) holds a draft nobody confirmed. The
    question is open unless auto chose an answer for it in at least one
    section (``_decided_questions``); a section whose refiner call failed and
    kept the template does not reopen it. Open questions therefore cover the
    investigator's not-settled entries (``NOT_SETTLED_VALUE``), questions only
    the answerer's template answered, and drafts kept because the requester
    gave no answer (``LedgerEntry.requester_unanswered``). Requester answers,
    checked repository facts and the answers auto chose are answers, so they
    are never open questions.
    """
    decided = _decided_questions(ledger)
    questions: list[str] = []
    for section_name in _ASSUMPTION_SECTIONS:
        questions.extend(
            entry.question.strip()
            for entry in _active_entries(ledger, section_name)
            if not entry.binds_seed
            and entry.question.strip()
            and entry.question.strip() not in decided
            and not _is_requester_answer(entry)
            and not _is_repo_fact(entry)
        )
    return tuple(dict.fromkeys(questions))


def with_ledger_assumptions(seed: Seed, ledger: SeedDraftLedger) -> Seed:
    """Carry the ledger's unconfirmed drafts into ``seed.assumptions``.

    The interview transcript withholds auto's guessed answers from Seed
    extraction (``bigbang/answer_provenance.py``), so they cannot become
    constraints or criteria; this keeps them in the Seed as non-binding
    assumptions. Existing assumptions keep their order first. The worker of a
    Seed with ``sourced_scope`` sees only the answers auto chose
    (``SourcedScope.assumptions``), not this mixed list.
    """
    merged = tuple(dict.fromkeys((*seed.assumptions, *ledger_assumptions(ledger))))
    if merged == seed.assumptions:
        return seed
    return seed.model_copy(update={"assumptions": merged})


def ledger_requester_answers(ledger: SeedDraftLedger) -> tuple[RequesterAnswer, ...]:
    """Every answer the requester gave, with its question, verbatim and in ledger order.

    One answer fills each section its question targeted, so the pairs are
    de-duplicated. A later answer for the same facet retires the earlier entry
    in ``add_entry``; both are still the requester's own words, so both are kept.
    """
    pairs: dict[tuple[str, str], RequesterAnswer] = {}
    for section in ledger.sections.values():
        for entry in section.entries:
            if not _is_requester_answer(entry) or not entry.value.strip():
                continue
            key = (entry.question.strip(), entry.value.strip())
            pairs.setdefault(key, RequesterAnswer(question=key[0], answer=key[1]))
    return tuple(pairs.values())


def with_requester_answers(seed: Seed, ledger: SeedDraftLedger) -> Seed:
    """Carry the requester's answers into ``seed.requester_answers``, verbatim."""
    merged = tuple(dict.fromkeys((*seed.requester_answers, *ledger_requester_answers(ledger))))
    if merged == seed.requester_answers:
        return seed
    return seed.model_copy(update={"requester_answers": merged})


def ledger_repo_facts(ledger: SeedDraftLedger) -> tuple[RepoFact, ...]:
    """Every checked repository fact, with its question and evidence, in ledger order.

    One fact fills each section its question targeted, so facts are
    de-duplicated by question and answer.
    """
    facts: dict[tuple[str, str], RepoFact] = {}
    for section in ledger.sections.values():
        for entry in section.entries:
            if not _is_repo_fact(entry) or not entry.value.strip():
                continue
            key = (entry.question.strip(), entry.value.strip())
            facts.setdefault(
                key, RepoFact(question=key[0], answer=key[1], evidence=tuple(entry.evidence))
            )
    return tuple(facts.values())


def ledger_sourced_scope(ledger: SeedDraftLedger) -> SourcedScope | None:
    """The goal, constraints and criteria whose source the ledger records.

    The goal is the user's request verbatim (the ``USER_GOAL_KEY`` entry). The
    constraints, non-goals and criteria are the entries that bind
    (``LedgerEntry.binds_seed``), split the same way a ledger-synthesized Seed
    splits them. The assumptions are the answers auto chose for its interview
    questions (:func:`ledger_assumed_answers`); they never bind and stay out of
    the constraints and criteria. The decision is by entry provenance only;
    nothing reads the text. Without an active binding user goal there is no
    record (``None``), and the Seed renders as before.
    """
    goal_entries = [
        entry
        for entry in _active_entries(ledger, "goal")
        if entry.key == USER_GOAL_KEY and entry.binds_seed
    ]
    if not goal_entries:
        return None
    constraints = _lines_from_section(ledger, "constraints", binding=True)
    non_goals = _lines_from_section(ledger, "non_goals", binding=True)
    return SourcedScope(
        goal=goal_entries[-1].value.strip(),
        constraints=(*constraints, *[f"Non-goal: {item}" for item in non_goals]),
        acceptance_criteria=_lines_from_section(
            ledger, "acceptance_criteria", split_clauses=False, binding=True
        ),
        assumptions=ledger_assumed_answers(ledger),
    )


def ledger_non_goal_constraints(ledger: SeedDraftLedger) -> tuple[str, ...]:
    """The ledger's binding non-goals as ``Non-goal:`` constraints, in ledger order.

    The same split a ledger-synthesized Seed uses: only entries that bind
    (``LedgerEntry.binds_seed``) qualify, so a defaulted guess stays an assumption.
    """
    non_goals = _lines_from_section(ledger, "non_goals", binding=True)
    return tuple(f"Non-goal: {item}" for item in non_goals)


def with_ledger_carryover(
    seed: Seed,
    ledger: SeedDraftLedger,
    *,
    expected: ExpectedExperience | None = None,
) -> Seed:
    """Carry the ledger's assumptions, requester answers, repo facts, open questions and sourced scope.

    The sourced scope (``Seed.sourced_scope``) is what the worker is told; the
    Seed's own goal, constraints, criteria, assumptions, ontology, principles
    and exit conditions are written by the Seed generator from the whole
    interview, unanswered questions included, so they stay in the Seed for the
    record and for verification only.

    The Seed's open questions are reconciled with the ledger: a question the
    Seed already lists stays unless auto has since chosen an answer for it
    (``_decided_questions``), so a Seed recorded again (repair, resume) never
    keeps a decided question open.

    ``expected`` is the expected-experience research for a new product
    (``auto/expectations.py``). Its lines and sources go into the sourced scope;
    without lines, a list the Seed's sourced scope already holds is kept.
    """
    seed = with_requester_answers(with_ledger_assumptions(seed, ledger), ledger)
    merged = tuple(dict.fromkeys((*seed.repo_facts, *ledger_repo_facts(ledger))))
    if merged != seed.repo_facts:
        seed = seed.model_copy(update={"repo_facts": merged})
    decided = _decided_questions(ledger)
    questions = tuple(
        question
        for question in dict.fromkeys((*seed.open_questions, *ledger_open_questions(ledger)))
        if question.strip() not in decided
    )
    if questions != seed.open_questions:
        seed = seed.model_copy(update={"open_questions": questions})
    scope = ledger_sourced_scope(ledger)
    if scope is None:
        return seed
    previous = seed.sourced_scope
    if expected is not None and expected.lines:
        scope = scope.model_copy(
            update={
                "expected_experience": expected.lines,
                "expected_experience_sources": expected.sources,
            }
        )
    elif previous is not None and previous.expected_experience:
        scope = scope.model_copy(
            update={
                "expected_experience": previous.expected_experience,
                "expected_experience_sources": previous.expected_experience_sources,
            }
        )
    if scope == previous:
        return seed
    return seed.model_copy(update={"sourced_scope": scope})


def _lines_from_section(
    ledger: SeedDraftLedger,
    section_name: str,
    *,
    split_clauses: bool = True,
    binding: bool | None = None,
    closure: bool = True,
    keep: Callable[[LedgerEntry], bool] | None = None,
) -> tuple[str, ...]:
    """Split a ledger section's active values into deduplicated lines.

    With ``split_clauses=False`` only explicit bullet/newline markers split a
    value; bare semicolons are preserved. This stops a model that writes dense,
    clause-rich prose (e.g. ``"Tasks persist to file; survives restart"``) from
    being mechanically exploded into many tiny acceptance criteria — each extra
    AC costs a full agent session at execution time, so over-atomization here is
    pure token waste.

    ``binding`` keeps only entries that bind (``True``) or only those that do
    not (``False``); ``None`` keeps both. ``closure=False`` drops the
    safe-default closure entry. ``keep`` keeps only the entries it accepts.
    """
    pattern = r"\r?\n\s*[-*]\s+" if not split_clauses else r"(?:\r?\n\s*[-*]\s+|\s*;\s+)"
    lines: list[str] = []
    for entry in _active_entries(ledger, section_name):
        if binding is not None and entry.binds_seed is not binding:
            continue
        if _is_requester_answer(entry):
            continue  # quoted verbatim in ``Seed.requester_answers`` instead
        if _is_repo_fact(entry):
            continue  # quoted verbatim in ``Seed.repo_facts`` instead
        if binding is False and entry.requester_unanswered:
            continue
        if not closure and entry.key == f"{section_name}.safe_default_finalization":
            continue
        if keep is not None and not keep(entry):
            continue
        value = entry.value.strip()
        parts = re.split(pattern, value)
        for part in parts:
            cleaned = part.strip(" \t\r\n-*:;.")
            if cleaned:
                lines.append(cleaned)
    return tuple(dict.fromkeys(lines))


def _active_values(ledger: SeedDraftLedger, section_name: str) -> tuple[str, ...]:
    return tuple(entry.value.strip() for entry in _active_entries(ledger, section_name))


def _active_entries(ledger: SeedDraftLedger, section_name: str) -> tuple[LedgerEntry, ...]:
    section = ledger.sections.get(section_name)
    if section is None:
        return ()
    return tuple(
        entry
        for entry in section.entries
        if entry.status not in _INACTIVE_STATUSES and entry.value.strip()
    )


def _ontology_name(goal: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", goal.title())
    name = "".join(words[:4]) or "AutoTask"
    if not name[0].isalpha():
        name = f"Auto{name}"
    return name[:64]
