"""Data models for parallel AC execution results.

These dataclasses and enums represent the outcome hierarchy for
parallel acceptance-criteria execution:

    ACExecutionResult → ParallelExecutionStageResult → ParallelExecutionResult

Extracted from :mod:`ouroboros.orchestrator.parallel_executor` to keep
the executor module focused on orchestration logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ouroboros.orchestrator.decomposition_policy import DecompositionDecisionRecord
from ouroboros.orchestrator.recoverable_failure import UsageLimitPauseConsequence

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
    from ouroboros.orchestrator.coordinator import CoordinatorReview
    from ouroboros.orchestrator.evidence_schema import EvidenceRecord, ValidationResult
    from ouroboros.orchestrator.level_context import ACContextSummary, LevelContext
    from ouroboros.orchestrator.route_policy import RouteCandidate
    from ouroboros.orchestrator.verifier import VerifierVerdict


class ACExecutionOutcome(str, Enum):  # noqa: UP042
    """Normalized outcome for a single AC execution."""

    SUCCEEDED = "succeeded"
    SATISFIED_EXTERNALLY = "satisfied_externally"
    FAILED = "failed"
    BLOCKED = "blocked"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ACExecutionResult:
    """Result of executing a single AC, including Sub-ACs if decomposed.

    Attributes:
        ac_index: 0-based AC index.
        ac_content: AC description.
        success: Whether execution succeeded.
        messages: All agent messages from execution.
        final_message: Final result message content.
        error: Error message if failed.
        duration_seconds: Execution duration.
        session_id: Claude session ID for this AC.
        retry_attempt: Retry attempt number (0 for the first execution).
        is_decomposed: Whether this AC was decomposed into Sub-ACs.
        sub_results: Results from Sub-AC parallel executions.
        depth: Depth in decomposition tree (0 = root AC).
        decomposition_depth_warning: True when decomposition was skipped because
            the soft depth safety net forced atomic execution.
        outcome: Normalized result classification for aggregation.
        runtime_handle: Backend-neutral runtime handle for same-attempt resume.
        typed_evidence: Parsed leaf evidence record observed at atomic
            completion. Observe-only until the sequenced verifier/default gates.
        typed_evidence_validation: Profile-schema validation result for
            typed_evidence, if parsing succeeded.
        typed_evidence_error: Parse/validation error observed at atomic
            completion, if any.
        atomic_verifier_verdict: Separate verifier pass verdict for the
            parsed typed evidence at atomic completion, when available.
        verify_gate_outcome: Cached seed-level verify gate result from an
            earlier layer. The executor treats this as opaque to avoid a model
            import cycle.
        decomposition_decision: Finalized decomposition decision for this node.
    """

    ac_index: int
    ac_content: str
    success: bool
    messages: tuple[AgentMessage, ...] = field(default_factory=tuple)
    final_message: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    session_id: str | None = None
    retry_attempt: int = 0
    is_decomposed: bool = False
    sub_results: tuple[ACExecutionResult, ...] = field(default_factory=tuple)
    depth: int = 0
    decomposition_depth_warning: bool = False
    outcome: ACExecutionOutcome | None = None
    runtime_handle: RuntimeHandle | None = None
    typed_evidence: EvidenceRecord | None = None
    typed_evidence_validation: ValidationResult | None = None
    typed_evidence_error: str | None = None
    atomic_verifier_verdict: VerifierVerdict | None = None
    verify_gate_outcome: Any | None = None
    decomposition_decision: DecompositionDecisionRecord | None = None
    # Provisional dispatch metadata only.  The selected candidate authorizes no
    # future effect and says nothing about Final Gate acceptance; the outer
    # bounded-escalation owner uses it to durably observe the attempt.
    route_candidate: RouteCandidate | None = None
    # Canonical bounded context sealed before an interrupted stage returns.
    # When present, downstream prompt construction and conflict detection must
    # consume this projection instead of attempting to reconstruct provider
    # messages that are intentionally not persisted.
    context_summary: ACContextSummary | None = None
    conflict_files: tuple[str, ...] | None = None
    # Check-package repair (set only when the check package is on): the
    # counterexample text for the retry prompt, and the failure class that
    # drives the retry kill criterion instead of the advisory legacy class.
    check_package_repair: str | None = None
    check_package_failure_class: str | None = None
    # The legacy verifier's rejection that the check package made advisory;
    # it decides the criteria no admitted check covers.
    legacy_rejection: str | None = None

    def __post_init__(self) -> None:
        """Normalize outcome so callers do not infer from error strings."""
        if self.outcome is None:
            object.__setattr__(self, "outcome", self._infer_outcome())

    def _infer_outcome(self) -> ACExecutionOutcome:
        if self.success:
            return ACExecutionOutcome.SUCCEEDED

        error_text = (self.error or "").lower()
        if "not included in dependency graph" in error_text:
            return ACExecutionOutcome.INVALID
        if "skipped: dependency failed" in error_text or "blocked: dependency" in error_text:
            return ACExecutionOutcome.BLOCKED
        return ACExecutionOutcome.FAILED

    @property
    def is_blocked(self) -> bool:
        """True when the AC was blocked by an upstream dependency outcome."""
        return self.outcome == ACExecutionOutcome.BLOCKED

    @property
    def is_satisfied_externally(self) -> bool:
        """True when the AC was skipped because the working tree already satisfied it."""
        return self.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY

    @property
    def is_failure(self) -> bool:
        """True when the AC executed and failed."""
        return self.outcome == ACExecutionOutcome.FAILED

    @property
    def is_invalid(self) -> bool:
        """True when the AC was not representable in the execution plan."""
        return self.outcome == ACExecutionOutcome.INVALID

    @property
    def attempt_number(self) -> int:
        """Human-readable execution attempt number (1-based)."""
        return self.retry_attempt + 1

    @property
    def decomposition_trustworthy(self) -> bool:
        """Whether this node's finalized split decision is explicitly trusted."""
        return (
            self.decomposition_decision is not None
            and self.decomposition_decision.trustworthy is True
        )


