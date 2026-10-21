"""Deterministic Seed synthesis from an auto Seed Draft Ledger."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from ouroboros.auto.grading import deterministic_floor
from ouroboros.auto.ledger import (
    REPO_FACT_KEY_INFIX,
    REQUESTER_KEY_SUFFIX,
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
)

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
# conditions); the rest are carried as Seed ``assumptions``.
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
    # downstream grading observers can see the deadline-driven uncertainty
    # without us having to special-case the gate logic here. The grade gate
    # (PR-C) decides whether to suppress the high_ambiguity_score blocker
    # based on ``metadata.degraded`` / ``generation_mode`` — it does not
    # re-derive uncertainty from the score itself.
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


def _latest_value(ledger: SeedDraftLedger, section_name: str) -> str:
    values = _active_values(ledger, section_name)
    if not values:
        msg = f"ledger section has no active value: {section_name}"
        raise ValueError(msg)
    return values[-1]


def ledger_assumptions(ledger: SeedDraftLedger) -> tuple[str, ...]:
    """Return the ledger's unconfirmed requirement drafts as Seed assumptions.

    These are the active entries of every section but the goal that do not
    bind (see :attr:`LedgerEntry.binds_seed`) and that no interviewer question
    produced: auto's conservative defaults and assumptions. The worker sees
    them as assumptions it may override. The generic safe-default closure
    block is interview bookkeeping, not a statement about the task, so it is
    left out.

    An entry that answers an interviewer question (``LedgerEntry.question``)
    renders nothing here, neither its drafted answer nor its question: the
    question goes to ``Seed.open_questions`` (``ledger_open_questions``), which
    no execution prompt renders. A requester's answer renders in
    ``Seed.requester_answers`` instead (``ledger_requester_answers``).
    """
    lines: list[str] = []
    for section_name in _ASSUMPTION_SECTIONS:
        prefix = _ASSUMPTION_PREFIXES.get(
            section_name, f"{section_name.replace('_', ' ').capitalize()}: "
        )
        values = _lines_from_section(
            ledger,
            section_name,
            split_clauses=False,
            binding=False,
            closure=False,
            asked=False,
        )
        lines.extend(f"{prefix}{value}" for value in values)
    return tuple(dict.fromkeys(lines))


def ledger_open_questions(ledger: SeedDraftLedger) -> tuple[str, ...]:
    """Every interviewer question nobody answered, verbatim and in ledger order.

    The split is by entry provenance, never by text: an active entry that does
    not bind and carries the interviewer's question (``LedgerEntry.question``,
    stamped where the entry is created) holds a draft nobody confirmed. That
    covers the investigator's not-found entries (``.investigated.<round>``),
    auto's own drafted answers, and drafts kept because the requester gave no
    answer (``LedgerEntry.requester_unanswered``). Requester answers and
    checked repository facts are answers, so they are never open questions.
    """
    questions: list[str] = []
    for section_name in _ASSUMPTION_SECTIONS:
        questions.extend(
            entry.question.strip()
            for entry in _active_entries(ledger, section_name)
            if not entry.binds_seed
            and entry.question.strip()
            and not _is_requester_answer(entry)
            and not _is_repo_fact(entry)
        )
    return tuple(dict.fromkeys(questions))


def with_ledger_assumptions(seed: Seed, ledger: SeedDraftLedger) -> Seed:
    """Carry the ledger's unconfirmed drafts into ``seed.assumptions``.

    The interview transcript withholds auto's guessed answers from Seed
    extraction (``bigbang/answer_provenance.py``), so they cannot become
    constraints or criteria; this keeps them visible to the worker as
    non-binding assumptions. Existing assumptions keep their order first.
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


def with_ledger_carryover(seed: Seed, ledger: SeedDraftLedger) -> Seed:
    """Carry the ledger's assumptions, requester answers, repo facts and open questions."""
    seed = with_requester_answers(with_ledger_assumptions(seed, ledger), ledger)
    merged = tuple(dict.fromkeys((*seed.repo_facts, *ledger_repo_facts(ledger))))
    if merged != seed.repo_facts:
        seed = seed.model_copy(update={"repo_facts": merged})
    questions = tuple(dict.fromkeys((*seed.open_questions, *ledger_open_questions(ledger))))
    if questions == seed.open_questions:
        return seed
    return seed.model_copy(update={"open_questions": questions})


def _lines_from_section(
    ledger: SeedDraftLedger,
    section_name: str,
    *,
    split_clauses: bool = True,
    binding: bool | None = None,
    closure: bool = True,
    asked: bool | None = None,
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
    safe-default closure entry. ``asked`` keeps only entries that answer an
    interviewer question (``True``) or only those that do not (``False``).
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
        if asked is not None and bool(entry.question.strip()) is not asked:
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
