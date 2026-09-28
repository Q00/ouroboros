"""Tests for PR-V verify-by-default: V1 gate, retry, lateral, trust leaks."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import (
    MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES,
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.harness.journal import EvidenceEntry, EvidenceKind, EvidenceManifest
from ouroboros.orchestrator.adapter import ParamSupport, RuntimeCapabilities
from ouroboros.orchestrator.decomposition_policy import (
    DecompositionChild,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionSource,
    SemanticAttestationStatus,
    StructuralCheckStatus,
)
from ouroboros.orchestrator.model_routing import ModelRouter, decide_model
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
    ParallelExecutionResult,
    _build_success_contract_block,
    _complete_sibling_acs_from_evidence,
    _deserialize_verify_gate_outcome,
    _missing_expected_artifacts,
    _serialize_verify_gate_outcome,
    _VerifyGateOutcome,
    render_parallel_completion_message,
    render_parallel_verification_report,
)
from ouroboros.orchestrator.retry_hints import is_retryable_failure
from ouroboros.orchestrator.verifier import VerifierVerdict
from ouroboros.orchestrator.verify_shell import verify_shell_path_from_identity


class _StubAdapter:
    """Minimal adapter satisfying the executor constructor + verify gate cwd."""

    def __init__(self, working_directory: str, runtime_backend: str = "claude") -> None:
        self.runtime_backend = runtime_backend
        self.self_governs_rate_limit = True
        self.working_directory = working_directory
        self.permission_mode = "acceptEdits"


def _make_executor(
    *,
    working_directory: str = "/workspace",
    run_verify_commands: bool = True,
    ac_retry_attempts: int = 0,
    verify_command_timeout_seconds: int = 30,
    runtime_backend: str = "claude",
) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=_StubAdapter(working_directory, runtime_backend),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=run_verify_commands,
        ac_retry_attempts=ac_retry_attempts,
        verify_command_timeout_seconds=verify_command_timeout_seconds,
    )


def _seed_with_specs(*specs: AcceptanceCriterionSpec | str) -> Seed:
    return Seed(
        goal="verify-by-default",
        acceptance_criteria=specs,
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


# ---------------------------------------------------------------------------
# V1 gate — _run_ac_verify_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_gate_passes_on_exit_zero(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="ok", verify_command="exit 0")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert outcome.reason is None


def test_expected_artifact_runtime_uses_shared_portable_path_grammar(tmp_path: Any) -> None:
    spaced = tmp_path / "Build Outputs"
    spaced.mkdir()
    encoded_overflow = "/".join(("😀" * 62, "a" * 7))
    assert len(encoded_overflow.encode("utf-8")) == MAX_AC_SUCCESS_CONTRACT_ARTIFACT_PATH_BYTES + 1

    assert _missing_expected_artifacts(("./Build Outputs",), str(tmp_path)) == ()
    invalid = _missing_expected_artifacts(
        (
            "bad\x00path",
            ".",
            "../outside",
            "NUL",
            "nul.txt",
            "dir/CON",
            "foo.",
            "docs/a:b",
            "a" * 256,
            "docs/" + ("\u00e9" * 128),
            encoded_overflow,
            "NONE",
        ),
        str(tmp_path),
    )
    assert len(invalid) == 12
    assert any("longer than 255 filesystem bytes" in artifact for artifact in invalid)
    assert any(
        "canonical path longer than 255 portable filesystem bytes" in artifact
        for artifact in invalid
    )
    assert any("NONE mixed with artifact paths" in artifact for artifact in invalid)
    assert "control character" in invalid[0]
    assert "workspace root" in invalid[1]
    assert "escapes workspace" in invalid[2]
    assert all("Windows" in item for item in invalid[3:8])


def test_expected_artifact_runtime_rejects_workspace_capacity_overflow(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX pathconf capacity regression")
    capacity = len(os.fsencode(str(tmp_path.resolve()))) + 2
    monkeypatch.setattr("ouroboros.core.seed.os.pathconf", lambda *_args: capacity)

    missing = _missing_expected_artifacts(("a" * 255,), str(tmp_path))

    assert len(missing) == 1
    assert "workspace path exceeds POSIX capacity" in missing[0]


@pytest.mark.asyncio
async def test_verify_gate_rejects_assertion_only_constructed_contract(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    invalid_spec = AcceptanceCriterionSpec.model_construct(
        description="Command output contains READY",
        verify_command=None,
        expected_artifacts=(),
        output_assertion="READY",
    )

    outcome = await executor._run_ac_verify_gate(spec=invalid_spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.reason == "output_assertion requires verify_command"


@pytest.mark.asyncio
async def test_verify_gate_fails_on_nonzero_exit(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="bad", verify_command="exit 3")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert "status 3" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_verify_gate_output_assertion_match_and_mismatch(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    match_spec = AcceptanceCriterionSpec(
        description="doc",
        verify_command="printf 'BUILD SUCCESS'",
        output_assertion="SUCCESS",
    )
    mismatch_spec = AcceptanceCriterionSpec(
        description="doc",
        verify_command="printf 'BUILD SUCCESS'",
        output_assertion="FAILURE",
    )

    assert (await executor._run_ac_verify_gate(spec=match_spec, cwd=str(tmp_path))).passed is True
    mismatch = await executor._run_ac_verify_gate(spec=mismatch_spec, cwd=str(tmp_path))
    assert mismatch.passed is False
    assert "output_assertion" in (mismatch.reason or "")


@pytest.mark.asyncio
async def test_verify_gate_rejects_commands_that_mutate_the_workspace(tmp_path: Any) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="read-only", verify_command="rm target.txt")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.workspace_mutated is True
    assert "mutated the workspace" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_verify_gate_defers_mutation_verdict_while_sibling_workers_write(
    tmp_path: Any,
) -> None:
    """A sibling's concurrent write is not this command's mutation.

    Parallel ACs share one cwd. When another worker is in flight, a digest
    change during the verify window cannot be attributed to the command, so
    the gate judges the exit code and asks settlement to replay it later
    instead of rejecting the AC (and, through settlement, the whole run).
    """
    executor = _make_executor(working_directory=str(tmp_path))
    # Two workers in flight: the caller plus one sibling still writing.
    executor._inflight_ac_workers = 2
    spec = AcceptanceCriterionSpec(
        description="tests pass while a sibling edits",
        verify_command=(
            'python3 -c "from pathlib import Path; '
            "Path('sibling-wrote-this.py').write_text('x = 1')\""
        ),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert outcome.workspace_mutated is False
    assert outcome.replay_required is True
    assert outcome.cause is None


@pytest.mark.asyncio
async def test_verify_gate_rejects_mutation_when_no_sibling_is_active(tmp_path: Any) -> None:
    """With nothing else in flight the digest change is the command's own."""
    executor = _make_executor(working_directory=str(tmp_path))
    executor._inflight_ac_workers = 1
    spec = AcceptanceCriterionSpec(
        description="mutating verifier",
        verify_command=(
            "python3 -c \"from pathlib import Path; Path('side-effect.py').write_text('x = 1')\""
        ),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.workspace_mutated is True
    assert outcome.replay_required is False
    assert outcome.cause == "workspace_mutated"


@pytest.mark.asyncio
async def test_deferred_verify_pass_is_replayed_at_settlement_and_rejected_if_mutating(
    tmp_path: Any,
) -> None:
    """Deferral is not forgiveness: the quiescent replay still judges mutation.

    The same command that passed provisionally under concurrency writes a
    source file again at settlement, where no sibling can own the change.
    """
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(
            description="mutating verifier",
            verify_command=(
                'python3 -c "from pathlib import Path; '
                "p=Path('touched.py'); p.write_text(p.read_text() + 'x' if p.exists() else 'x')\""
            ),
        ),
        AcceptanceCriterionSpec(description="plain sibling"),
    )
    executor._inflight_ac_workers = 2
    provisional = await executor._run_ac_verify_gate(
        spec=seed.acceptance_criteria[0], cwd=str(tmp_path)
    )
    executor._inflight_ac_workers = 0
    assert provisional.passed is True
    assert provisional.replay_required is True

    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="mutating verifier",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=provisional,
            ),
            ACExecutionResult(
                ac_index=1,
                ac_content="plain sibling",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
            ),
        ],
        session_id="s",
        execution_id="e",
    )

    assert [result.success for result in settled] == [False, False]
    assert all("mutated the workspace" in (result.error or "") for result in settled)


@pytest.mark.asyncio
async def test_deferred_verify_pass_is_replayed_at_settlement_even_when_digest_matches(
    tmp_path: Any,
) -> None:
    """A deferred verdict forces the replay regardless of digest drift."""
    counter = tmp_path.parent / f"deferred-replay-count-{tmp_path.name}.txt"
    command = (
        'python3 -c "from pathlib import Path; '
        f"counter=Path({str(counter)!r}); "
        "n=int(counter.read_text()) if counter.exists() else 0; "
        "counter.write_text(str(n+1)); "
        'raise SystemExit(0)"'
    )
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command=command))
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    assert cached.passed is True
    deferred = replace(cached, replay_required=True)

    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="ac",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=deferred,
            )
        ],
        session_id="s",
        execution_id="e",
    )

    assert counter.read_text(encoding="utf-8") == "2"
    assert settled[0].success is True
    assert settled[0].verify_gate_outcome is not None
    assert settled[0].verify_gate_outcome.replay_required is False


def test_verify_gate_outcome_replay_required_roundtrips_through_checkpoint() -> None:
    original = _VerifyGateOutcome(
        passed=True,
        reason=None,
        output_tail="",
        workspace_digest="a" * 64,
        replay_required=True,
    )
    serialized = _serialize_verify_gate_outcome(original)
    assert serialized is not None
    assert serialized["replay_required"] is True
    assert _deserialize_verify_gate_outcome(serialized) == original

    legacy = dict(serialized)
    del legacy["replay_required"]
    decoded = _deserialize_verify_gate_outcome(legacy)
    assert decoded is not None
    assert decoded.replay_required is False

    corrupt = dict(serialized)
    corrupt["replay_required"] = "yes"
    assert _deserialize_verify_gate_outcome(corrupt) is None


