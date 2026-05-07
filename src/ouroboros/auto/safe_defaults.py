"""Safe-default finalization for bounded auto interviews."""

from __future__ import annotations

from dataclasses import dataclass
import re

from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)


@dataclass(frozen=True, slots=True)
class SafeDefaultFinalization:
    """Outcome of trying to close required ledger gaps with safe defaults."""

    defaulted_sections: tuple[str, ...]
    unsafe_gaps: tuple[str, ...]

    @property
    def completed(self) -> bool:
        """Return True when all remaining gaps were safely defaulted."""
        return bool(self.defaulted_sections) and not self.unsafe_gaps


@dataclass(frozen=True, slots=True)
class _DefaultSpec:
    value: str
    rationale: str


_SAFE_DEFAULTS: dict[str, _DefaultSpec] = {
    "actors": _DefaultSpec(
        "Assume the primary actor is the user or automation agent described by the goal; "
        "do not introduce additional actor classes.",
        "No explicit actor split remained after the bounded interview.",
    ),
    "inputs": _DefaultSpec(
        "Use only inputs already present in the goal, repository state, or explicit interview "
        "answers; do not require new external data.",
        "Unspecified inputs can be safely bounded to existing local context.",
    ),
    "outputs": _DefaultSpec(
        "Produce the smallest observable artifact, state change, or response needed to satisfy "
        "the goal and make verification possible.",
        "Unspecified outputs can be safely bounded to observable MVP behavior.",
    ),
    "constraints": _DefaultSpec(
        "Keep scope to a reversible local MVP, preserve existing project patterns, and avoid new "
        "dependencies unless explicit acceptance criteria require them.",
        "Conservative local constraints reduce execution risk.",
    ),
    "non_goals": _DefaultSpec(
        "Do not perform credential handling, billing, production deployment, legal or medical "
        "judgment, security-sensitive authority choices, or ambiguous external side effects.",
        "Unsafe authority remains out of scope without explicit user direction.",
    ),
    "acceptance_criteria": _DefaultSpec(
        "Completion requires an observable check that demonstrates the requested behavior and a "
        "negative or edge-path check where the implementation surface supports one.",
        "A Seed needs testable acceptance criteria before generation.",
    ),
    "verification_plan": _DefaultSpec(
        "Run the narrowest relevant local tests, type checks, or smoke checks for the changed "
        "behavior; report any verification gap explicitly.",
        "Verification can be safely scoped to local, non-destructive checks.",
    ),
    "failure_modes": _DefaultSpec(
        "Failure includes unverified behavior, scope expansion, dependency churn, non-reproducible "
        "output, or external side effects not authorized by the goal.",
        "Generic failure boundaries keep the Seed auditable without domain assumptions.",
    ),
    "runtime_context": _DefaultSpec(
        "Use the current repository/worktree runtime and established project conventions; do not "
        "choose a new framework, provider, or deployment target.",
        "Existing local conventions are the safest runtime default.",
    ),
}

_UNSAFE_CONTEXT_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "credentials/secrets",
        r"\b(credential|credentials|secret|secrets|access token|auth token|private key|api key|password|"
        r"passphrase)\b",
    ),
    (
        "destructive production action",
        r"\b(delete|drop|erase|wipe|destroy|remove|truncate)\b.+\b(production|prod|live|database|db|"
        r"branch|bucket|account)\b|\b(production|prod|live)\b.+\b(delete|drop|erase|wipe|destroy|"
        r"remove|truncate)\b",
    ),
    (
        "payment/billing",
        r"\b(payment|billing|paid service|credit card|bank account|invoice|charge|purchase|subscribe|"
        r"subscription)\b",
    ),
    (
        "legal/medical judgment",
        r"\b(legal|compliance|license|contract|liability|medical|clinical|diagnosis|treatment|"
        r"healthcare|patient)\b",
    ),
    (
        "security-sensitive choice",
        r"\b(security|encryption|authentication|authorization|authz|oauth|sso|access control|"
        r"permissions|vulnerability|exploit|threat model)\b",
    ),
    (
        "ambiguous external side effect",
        r"\b(deploy|release|publish|production|prod|live|external|send email|webhook|notify users|"
        r"create account|delete branch|database migration)\b",
    ),
)


def finalize_safe_defaultable_gaps(
    ledger: SeedDraftLedger,
    *,
    goal: str,
    provenance: str,
    pending_question: str | None = None,
) -> SafeDefaultFinalization:
    """Fill safe-defaultable required gaps with auditable assumptions.

    The policy is intentionally general: only missing or weak required Seed
    sections may be defaulted, and only when the unresolved context does not
    include unsafe authority, irreversible production actions, payment/billing,
    legal/medical/security-sensitive decisions, or ambiguous external effects.
    Conflicting, blocked, or missing-goal gaps remain hard blockers.
    """
    gaps = ledger.open_gaps()
    if not gaps:
        return SafeDefaultFinalization((), ())

    unsafe_reason = _unsafe_context_reason(ledger, goal=goal, pending_question=pending_question)
    statuses = ledger.section_statuses()
    defaulted: list[str] = []
    unsafe: list[str] = []

    for section in gaps:
        status = statuses[section]
        if section == "goal":
            unsafe.append(f"{section}: primary goal cannot be defaulted")
            continue
        if status in {LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}:
            unsafe.append(f"{section}: {status.value} ledger state cannot be defaulted")
            continue
        if unsafe_reason is not None:
            unsafe.append(f"{section}: unsafe default context ({unsafe_reason})")
            continue
        spec = _SAFE_DEFAULTS.get(section)
        if spec is None:
            unsafe.append(f"{section}: no safe default policy")
            continue
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.safe_default_finalization",
                value=spec.value,
                source=LedgerSource.ASSUMPTION,
                confidence=0.68,
                status=LedgerStatus.DEFAULTED,
                rationale=f"{spec.rationale} Applied at {provenance}.",
                evidence=[
                    provenance,
                    "safe-default policy: missing/weak required gap, local and reversible, no unsafe context detected",
                ],
            ),
        )
        defaulted.append(section)

    if not unsafe and ledger.open_gaps():
        unsafe.extend(
            f"{section}: still unresolved after safe-default finalization"
            for section in ledger.open_gaps()
        )

    return SafeDefaultFinalization(tuple(defaulted), tuple(unsafe))


def _unsafe_context_reason(
    ledger: SeedDraftLedger, *, goal: str, pending_question: str | None
) -> str | None:
    context = "\n".join(
        value
        for value in (
            goal,
            pending_question or "",
            *_unsafe_ledger_values(ledger),
            *_interview_questions(ledger),
        )
        if value.strip()
    ).lower()
    for reason, pattern in _UNSAFE_CONTEXT_PATTERNS:
        if re.search(pattern, context):
            return reason
    return None


def _unsafe_ledger_values(ledger: SeedDraftLedger) -> tuple[str, ...]:
    values: list[str] = []
    for section in ledger.sections.values():
        for entry in section.entries:
            if entry.source == LedgerSource.USER_GOAL:
                values.append(entry.value)
    return tuple(values)


def _interview_questions(ledger: SeedDraftLedger) -> tuple[str, ...]:
    values: list[str] = []
    for item in ledger.question_history:
        values.append(item.get("question", ""))
    return tuple(values)
