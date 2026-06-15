"""Deterministic Seed synthesis from an auto Seed Draft Ledger."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from ouroboros.auto.grading import deterministic_floor
from ouroboros.auto.ledger import LedgerStatus, SeedDraftLedger
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
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


def synthesize_seed_from_ledger(
    ledger: SeedDraftLedger,
    *,
    interview_id: str | None = None,
    recovery_reason: str | None = None,
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
    """
    if not ledger.is_seed_ready():
        msg = f"cannot synthesize Seed from incomplete ledger: {', '.join(ledger.open_gaps())}"
        raise ValueError(msg)

    goal = _latest_value(ledger, "goal")
    constraints = _lines_from_section(ledger, "constraints")
    non_goals = _lines_from_section(ledger, "non_goals")
    if non_goals:
        constraints = (*constraints, *[f"Non-goal: {item}" for item in non_goals])

    acceptance_criteria = _lines_from_section(ledger, "acceptance_criteria", split_clauses=False)
    verification_plan = _joined_section(ledger, "verification_plan")
    failure_modes = _joined_section(ledger, "failure_modes")

    return Seed(
        goal=goal,
        constraints=constraints,
        acceptance_criteria=acceptance_criteria,
        ontology_schema=OntologySchema(
            name=_ontology_name(goal),
            description="Auto-synthesized ontology from the completed Seed Draft Ledger.",
            fields=(
                OntologyField(
                    name="actors",
                    field_type="string",
                    description=_joined_section(ledger, "actors"),
                ),
                OntologyField(
                    name="inputs",
                    field_type="string",
                    description=_joined_section(ledger, "inputs"),
                ),
                OntologyField(
                    name="outputs",
                    field_type="string",
                    description=_joined_section(ledger, "outputs"),
                ),
                OntologyField(
                    name="runtime_context",
                    field_type="string",
                    description=_joined_section(ledger, "runtime_context"),
                ),
            ),
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

    constraints = list(_lines_from_section(ledger, "constraints"))
    non_goals = _lines_from_section(ledger, "non_goals")
    for item in non_goals:
        constraints.append(f"Non-goal: {item}")
    for slot in unresolved_slots:
        constraints.append(
            f"Known unresolved slot ({slot}): treat as next-step requirement, not implicit success."
        )

    acceptance = _lines_from_section(ledger, "acceptance_criteria", split_clauses=False)
    if not acceptance:
        acceptance = _PARTIAL_DEFAULT_ACCEPTANCE

    verification_plan = (
        _joined_section(ledger, "verification_plan") or _PARTIAL_DEFAULT_VERIFICATION
    )
    failure_modes = _joined_section(ledger, "failure_modes") or _PARTIAL_DEFAULT_FAILURE_MODES

    ontology_schema = OntologySchema(
        name=_ontology_name(goal),
        description=(
            "Partial ontology synthesized from incomplete Seed Draft Ledger "
            f"under degraded-recovery reason: {reason}."
        ),
        fields=(
            OntologyField(
                name="actors",
                field_type="string",
                description=_joined_section(ledger, "actors") or _PARTIAL_PLACEHOLDER_FIELD,
            ),
            OntologyField(
                name="inputs",
                field_type="string",
                description=_joined_section(ledger, "inputs") or _PARTIAL_PLACEHOLDER_FIELD,
            ),
            OntologyField(
                name="outputs",
                field_type="string",
                description=_joined_section(ledger, "outputs") or _PARTIAL_PLACEHOLDER_FIELD,
            ),
            OntologyField(
                name="runtime_context",
                field_type="string",
                description=_joined_section(ledger, "runtime_context")
                or _PARTIAL_PLACEHOLDER_FIELD,
            ),
        ),
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
        ),
    )


def _latest_value(ledger: SeedDraftLedger, section_name: str) -> str:
    values = _active_values(ledger, section_name)
    if not values:
        msg = f"ledger section has no active value: {section_name}"
        raise ValueError(msg)
    return values[-1]


def _joined_section(ledger: SeedDraftLedger, section_name: str) -> str:
    return "; ".join(_active_values(ledger, section_name))


def _lines_from_section(
    ledger: SeedDraftLedger, section_name: str, *, split_clauses: bool = True
) -> tuple[str, ...]:
    """Split a ledger section's active values into deduplicated lines.

    With ``split_clauses=False`` only explicit bullet/newline markers split a
    value; bare semicolons are preserved. This stops a model that writes dense,
    clause-rich prose (e.g. ``"Tasks persist to file; survives restart"``) from
    being mechanically exploded into many tiny acceptance criteria — each extra
    AC costs a full agent session at execution time, so over-atomization here is
    pure token waste.
    """
    pattern = r"\r?\n\s*[-*]\s+" if not split_clauses else r"(?:\r?\n\s*[-*]\s+|\s*;\s+)"
    lines: list[str] = []
    for value in _active_values(ledger, section_name):
        parts = re.split(pattern, value)
        for part in parts:
            cleaned = part.strip(" \t\r\n-*:;.")
            if cleaned:
                lines.append(cleaned)
    return tuple(dict.fromkeys(lines))


def _active_values(ledger: SeedDraftLedger, section_name: str) -> tuple[str, ...]:
    section = ledger.sections.get(section_name)
    if section is None:
        return ()
    return tuple(
        entry.value.strip()
        for entry in section.entries
        if entry.status not in _INACTIVE_STATUSES and entry.value.strip()
    )


def _ontology_name(goal: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", goal.title())
    name = "".join(words[:4]) or "AutoTask"
    if not name[0].isalpha():
        name = f"Auto{name}"
    return name[:64]