@pytest.mark.asyncio
async def test_batch_tracks_inflight_workers_for_the_verify_gate(tmp_path: Any) -> None:
    """The batch runner counts every worker task in flight, including itself."""
    from tests.unit.orchestrator.parallel_executor_test_support import ProcessLocalTestExecutor

    executor = ProcessLocalTestExecutor(
        adapter=_StubAdapter(str(tmp_path)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=True,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    seed = _seed_with_specs("first", "second")
    observed: list[int] = []
    release = asyncio.Event()

    async def fake_execute(*, ac_index: int, **_: Any) -> ACExecutionResult:
        observed.append(executor._inflight_ac_workers)
        if ac_index == 0:
            await release.wait()
        else:
            release.set()
        return ACExecutionResult(
            ac_index=ac_index,
            ac_content=f"ac {ac_index}",
            success=True,
            outcome=ACExecutionOutcome.SUCCEEDED,
        )

    executor._execute_single_ac = fake_execute  # type: ignore[method-assign]
    results = await executor._execute_ac_batch(
        seed=seed,
        batch_indices=[0, 1],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="",
        level_contexts=[],
        ac_retry_attempts={0: 0, 1: 0},
    )

    assert [type(result).__name__ for result in results] == ["ACExecutionResult"] * 2, results
    assert max(observed) == 2
    assert executor._inflight_ac_workers == 0


@pytest.mark.parametrize("runtime_backend", ["claude", "codex"])
@pytest.mark.asyncio
async def test_verify_gate_accepts_created_python_bytecode_for_all_backends(
    tmp_path: Any,
    runtime_backend: str,
) -> None:
    (tmp_path / "pkg").mkdir()
    executor = _make_executor(
        working_directory=str(tmp_path),
        runtime_backend=runtime_backend,
    )
    spec = AcceptanceCriterionSpec(
        description="tests pass",
        verify_command=(
            'python3 -c "from pathlib import Path; '
            "p=Path('pkg/__pycache__/module.cpython-test.pyc'); "
            "p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'cache')\""
        ),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert outcome.workspace_mutated is False
    assert (tmp_path / "pkg/__pycache__/module.cpython-test.pyc").is_file()


@pytest.mark.asyncio
async def test_verify_gate_accepts_refreshed_python_bytecode(tmp_path: Any) -> None:
    bytecode = tmp_path / "pkg/module.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"old")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="tests pass",
        verify_command=(
            'python3 -c "from pathlib import Path; '
            "Path('pkg/module.pyc').write_bytes(b'refreshed')\""
        ),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert bytecode.read_bytes() == b"refreshed"


@pytest.mark.parametrize("source_exists", [False, True], ids=["created", "modified"])
@pytest.mark.asyncio
async def test_verify_gate_still_rejects_source_mutation(
    tmp_path: Any,
    source_exists: bool,
) -> None:
    source = tmp_path / "module.py"
    if source_exists:
        source.write_text("before\n", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="read-only verification",
        verify_command=(
            "python3 -c \"from pathlib import Path; Path('module.py').write_text('after\\n')\""
        ),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.workspace_mutated is True


@pytest.mark.asyncio
async def test_declared_bytecode_artifact_remains_mutation_sensitive(tmp_path: Any) -> None:
    bytecode = tmp_path / "pkg/__pycache__/module.cpython-test.pyc"
    bytecode.parent.mkdir(parents=True)
    bytecode.write_bytes(b"old")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="declared cache artifact is stable",
        expected_artifacts=("pkg/__pycache__/module.cpython-test.pyc",),
        verify_command=(
            'python3 -c "from pathlib import Path; '
            "Path('pkg/__pycache__/module.cpython-test.pyc').write_bytes(b'refreshed')\""
        ),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.workspace_mutated is True


@pytest.mark.parametrize("entry_kind", ["directory", "symlink"])
@pytest.mark.parametrize("suffix", [".pyc", ".pyo"])
def test_workspace_digest_observes_non_regular_bytecode_suffix_paths(
    tmp_path: Any,
    entry_kind: str,
    suffix: str,
) -> None:
    target = tmp_path / f"runtime{suffix}"
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    if entry_kind == "directory":
        target.mkdir()
    else:
        target.symlink_to("missing-bytecode-target")

    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))
    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.asyncio
async def test_verify_gate_ignores_normalized_exit_code_output_assertion(
    tmp_path: Any,
) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="exit code is already enforced by verify_command",
        verify_command="exit 0",
        output_assertion="exit code 0",
    )

    assert spec.output_assertion is None
    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert outcome.reason is None


# ---------------------------------------------------------------------------
# V1 gate integration — _apply_verify_gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_verify_gate_does_not_skip_assertion_only_constructed_contract(
    tmp_path: Any,
) -> None:
    invalid_spec = AcceptanceCriterionSpec.model_construct(
        description="Command output contains READY",
        verify_command=None,
        expected_artifacts=(),
        output_assertion="READY",
    )
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(
            description="Command output contains READY",
            verify_command="printf READY",
        )
    )
    object.__setattr__(seed, "acceptance_criteria", (invalid_spec,))
    executor = _make_executor(working_directory=str(tmp_path))
    result = ACExecutionResult(ac_index=0, ac_content=invalid_spec.description, success=True)

    gated = await executor._apply_verify_gate(
        seed=seed,
        ac_index=0,
        result=result,
        session_id="session",
        execution_id="execution",
    )

    assert gated.success is False
    assert "output_assertion requires verify_command" in (gated.error or "")


@pytest.mark.asyncio
async def test_apply_verify_gate_flips_success_to_failed(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))
    result = ACExecutionResult(ac_index=0, ac_content="ac", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated.success is False
    assert gated.outcome == ACExecutionOutcome.FAILED
    assert "Verify gate failed" in (gated.error or "")
    assert gated.atomic_verifier_verdict is not None
    assert gated.atomic_verifier_verdict.failure_class == "EVIDENCE_MISSING"


@pytest.mark.asyncio
async def test_apply_verify_gate_reuses_cached_success_outcome(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 0"))
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ac",
        success=True,
        verify_gate_outcome=cached,
    )

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result


@pytest.mark.asyncio
async def test_apply_verify_gate_rechecks_artifacts_without_replaying_cached_command(
    tmp_path: Any,
) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("ready", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(
            description="ac",
            verify_command="exit 0",
            expected_artifacts=("artifact.txt",),
        )
    )
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    artifact.unlink()
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ac",
        success=True,
        verify_gate_outcome=cached,
    )

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated.success is False
    assert gated.verify_gate_outcome is not None
    assert gated.verify_gate_outcome.missing_artifacts == ("artifact.txt",)


@pytest.mark.asyncio
async def test_final_verify_settlement_invalidates_prior_success_after_mutation(
    tmp_path: Any,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="artifact", expected_artifacts=("target.txt",)),
        AcceptanceCriterionSpec(description="mutator", verify_command="rm target.txt"),
        "contractless",
    )
    first_outcome = await executor._run_ac_verify_gate(
        spec=seed.acceptance_criteria[0], cwd=str(tmp_path)
    )
    mutating_outcome = await executor._run_ac_verify_gate(
        spec=seed.acceptance_criteria[1], cwd=str(tmp_path)
    )
    results = await executor._settle_verify_gate_results(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="artifact",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=first_outcome,
            ),
            ACExecutionResult(
                ac_index=1,
                ac_content="mutator",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=mutating_outcome,
            ),
            ACExecutionResult(
                ac_index=2,
                ac_content="contractless",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
            ),
        ],
        session_id="s",
        execution_id="e",
    )

    assert [result.success for result in results] == [False, False, False]
    assert all(result.outcome is ACExecutionOutcome.FAILED for result in results)


@pytest.mark.asyncio
async def test_apply_verify_gate_never_recovers_failed_worker_result(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="test -d ."))
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ac",
        success=False,
        error="runtime false negative",
        outcome=ACExecutionOutcome.FAILED,
    )

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result
    assert gated.success is False
    executor._event_store.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_verify_gate_times_out_hung_command(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path), verify_command_timeout_seconds=1)
    spec = AcceptanceCriterionSpec(
        description="hung",
        verify_command='python3 -c "import time; time.sleep(10)"',
    )

    started = time.monotonic()
    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert time.monotonic() - started < 5
    assert outcome.passed is False
    assert outcome.reason == "verify_command timed out after 1s"
    assert outcome.environment_unverifiable is True


@pytest.mark.asyncio
async def test_batch_emits_outer_outcome_marker_after_verify_failure(tmp_path: Any) -> None:
    """Provisional leaf proof events cannot outlive a seed-level rejection."""
    executor = _make_executor(working_directory=str(tmp_path), ac_retry_attempts=0)
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))

    async def fake_batch(**_kwargs: Any) -> list[ACExecutionResult]:
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ac",
                success=True,
                retry_attempt=0,
                is_decomposed=True,
            )
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )

    assert isinstance(results[0], ACExecutionResult)
    assert results[0].success is False
    emitted = [call.args[0] for call in executor._event_store.append.await_args_list]
    markers = [event for event in emitted if event.type == "execution.ac.attempt_judged"]
    assert len(markers) == 1
    assert markers[0].data == {
        "execution_id": "e",
        "session_id": "s",
        "root_ac_index": 0,
        "ac_index": 0,
        "retry_attempt": 0,
        "attempt_number": 1,
        "success": False,
        "outcome": "failed",
        "is_decomposed": True,
        "is_decomposed_child": True,
    }


