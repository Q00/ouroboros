"""Tests for ouroboros.orchestrator.verifier (RFC v2 #830, PR 3)."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

import pytest

from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.profile_loader import ExecutionProfile, load_profile
from ouroboros.orchestrator.verifier import (
    DEFAULT_MAX_RETRIES,
    LoopResult,
    VerifierContractError,
    VerifierVerdict,
    run_with_verifier,
)


def _code_evidence(tests_passed: list[str] | None = None) -> str:
    return json.dumps(
        {
            "files_touched": ["src/a.py"],
            "commands_run": ["pytest"],
            "tests_passed": tests_passed if tests_passed is not None else ["test_a"],
        }
    )


@dataclass
class ScriptedExecutor:
    """Executor that returns canned outputs in order, recording feedback it saw."""

    outputs: list[str]
    feedbacks: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, *, ac: str, feedback: tuple[str, ...]) -> str:
        self.feedbacks.append(feedback)
        if not self.outputs:
            msg = "ScriptedExecutor ran out of outputs"
            raise AssertionError(msg)
        return self.outputs.pop(0)


@dataclass
class ScriptedVerifier:
    """Verifier that returns canned verdicts in order, recording invocations."""

    verdicts: list[VerifierVerdict]
    calls: int = 0

    def __call__(
        self,
        *,
        profile: ExecutionProfile,
        ac: str,
        leaf_output: str,
        record: EvidenceRecord,
    ) -> VerifierVerdict:
        self.calls += 1
        return self.verdicts.pop(0)


@pytest.fixture
def code_profile() -> ExecutionProfile:
    return load_profile("code")


class TestVerifierVerdict:
    def test_pass_with_reasons_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not carry reasons"):
            VerifierVerdict(passed=True, reasons=("noise",))

    def test_pass_with_failure_class_rejected(self) -> None:
        with pytest.raises(ValueError, match="failure_class"):
            VerifierVerdict(passed=True, failure_class="STALL")

    def test_fail_can_carry_class_and_reasons(self) -> None:
        verdict = VerifierVerdict(
            passed=False, reasons=("missing test",), failure_class="EVIDENCE_MISSING"
        )
        assert verdict.passed is False

    def test_fail_without_reasons_rejected(self) -> None:
        # A bare FAIL produces no feedback for the retry executor and no
        # explanation on budget exhaustion — rejected at construction
        # time per #884 review.
        with pytest.raises(ValueError, match="must include at least one reason"):
            VerifierVerdict(passed=False)

    def test_fail_empty_reasons_tuple_rejected(self) -> None:
        with pytest.raises(ValueError, match="must include at least one reason"):
            VerifierVerdict(passed=False, reasons=())

    def test_fail_with_class_still_needs_reasons(self) -> None:
        # failure_class alone is not enough — the executor needs prose
        # to act on. Both are required when passed=False.
        with pytest.raises(ValueError, match="must include at least one reason"):
            VerifierVerdict(passed=False, failure_class="STALL")

    def test_unknown_failure_class_rejected(self) -> None:
        # The H7 classifier would silently degrade an unknown class to
        # STALL, masking real fabrication / scope-creep signals from a
        # verifier impl that typo'd the tag. Reject at construction.
        with pytest.raises(ValueError, match="not a recognized taxonomy"):
            VerifierVerdict(passed=False, reasons=("bad",), failure_class="MYSTERY")

    @pytest.mark.parametrize(
        "klass",
        [
            "EVIDENCE_MISSING",
            "FABRICATION_SUSPECTED",
            "SCOPE_CREEP",
            "STALL",
            "BLOCKED",
        ],
    )
    def test_known_failure_classes_accepted(self, klass: str) -> None:
        verdict = VerifierVerdict(passed=False, reasons=("r",), failure_class=klass)
        assert verdict.failure_class == klass


class TestVerifierExceptionWrapping:
    """Verifier impls run tests / LLMs — transient failures become FAIL.

    Per bot review on #884, letting a verifier exception escape would
    skip the bounded-retry path and produce no LoopResult transcript
    for upstream escalation. The loop must trap and convert to a FAIL
    verdict with surfaceable reasons.
    """

    def test_verifier_runtime_error_becomes_fail_verdict(
        self, code_profile: ExecutionProfile
    ) -> None:
        call_count = 0

        def flaky_verifier(
            *,
            profile: ExecutionProfile,
            ac: str,
            leaf_output: str,
            record: EvidenceRecord,
        ) -> VerifierVerdict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("LLM call timed out")
            return VerifierVerdict(passed=True)

        executor = ScriptedExecutor(outputs=[_code_evidence(), _code_evidence()])
        result = run_with_verifier(
            executor=executor,
            verifier=flaky_verifier,
            profile=code_profile,
            ac="x",
        )
        assert result.accepted is True
        assert len(result.attempts) == 2
        first = result.attempts[0]
        assert first.verdict is not None
        assert first.verdict.passed is False
        assert first.verdict.failure_class == "STALL"
        assert any("TimeoutError" in r for r in first.verdict.reasons)
        # Retry executor must have seen the wrapped reason as feedback.
        assert any("TimeoutError" in line for line in executor.feedbacks[1])

    def test_contract_error_propagates_not_masked_as_stall(
        self, code_profile: ExecutionProfile
    ) -> None:
        # A verifier impl that constructs an invalid verdict is a
        # deterministic programming bug — masking it as STALL would
        # burn retry budget and ship a broken verifier. The loop must
        # let VerifierContractError propagate (bot finding on #884 r3).
        def buggy_verifier(
            *,
            profile: ExecutionProfile,
            ac: str,
            leaf_output: str,
            record: EvidenceRecord,
        ) -> VerifierVerdict:
            # passed=True with reasons → __post_init__ raises
            # VerifierContractError.
            return VerifierVerdict(passed=True, reasons=("oops",))

        executor = ScriptedExecutor(outputs=[_code_evidence()])
        with pytest.raises(VerifierContractError):
            run_with_verifier(
                executor=executor,
                verifier=buggy_verifier,
                profile=code_profile,
                ac="x",
            )

    def test_contract_error_is_value_error_subclass(self) -> None:
        # Subclassing ValueError preserves backward compatibility for
        # callers that already check `except ValueError`.
        with pytest.raises(ValueError):
            VerifierVerdict(passed=False)

    def test_verifier_exhausts_budget_when_always_raises(
        self, code_profile: ExecutionProfile
    ) -> None:
        def always_boom(
            *,
            profile: ExecutionProfile,
            ac: str,
            leaf_output: str,
            record: EvidenceRecord,
        ) -> VerifierVerdict:
            raise RuntimeError("verifier crashed")

        executor = ScriptedExecutor(outputs=[_code_evidence()] * 3)
        result = run_with_verifier(
            executor=executor,
            verifier=always_boom,
            profile=code_profile,
            ac="x",
            max_retries=2,
        )
        assert result.accepted is False
        assert len(result.attempts) == 3
        for a in result.attempts:
            assert a.verdict is not None
            assert a.verdict.failure_class == "STALL"


class TestHappyPath:
    def test_passes_on_first_attempt(self, code_profile: ExecutionProfile) -> None:
        executor = ScriptedExecutor(outputs=[_code_evidence()])
        verifier = ScriptedVerifier(verdicts=[VerifierVerdict(passed=True)])

        result = run_with_verifier(
            executor=executor, verifier=verifier, profile=code_profile, ac="do thing"
        )

        assert result.accepted is True
        assert len(result.attempts) == 1
        assert result.final.accepted is True
        assert verifier.calls == 1
        # First call must see empty feedback.
        assert executor.feedbacks == [()]


class TestRetryWithFeedback:
    def test_fail_then_pass_within_budget(self, code_profile: ExecutionProfile) -> None:
        executor = ScriptedExecutor(outputs=[_code_evidence(), _code_evidence()])
        verifier = ScriptedVerifier(
            verdicts=[
                VerifierVerdict(passed=False, reasons=("tests look fake",)),
                VerifierVerdict(passed=True),
            ]
        )

        result = run_with_verifier(
            executor=executor, verifier=verifier, profile=code_profile, ac="do thing"
        )

        assert result.accepted is True
        assert len(result.attempts) == 2
        # Second executor invocation must see the verifier's reason as feedback.
        assert executor.feedbacks[0] == ()
        assert executor.feedbacks[1] == ("tests look fake",)

    def test_exhaust_retries_returns_unaccepted(self, code_profile: ExecutionProfile) -> None:
        outputs = [_code_evidence() for _ in range(3)]
        verdicts = [
            VerifierVerdict(passed=False, reasons=("bad",), failure_class="STALL") for _ in range(3)
        ]
        executor = ScriptedExecutor(outputs=outputs)
        verifier = ScriptedVerifier(verdicts=verdicts)

        result = run_with_verifier(
            executor=executor,
            verifier=verifier,
            profile=code_profile,
            ac="x",
            max_retries=2,
        )

        assert result.accepted is False
        assert len(result.attempts) == 3
        assert verifier.calls == 3
        # Final attempt is not accepted but verdict is recorded.
        assert result.final.verdict is not None
        assert result.final.verdict.failure_class == "STALL"

    def test_default_max_retries_is_two(self) -> None:
        assert DEFAULT_MAX_RETRIES == 2


class TestEvidenceShortCircuit:
    def test_evidence_parse_error_skips_verifier(self, code_profile: ExecutionProfile) -> None:
        executor = ScriptedExecutor(outputs=["not json at all", _code_evidence()])
        verifier = ScriptedVerifier(verdicts=[VerifierVerdict(passed=True)])

        result = run_with_verifier(
            executor=executor, verifier=verifier, profile=code_profile, ac="x"
        )

        assert result.accepted is True
        # Verifier called exactly once — on the second (well-formed) attempt.
        assert verifier.calls == 1
        first = result.attempts[0]
        assert first.evidence_error is not None
        assert first.record is None
        assert first.verdict is None
        # Feedback to the retry should mention the parse failure.
        assert any("evidence parse failed" in line for line in executor.feedbacks[1])

    def test_evidence_validation_fail_skips_verifier(self, code_profile: ExecutionProfile) -> None:
        empty_tests = _code_evidence(tests_passed=[])
        executor = ScriptedExecutor(outputs=[empty_tests, _code_evidence()])
        verifier = ScriptedVerifier(verdicts=[VerifierVerdict(passed=True)])

        result = run_with_verifier(
            executor=executor, verifier=verifier, profile=code_profile, ac="x"
        )

        assert result.accepted is True
        # First attempt failed H2 validation; verifier must not have been called yet.
        assert verifier.calls == 1
        first = result.attempts[0]
        assert first.validation is not None and not first.validation.ok
        assert first.verdict is None
        assert any("tests_passed == []" in line for line in executor.feedbacks[1])

    def test_evidence_fail_exhausts_budget_without_verifier(
        self, code_profile: ExecutionProfile
    ) -> None:
        executor = ScriptedExecutor(outputs=["garbage"] * 3)
        verifier = ScriptedVerifier(verdicts=[])  # would raise if invoked

        result = run_with_verifier(
            executor=executor,
            verifier=verifier,
            profile=code_profile,
            ac="x",
            max_retries=2,
        )

        assert result.accepted is False
        assert verifier.calls == 0
        assert all(a.evidence_error is not None for a in result.attempts)


class TestErrorBubbling:
    def test_executor_exception_bubbles(self, code_profile: ExecutionProfile) -> None:
        def boom(*, ac: str, feedback: tuple[str, ...]) -> str:
            raise RuntimeError("network died")

        verifier = ScriptedVerifier(verdicts=[])

        with pytest.raises(RuntimeError, match="network died"):
            run_with_verifier(executor=boom, verifier=verifier, profile=code_profile, ac="x")

    def test_negative_max_retries_rejected(self, code_profile: ExecutionProfile) -> None:
        executor = ScriptedExecutor(outputs=[])
        verifier = ScriptedVerifier(verdicts=[])
        with pytest.raises(ValueError, match="max_retries must be >= 0"):
            run_with_verifier(
                executor=executor,
                verifier=verifier,
                profile=code_profile,
                ac="x",
                max_retries=-1,
            )

    def test_loop_result_final_without_attempts_raises(self) -> None:
        empty = LoopResult(accepted=False, attempts=())
        with pytest.raises(RuntimeError, match="no attempts"):
            _ = empty.final


class TestZeroRetryBudget:
    def test_max_retries_zero_runs_exactly_once(self, code_profile: ExecutionProfile) -> None:
        executor = ScriptedExecutor(outputs=[_code_evidence()])
        verifier = ScriptedVerifier(verdicts=[VerifierVerdict(passed=False, reasons=("nope",))])

        result = run_with_verifier(
            executor=executor,
            verifier=verifier,
            profile=code_profile,
            ac="x",
            max_retries=0,
        )

        assert result.accepted is False
        assert len(result.attempts) == 1
        assert verifier.calls == 1


def test_callable_verifier_via_function(code_profile: ExecutionProfile) -> None:
    """Verifier Protocol must accept a plain function, not only dataclasses."""

    def verifier(
        *,
        profile: ExecutionProfile,
        ac: str,
        leaf_output: str,
        record: EvidenceRecord,
    ) -> VerifierVerdict:
        return VerifierVerdict(passed=True)

    def executor(*, ac: str, feedback: tuple[str, ...]) -> str:
        return _code_evidence()

    result = run_with_verifier(executor=executor, verifier=verifier, profile=code_profile, ac="x")
    assert result.accepted is True
