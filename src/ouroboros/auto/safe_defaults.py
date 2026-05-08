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
        # Concrete external side effects only: deploy/release/publish flows,
        # production/prod/live targets, explicit messaging or account/branch
        # mutations, and database migrations. Earlier revisions matched bare
        # ``external``, which incorrectly flagged benign phrases such as
        # "no external dependencies" or "use existing external API schema
        # files only" — matched phrases must imply an actual side effect.
        r"\b(deploy|release|publish|production|prod|live|send email|webhook|notify users|"
        r"create account|delete branch|database migration)\b",
    ),
)


# Prefix matching :pyattr:`AutoAnswer.prefixed_text` and tagging this module's
# safe-default synthesis. ``_interview_answers`` filters on this prefix so the
# unsafe-context gate never re-feeds policy-emitted answers (auto answers or
# our own synthesis) back into itself — keeping safe-default finalization
# idempotent across resume/re-finalize calls.
_AUTO_ANSWER_PREFIX = "[from-auto]"
_SAFE_DEFAULT_SYNTHESIS_TAG = "[safe-default-synthesis]"


def build_safe_default_synthesis(finalization: SafeDefaultFinalization) -> str:
    """Build a synthesis answer text describing every defaulted section.

    The synthesis is pushed back into the interview transcript (via
    ``backend.answer``) so the downstream seed generator — which reads the
    persisted interview rounds, not the in-memory ledger — sees the same
    assumptions the ledger now records. The text is tagged with the same
    ``[from-auto]`` prefix that :class:`AutoAnswerer` uses so the
    unsafe-context gate skips it on a later pass.
    """
    if not finalization.defaulted_sections:
        return ""
    # The leading line is recognised as an interview-completion signal by
    # ``GenerateSeedHandler`` (matches ``_INTERVIEW_COMPLETION_PHRASES``:
    # "mark the interview complete" / "ready for seed generation"). That
    # tells the production interview handler to close the session in the
    # same turn — so the persisted transcript does not gain a trailing
    # unanswered question while auto state declares the interview done.
    lines = [
        f"{_AUTO_ANSWER_PREFIX}{_SAFE_DEFAULT_SYNTHESIS_TAG} "
        "Mark the interview complete and hand off for seed generation. "
        "Auto safe-default synthesis (max interview rounds reached). "
        "The following conservative assumptions close the remaining required "
        "Seed sections; treat them as auditable defaults that may be revised "
        "if a stricter answer is required.",
    ]
    for section in finalization.defaulted_sections:
        spec = _SAFE_DEFAULTS.get(section)
        if spec is None:
            continue
        lines.append(f"- {section}: {spec.value} ({spec.rationale})")
    return "\n".join(lines)


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
    ledger: SeedDraftLedger,
    *,
    goal: str,
    pending_question: str | None,  # noqa: ARG001 - kept for backward-compatible call sites
) -> str | None:
    """Detect whether the user-asserted context authorizes any unsafe action.

    The detector inspects only assertions the user (or repo) has actually
    affirmed: the original goal, active non-NON_GOAL ledger entries, and the
    interview answers the user gave. It deliberately ignores backend-authored
    interview questions and the still-open ``pending_question``, because a
    clarifying question like "should this deploy to production?" does not
    authorize a deploy — only the answer does. It also ignores ``NON_GOAL``
    entries because confirmed non-goals are explicit *exclusions*; treating
    "non-goals are credentials and production deployment" as active unsafe
    scope would invert the user's intent.
    """
    context = "\n".join(
        value
        for value in (
            goal,
            *_unsafe_ledger_values(ledger),
            *_interview_answers(ledger),
        )
        if value.strip()
    ).lower()
    context = _strip_negated_clauses(context)
    for reason, pattern in _UNSAFE_CONTEXT_PATTERNS:
        if re.search(pattern, context):
            return reason
    return None