@pytest.mark.asyncio
async def test_early_stop_alt_success_still_verify_gated(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An alternate 'success' taken on the retry early-stop path must be verify-gated.

    Regression: the early-stop cross-harness hook replaces the stored result with
    the alternate's, and the alternate runs via ``_execute_single_ac``, which has
    no seed-level success contract. A failing ``verify_command`` must still flip an
    alternate ``success=True`` to FAILED, exactly like the same-runtime path — the
    alternate must not bypass the verify-by-default contract.
    """
    from ouroboros.orchestrator import cross_harness_redispatch as chr

    executor = ParallelACExecutor(
        adapter=_StubAdapter(str(tmp_path)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=True,
        ac_retry_attempts=2,
        cross_harness_redispatch=True,
    )
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))
    monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")

    fab = VerifierVerdict(
        passed=False,
        reasons=("fabricated a file",),
        failure_class="FABRICATION_SUSPECTED",
    )

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        # Same eligible failure class on the initial attempt and retry 1 so the
        # loop early-stops before the counter cap and reaches the alt-harness hook.
        return [
            ACExecutionResult(
                ac_index=idx,
                ac_content="ac",
                success=False,
                error="fabricated",
                outcome=ACExecutionOutcome.FAILED,
                atomic_verifier_verdict=fab,
            )
            for idx in kwargs["batch_indices"]
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    async def alt_reports_success(backend: str, **kwargs: Any) -> ACExecutionResult:
        # The alternate backend claims success without honoring the contract.
        return ACExecutionResult(ac_index=0, ac_content="ac", success=True, session_id="alt-sess")

    executor._run_single_ac_on_backend = alt_reports_success  # type: ignore[method-assign]

    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )

    # The alternate reported success, but 'verify_command: exit 1' must gate it
    # to FAILED just like a same-runtime success — no contract bypass.
    assert isinstance(results[0], ACExecutionResult)
    assert results[0].success is False
    assert results[0].outcome == ACExecutionOutcome.FAILED
    assert "Verify gate failed" in (results[0].error or "")


@pytest.mark.asyncio
async def test_apply_verify_gate_contract_less_is_noop(tmp_path: Any) -> None:
    """A description-only AC (no verify_command) is byte-identical to today."""
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs("plain string AC")
    result = ACExecutionResult(ac_index=0, ac_content="plain string AC", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result


@pytest.mark.asyncio
async def test_apply_verify_gate_disabled_is_noop(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path), run_verify_commands=False)
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))
    result = ACExecutionResult(ac_index=0, ac_content="ac", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result


@pytest.mark.asyncio
async def test_apply_verify_gate_skips_already_failed(tmp_path: Any) -> None:
    """No double-fail: an already-failed AC is not re-gated (one root cause)."""
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 1"))
    result = ACExecutionResult(ac_index=0, ac_content="ac", success=False, error="already failed")

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated is result


# ---------------------------------------------------------------------------
# V3 retry — _run_batch_with_verify_and_retry
# ---------------------------------------------------------------------------


def _fail(ac_index: int, failure_class: str) -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content="ac",
        success=False,
        error="boom",
        outcome=ACExecutionOutcome.FAILED,
        atomic_verifier_verdict=VerifierVerdict(
            passed=False, reasons=("boom",), failure_class=failure_class
        ),
    )


def _ok(ac_index: int) -> ACExecutionResult:
    return ACExecutionResult(ac_index=ac_index, ac_content="ac", success=True)


async def _run_retry(executor: ParallelACExecutor, seed: Seed) -> list[Any]:
    return await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )


@pytest.mark.asyncio
async def test_retry_redispatches_and_exhausts(tmp_path: Any) -> None:
    executor = _make_executor(
        working_directory=str(tmp_path), run_verify_commands=False, ac_retry_attempts=2
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        # Distinct classes each attempt so early-stop does not trigger.
        cls = ["EVIDENCE_MISSING", "STALL", "SCOPE_CREEP"][len(calls) - 1]
        return [_fail(0, cls)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    # initial + 2 retries = 3 dispatches; counter incremented to the limit.
    assert calls == [[0], [0], [0]]
    assert ac_retry_attempts[0] == 2
    assert results[0].success is False


@pytest.mark.asyncio
async def test_retry_early_stop_on_identical_failure_class(tmp_path: Any) -> None:
    executor = _make_executor(
        working_directory=str(tmp_path), run_verify_commands=False, ac_retry_attempts=2
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")]  # identical class every time

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    # Initial dispatch + a single retry that returns the identical class stops
    # early rather than burning the last attempt (2 dispatches, not 3).
    assert calls == [[0], [0]]
    assert ac_retry_attempts[0] == 1


@pytest.mark.asyncio
async def test_retry_reaches_pending_native_model_escalation(tmp_path: Any) -> None:
    adapter = _StubAdapter(str(tmp_path))
    adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=ParamSupport.NATIVE,
    )
    executor = ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        ac_retry_attempts=2,
        model_router=ModelRouter(
            tier_models={
                "frugal": "haiku-x",
                "standard": "sonnet-x",
                "frontier": "opus-x",
            },
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=2,
        ),
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    assert calls == [[0], [0], [0]]
    assert ac_retry_attempts[0] == 2


def _native_escalation_executor(tmp_path: Any, *, ac_retry_attempts: int) -> ParallelACExecutor:
    """A verify-off executor whose adapter enforces model overrides natively and
    whose router escalates from the ``standard`` base at retry threshold 2.

    Shared by the ladder-walk regressions below. The three-tier ladder plus a
    NATIVE ``model_override_support`` are exactly the conditions the retry loop's
    ``pending_enforced_escalation`` branch requires before it lets an identical
    failure class keep dispatching instead of early-stopping.
    """
    adapter = _StubAdapter(str(tmp_path))
    adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=ParamSupport.NATIVE,
    )
    return ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        ac_retry_attempts=ac_retry_attempts,
        model_router=ModelRouter(
            tier_models={"frugal": "haiku-x", "standard": "sonnet-x", "frontier": "opus-x"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            escalation_retry_threshold=2,
        ),
    )


@pytest.mark.asyncio
async def test_retry_top_level_walks_whole_ladder_to_frontier(tmp_path: Any) -> None:
    """Executor-level pin for the ``escalation_threshold`` doc claim
    (docs/config-reference.md) that "a persistently failing unit walks the whole
    ladder rather than stalling one tier up" — the early-stop truncation is part
    of the *effective* runtime contract, not just the pure routing policy.

    ac_retry_attempts=3, threshold=2, identical failure class every attempt. A
    TOP-LEVEL unit starts at ``standard`` and, under the model ladder, would be
    routed ``standard`` (retry 0) -> ``standard`` (retry 1) -> ``frontier``
    (retry 2). The retry loop must:
      * defeat early-stop while a stronger tier is still pending (retry 1's
        next attempt is ``frontier``), so it keeps dispatching, and
      * resume early-stop once the frontier ceiling is dispatched — retry 3
        would still be ``frontier`` (no escalation pending beyond the cap), so a
        4th identical-class attempt must NOT be burned.

    So exactly 3 dispatches occur and the final one lands at ``frontier``.
    """
    executor = _native_escalation_executor(tmp_path, ac_retry_attempts=3)
    router = executor._model_router
    assert router is not None
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []
    routed_tiers: list[str | None] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        # Mirror the production seam: the tier a top-level unit would be routed
        # to for this same dispatch is a pure function of the retry_attempt the
        # loop advanced before dispatching.
        routed_tiers.append(
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=False,
                retry_attempt=kwargs["ac_retry_attempts"][0],
            ).tier
        )
        return [_fail(0, "EVIDENCE_MISSING")]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    # Ladder walked to the ceiling, then early-stop resumed: no 4th burn.
    assert calls == [[0], [0], [0]]
    assert ac_retry_attempts[0] == 2
    assert routed_tiers == ["standard", "standard", "frontier"]


@pytest.mark.asyncio
async def test_retry_decomposed_child_reaches_retry3_frontier(tmp_path: Any) -> None:
    """A decomposed CHILD (routed one tier below top-level) must also walk its
    whole ladder to ``frontier`` — the finding's expectation.

    The batch retry loop carries top-level indices; the child start tier is not a
    loop input but a property of the routing seam (``resolve_execute_model`` /
    ``decide_model`` with ``is_decomposed_child=True``). A decomposed parent
    re-runs its children — routed one tier cheaper and sharing the parent's retry
    counter — on every retry, so the early-stop predicate reads ``is_decomposed``
    off the dispatched result and probes the CHILD ladder for a pending escalation.
    This mirrors reality by returning a decomposed failing result and computing the
    child tier the loop's per-attempt ``retry_attempt`` would route to, exactly as
    the executor does inside ``_execute_single_ac``.

    ac_retry_attempts=3, threshold=2, identical failure class every attempt. The
    child ladder is frugal, frugal, standard, frontier (retry 0..3), so reaching
    ``frontier`` requires a 4th dispatch at retry 3. The ladder-truth predicate
    keeps dispatching while the next retry resolves to a stronger enforced model
    and resumes early-stop only once the frontier ceiling is reached.
    """
    executor = _native_escalation_executor(tmp_path, ac_retry_attempts=3)
    router = executor._model_router
    assert router is not None
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []
    routed_tiers: list[str | None] = []

    def _decomposed_fail() -> ACExecutionResult:
        # A decomposed parent whose children (routed one tier cheaper) failed:
        # the predicate requires both child status and a trusted split record.
        base = _fail(0, "EVIDENCE_MISSING")
        return replace(
            base,
            is_decomposed=True,
            decomposition_decision=DecompositionDecisionRecord(
                node_id="trusted-decomposed-parent",
                source=DecompositionSource.PREFLIGHT,
                disposition=DecompositionDisposition.SPLIT,
                children=(
                    DecompositionChild("child a", ("scope a",), "verify a"),
                    DecompositionChild("child b", ("scope b",), "verify b"),
                ),
                structural_status=StructuralCheckStatus.PASSED,
                semantic_status=SemanticAttestationStatus.ESTABLISHED,
                trustworthy=True,
            ),
        )

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        routed_tiers.append(
            decide_model(
                ParamSupport.NATIVE,
                router=router,
                is_decomposed_child=True,
                decomposition_trustworthy=True,
                retry_attempt=kwargs["ac_retry_attempts"][0],
            ).tier
        )
        return [_decomposed_fail()]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    # The child is re-dispatched through retry 3 so its ladder reaches the
    # frontier ceiling despite the repeated failure class.
    assert calls == [[0], [0], [0], [0]]
    assert routed_tiers[-1] == "frontier"


@pytest.mark.asyncio
async def test_retry_non_native_runtime_plain_early_stops(tmp_path: Any) -> None:
    """A router whose model override is only advised (not NATIVE) cannot enforce a
    stronger model, so the ladder-truth probe must degrade to the plain early-stop:
    an identical failure class stops at 2 dispatches even though the tier ladder
    WOULD escalate on the next retry under a native runtime.
    """
    adapter = _StubAdapter(str(tmp_path))
    adapter.capabilities = RuntimeCapabilities(
        skill_dispatch=True,
        targeted_resume=True,
        structured_output=True,
        model_override_support=ParamSupport.TRANSLATED,
    )
    executor = ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        ac_retry_attempts=2,
        model_router=ModelRouter(
            tier_models={"frugal": "haiku-x", "standard": "sonnet-x", "frontier": "opus-x"},
            runtime_backend="claude",
            child_tier="frugal",
            base_tier="standard",
            # Threshold 1: under NATIVE the retry-1 dispatch would escalate to
            # ``frontier`` and defeat early-stop — the ADVISED guard must suppress that.
            escalation_retry_threshold=1,
        ),
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    assert calls == [[0], [0]]
    assert ac_retry_attempts[0] == 1


@pytest.mark.asyncio
async def test_retry_succeeds_before_dependents(tmp_path: Any) -> None:
    executor = _make_executor(
        working_directory=str(tmp_path), run_verify_commands=False, ac_retry_attempts=2
    )
    seed = _seed_with_specs("ac")
    ac_retry_attempts = {0: 0}
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")] if len(calls) == 1 else [_ok(0)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts=ac_retry_attempts,
        execution_counters=None,
    )

    assert calls == [[0], [0]]
    assert results[0].success is True


@pytest.mark.asyncio
async def test_no_retry_when_attempts_zero(tmp_path: Any) -> None:
    executor = _make_executor(
        working_directory=str(tmp_path), run_verify_commands=False, ac_retry_attempts=0
    )
    seed = _seed_with_specs("ac")
    calls: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        calls.append(list(kwargs["batch_indices"]))
        return [_fail(0, "EVIDENCE_MISSING")]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

    await _run_retry(executor, seed)

    assert calls == [[0]]


# ---------------------------------------------------------------------------
# V4 lateral directive — _build_ac_retry_prompt
# ---------------------------------------------------------------------------


def test_retry_prompt_final_attempt_carries_lateral_directive() -> None:
    executor = _make_executor()
    result = _fail(0, "EVIDENCE_MISSING")

    final = executor._build_ac_retry_prompt(
        result=result, ac_content="build the thing", is_final_attempt=True
    )
    interim = executor._build_ac_retry_prompt(
        result=result, ac_content="build the thing", is_final_attempt=False
    )

    assert "Change of Approach" in final
    assert "EVIDENCE_MISSING" in final
    assert "Change of Approach" not in interim


def test_retry_prompt_redacts_secret_like_failure_values() -> None:
    executor = _make_executor()
    long_secret = "s" * 505
    result = ACExecutionResult(
        ac_index=0,
        ac_content="build the thing",
        success=False,
        error=(
            f"provider failed with password=hunter2 and API_KEY=secret-value token={long_secret}"
        ),
    )

    prompt = executor._build_ac_retry_prompt(
        result=result,
        ac_content="build the thing",
        is_final_attempt=False,
    )

    assert "hunter2" not in prompt
    assert "secret-value" not in prompt
    assert long_secret[-100:] not in prompt
    assert prompt.count("[REDACTED]") == 3


def test_retry_prompt_uses_trace_facts_without_hidden_contract_values() -> None:
    executor = _make_executor()
    assertion = "PRIVATE_SENTINEL"
    command = "python hidden_grader.py"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command=command,
        expected_artifacts=("dist/result.json",),
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason="output_assertion not satisfied by verify_command output",
        output_tail=f"actual result; expected context echoed {assertion}",
        missing_artifacts=("dist/result.json",),
    )
    manifest = EvidenceManifest(
        ac_id="ac_0",
        entries=(
            EvidenceEntry(
                kind=EvidenceKind.COMMAND_EXECUTED,
                ok=False,
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                payload={"tool_name": "Bash", "args_preview": "uv run pytest tests/unit"},
                source_event_ids=("event-1",),
            ),
            EvidenceEntry(
                kind=EvidenceKind.FILE_MODIFIED,
                ok=True,
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                payload={"tool_name": "Write", "args_preview": "dist/result.json"},
                source_event_ids=("event-2",),
            ),
        ),
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        error=f"Verify gate failed: assertion {assertion}; command {command}",
        verify_gate_outcome=outcome,
    )

    prompt = executor._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        manifest=manifest,
        spec=spec,
    )

    assert "dist/result.json" in prompt
    assert "uv run pytest tests/unit" in prompt
    assert "File operation observed (succeeded)" in prompt
    assert assertion not in prompt
    assert command not in prompt


@pytest.mark.parametrize(
    ("assertion", "transformed"),
    (
        ("MIGRATION_COMPLETE_v2", "MIGRATION_COMPLETE_\nv2"),
        ("MIGRATION_COMPLETE_v2", "+ MIGRATION_COMPLETE_\n+ v2"),
        ("MIGRATION_COMPLETE_v2", "\x1b[31mMIGRATION\x1b[0m_COMPLETE_v2"),
        ("MIGRATION_COMPLETE_v2", "migration_complete_V2"),
        ("MIGRATION<COMPLETE>&v2", "MIGRATION&lt;COMPLETE&gt;&amp;v2"),
        (
            "MIGRATION_COMPLETE_v2",
            "E   assert 'MIGRATION_COMPLETE_' +\nE       'v2'",
        ),
    ),
)
def test_retry_prompt_drops_transformed_hidden_assertion(
    assertion: str,
    transformed: str,
) -> None:
    executor = _make_executor()
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail=transformed,
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )

    prompt = executor._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )
    assert "Harness verification output" not in prompt
    assert transformed not in prompt


@pytest.mark.parametrize(
    ("assertion", "transformed"),
    (
        ("<=>", "&amp;amp;amp;lt;=&amp;amp;amp;gt;"),
        ("!!!", "!\u200b!\u200b!"),
        ("<=>", "<\u2060=\u2060>"),
        ("!!!", "!\ufe0f!\ufe0f!"),
        ("<=>", "&" + "amp;" * 65 + "lt;=&" + "amp;" * 65 + "gt;"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\u200bSENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\U0000200bSENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\uFE0FSENTINEL"),
        ("PRIVATE_SENTINEL", "PRIVATE_&#27;[31mSENTINEL"),
        ("PRIVATE_SENTINEL", "PRIVATE_&#27;[5DSENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\u0053ENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\x53ENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\UFFFFFFFFSENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\u005cu0053ENTINEL"),
        ("<=>", r"\x26lt;=\x26gt;"),
        ("ERROR", r"E\nRROR"),
        ("+++", r"+\n+\n+"),
        ("café", r"b'caf\xc3\xa9'"),
        ("PRIVATE_SENTINEL", "\x1b]0;PRIVATE_SENTINEL\x07"),
        ("!!!", "!\x7f!!"),
        ("PRIVATE", "＼ｘ５０＼ｘ５２＼ｘ４９＼ｘ５６＼ｘ４１＼ｘ５４＼ｘ４５"),
        ("PRIVATE", r"\120\122\111\126\101\124\105"),
        ("😀", r"\ud83d\ude00"),
        ("PRIVATE_SENTINEL", "PRIVATE%5FSENTINEL"),
        ("PRIVATE_SENTINEL", "PRIVATE%255FSENTINEL"),
        ("PRIVATE_SENTINEL", "%50%52%49%56%41%54%45%5F%53%45%4E%54%49%4E%45%4C"),
        ("PRIVATE_SENTINEL", "PRIVATE&#37;5FSENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\400SENTINEL"),
        ("SECRET", "\u202eTERCES\u202c"),
        ("PRIVATE_SENTINEL", "PRIVATE_&#x110000;SENTINEL"),
        ("PRIVATE_SENTINEL", "PRIVATE_&#xD800;SENTINEL"),
        ("PRIVATE_SENTINEL", "PRIVATE_&#999999999999999999999;SENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\xG0SENTINEL"),
        ("PRIVATE_SENTINEL", "PRIVATE_%G0SENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\uZZZZSENTINEL"),
        ("PRIVATE_SENTINEL", "PRIVATE_&#xZZ;SENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\12SENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\11SENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\15SENTINEL"),
        ("PRIVATE_SENTINEL", r"PRIVATE_\0SENTINEL"),
        ("PRIVATE_SENTINEL", "safe\ud800diagnostic"),
    ),
)
def test_retry_prompt_drops_deep_entities_and_invisible_formats(
    assertion: str,
    transformed: str,
) -> None:
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(passed=False, reason=None, output_tail=transformed)
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )
    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )
    assert "Harness verification output" not in prompt


@pytest.mark.parametrize(
    ("assertion", "transformed"),
    (
        ("café", "cafe\u0301"),
        ("ＳＥＣＲＥＴ", "SECRET"),
        ("MIGRATION<COMPLETE>", "MIGRATION&amp;lt;COMPLETE&amp;gt;"),
        ("PRİVATE_SENTINEL", "private_sentinel"),
    ),
)
def test_retry_prompt_drops_unicode_and_nested_entity_equivalents(
    assertion: str,
    transformed: str,
) -> None:
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(passed=False, reason=None, output_tail=transformed)
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )
    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )
    assert "Harness verification output" not in prompt


@pytest.mark.parametrize("escaped_whitespace", (r"\n", r"\r", r"\t", r"\x0a", r"\u0009"))
def test_retry_prompt_drops_pytest_escaped_whitespace(escaped_whitespace: str) -> None:
    assertion = "PRIVATE_SENTINEL"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail=f"E assert 'PRIVATE_{escaped_whitespace}SENTINEL'",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )
    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )
    assert "Harness verification output" not in prompt


@pytest.mark.parametrize(
    "escaped_control",
    (r"\x1b[31m", r"\u001b[31m", r"\033[31m", r"\e[31m", r"\^[31m"),
)
def test_retry_prompt_drops_pytest_escaped_terminal_controls(escaped_control: str) -> None:
    assertion = "PRIVATE_SENTINEL"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail=f"E assert 'PRIVATE_{escaped_control}SENTINEL'",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )
    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )
    assert "Harness verification output" not in prompt


@pytest.mark.parametrize("control", ("\x9b31m", "\x1b(B", "\x1b[5D", "\b"))
def test_retry_prompt_drops_residual_terminal_controls(control: str) -> None:
    assertion = "PRIVATE_SENTINEL"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail=f"PRIVATE_{control}SENTINEL",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )
    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )
    assert "Harness verification output" not in prompt


@pytest.mark.parametrize(
    "control",
    ("\x1bP1;2\x1b\\", "\x90payload\x9c"),
)
def test_retry_prompt_drops_unsupported_terminal_control_strings(control: str) -> None:
    assertion = "PRIVATE_SENTINEL"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail=f"PRIVATE_{control}SENTINEL",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )

    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )

    assert "Harness verification output" not in prompt


def test_retry_prompt_drops_osc_split_hidden_assertion() -> None:
    assertion = "PRIVATE_SENTINEL"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    osc = "\x1b]8;;https://example.invalid\x07"
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail=f"{osc}PRIVATE_{osc}\x1b]8;;\x07SENTINEL",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )

    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )

    assert "Harness verification output" not in prompt


def test_retry_prompt_drops_mixed_exact_and_transformed_hidden_copies() -> None:
    assertion = "PRIVATE_SENTINEL"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail=f"expected {assertion} but received private_ sentinel",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )

    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )

    assert "Harness verification output" not in prompt


def test_retry_prompt_preserves_safe_context_around_exact_hidden_value() -> None:
    assertion = "PRIVATE_SENTINEL"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail=f"actual result; expected {assertion}",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )

    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )

    assert "actual result; expected [REDACTED CONTRACT VALUE]" in prompt


@pytest.mark.parametrize(
    ("assertion", "transformed"),
    (("!!!", "! ! !"), ("<=>", "&lt; = &gt;")),
)
def test_retry_prompt_drops_transformed_punctuation_only_assertion(
    assertion: str,
    transformed: str,
) -> None:
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command="python hidden_grader.py",
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(passed=False, reason=None, output_tail=transformed)
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )

    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )

    assert "Harness verification output" not in prompt


def test_retry_prompt_drops_transformed_command_with_exact_assertion() -> None:
    assertion = "PRIVATE_SENTINEL"
    command = "python hidden_grader.py --expect PRIVATE_SENTINEL"
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command=command,
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason=None,
        output_tail="PYTHON HIDDEN_GRADER.PY --EXPECT PRIVATE_SENTINEL",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )

    prompt = _make_executor()._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )

    assert "Harness verification output" not in prompt
    assert "HIDDEN_GRADER" not in prompt


@pytest.mark.parametrize(
    "render_hidden",
    [
        pytest.param(lambda value: value, id="raw"),
        pytest.param(repr, id="quoted"),
        pytest.param(lambda value: json.dumps(value)[1:-1], id="escaped"),
    ],
)
def test_retry_prompt_redacts_overlapping_hidden_command_before_assertion(
    render_hidden,
) -> None:
    executor = _make_executor()
    assertion = "PRIVATE_SENTINEL"
    command = 'python hidden_grader.py --expect "PRIVATE_SENTINEL"'
    spec = AcceptanceCriterionSpec(
        description="build the thing",
        verify_command=command,
        output_assertion=assertion,
    )
    outcome = _VerifyGateOutcome(
        passed=False,
        reason="output_assertion not satisfied by verify_command output",
        output_tail=f"grader invocation: {render_hidden(command)}",
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content=spec.description,
        success=False,
        verify_gate_outcome=outcome,
    )

    prompt = executor._build_ac_retry_prompt(
        result=result,
        ac_content=spec.description,
        is_final_attempt=False,
        spec=spec,
    )

    assert "hidden_grader.py" not in prompt
    assert "--expect" not in prompt
    assert assertion not in prompt


# ---------------------------------------------------------------------------
# V4 trust leaks — sibling flip gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_sibling_flip_gated_out_blocks_failing_contract(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        "sibling did work",
        AcceptanceCriterionSpec(description="contract", verify_command="exit 1"),
        AcceptanceCriterionSpec(description="passing", verify_command="exit 0"),
        "plain",
    )
    level_results = [
        ACExecutionResult(ac_index=0, ac_content="sibling did work", success=True),
        ACExecutionResult(
            ac_index=1, ac_content="contract", success=False, outcome=ACExecutionOutcome.FAILED
        ),
        ACExecutionResult(
            ac_index=2, ac_content="passing", success=False, outcome=ACExecutionOutcome.FAILED
        ),
        ACExecutionResult(
            ac_index=3, ac_content="plain", success=False, outcome=ACExecutionOutcome.FAILED
        ),
    ]

    gated = await executor._compute_sibling_flip_gated_out(
        seed=seed, level_results=level_results, session_id="s", execution_id="e"
    )

    # AC 1's verify fails → gated out; AC 2 passes → allowed; AC 3 has no
    # contract → never gated.
    assert gated == frozenset({1})


@pytest.mark.asyncio
async def test_sibling_flip_reuses_cached_failed_verify_gate(tmp_path: Any) -> None:
    counter = tmp_path / "verify-count.txt"
    command = (
        "python3 -c \"from pathlib import Path; p=Path('verify-count.txt'); "
        "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
        'raise SystemExit(1)"'
    )
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="contract", verify_command=command))
    result = ACExecutionResult(ac_index=0, ac_content="contract", success=True)

    failed = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )
    assert failed.success is False
    assert counter.read_text(encoding="utf-8") == "1"

    gated = await executor._compute_sibling_flip_gated_out(
        seed=seed, level_results=[failed], session_id="s", execution_id="e"
    )

    assert gated == frozenset({0})
    assert counter.read_text(encoding="utf-8") == "1"


def test_sibling_flip_respects_gated_out(tmp_path: Any) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hello_auto.py").write_text("def test_hello(): pass\n")
    from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
    from ouroboros.orchestrator.evidence_schema import EvidenceRecord

    success = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` defines `hello_auto()`.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="write test",
                tool_name="Write",
                data={"tool_input": {"file_path": "tests/test_hello_auto.py"}},
            ),
        ),
        typed_evidence=EvidenceRecord(data={"files_touched": ["tests/test_hello_auto.py"]}),
        runtime_handle=RuntimeHandle(backend="codex_cli", cwd=str(tmp_path)),
    )
    failed = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="not done separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    # Without gating, the failed AC is flipped to satisfied by sibling evidence.
    _, _, _, open_results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )
    assert open_results[1].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY

    # With AC 1 gated out (its own verify_command did not pass), it stays FAILED.
    _, _, _, gated_results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
        flip_gated_out=frozenset({1}),
    )
    assert gated_results[1].outcome == ACExecutionOutcome.FAILED


# ---------------------------------------------------------------------------
# V4 trust leaks — --skip-completed gate + verification_status stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_completed_stamps_assumed_for_contract_less(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    seed = _seed_with_specs("plain AC")
    executor = _make_executor(working_directory=str(tmp_path))
    executor._execute_ac_batch = AsyncMock(return_value=[])  # type: ignore[method-assign]
    graph = DependencyGraph(
        nodes=(ACNode(index=0, content="plain AC", depends_on=()),),
        execution_levels=((0,),),
    )

    result = await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="s",
        execution_id="e",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="sys",
        externally_satisfied_acs={0: {"reason": "done manually"}},
    )

    assert result.externally_satisfied_count == 1
    assert "verification_status=assumed" in result.results[0].final_message


@pytest.mark.asyncio
async def test_skip_completed_executes_when_verify_gate_fails(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="contract AC", verify_command="exit 1")
    )
    executor = _make_executor(working_directory=str(tmp_path))
    dispatched: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        dispatched.append(list(kwargs["batch_indices"]))
        return [ACExecutionResult(ac_index=0, ac_content="contract AC", success=True)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    graph = DependencyGraph(
        nodes=(ACNode(index=0, content="contract AC", depends_on=()),),
        execution_levels=((0,),),
    )

    await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="s",
        execution_id="e",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="sys",
        externally_satisfied_acs={0: {"reason": "claims done"}},
    )

    # The failing verify gate forced normal execution instead of skipping.
    assert dispatched == [[0]]


# ---------------------------------------------------------------------------
# V1 gate — expected_artifacts enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifacts_only_gate_passes_when_files_exist(tmp_path: Any) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("guide\n")
    (tmp_path / "Build Outputs").mkdir()
    (tmp_path / "README.md").write_text("readme\n")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="docs exist",
        expected_artifacts=("README.md", "docs/guide.md", "docs", "./Build Outputs"),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert outcome.missing_artifacts == ()


@pytest.mark.asyncio
async def test_artifacts_only_gate_reports_all_missing(tmp_path: Any) -> None:
    (tmp_path / "present.md").write_text("here\n")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(
        description="docs exist",
        expected_artifacts=("present.md", "absent-one.md", "absent/two.md"),
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.missing_artifacts == ("absent-one.md", "absent/two.md")
    assert "absent-one.md" in (outcome.reason or "")
    assert "absent/two.md" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_artifacts_only_gate_rejects_nonportable_constructed_paths(
    tmp_path: Any,
) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "summary,v2.json").write_text("{}\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("guide\n")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec.model_construct(
        description="docs exist",
        verify_command=None,
        expected_artifacts=("reports/summary,v2.json", r"docs\guide.md"),
        output_assertion=None,
        investment=None,
        semantic_ac_key=None,
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert len(outcome.missing_artifacts) == 2
    assert "contains a comma" in outcome.missing_artifacts[0]
    assert "contains a backslash" in outcome.missing_artifacts[1]


@pytest.mark.asyncio
async def test_artifacts_only_gate_rejects_overlong_constructed_path_component(
    tmp_path: Any,
) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec.model_construct(
        description="artifact exists",
        verify_command=None,
        expected_artifacts=("a" * 256,),
        output_assertion=None,
        investment=None,
        semantic_ac_key=None,
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.missing_artifacts == (
        f"{'a' * 256!r} (contains a path component longer than 255 filesystem bytes)",
    )


@pytest.mark.asyncio
async def test_artifact_path_escape_is_treated_as_missing(tmp_path: Any) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The escape target EXISTS outside the workspace — it still must not count.
    (tmp_path / "outside.md").write_text("outside\n")
    executor = _make_executor(working_directory=str(workspace))
    relative_escape = AcceptanceCriterionSpec.model_construct(
        description="escape",
        verify_command=None,
        expected_artifacts=("../outside.md",),
        output_assertion=None,
        investment=None,
        semantic_ac_key=None,
    )
    absolute_escape = AcceptanceCriterionSpec.model_construct(
        description="escape",
        verify_command=None,
        expected_artifacts=(str(tmp_path / "outside.md"),),
        output_assertion=None,
        investment=None,
        semantic_ac_key=None,
    )

    for spec in (relative_escape, absolute_escape):
        outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(workspace))
        assert outcome.passed is False
        assert len(outcome.missing_artifacts) == 1
        assert "escapes workspace" in outcome.missing_artifacts[0]


@pytest.mark.asyncio
async def test_combined_contract_fails_when_either_leg_fails(tmp_path: Any) -> None:
    (tmp_path / "artifact.md").write_text("built\n")
    executor = _make_executor(working_directory=str(tmp_path))

    command_ok_artifact_missing = AcceptanceCriterionSpec(
        description="combined",
        verify_command="exit 0",
        expected_artifacts=("missing.md",),
    )
    artifact_ok_command_fails = AcceptanceCriterionSpec(
        description="combined",
        verify_command="exit 1",
        expected_artifacts=("artifact.md",),
    )
    both_ok = AcceptanceCriterionSpec(
        description="combined",
        verify_command="exit 0",
        expected_artifacts=("artifact.md",),
    )

    missing_leg = await executor._run_ac_verify_gate(
        spec=command_ok_artifact_missing, cwd=str(tmp_path)
    )
    assert missing_leg.passed is False
    assert missing_leg.missing_artifacts == ("missing.md",)

    command_leg = await executor._run_ac_verify_gate(
        spec=artifact_ok_command_fails, cwd=str(tmp_path)
    )
    assert command_leg.passed is False
    assert "status 1" in (command_leg.reason or "")

    assert (await executor._run_ac_verify_gate(spec=both_ok, cwd=str(tmp_path))).passed is True


@pytest.mark.asyncio
async def test_apply_verify_gate_fails_artifacts_only_ac(tmp_path: Any) -> None:
    """An artifacts-only contract (verify: NONE) is enforced, not decorative."""
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="docs AC", expected_artifacts=("docs/out.md",))
    )
    result = ACExecutionResult(ac_index=0, ac_content="docs AC", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated.success is False
    assert gated.outcome == ACExecutionOutcome.FAILED
    assert "expected_artifacts missing" in (gated.error or "")
    assert gated.atomic_verifier_verdict is not None
    assert gated.atomic_verifier_verdict.failure_class == "EVIDENCE_MISSING"

    # And with the artifact present the same AC passes with durable evidence.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "out.md").write_text("done\n")
    passed = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )
    assert passed.success is True
    assert passed.verify_gate_outcome is not None


@pytest.mark.asyncio
async def test_final_workspace_revalidation_defers_command_replay_to_settlement(
    tmp_path: Any,
) -> None:
    """Coordinator revalidation runs no command; settlement replays it once.

    The coordinator has drained by the time this boundary runs, so the
    quiescent-workspace replay that settlement already performs is the
    authoritative re-judgment. Revalidation only marks the cached pass as
    requiring that replay.
    """
    counter = tmp_path.parent / f"coordinator-defer-count-{tmp_path.name}.txt"
    command = (
        'python3 -c "from pathlib import Path; '
        f"p=Path({str(counter)!r}); "
        "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
        'raise SystemExit(0)"'
    )
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command=command))
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    assert cached.passed is True
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ac",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
        verify_gate_outcome=cached,
    )

    revalidated = await executor._revalidate_results_after_coordinator(
        seed=seed,
        results=[result],
        session_id="s",
        execution_id="e",
    )

    assert counter.read_text(encoding="utf-8") == "1"
    assert revalidated[0].success is True
    assert revalidated[0].verify_gate_outcome is not None
    assert revalidated[0].verify_gate_outcome.replay_required is True

    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=revalidated,
        session_id="s",
        execution_id="e",
    )

    assert counter.read_text(encoding="utf-8") == "2"
    assert settled[0].success is True
    assert settled[0].outcome is ACExecutionOutcome.SUCCEEDED


@pytest.mark.asyncio
async def test_final_verify_mutation_invalidates_prior_successes(tmp_path: Any) -> None:
    """A mutating verifier is caught by the settlement replay, not skipped.

    Coordinator revalidation defers command legs; the replay on the quiescent
    workspace then observes the side effect and invalidates every provisional
    success, including the stable sibling.
    """
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="stable", verify_command="exit 0"),
        AcceptanceCriterionSpec(
            description="mutating verifier",
            verify_command=(
                'python3 -c "from pathlib import Path; '
                "Path('verify-side-effect.txt').write_text('side effect')\""
            ),
        ),
    )
    results = [
        ACExecutionResult(
            ac_index=0,
            ac_content="stable",
            success=True,
            outcome=ACExecutionOutcome.SUCCEEDED,
        ),
        ACExecutionResult(
            ac_index=1,
            ac_content="mutating verifier",
            success=True,
            outcome=ACExecutionOutcome.SUCCEEDED,
        ),
    ]

    revalidated = await executor._revalidate_results_after_coordinator(
        seed=seed,
        results=results,
        session_id="s",
        execution_id="e",
    )
    assert all(result.success for result in revalidated)
    assert all(
        result.verify_gate_outcome is not None and result.verify_gate_outcome.replay_required
        for result in revalidated
    )

    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=revalidated,
        session_id="s",
        execution_id="e",
    )

    assert [result.outcome for result in settled] == [
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.FAILED,
    ]
    assert all("mutated the workspace" in (result.error or "") for result in settled)


@pytest.mark.asyncio
@pytest.mark.parametrize("final_condition", ["before", "after"])
async def test_final_settlement_replays_stale_command_against_final_workspace(
    tmp_path: Any,
    final_condition: str,
) -> None:
    """A sibling's later edit does not discard this AC: its contract is re-judged.

    The verify gate already rejects a command that mutates the workspace, so a
    passing contract is an observation and may be run once more at settlement.
    The final verdict follows that replay in both directions.
    """
    counter = tmp_path.parent / f"final-settlement-count-{tmp_path.name}.txt"
    target = tmp_path / "condition.txt"
    target.write_text("before", encoding="utf-8")
    command = (
        'python3 -c "from pathlib import Path; '
        f"counter=Path({str(counter)!r}); "
        "n=int(counter.read_text()) if counter.exists() else 0; "
        "counter.write_text(str(n+1)); "
        "raise SystemExit(0 if Path('condition.txt').read_text() == 'before' else 7)\""
    )
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command=command))
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    assert cached.passed is True
    # A later worker touches the workspace after the command passed.
    (tmp_path / "sibling.txt").write_text("edited by a later AC", encoding="utf-8")
    target.write_text(final_condition, encoding="utf-8")

    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="ac",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=cached,
            )
        ],
        session_id="s",
        execution_id="e",
    )

    assert counter.read_text(encoding="utf-8") == "2"
    if final_condition == "before":
        assert settled[0].success is True
        assert settled[0].outcome is ACExecutionOutcome.SUCCEEDED
        assert settled[0].verify_gate_outcome is not None
        assert settled[0].verify_gate_outcome.passed is True
    else:
        assert settled[0].success is False
        assert settled[0].outcome is ACExecutionOutcome.FAILED
        assert "verify_command failed on the final workspace" in (settled[0].error or "")


@pytest.mark.asyncio
async def test_settlement_replay_mutation_invalidates_every_provisional_success(
    tmp_path: Any,
) -> None:
    """A replay that mutates the workspace poisons the whole success set.

    The first run of the command leaves the workspace untouched and passes;
    the replay (triggered by a sibling edit) writes a side-effect file. That
    mutation must fold into the settlement-wide state and reject every
    provisional success — including a contract-less sibling — not just add an
    individual failure for the replayed AC.
    """
    counter = tmp_path.parent / f"settle-mutating-replay-{tmp_path.name}.txt"
    command = (
        'python3 -c "from pathlib import Path; '
        f"counter=Path({str(counter)!r}); "
        "n=int(counter.read_text()) if counter.exists() else 0; "
        "counter.write_text(str(n+1)); "
        "n and Path('replay-side-effect.txt').write_text('mutated'); "
        'raise SystemExit(0)"'
    )
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="contract ac", verify_command=command),
        AcceptanceCriterionSpec(description="plain sibling"),
    )
    cached = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    assert cached.passed is True
    # A later worker changes the workspace after the command passed.
    (tmp_path / "sibling.txt").write_text("edited by a later AC", encoding="utf-8")

    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="contract ac",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=cached,
            ),
            ACExecutionResult(
                ac_index=1,
                ac_content="plain sibling",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
            ),
        ],
        session_id="s",
        execution_id="e",
    )

    assert counter.read_text(encoding="utf-8") == "2"
    assert [result.success for result in settled] == [False, False]
    assert [result.outcome for result in settled] == [
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.FAILED,
    ]
    assert all("mutated the workspace" in (result.error or "") for result in settled)


@pytest.mark.asyncio
async def test_checkpoint_restores_verify_evidence_and_result_retry_identity(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    seed = _seed_with_specs(AcceptanceCriterionSpec(description="ac", verify_command="exit 0"))
    plan = DependencyGraph(
        nodes=(ACNode(index=0, content="ac", depends_on=()),),
        execution_levels=((0,),),
    ).to_execution_plan()
    checkpoint_store = MagicMock()
    checkpoint_store.load.return_value = type("LoadResult", (), {"is_ok": False})()
    checkpoint_store.save.return_value = type("SaveResult", (), {"is_ok": True})()
    executor = _make_executor(working_directory=str(tmp_path))
    executor._checkpoint_store = checkpoint_store
    gate = await executor._run_ac_verify_gate(spec=seed.acceptance_criteria[0], cwd=str(tmp_path))
    executor._execute_ac_batch = AsyncMock(
        return_value=[
            ACExecutionResult(
                ac_index=0,
                ac_content="ac",
                success=True,
                retry_attempt=3,
                verify_gate_outcome=gate,
            )
        ]
    )

    first = await executor.execute_parallel(
        seed=seed,
        execution_plan=plan,
        session_id="session-checkpoint-verify",
        execution_id="execution-checkpoint-verify",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
    )
    assert first.results[0].success is True
    checkpoint = checkpoint_store.save.call_args.args[0]
    assert checkpoint.state["result_retry_attempts"] == {"0": 3}
    assert checkpoint.state["verify_gate_outcomes"]["0"]["workspace_digest"] == (
        gate.workspace_digest
    )

    restore_store = MagicMock()
    restore_store.load.return_value = type("LoadResult", (), {"is_ok": True, "value": checkpoint})()
    restore_store.save.return_value = type("SaveResult", (), {"is_ok": True})()
    restored = _make_executor(working_directory=str(tmp_path))
    restored._checkpoint_store = restore_store
    restored._execute_ac_batch = AsyncMock()

    recovered = await restored.execute_parallel(
        seed=seed,
        execution_plan=plan,
        session_id="session-checkpoint-verify",
        execution_id="execution-checkpoint-verify",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
    )

    assert recovered.results[0].success is True
    assert recovered.results[0].retry_attempt == 3
    assert isinstance(recovered.results[0].verify_gate_outcome, _VerifyGateOutcome)
    restored._execute_ac_batch.assert_not_awaited()


def test_workspace_digest_includes_empty_directories(tmp_path: Any) -> None:
    empty_directory = tmp_path / "expected-artifact-directory"
    empty_directory.mkdir()
    before = ParallelACExecutor._workspace_content_digest(str(tmp_path))

    empty_directory.rmdir()

    after = ParallelACExecutor._workspace_content_digest(str(tmp_path))
    assert before is not None
    assert after is not None
    assert before != after


@pytest.mark.asyncio
async def test_cache_only_verify_finishes_acceptance_and_completed_progress(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    (tmp_path / "pkg").mkdir()
    spec = AcceptanceCriterionSpec(
        description="tests pass",
        verify_command=(
            'python3 -c "from pathlib import Path; '
            "p=Path('pkg/__pycache__/module.cpython-test.pyc'); "
            "p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'cache')\""
        ),
    )
    seed = _seed_with_specs(spec)
    plan = DependencyGraph(
        nodes=(ACNode(index=0, content="tests pass", depends_on=()),),
        execution_levels=((0,),),
    ).to_execution_plan()
    executor = _make_executor(working_directory=str(tmp_path))
    executor._execute_ac_batch = AsyncMock(
        return_value=[
            ACExecutionResult(
                ac_index=0,
                ac_content="tests pass",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
            )
        ]
    )
    executor._emit_workflow_progress = AsyncMock()

    result = await executor.execute_parallel(
        seed=seed,
        execution_plan=plan,
        session_id="cache-session",
        execution_id="cache-execution",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
    )

    assert result.all_succeeded is True
    assert result.success_count == 1
    assert result.results[0].outcome is ACExecutionOutcome.SUCCEEDED
    assert result.results[0].verify_gate_outcome is not None
    assert result.results[0].verify_gate_outcome.passed is True
    assert any(
        call.kwargs["completed_count"] == 1 and call.kwargs["ac_statuses"] == {0: "completed"}
        for call in executor._emit_workflow_progress.await_args_list
    )

    emitted = [call.args[0] for call in executor._event_store.append.await_args_list]
    judgments = [event for event in emitted if event.type == "execution.ac.attempt_judged"]
    assert len(judgments) == 1
    assert judgments[0].data["success"] is True


@pytest.mark.asyncio
async def test_final_workspace_revalidation_keeps_description_only_verdict(tmp_path: Any) -> None:
    """A coordinator write does not, by itself, fail a description-only AC.

    No deterministic contract exists to re-check, and settlement already
    leaves such ACs to the evaluate stage when later workers write. The
    coordinator is the harness's own reconciliation step, not an untrusted
    verifier, so it gets the same treatment.
    """
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs("description-only AC")
    result = ACExecutionResult(
        ac_index=0,
        ac_content="description-only AC",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
    )

    revalidated = await executor._revalidate_results_after_coordinator(
        seed=seed,
        results=[result],
        session_id="s",
        execution_id="e",
    )

    assert revalidated[0].success is True
    assert revalidated[0].outcome is ACExecutionOutcome.SUCCEEDED


@pytest.mark.asyncio
async def test_sibling_flip_gated_out_by_artifacts_only_contract(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    (tmp_path / "present.md").write_text("here\n")
    seed = _seed_with_specs(
        "sibling did work",
        AcceptanceCriterionSpec(description="missing docs", expected_artifacts=("absent.md",)),
        AcceptanceCriterionSpec(description="present docs", expected_artifacts=("present.md",)),
    )
    level_results = [
        ACExecutionResult(ac_index=0, ac_content="sibling did work", success=True),
        ACExecutionResult(
            ac_index=1, ac_content="missing docs", success=False, outcome=ACExecutionOutcome.FAILED
        ),
        ACExecutionResult(
            ac_index=2, ac_content="present docs", success=False, outcome=ACExecutionOutcome.FAILED
        ),
    ]

    gated = await executor._compute_sibling_flip_gated_out(
        seed=seed, level_results=level_results, session_id="s", execution_id="e"
    )

    assert gated == frozenset({1})


@pytest.mark.asyncio
async def test_skip_completed_gates_artifacts_only_contract(tmp_path: Any) -> None:
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="docs AC", expected_artifacts=("out.md",))
    )
    executor = _make_executor(working_directory=str(tmp_path))
    dispatched: list[list[int]] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        dispatched.append(list(kwargs["batch_indices"]))
        return [ACExecutionResult(ac_index=0, ac_content="docs AC", success=True)]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    graph = DependencyGraph(
        nodes=(ACNode(index=0, content="docs AC", depends_on=()),),
        execution_levels=((0,),),
    )

    # Missing artifact → the skip is refused and the AC executes normally.
    await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="s1",
        execution_id="e1",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="sys",
        externally_satisfied_acs={0: {"reason": "claims done"}},
    )
    assert dispatched == [[0]]

    # Present artifact → skipped and stamped verified.
    (tmp_path / "out.md").write_text("done\n")
    dispatched.clear()
    result = await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="s2",
        execution_id="e2",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="sys",
        externally_satisfied_acs={0: {"reason": "claims done"}},
    )
    assert dispatched == []
    assert result.externally_satisfied_count == 1
    assert "verification_status=verified" in result.results[0].final_message


class TestSuccessContractBlock:
    """The worker-facing SUCCESS CONTRACT block surfaced in the leaf prompt."""

    def test_none_spec_yields_empty_block(self) -> None:
        assert _build_success_contract_block(None) == ""

    def test_contract_less_spec_yields_empty_block(self) -> None:
        spec = AcceptanceCriterionSpec(description="just a description")
        assert _build_success_contract_block(spec) == ""

    def test_full_contract_hides_harness_answer_key(self) -> None:
        spec = AcceptanceCriterionSpec(
            description="build succeeds",
            verify_command="make build",
            expected_artifacts=("dist/app", "dist/app.map"),
            output_assertion="BUILD OK",
        )
        block = _build_success_contract_block(spec)
        assert block.startswith("SUCCESS CONTRACT for this AC:")
        assert (
            "- Expected artifacts: dist/app, dist/app.map — ensure they exist in the workspace"
            in block
        )
        assert "independently verifies this contract" in block
        assert "make build" not in block
        assert "BUILD OK" not in block

    def test_partial_contract_only_renders_present_fields(self) -> None:
        spec = AcceptanceCriterionSpec(description="verify only", verify_command="pytest -q")
        block = _build_success_contract_block(spec)
        assert "independently verifies this contract" in block
        assert "pytest -q" not in block
        assert "Expected artifacts" not in block
        assert "Expected output" not in block


# ---------------------------------------------------------------------------
# RFC Part A — Windows-safe verify_command execution + quarantine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_gate_runs_the_command_through_a_resolved_posix_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The gate must exec a real interpreter, not inherit the platform shell."""
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="ok", verify_command="exit 0")
    recorded: dict[str, Any] = {}

    expected_shell = verify_shell_path_from_identity(executor._verify_shell_identity)
    assert expected_shell is not None
    real_exec = asyncio.create_subprocess_exec

    async def spy_exec(*argv: str, **kwargs: Any) -> Any:
        recorded["argv"] = argv
        return await real_exec("/bin/sh", "-c", "exit 0", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is True
    assert recorded["argv"] == (expected_shell, "-c", "exit 0")


@pytest.mark.asyncio
async def test_verify_gate_quarantines_when_no_posix_shell_exists(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    # No real shell means the arbitrary pipeline is unavailable, never emulated.
    spec = AcceptanceCriterionSpec(description="ok", verify_command="echo ok | tee log")
    executor._verify_shell_identity = None

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.environment_unverifiable is True
    assert "needs bash" in (outcome.reason or "")


@pytest.mark.asyncio
async def test_unverifiable_ac_keeps_worker_success_without_retry(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="ok", verify_command="echo ok | tee log")
    seed = _seed_with_specs(spec)
    executor._verify_shell_identity = None
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ok",
        success=True,
        messages=(),
        final_message="done",
        outcome=ACExecutionOutcome.SUCCEEDED,
    )

    gated = await executor._apply_verify_gate(
        seed=seed,
        ac_index=0,
        result=result,
        session_id="s",
        execution_id="e",
    )

    assert gated.success is True
    assert gated is not result
    assert gated.verify_gate_outcome.environment_unverifiable is True
    assert gated.error is None
    assert is_retryable_failure(gated) is False


@pytest.mark.asyncio
async def test_final_settlement_preserves_unverified_success(tmp_path: Any) -> None:
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="ok", verify_command="echo ok")
    seed = _seed_with_specs(spec)
    executor._verify_shell_identity = None
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ok",
        success=True,
        final_message="done",
        outcome=ACExecutionOutcome.SUCCEEDED,
    )
    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    settled = await executor._settle_verify_gate_results(
        seed=seed,
        results=[gated],
        session_id="s",
        execution_id="e",
    )

    assert settled[0].success is True
    assert settled[0].verify_gate_outcome.environment_unverifiable is True


