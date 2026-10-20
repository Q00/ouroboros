"""Safe-default finalization for bounded auto interviews."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from ouroboros.auto.ledger import (
    DecisionProvenance,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)

# Forward reference only — imported lazily to avoid circular imports.
# Callers that pass ``active_profile`` will have already imported DomainProfile.
if TYPE_CHECKING:
    from ouroboros.auto.domain_profile import DomainProfile

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _DefaultSpec:
    value: str
    rationale: str


@dataclass(frozen=True, slots=True)
class SafeDefaultFinalization:
    """Outcome of trying to close required ledger gaps with safe defaults."""

    defaulted_sections: tuple[str, ...]
    unsafe_gaps: tuple[str, ...]
    defaulted_specs: tuple[tuple[str, _DefaultSpec], ...] = ()

    @property
    def completed(self) -> bool:
        """Return True when all remaining gaps were safely defaulted."""
        return bool(self.defaulted_sections) and not self.unsafe_gaps

    def default_spec_for(self, section: str) -> _DefaultSpec | None:
        """Return the resolved default spec written for *section*, if tracked."""
        for spec_section, spec in self.defaulted_specs:
            if spec_section == section:
                return spec
        return None


def _is_valid_default_spec(spec: _DefaultSpec) -> bool:
    return bool(spec.value.strip() and spec.rationale.strip())


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


# Prefix matching :pyattr:`AutoAnswer.prefixed_text` and tagging this module's
# safe-default synthesis.
#
# These constants are public because the interview driver also needs to build
# follow-up completion signals tagged the same way (see
# :meth:`AutoInterviewDriver._record_safe_default_synthesis`).
AUTO_ANSWER_PREFIX = "[from-auto]"
SAFE_DEFAULT_SYNTHESIS_TAG = "[safe-default-synthesis]"
# Backwards-compatible aliases (kept underscore-private for the local helpers
# below that already reference them inline).
_AUTO_ANSWER_PREFIX = AUTO_ANSWER_PREFIX
_SAFE_DEFAULT_SYNTHESIS_TAG = SAFE_DEFAULT_SYNTHESIS_TAG


def _resolve_spec(
    section: str,
    active_profile: DomainProfile | None,
) -> _DefaultSpec | None:
    """Return the _DefaultSpec for *section*, preferring *active_profile* over the hardcoded dict."""
    if active_profile is not None:
        profile_raw = active_profile.safe_defaults.get(section)
        if isinstance(profile_raw, _DefaultSpec):
            if _is_valid_default_spec(profile_raw):
                return profile_raw
        if isinstance(profile_raw, Mapping):
            value = profile_raw.get("value")
            rationale = profile_raw.get("rationale")
            if (
                isinstance(value, str)
                and value.strip()
                and isinstance(rationale, str)
                and rationale.strip()
            ):
                spec = _DefaultSpec(value=value, rationale=rationale)
                if _is_valid_default_spec(spec):
                    return spec
        if isinstance(profile_raw, str) and profile_raw.strip():
            return _DefaultSpec(value=profile_raw, rationale=f"{section} domain default")
    return _SAFE_DEFAULTS.get(section)


def build_safe_default_synthesis(finalization: SafeDefaultFinalization) -> str:
    """Build a synthesis answer text describing every defaulted section.

    The synthesis is pushed back into the interview transcript (via
    ``backend.answer``) so the downstream seed generator — which reads the
    persisted interview rounds, not the in-memory ledger — sees the same
    assumptions the ledger now records. The text is tagged with the same
    ``[from-auto]`` prefix that :class:`AutoAnswerer` uses.
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
        spec = finalization.default_spec_for(section) or _SAFE_DEFAULTS.get(section)
        if spec is None:
            continue
        lines.append(f"- {section}: {spec.value} ({spec.rationale})")
    return "\n".join(lines)


def finalize_safe_defaultable_gaps(
    ledger: SeedDraftLedger,
    *,
    goal: str,  # noqa: ARG001 - kept for call-site compatibility
    provenance: str,
    pending_question: str | None = None,  # noqa: ARG001 - kept for call-site compatibility
    active_profile: DomainProfile | None = None,
) -> SafeDefaultFinalization:
    """Fill safe-defaultable required gaps with auditable assumptions.

    Only missing or weak required Seed sections may be defaulted; conflicting,
    blocked, and missing-goal gaps remain hard blockers regardless of policy.

    No text is matched to decide this. A former keyword veto on the goal and
    answers (credential, payment, production, legal or medical words) is gone;
    it had shipped empty under the freedom policy (an operator decision), so
    removing it changes no closure. Every default written here is an
    ``ASSUMPTION`` with status ``DEFAULTED``: it never binds a Seed. These
    closure defaults are interview bookkeeping: ``ledger_seed`` leaves them
    out of the worker's assumptions as before, so they give the worker no
    instruction in either direction, and the goal, the requester's answers
    and the code decide the scope.

    ``goal`` and ``pending_question`` are kept for call-site compatibility.

    When *active_profile* is supplied its ``safe_defaults`` dict is consulted
    first for each section; missing keys fall through to the hardcoded
    ``_SAFE_DEFAULTS`` dict so the coding-domain fallback always applies when
    no domain-specific override exists.
    """
    gaps = ledger.open_gaps()
    if not gaps:
        return SafeDefaultFinalization((), ())

    statuses = ledger.section_statuses()
    defaulted: list[str] = []
    defaulted_specs: list[tuple[str, _DefaultSpec]] = []
    unsafe: list[str] = []

    for section in gaps:
        status = statuses[section]
        if section == "goal":
            unsafe.append(f"{section}: primary goal cannot be defaulted")
            continue
        if status in {LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}:
            unsafe.append(f"{section}: {status.value} ledger state cannot be defaulted")
            continue
        # Prefer domain-profile override; fall back to hardcoded coding defaults.
        spec = _resolve_spec(section, active_profile)
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
                # Forced closure of an unconverged interview (max_rounds /
                # phase deadline) — same #1485 backstop class as auto-fill, so
                # stamp it timeout_default rather than letting ASSUMPTION derive
                # to a plain model inference.
                provenance=DecisionProvenance.TIMEOUT_DEFAULT,
                rationale=f"{spec.rationale} Applied at {provenance}.",
                evidence=[
                    provenance,
                    "safe-default policy: missing/weak required gap, local and reversible",
                ],
            ),
        )
        defaulted.append(section)
        defaulted_specs.append((section, spec))

    if not unsafe and ledger.open_gaps():
        unsafe.extend(
            f"{section}: still unresolved after safe-default finalization"
            for section in ledger.open_gaps()
        )

    return SafeDefaultFinalization(tuple(defaulted), tuple(unsafe), tuple(defaulted_specs))