class StageExecutionOutcome(str, Enum):  # noqa: UP042
    """Aggregate outcome for a serial execution stage."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class CoordinatorQuotaPause:
    """Exact coordinator effect whose published PAUSED state may be consumed."""

    execution_id: str
    session_id: str
    level_number: int
    coordinator_aggregate_id: str
    consequence: UsageLimitPauseConsequence

    def __post_init__(self) -> None:
        if (
            type(self.execution_id) is not str
            or not self.execution_id
            or len(self.execution_id) > 1_024
            or type(self.session_id) is not str
            or not self.session_id
            or len(self.session_id) > 1_024
            or type(self.level_number) is not int
            or self.level_number < 1
            or type(self.coordinator_aggregate_id) is not str
            or not self.coordinator_aggregate_id
            or len(self.coordinator_aggregate_id) > 4_096
            or not isinstance(self.consequence, UsageLimitPauseConsequence)
        ):
            raise ValueError("coordinator quota pause owner is invalid")
        self.consequence.to_payload()

    def owner_payload(self) -> dict[str, object]:
        """Return the closed identity embedded atomically in session PAUSED."""

        return {
            "schema_version": 1,
            "kind": "coordinator_quota",
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "level_number": self.level_number,
            "coordinator_aggregate_id": self.coordinator_aggregate_id,
        }


@dataclass(frozen=True, slots=True)
class ParallelExecutionStageResult:
    """Aggregate result for one serial stage of AC execution."""

    stage_index: int
    ac_indices: tuple[int, ...]
    results: tuple[ACExecutionResult, ...] = field(default_factory=tuple)
    started: bool = True
    coordinator_review: CoordinatorReview | None = None

    @property
    def level_number(self) -> int:
        """Legacy 1-based level number."""
        return self.stage_index + 1

    @property
    def success_count(self) -> int:
        """Number of successful ACs in this stage."""
        return sum(
            1
            for result in self.results
            if result.outcome
            in {
                ACExecutionOutcome.SUCCEEDED,
                ACExecutionOutcome.SATISFIED_EXTERNALLY,
            }
        )

    @property
    def externally_satisfied_count(self) -> int:
        """Number of ACs skipped because the working tree already satisfies them."""
        return sum(
            1
            for result in self.results
            if result.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        )

    @property
    def failure_count(self) -> int:
        """Number of failed ACs in this stage."""
        return sum(1 for result in self.results if result.outcome == ACExecutionOutcome.FAILED)

    @property
    def blocked_count(self) -> int:
        """Number of dependency-blocked ACs in this stage."""
        return sum(1 for result in self.results if result.outcome == ACExecutionOutcome.BLOCKED)

    @property
    def invalid_count(self) -> int:
        """Number of invalidly planned ACs in this stage."""
        return sum(1 for result in self.results if result.outcome == ACExecutionOutcome.INVALID)

    @property
    def skipped_count(self) -> int:
        """Legacy alias for blocked and invalid ACs."""
        return self.blocked_count + self.invalid_count

    @property
    def outcome(self) -> StageExecutionOutcome:
        """Aggregate stage outcome for hybrid execution handling."""
        if not self.results:
            return (
                StageExecutionOutcome.BLOCKED
                if not self.started
                else StageExecutionOutcome.SUCCEEDED
            )
        if self.failure_count == 0 and self.blocked_count == 0 and self.invalid_count == 0:
            return StageExecutionOutcome.SUCCEEDED
        if self.success_count == 0 and self.failure_count == 0:
            return StageExecutionOutcome.BLOCKED
        if self.success_count == 0 and self.blocked_count == 0 and self.invalid_count == 0:
            return StageExecutionOutcome.FAILED
        return StageExecutionOutcome.PARTIAL

    @property
    def has_terminal_issue(self) -> bool:
        """True when the stage should block some downstream work."""
        return self.failure_count > 0 or self.blocked_count > 0


@dataclass(frozen=True, slots=True)
class ParallelExecutionResult:
    """Result of parallel AC execution.

    Attributes:
        results: Individual results for each AC.
        success_count: Number of successful ACs.
        externally_satisfied_count: Number of ACs satisfied without re-execution.
        failure_count: Number of failed ACs.
        skipped_count: Number of skipped ACs (due to failed dependencies).
        blocked_count: Number of ACs blocked by dependency failures.
        invalid_count: Number of ACs missing from the execution plan.
        stages: Per-stage aggregated outcomes.
        reconciled_level_contexts: Current shared-workspace handoff contexts
            accumulated after each completed stage. Retry/reopen orchestration
            can pass these back into a later execution attempt so reopened ACs
            start from the post-reconcile workspace state instead of the
            original pre-failure context.
        total_messages: Total messages processed across all ACs.
        total_duration_seconds: Total execution time.
    """

    results: tuple[ACExecutionResult, ...]
    success_count: int
    failure_count: int
    externally_satisfied_count: int = 0
    skipped_count: int = 0
    blocked_count: int = 0
    invalid_count: int = 0
    stages: tuple[ParallelExecutionStageResult, ...] = field(default_factory=tuple)
    reconciled_level_contexts: tuple[LevelContext, ...] = field(default_factory=tuple)
    total_messages: int = 0
    total_duration_seconds: float = 0.0
    # A bounded-routing quota pause is not an ordinary failed execution.  The
    # owner uses this durable signal to publish PAUSED even when another AC in
    # the same round already persisted a valid next-route decision.
    recoverable_route_pause: bool = False
    # A coordinator review is also a provider call. Preserve the complete
    # consequence and exact effect owner so the runner can atomically publish
    # PAUSED and a later resume can consume that one owner exactly once.
    recoverable_coordinator_pause: CoordinatorQuotaPause | None = None

    @property
    def all_succeeded(self) -> bool:
        """Return True if all ACs satisfied (executed or externally) with no failures.

        An empty result set is considered trivially successful — callers that care
        about non-empty coverage should also check len(self.results).
        """
        has_no_failures = (
            self.failure_count == 0
            and self.blocked_count == 0
            and self.invalid_count == 0
            and self.recoverable_coordinator_pause is None
        )
        # Empty set is trivially successful (no failures); non-empty requires >=1 satisfied
        if not self.results:
            return has_no_failures
        return has_no_failures and self.total_satisfied > 0

    @property
    def any_succeeded(self) -> bool:
        """Return True if at least one AC succeeded."""
        return self.success_count > 0 or self.externally_satisfied_count > 0

    @property
    def total_satisfied(self) -> int:
        """Total ACs that passed, whether executed or externally satisfied."""
        return self.success_count + self.externally_satisfied_count


def collect_decomposition_depth_warning_paths(
    result: ACExecutionResult,
    *,
    index_path: tuple[int, ...],
) -> list[str]:
    """Collect dotted AC paths that hit the soft decomposition-depth safety net."""

    paths = [".".join(str(i) for i in index_path)] if result.decomposition_depth_warning else []
    for idx, sub_result in enumerate(result.sub_results, start=1):
        paths.extend(
            collect_decomposition_depth_warning_paths(
                sub_result,
                index_path=(*index_path, idx),
            )
        )
    return paths


def checkpoint_outcome(result: ACExecutionResult) -> str:
    """The outcome a checkpoint records for ``result``.

    With the check package on, the legacy verifier's rejection is advisory
    (``legacy_rejection``, on the result or on a sub-AC of a decomposed root)
    and the package decides after the worker stops. A resumed run recomputes
    the package decision (``boundary/resume.py``); work the legacy verifier
    rejected is still checkpointed as failed, so it is never restored as
    succeeded whatever the resumed run can decide.
    """
    if result.outcome is not None:
        outcome = result.outcome.value
    else:
        outcome = "succeeded" if result.success else "failed"
    if outcome in ("succeeded", "satisfied_externally") and _legacy_rejected(result):
        return "failed"
    return outcome


def _legacy_rejected(result: ACExecutionResult) -> bool:
    return bool(result.legacy_rejection) or any(_legacy_rejected(sub) for sub in result.sub_results)


# Check-package annotations a Routing D record keeps across resume.
_CHECK_PACKAGE_FIELDS = ("legacy_rejection", "check_package_repair", "check_package_failure_class")
_CHECK_PACKAGE_FIELD_CHARS = 20_000


def check_package_record(result: ACExecutionResult) -> dict[str, object]:
    """``{"check_package": {...}}`` for a result carrying check package annotations, else ``{}``.

    A gate-passed, legacy-rejected attempt is a provisional success whose
    ``legacy_rejection`` must survive a resume; otherwise the resumed run
    restores it as a clean success. With the check package off none of these
    fields is set, so its records keep their earlier bytes.
    """
    fields: dict[str, str] = {}
    for name in _CHECK_PACKAGE_FIELDS:
        value = getattr(result, name)
        if value is None or value == "":
            # An empty annotation carries nothing (``legacy_verdict_in_tree``
            # reads it as no rejection); never fail a live persist on it.
            continue
        if not isinstance(value, str) or not value:
            raise RuntimeError("check package annotation is malformed")
        fields[name] = value[:_CHECK_PACKAGE_FIELD_CHARS]
    return {"check_package": fields} if fields else {}


# Failure-class prefixes the check package gate sets (``boundary/authority.py``).
PACKAGE_FAILURE_CLASS_PREFIX = "CHECK_PACKAGE_FAIL"
LEGACY_DECIDED_FAILURE_CLASS_PREFIX = "LEGACY_DECIDED"
# What ``CheckPackageAuthority.install`` sets on an executor.
_CHECK_PACKAGE_HOOKS = ("check_package_gate", "check_package_interfaces")


def governing_verifier_verdict(result: ACExecutionResult) -> Any:
    """The verifier verdict that routes control flow (redispatch, bounce) for ``result``.

    With the check package gate installed, the executor keeps a legacy
    rejection as an advisory annotation (``legacy_rejection``): its verdict
    routes nothing, unless the gate handed the criterion back to the legacy
    verifier (a ``LEGACY_DECIDED`` failure class). Otherwise the verdict is
    ``atomic_verifier_verdict`` as before.
    """
    handed_back = (result.check_package_failure_class or "").startswith(
        LEGACY_DECIDED_FAILURE_CLASS_PREFIX
    )
    if result.legacy_rejection and not handed_back:
        return None
    return result.atomic_verifier_verdict


def inherit_check_package_hooks(parent: object, child: object) -> None:
    """Give an executor derived from ``parent`` the check package hooks ``parent`` has.

    A derived executor (the cross-harness alternate) runs the same criterion
    under the same acceptance authority: the same per-attempt gate, the same
    advisory legacy verdict, and the same entry-point interfaces in its prompt.
    """
    for name in _CHECK_PACKAGE_HOOKS:
        value = getattr(parent, name, None)
        if value is not None:
            setattr(child, name, value)


def restore_check_package_record(value: object) -> dict[str, str | None]:
    """The annotations of a ``check_package_record`` block; fail closed on anything else."""
    if (
        not isinstance(value, dict)
        or not value
        or not set(value) <= set(_CHECK_PACKAGE_FIELDS)
        or any(
            not isinstance(item, str) or not item or len(item) > _CHECK_PACKAGE_FIELD_CHARS
            for item in value.values()
        )
    ):
        raise RuntimeError("check package annotation is malformed")
    return {name: value.get(name) for name in _CHECK_PACKAGE_FIELDS}


__all__ = [
    "ACExecutionOutcome",
    "ACExecutionResult",
    "CoordinatorQuotaPause",
    "ParallelExecutionResult",
    "ParallelExecutionStageResult",
    "StageExecutionOutcome",
    "LEGACY_DECIDED_FAILURE_CLASS_PREFIX",
    "PACKAGE_FAILURE_CLASS_PREFIX",
    "check_package_record",
    "checkpoint_outcome",
    "collect_decomposition_depth_warning_paths",
    "governing_verifier_verdict",
    "inherit_check_package_hooks",
    "restore_check_package_record",
]