def test_report_surfaces_unverified_success() -> None:
    outcome = _VerifyGateOutcome(
        passed=False,
        reason="verify_command needs a POSIX shell",
        output_tail="",
        environment_unverifiable=True,
    )
    result = ACExecutionResult(
        ac_index=0,
        ac_content="ok",
        success=True,
        messages=(),
        final_message="done",
        outcome=ACExecutionOutcome.SUCCEEDED,
        verify_gate_outcome=outcome,
    )
    parallel_result = ParallelExecutionResult(
        results=[result],
        success_count=1,
        failure_count=0,
        blocked_count=0,
        skipped_count=0,
        total_duration_seconds=0.0,
    )

    report = render_parallel_verification_report(parallel_result, 1)

    assert "Success: 1/1" in report
    assert "needs confirmation: AC 1" in report


def test_completion_message_distinguishes_blocked_and_failure_reasons() -> None:
    failed = ACExecutionResult(
        ac_index=0,
        ac_content="Run the tests",
        success=False,
        error="unsupported uv evidence command",
        outcome=ACExecutionOutcome.FAILED,
    )
    blocked = ACExecutionResult(
        ac_index=1,
        ac_content="Package the result",
        success=False,
        error="Skipped: dependency failed",
        outcome=ACExecutionOutcome.BLOCKED,
    )
    parallel_result = ParallelExecutionResult(
        results=(failed, blocked),
        success_count=0,
        failure_count=1,
        blocked_count=1,
        skipped_count=1,
        total_duration_seconds=0.0,
    )

    message = render_parallel_completion_message(parallel_result, 2)

    assert "Failed: 1" in message
    assert "Blocked: 1" in message
    assert "\nSkipped:" not in message
    assert "[FAILED] Run the tests — unsupported uv evidence command" in message
    assert "[BLOCKED] Package the result — Skipped: dependency failed" in message