_NEGATION_CLAUSE_PATTERN = re.compile(
    # Negation cue at a word boundary, then the rest of the clause it scopes
    # up to the next sentence break or contrastive conjunction. This lets
    # ``No production deployment`` and ``Do not use customer credentials`` be
    # stripped before the unsafe regex bank runs, while keeping a positively
    # asserted clause that follows ``but``/``however``/``although`` visible.
    r"\b(?:no|not|never|don[’']t|do not|do n[’']t|without|none(?:\s+of)?|neither|nor|"
    r"skip|skips|skipped|avoid|avoids|avoided|exclude|excludes|excluded|forbid|forbids|forbidden)\b"
    r"(?:(?!\b(?:but|however|although|except)\b)[^.;?!\n])*",
    re.IGNORECASE,
)


def _strip_negated_clauses(text: str) -> str:
    """Blank clauses the user has explicitly negated.

    The unsafe-context gate must not trip on phrases like ``No production
    deployment`` or ``Do not use customer credentials``: those are explicit
    *exclusions*, not authorizations. We replace the negation cue plus the
    rest of the clause it scopes (up to the next sentence-break punctuation
    or contrastive conjunction) with whitespace so the regex bank only sees
    positively asserted scope. The implementation is deliberately a coarse
    lexical heuristic — it stops at ``.``, ``;``, ``?``, ``!``, newlines, and
    contrastive conjunctions (``but``, ``however``, ``although``, ``except``)
    so a clause like ``no prod deploys, but log credentials`` keeps the
    second half visible to the unsafe gate.
    """
    return _NEGATION_CLAUSE_PATTERN.sub(" ", text)


_INACTIVE_LEDGER_STATUSES: frozenset[LedgerStatus] = frozenset(
    {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
)

# Sources whose entries describe boundary defaults or explicit exclusions
# rather than user-asserted scope. Filtering on source (not status) keeps
# CONSERVATIVE_DEFAULT entries — which can land with status DEFAULTED but
# may still carry production/auth/billing scope that needs to flag — visible
# to the unsafe-context gate.
_SKIP_SOURCES_FOR_UNSAFE_GATE: frozenset[LedgerSource] = frozenset(
    {LedgerSource.ASSUMPTION, LedgerSource.NON_GOAL}
)


def _unsafe_ledger_values(ledger: SeedDraftLedger) -> tuple[str, ...]:
    """Return active ledger entry values that may carry unsafe user assertions.

    Includes USER_GOAL, REPO_FACT, EXISTING_CONVENTION, INFERENCE, BLOCKER,
    and CONSERVATIVE_DEFAULT entries. Excludes:

    * inactive entries (weak/conflicting/blocked) — superseded or rejected,
    * any ASSUMPTION-source entry — assumptions describe boundary defaults
      (including the safe-default policy's own outputs), not user-affirmed
      scope, so re-feeding them would re-flag the gate's own boundary text
      on a subsequent pass, and
    * ``NON_GOAL`` entries — confirmed non-goals are explicit exclusions
      ("non-goals are auth and production deployment"), and reading them as
      active unsafe scope would invert the user's intent.

    DEFAULTED-status entries from other sources (notably
    ``CONSERVATIVE_DEFAULT``) remain visible because they can still encode
    user-derived unsafe scope — for example, a prior round may have recorded
    a conservative default that nonetheless authorizes a production deploy.
    """
    values: list[str] = []
    for section in ledger.sections.values():
        for entry in section.entries:
            if entry.status in _INACTIVE_LEDGER_STATUSES:
                continue
            if entry.source in _SKIP_SOURCES_FOR_UNSAFE_GATE:
                continue
            values.append(entry.value)
    return tuple(values)


def _interview_answers(ledger: SeedDraftLedger) -> tuple[str, ...]:
    """Return user-supplied interview answers only.

    Backend-authored questions are deliberately excluded because a clarifying
    question (for example "Does this deploy to production?") does not assert
    that the deploy will happen — only an answer can.

    Policy-authored answers are also excluded. :class:`AutoAnswerer` records
    its own answers with a ``[from-auto]`` prefix, and this module's safe-
    default synthesis is tagged the same way. Re-feeding either of those
    into the unsafe-context gate would let the gate flag its own boundary
    text on a subsequent pass and break finalization idempotence (a problem
    visible on resume/re-finalize flows).
    """
    values: list[str] = []
    for item in ledger.question_history:
        answer = item.get("answer", "")
        if not answer:
            continue
        if answer.lstrip().startswith(_AUTO_ANSWER_PREFIX):
            continue
        values.append(answer)
    return tuple(values)
