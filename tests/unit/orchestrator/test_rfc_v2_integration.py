"""Cross-module integration smoke tests for the RFC v2 stack (#830).

These tests assert that the eight modules introduced by #830 PR 1-8 +
the ProfileBackedStrategy from PR 9 compose into a coherent surface
without any one PR contradicting another's contract.

They are intentionally narrow: each smoke verifies one cross-module
invariant. Behavior of parallel_executor's live dispatch path is not
exercised here — that lands in the follow-up flip-the-default PR once
the stack merges.
"""

from __future__ import annotations

import json

import pytest

from ouroboros.orchestrator.context_governor import (
    SiblingStatus,
    compose_context,
)
from ouroboros.orchestrator.decomposition_params import (
    build_decomposition_system_prompt,
    params_from_profile,
)
from ouroboros.orchestrator.evidence_schema import (
    extract_evidence,
    validate_evidence,
)
from ouroboros.orchestrator.failure_taxonomy import (
    FailureClass,
    classify,
    policy_for,
)
from ouroboros.orchestrator.phase_wrappers import wrap_prompt
from ouroboros.orchestrator.profile_loader import ExecutionProfile, load_profile
from ouroboros.orchestrator.profile_strategy import ProfileBackedStrategy
from ouroboros.orchestrator.routing import DispatchRole, ModelTier, decide_route
from ouroboros.orchestrator.verifier import (
    Attempt,
    VerifierVerdict,
    run_with_verifier,
)


@pytest.fixture(params=["code", "research", "analysis"])
def profile(request: pytest.FixtureRequest) -> ExecutionProfile:
    return load_profile(request.param)


def test_profile_drives_strategy_decomposer_and_router(
    profile: ExecutionProfile,
) -> None:
    """A single profile flows into strategy, decomposer, and router consistently."""
    strategy = ProfileBackedStrategy(profile)
    assert strategy.get_tools() == list(profile.suggested_tools)

    decomp_prompt = build_decomposition_system_prompt(params_from_profile(profile))
    assert profile.axis in decomp_prompt

    executor_route = decide_route(role=DispatchRole.EXECUTOR, profile=profile)
    assert executor_route.tools == profile.suggested_tools
    assert executor_route.tier == ModelTier.SONNET


def test_phase_wrapper_carries_schema_required_fields(
    profile: ExecutionProfile,
) -> None:
    """The H3 wrapper surfaces every required evidence field by name."""
    wrapped = wrap_prompt(profile, "do the AC", "execute body").render()
    for required in profile.evidence_schema.required:
        assert required in wrapped, f"{required!r} missing from wrapped prompt"


def test_evidence_validator_and_taxonomy_agree_on_missing(profile: ExecutionProfile) -> None:
    """Missing required fields → ValidationResult → FailureClass.EVIDENCE_MISSING."""
    leaf_output = json.dumps({})  # empty payload — every required field is missing.
    record = extract_evidence(leaf_output)
    validation = validate_evidence(profile, record)
    assert validation.ok is False

    attempt = Attempt(
        leaf_output=leaf_output,
        record=record,
        evidence_error=None,
        validation=validation,
        verdict=None,
    )
    assert classify(attempt) == FailureClass.EVIDENCE_MISSING
    assert policy_for(FailureClass.EVIDENCE_MISSING).action.name == "RETRY"


def test_verifier_loop_with_profile_backed_inputs() -> None:
    """End-to-end loop against the code profile with scripted leaves."""
    profile = load_profile("code")

    good_output = json.dumps(
        {
            "files_touched": ["src/a.py"],
            "commands_run": ["pytest"],
            "tests_passed": ["test_a"],
        }
    )

    def executor(*, ac: str, feedback: tuple[str, ...]) -> str:
        return good_output

    def verifier(
        *,
        profile: ExecutionProfile,
        ac: str,
        leaf_output: str,
        record,
    ) -> VerifierVerdict:
        return VerifierVerdict(passed=True)

    result = run_with_verifier(
        executor=executor,
        verifier=verifier,
        profile=profile,
        ac="ship feature",
    )
    assert result.accepted is True
    assert result.final.validation is not None and result.final.validation.ok


def test_context_governor_keeps_ac_verbatim_under_pressure() -> None:
    """compose_context never truncates the AC, only the parent summary."""
    big_parent = "x" * 5000
    result = compose_context(
        ac="critical AC",
        parent_summary=big_parent,
        siblings=[SiblingStatus("AC1", accepted=True)],
    )
    rendered = result.render()
    assert "critical AC" in rendered
    # Parent summary may have been truncated, but the AC must be intact.
    assert result.ac == "critical AC"