def test_verify_gate_outcome_roundtrips_the_quarantine_flag() -> None:
    outcome = _VerifyGateOutcome(
        passed=False,
        reason="no shell",
        output_tail="",
        environment_unverifiable=True,
    )

    payload = _serialize_verify_gate_outcome(outcome)
    assert payload is not None
    assert _deserialize_verify_gate_outcome(payload) == outcome


def test_legacy_verify_gate_checkpoints_stay_readable() -> None:
    legacy_payload = {
        "passed": True,
        "reason": None,
        "output_tail": "",
        "missing_artifacts": [],
        "workspace_mutated": False,
        "workspace_digest": None,
    }

    restored = _deserialize_verify_gate_outcome(legacy_payload)

    assert restored is not None
    assert restored.passed is True
    assert restored.environment_unverifiable is False


@pytest.mark.asyncio
async def test_a_repo_supplied_bash_startup_file_cannot_flip_a_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Bash sources `BASH_ENV` before it evaluates a `-c` command, so a file the
    repository controls would otherwise run inside the gate itself and turn a
    failing contract into a pass."""
    import shutil as _shutil

    if _shutil.which("bash") is None:  # pragma: no cover - CI always has bash
        pytest.skip("no bash on this machine")

    injected = tmp_path / "inject.sh"
    injected.write_text("exit 0\n")
    monkeypatch.setenv("BASH_ENV", str(injected))

    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec(description="fails honestly", verify_command="exit 23")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False


# ---------------------------------------------------------------------------
# Rejection-cause vocabulary (verify_cause) — machine-readable failure causes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_gate_stamps_machine_readable_causes(tmp_path: Any) -> None:
    """Each gate failure branch stamps its closed-vocabulary cause; passing
    outcomes stay unattributed. Rejection analytics must never have to parse
    the prose `reason` strings."""
    executor = _make_executor(working_directory=str(tmp_path))

    passed = await executor._run_ac_verify_gate(
        spec=AcceptanceCriterionSpec(description="ok", verify_command="exit 0"),
        cwd=str(tmp_path),
    )
    assert passed.cause is None

    nonzero = await executor._run_ac_verify_gate(
        spec=AcceptanceCriterionSpec(description="bad", verify_command="exit 3"),
        cwd=str(tmp_path),
    )
    assert nonzero.cause == "exit_nonzero"

    mismatch = await executor._run_ac_verify_gate(
        spec=AcceptanceCriterionSpec(
            description="doc",
            verify_command="printf 'BUILD SUCCESS'",
            output_assertion="FAILURE",
        ),
        cwd=str(tmp_path),
    )
    assert mismatch.cause == "output_assertion_unmatched"

    invalid = await executor._run_ac_verify_gate(
        spec=AcceptanceCriterionSpec.model_construct(
            description="assertion only",
            verify_command=None,
            expected_artifacts=(),
            output_assertion="READY",
        ),
        cwd=str(tmp_path),
    )
    assert invalid.cause == "invalid_contract"

    missing = await executor._run_ac_verify_gate(
        spec=AcceptanceCriterionSpec.model_construct(
            description="artifact",
            verify_command=None,
            expected_artifacts=("dist/report.md",),
            output_assertion=None,
        ),
        cwd=str(tmp_path),
    )
    assert missing.cause == "artifacts_missing"

    (tmp_path / "keep.txt").write_text("keep", encoding="utf-8")
    mutated = await executor._run_ac_verify_gate(
        spec=AcceptanceCriterionSpec(description="read-only", verify_command="rm keep.txt"),
        cwd=str(tmp_path),
    )
    assert mutated.cause == "workspace_mutated"


@pytest.mark.asyncio
async def test_missing_artifact_found_elsewhere_flags_worker_cd_signature(
    tmp_path: Any,
) -> None:
    """The contract path absent at the gate cwd but present under a
    subdirectory is the worker-`cd` failure mode discovered in real user
    transcripts -- it must classify distinctly from a never-created artifact."""
    nested = tmp_path / "packages" / "app" / "dist"
    nested.mkdir(parents=True)
    (nested / "report.md").write_text("built", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    spec = AcceptanceCriterionSpec.model_construct(
        description="artifact",
        verify_command=None,
        expected_artifacts=("dist/report.md",),
        output_assertion=None,
    )

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.cause == "artifacts_missing_found_elsewhere"


def test_verify_gate_outcome_cause_roundtrips_and_legacy_checkpoints_decode() -> None:
    from ouroboros.orchestrator.parallel_executor import (
        _deserialize_verify_gate_outcome,
        _serialize_verify_gate_outcome,
    )

    original = _VerifyGateOutcome(
        passed=False,
        reason="verify_command exited with status 3",
        output_tail="boom",
        cause="exit_nonzero",
    )
    serialized = _serialize_verify_gate_outcome(original)
    assert serialized is not None
    assert serialized["cause"] == "exit_nonzero"
    assert _deserialize_verify_gate_outcome(serialized) == original

    # Checkpoints written before `cause` existed omit the key and must still
    # decode (as unattributed) so cached non-idempotent verify results survive
    # a version upgrade.
    legacy = dict(serialized)
    del legacy["cause"]
    decoded = _deserialize_verify_gate_outcome(legacy)
    assert decoded is not None
    assert decoded.cause is None

    # A cause outside the closed vocabulary is rejected, not forwarded.
    hostile = dict(serialized)
    hostile["cause"] = "/private/path: boom"
    assert _deserialize_verify_gate_outcome(hostile) is None


@pytest.mark.asyncio
async def test_quarantined_outcomes_still_reach_rejection_cause_telemetry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`timeout` and `environment_unverifiable` quarantine instead of failing,
    but they are documented causes — the analytics event must still fire."""
    import ouroboros.orchestrator.parallel_executor as pe

    captured: list[str | None] = []
    monkeypatch.setattr(
        pe.usage_telemetry,
        "capture_ac_verify_failed",
        lambda cause: captured.append(cause),
    )

    executor = _make_executor(working_directory=str(tmp_path), verify_command_timeout_seconds=1)
    seed = _seed_with_specs(AcceptanceCriterionSpec(description="slow", verify_command="sleep 5"))
    gated = await executor._apply_verify_gate(
        seed=seed,
        ac_index=0,
        result=ACExecutionResult(ac_index=0, ac_content="slow", success=True),
        session_id="s",
        execution_id="e",
    )
    assert gated.success is True  # quarantined, not failed
    assert captured == ["timeout"]

    captured.clear()
    executor_no_shell = _make_executor(working_directory=str(tmp_path))
    executor_no_shell._verify_shell_identity = None
    seed_pipe = _seed_with_specs(
        AcceptanceCriterionSpec(description="pipe", verify_command="echo ok | tee log")
    )
    await executor_no_shell._apply_verify_gate(
        seed=seed_pipe,
        ac_index=0,
        result=ACExecutionResult(ac_index=0, ac_content="pipe", success=True),
        session_id="s",
        execution_id="e",
    )
    assert captured == ["environment_unverifiable"]


