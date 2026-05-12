"""External verifier loop (RFC v2 H1, #830).

Wraps a leaf executor with a separate read-only verifier pass plus a
bounded retry. The verifier is intentionally model-agnostic at this
layer: it is any callable that, given the active profile, the leaf's
parsed evidence record, the AC text, and the raw leaf output, returns a
VerifierVerdict.

This module is wiring-only at the orchestrator seam — `parallel_executor`
is not yet routed through `run_with_verifier`. The integration PR follows
once the failure taxonomy (H7) and routing (H5) hooks are in place. For
now this gives downstream PRs a stable, fully-tested loop they can plug
the real LLM-backed verifier into.

Loop semantics:
    1. Executor produces a leaf output.
    2. The harness parses evidence (H2). If evidence cannot be extracted,
       that counts as a FAIL with a parser reason — verifier is NOT called.
    3. The harness validates the record against profile.evidence_schema.
       If the record fails validation, the verifier is short-circuited
       and the loop retries (the leaf would never satisfy the contract
       even if the verifier said PASS).
    4. Otherwise the verifier is invoked. PASS → return. FAIL → retry,
       feeding the verdict reasons back to the executor.
    5. After `max_retries` exhausted FAILs, return the last attempt with
       accepted=False so the outer orchestrator can escalate.

The retry budget defaults to K=2 per shaun0927's H1 sketch in #830.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import subprocess
from typing import Protocol

from ouroboros.orchestrator.evidence_schema import (
    EvidenceError,
    EvidenceRecord,
    ValidationResult,
    extract_evidence,
    validate_evidence,
)
from ouroboros.orchestrator.profile_loader import ExecutionProfile

DEFAULT_MAX_RETRIES: int = 2

# Exception types treated as *operational* failures (transient: network
# blip, LLM timeout, subprocess crash, non-zero test return code).
# Anything outside this tuple is treated as a deterministic programming
# bug and re-raised so production diagnosis is not blocked by silent
# STALL retries. (Bot findings on PR #884: r5 — programming bugs must
# propagate; r6 — subprocess.TimeoutExpired / subprocess.CalledProcessError
# from test-running verifiers must be absorbed as STALL.)
_OPERATIONAL_VERIFIER_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,  # parent of BrokenPipe/ConnectionRefused/etc.
    # OSError catches transient FS/subprocess/sockets. ConnectionError
    # subclasses OSError, but we list it explicitly above for clarity.
    OSError,
    # Verifier impls following the module's documented model run tests
    # via subprocess. Both timeout and non-zero exit must be retryable.
    subprocess.SubprocessError,
)


class VerifierContractError(ValueError):
    """Raised when a VerifierVerdict is constructed in an invalid shape.

    Subclasses ValueError for backward compatibility with callers that
    check `ValueError`, but acts as a distinct exception type so the
    bounded-retry loop can distinguish a verifier *implementation bug*
    (this class — must propagate) from an *operational failure* (any
    other Exception — converted to a STALL verdict and retried).
    """


# Canonical failure classes the H7 router knows how to route. Verifier
# implementations may set VerifierVerdict.failure_class to any of these
# (or leave it None); any other string is rejected up front so a typo
# in a verifier impl surfaces at construction time, not when the
# downstream classifier silently degrades to STALL.
_VALID_FAILURE_CLASSES: frozenset[str] = frozenset(
    {
        "EVIDENCE_MISSING",
        "FABRICATION_SUSPECTED",
        "SCOPE_CREEP",
        "STALL",
        "BLOCKED",
    }
)


@dataclass(frozen=True)
class VerifierVerdict:
    """Outcome of a single verifier pass.

    Attributes:
        passed: True iff the verifier accepted the leaf result.
        reasons: Human-readable, harness-surfaceable failure reasons.
            Must be empty when `passed` is True; must be non-empty
            when `passed` is False (the retry loop feeds these back
            to the executor — a bare FAIL with no reasons is opaque
            and indistinguishable from the first attempt).
        failure_class: Optional hint for H7 (failure taxonomy). One of
            "EVIDENCE_MISSING", "FABRICATION_SUSPECTED", "SCOPE_CREEP",
            "STALL", "BLOCKED", or None for an unclassified failure.
    """

    passed: bool
    reasons: tuple[str, ...] = ()
    failure_class: str | None = None

    def __post_init__(self) -> None:
        if self.passed and self.reasons:
            msg = "VerifierVerdict(passed=True) must not carry reasons"
            raise VerifierContractError(msg)
        if self.passed and self.failure_class is not None:
            msg = "VerifierVerdict(passed=True) must not carry a failure_class"
            raise VerifierContractError(msg)
        if not self.passed and not self.reasons:
            # A bare FAIL with no reasons produces no feedback for the
            # retry executor and no surfaceable explanation on budget
            # exhaustion. The harness cannot recover from an opaque
            # FAIL, so reject it at construction time.
            msg = (
                "VerifierVerdict(passed=False) must include at least one "
                "reason; the retry loop feeds reasons back to the "
                "executor and surfaces them on exhaustion."
            )
            raise VerifierContractError(msg)
        if self.failure_class is not None and self.failure_class not in _VALID_FAILURE_CLASSES:
            # Verifier impls that emit an unknown failure_class would
            # silently degrade to STALL in the H7 classifier — a typo
            # would mask a real fabrication or scope-creep signal.
            # Reject up front so the H1↔H7 contract stays explicit.
            valid = ", ".join(sorted(_VALID_FAILURE_CLASSES))
            msg = (
                f"VerifierVerdict.failure_class={self.failure_class!r} is "
                f"not a recognized taxonomy value. Valid: {valid}, or None."
            )
            raise VerifierContractError(msg)


class Verifier(Protocol):
    """Callable that adjudicates a leaf result against the active profile.

    Implementations are expected to be **read-only** with respect to the
    workspace — they may inspect files, run test commands, or query an
    LLM, but must not mutate state the executor produced.
    """

    def __call__(
        self,
        *,
        profile: ExecutionProfile,
        ac: str,
        leaf_output: str,
        record: EvidenceRecord,
    ) -> VerifierVerdict: ...


class LeafExecutor(Protocol):
    """Callable that runs the leaf executor for a given AC.

    The `feedback` argument carries verifier reasons from the previous
    attempt; it is empty on the first call and non-empty on retries so
    the executor can address the verifier's complaints directly.
    """

    def __call__(self, *, ac: str, feedback: tuple[str, ...]) -> str: ...


@dataclass(frozen=True)
class Attempt:
    """One executor + verifier pass within a single AC."""

    leaf_output: str
    record: EvidenceRecord | None
    evidence_error: str | None
    validation: ValidationResult | None
    verdict: VerifierVerdict | None
    validation_error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.verdict is not None and self.verdict.passed

    @property
    def blocked(self) -> bool:
        return self.validation is not None and self.validation.blocker is not None


@dataclass(frozen=True)
class LoopResult:
    """Aggregate outcome of run_with_verifier."""

    accepted: bool
    attempts: tuple[Attempt, ...] = field(default_factory=tuple)

    @property
    def final(self) -> Attempt:
        if not self.attempts:
            msg = "LoopResult has no attempts (executor was never called)"
            raise RuntimeError(msg)
        return self.attempts[-1]


def _failure_reasons(attempt: Attempt) -> tuple[str, ...]:
    """Collect surfaceable reasons from an attempt's failure mode."""
    if attempt.evidence_error is not None:
        return (f"evidence parse failed: {attempt.evidence_error}",)
    if attempt.validation_error is not None:
        return (f"evidence validation failed: {attempt.validation_error}",)
    out: list[str] = []
    if attempt.validation is not None and not attempt.validation.ok:
        out.extend(attempt.validation.reasons())
    if attempt.verdict is not None and not attempt.verdict.passed:
        out.extend(attempt.verdict.reasons)
    return tuple(out)