@pytest.mark.asyncio
async def test_final_settlement_classifies_missing_artifact_not_as_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A final-boundary artifact loss must be reported as artifacts_missing,
    never guessed as concurrent workspace mutation."""
    import ouroboros.orchestrator.parallel_executor as pe

    captured: list[str | None] = []
    monkeypatch.setattr(
        pe.usage_telemetry,
        "capture_ac_verify_failed",
        lambda cause: captured.append(cause),
    )

    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="artifact", expected_artifacts=("target.txt",))
    )
    passing = await executor._run_ac_verify_gate(
        spec=seed.acceptance_criteria[0], cwd=str(tmp_path)
    )
    assert passing.passed is True

    target.unlink()  # a sibling deleted the artifact after the atomic gate

    results = await executor._settle_verify_gate_results(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="artifact",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=passing,
            )
        ],
        session_id="s",
        execution_id="e",
    )

    assert results[0].success is False
    assert captured == ["artifacts_missing"]


@pytest.mark.asyncio
async def test_coordinator_revalidation_missing_artifact_emits_cause_analytics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The artifacts-only post-coordinator rejection owns its failure
    (settlement skips non-successful results), so it must emit the local
    verify-failed event and the closed-cause analytics like every other
    deterministic gate rejection."""
    import ouroboros.orchestrator.parallel_executor as pe

    captured: list[str | None] = []
    monkeypatch.setattr(
        pe.usage_telemetry,
        "capture_ac_verify_failed",
        lambda cause: captured.append(cause),
    )

    artifact = tmp_path / "target.txt"
    artifact.write_text("keep", encoding="utf-8")
    executor = _make_executor(working_directory=str(tmp_path))
    emitted: list[Any] = []

    async def record_event(event: Any) -> None:
        emitted.append(event)

    executor._safe_emit_event = record_event
    seed = _seed_with_specs(
        AcceptanceCriterionSpec(description="artifact", expected_artifacts=("target.txt",))
    )
    passing = await executor._run_ac_verify_gate(
        spec=seed.acceptance_criteria[0], cwd=str(tmp_path)
    )
    assert passing.passed is True

    artifact.unlink()  # the coordinator's reconciliation removed the artifact

    revalidated = await executor._revalidate_results_after_coordinator(
        seed=seed,
        results=[
            ACExecutionResult(
                ac_index=0,
                ac_content="artifact",
                success=True,
                outcome=ACExecutionOutcome.SUCCEEDED,
                verify_gate_outcome=passing,
            )
        ],
        session_id="s",
        execution_id="e",
    )

    assert revalidated[0].success is False
    assert captured == ["artifacts_missing"]
    failures = [event for event in emitted if event.type == "execution.verify.failed"]
    assert len(failures) == 1
    assert failures[0].data["verify_cause"] == "artifacts_missing"
    assert failures[0].data["missing_artifacts"] == ["target.txt"]