def run_with_verifier(
    *,
    executor: LeafExecutor,
    verifier: Verifier,
    profile: ExecutionProfile,
    ac: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> LoopResult:
    """Run the leaf executor with verifier-backed bounded retry.

    Args:
        executor: Leaf executor callable (see LeafExecutor).
        verifier: Verifier callable (see Verifier). Only invoked when the
            evidence record is structurally valid.
        profile: Active ExecutionProfile.
        ac: AC text passed unchanged to the executor each attempt.
        max_retries: Number of retries *after* the first attempt. K=2
            means up to 3 total executor calls.

    Returns:
        LoopResult with `accepted=True` iff some attempt passed the
        verifier, plus the full attempt transcript for upstream logging
        and failure-taxonomy classification (H7).
    """
    if max_retries < 0:
        msg = f"max_retries must be >= 0, got {max_retries}"
        raise ValueError(msg)

    attempts: list[Attempt] = []
    feedback: tuple[str, ...] = ()

    for _ in range(max_retries + 1):
        leaf_output = executor(ac=ac, feedback=feedback)

        try:
            record: EvidenceRecord | None = extract_evidence(leaf_output)
            evidence_error: str | None = None
        except EvidenceError as exc:
            record = None
            evidence_error = str(exc)

        validation: ValidationResult | None = None
        validation_error: str | None = None
        verdict: VerifierVerdict | None = None

        if record is not None:
            try:
                validation = validate_evidence(profile, record)
            except EvidenceError as exc:
                validation_error = str(exc)
                validation = None
            if validation is not None and validation.ok:
                try:
                    raw_verdict = verifier(
                        profile=profile,
                        ac=ac,
                        leaf_output=leaf_output,
                        record=record,
                    )
                except VerifierContractError:
                    # A verifier impl that constructs an invalid verdict
                    # is a deterministic programming bug, not a transient
                    # operational failure. Surface it — masking it as
                    # STALL would burn the retry budget and ship a
                    # broken verifier to production.
                    raise
                except _OPERATIONAL_VERIFIER_ERRORS as exc:
                    # Operational failures (timeout, network blip,
                    # subprocess crash) are part of normal production
                    # for verifier impls that run tests or query LLMs.
                    # Treat them as a FAIL with a surfaceable reason so
                    # the retry budget can absorb the blip.
                    #
                    # Programming bugs (AttributeError, KeyError, etc.)
                    # are NOT in the catch list — they propagate so the
                    # operator can fix the broken verifier instead of
                    # watching it silently exhaust retries.
                    verdict = VerifierVerdict(
                        passed=False,
                        reasons=(f"verifier raised {type(exc).__name__}: {exc}",),
                        failure_class="STALL",
                    )
                else:
                    # Verifier is only a static Protocol — Python won't
                    # enforce the return type at runtime. A buggy impl
                    # that returns None (or any non-VerifierVerdict) would
                    # otherwise sit as `attempt.verdict`, produce empty
                    # _failure_reasons(), and silently burn the entire
                    # retry budget. Surface the contract violation here.
                    if not isinstance(raw_verdict, VerifierVerdict):
                        msg = (
                            f"Verifier returned {type(raw_verdict).__name__}, "
                            "expected VerifierVerdict. This is a verifier "
                            "implementation bug, not a transient failure."
                        )
                        raise VerifierContractError(msg)
                    verdict = raw_verdict

        attempt = Attempt(
            leaf_output=leaf_output,
            record=record,
            evidence_error=evidence_error,
            validation_error=validation_error,
            validation=validation,
            verdict=verdict,
        )
        attempts.append(attempt)

        if attempt.accepted:
            return LoopResult(accepted=True, attempts=tuple(attempts))
        if attempt.blocked:
            return LoopResult(accepted=False, attempts=tuple(attempts))

        feedback = _failure_reasons(attempt)

    return LoopResult(accepted=False, attempts=tuple(attempts))


__all__ = [
    "DEFAULT_MAX_RETRIES",
    "Attempt",
    "LeafExecutor",
    "LoopResult",
    "Verifier",
    "VerifierContractError",
    "VerifierVerdict",
    "run_with_verifier",
]
