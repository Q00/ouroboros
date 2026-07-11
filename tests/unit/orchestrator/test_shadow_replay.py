"""Opt-in shadow-replay baseline harness (frugality-proof AC5).

The sibling of ``test_frugality_producers.py``: those pin the token/effort/
grounding axes; this pins the LAST axis — the shadow-replay BASELINE (AC5) — and
proves that supplying it closes the deterministic proof's triad contract.

Three layers are covered:

* **Proof contract** — real emitter payloads (effort + token + deliver(+regression)
  + shadow_replay) fed back through ``assemble_triads`` / ``evaluate_proof``: a
  fully-measured enforced child counts, a regression FAILs, a thin sample is
  INSUFFICIENT_SAMPLE.
* **Harness** — ``run_shadow_replay`` with a mocked runtime factory: isolation is
  established and cleaned, the baseline runs at the PARENT tier, the event payload
  shape is right, a usage-less baseline emits nothing, and an isolation failure
  skips the replay entirely (never touching the live workspace).
* **Wiring** — flag OFF is byte-identical (no replay, no event, factory untouched).

No real CLI is ever spawned; every runtime/factory here is a scripted double.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config.models import EconomicsConfig, ModelConfig, TierConfig
from ouroboros.orchestrator import shadow_replay as sr_module
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
from ouroboros.orchestrator.execution_runtime_scope import build_ac_runtime_identity
from ouroboros.orchestrator.frugality_proof import (
    EVENT_SHADOW_REPLAY,
    ProofStatus,
    assemble_triads,
    evaluate_proof,
)
from ouroboros.orchestrator.model_routing import ModelRouter, build_model_router
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.shadow_replay import (
    isolated_workspace,
    run_shadow_replay,
    shadow_replay_enabled_from_env,
)


# -- Shared doubles -----------------------------------------------------------
def _capturing_event_store() -> tuple[AsyncMock, list]:
    store = AsyncMock()
    events: list = []

    async def _append(event):
        events.append(event)

    store.append.side_effect = _append
    return store, events


def _economics() -> EconomicsConfig:
    return EconomicsConfig(  # type: ignore[arg-type]
        default_tier="frugal",
        escalation_threshold=2,
        tiers={
            "frugal": TierConfig(
                cost_factor=1,
                models=[ModelConfig(provider="anthropic", model="haiku-x")],
            ),
            "standard": TierConfig(
                cost_factor=10,
                models=[ModelConfig(provider="anthropic", model="sonnet-x")],
            ),
            "frontier": TierConfig(
                cost_factor=30,
                models=[ModelConfig(provider="anthropic", model="opus-x")],
            ),
        },
    )


def _claude_router() -> ModelRouter:
    router = build_model_router(_economics(), runtime_backend="claude")
    assert router is not None
    return router


def _usage_result(usage: dict | None) -> AgentMessage:
    data: dict = {"subtype": "success"}
    if usage is not None:
        data["usage"] = usage
    return AgentMessage(type="result", content="[TASK_COMPLETE]", data=data)


class _FakeBaselineRuntime:
    """A throwaway baseline runtime the mocked factory returns.

    Records the constructor kwargs so the test can assert the baseline was built
    at the parent tier + isolated cwd, and yields a scripted usage stream.
    """

    def __init__(self, *, backend, model, cwd, messages: list[AgentMessage]) -> None:
        self.backend = backend
        self.model = model
        self.cwd = cwd
        self.cwd_existed_at_build = bool(cwd) and os.path.isdir(cwd)
        self._messages = messages

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        **kwargs,
    ):
        self.received_resume_handle = resume_handle
        for message in self._messages:
            yield message


def _executor_with_router(*, task_cwd: str, store: AsyncMock) -> ParallelACExecutor:
    adapter = MagicMock()
    adapter.runtime_backend = "claude"
    adapter.working_directory = task_cwd
    return ParallelACExecutor(
        adapter=adapter,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        model_router=_claude_router(),
        task_cwd=task_cwd,
    )


def _identity():
    return build_ac_runtime_identity(
        1,
        execution_context_id="exec_frugal",
        is_sub_ac=True,
        parent_ac_index=0,
        sub_ac_index=0,
    )


def _shadow_events(events: list) -> list:
    return [e for e in events if getattr(e, "type", None) == EVENT_SHADOW_REPLAY]


# -- Layer 1: proof contract closes with real emitter payloads ----------------
class _EmitterHarness:
    """Emit the four proof-axis events for one AC via the REAL emitter."""

    def __init__(self) -> None:
        self.events: list = []

        async def _safe_emit(event) -> bool:
            self.events.append(event)
            return True

        self.emitter = ExecutionEventEmitter(AsyncMock(), safe_emit_event=_safe_emit)

    async def emit_row(
        self,
        *,
        run_id: str,
        sub_index: int,
        spend: float,
        baseline: float,
        regression: bool = False,
        effort_mode: str = "enforced",
        trustworthy: bool = True,
    ) -> None:
        identity = build_ac_runtime_identity(
            1,
            execution_context_id=run_id,
            is_sub_ac=True,
            parent_ac_index=0,
            sub_ac_index=sub_index,
        )
        session_id = f"sess-{run_id}"
        await self.emitter.emit_effort_routed(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            ac_index=1,
            is_sub_ac=True,
            effort_level="high",
            effort_mode=effort_mode,
            base_reasoning_effort="high",
            runtime_backend="claude",
        )
        await self.emitter.emit_token_attribution(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            ac_index=1,
            is_sub_ac=True,
            retry_attempt=0,
            token_spend=spend,
            usage_breakdown={"input_tokens": spend},
            model="haiku-x",
            model_tier="frugal",
            model_mode="enforced",
            effort_level="high",
            runtime_backend="claude",
        )
        await self.emitter.emit_deliver_verdict(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            is_sub_ac=True,
            traceguard_verdict="rejected" if regression else "accepted",
            unsupported_claim_rate=1.0 if regression else 0.0,
            rejected_reasons=["unsupported"] if regression else [],
            accepted_fact_count=0 if regression else 1,
            grounding_regression=regression,
        )
        await self.emitter.emit_shadow_replay(
            runtime_identity=identity,
            execution_id=run_id,
            session_id=session_id,
            ac_index=1,
            is_sub_ac=True,
            baseline_token_spend=baseline,
            baseline_mode="shadow_replay",
            baseline_model="sonnet-x",
            baseline_tier="standard",
            decomposition_trustworthy=trustworthy,
        )


class TestProofContractClosesWithBaseline:
    @pytest.mark.asyncio
    async def test_single_fully_measured_child_counts(self) -> None:
        harness = _EmitterHarness()
        await harness.emit_row(run_id="run-0", sub_index=0, spend=50, baseline=100)

        rows = assemble_triads(harness.events)
        assert len(rows) == 1
        row = rows[0]
        assert row.has_all_axes is True
        # The last axis closes the triad: this enforced, trustworthy, decomposed
        # child with a positive baseline now counts toward the proof.
        assert row.counts_in_proof is True
        assert row.baseline_token_spend == pytest.approx(100)
        assert row.baseline_mode == "shadow_replay"
        assert row.grounding_regression is False

    @pytest.mark.asyncio
    async def test_full_sample_passes(self) -> None:
        harness = _EmitterHarness()
        for run in range(3):
            for sub in range(7):
                await harness.emit_row(run_id=f"run-{run}", sub_index=sub, spend=50, baseline=100)

        verdict = evaluate_proof(assemble_triads(harness.events))
        assert verdict.status is ProofStatus.PASS
        assert verdict.counted_rows == 21
        assert verdict.runs == 3
        assert verdict.token_reduction_pct == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_grounding_regression_fails_proof(self) -> None:
        harness = _EmitterHarness()
        for run in range(3):
            for sub in range(7):
                # One child lost grounding at the cheap tier — a per-AC veto.
                regression = run == 0 and sub == 0
                await harness.emit_row(
                    run_id=f"run-{run}",
                    sub_index=sub,
                    spend=50,
                    baseline=100,
                    regression=regression,
                )

        verdict = evaluate_proof(assemble_triads(harness.events))
        assert verdict.status is ProofStatus.FAIL_GROUNDING_REGRESSION
        assert verdict.grounding_regressions == 1

    @pytest.mark.asyncio
    async def test_thin_sample_is_insufficient(self) -> None:
        harness = _EmitterHarness()
        for run in range(2):
            for sub in range(2):
                await harness.emit_row(run_id=f"run-{run}", sub_index=sub, spend=50, baseline=100)

        verdict = evaluate_proof(assemble_triads(harness.events))
        # Rows are fully measured but too few over too few runs.
        assert verdict.status is ProofStatus.INSUFFICIENT_SAMPLE
        assert verdict.counted_rows == 4

    @pytest.mark.asyncio
    async def test_deliver_verdict_without_regression_omits_key(self) -> None:
        # The live path leaves grounding_regression None → the key is ABSENT, so
        # the proof honestly excludes the row (has_all_axes needs the flag set).
        harness = _EmitterHarness()
        await harness.emitter.emit_deliver_verdict(
            runtime_identity=_identity(),
            execution_id="exec_frugal",
            session_id="sess",
            is_sub_ac=True,
            traceguard_verdict="accepted",
            unsupported_claim_rate=0.0,
            rejected_reasons=[],
            accepted_fact_count=1,
        )
        assert "grounding_regression" not in harness.events[0].data


# -- Layer 2: the harness itself ---------------------------------------------
class TestShadowReplayHarness:
    @pytest.mark.asyncio
    async def test_baseline_runs_at_parent_tier_in_isolated_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A real task cwd with content; force the copytree isolation branch.
        (tmp_path / "src.py").write_text("print('hi')\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "junk.js").write_text("x")
        monkeypatch.setattr(sr_module, "is_git_repo", lambda _cwd: False)

        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, model, cwd):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                messages=[_usage_result({"input_tokens": 80, "output_tokens": 20})],
            )
            # Capture whether the isolated copy actually exists (and excludes junk)
            # at the moment the baseline runtime is constructed.
            runtime.isolated_has_src = os.path.isfile(os.path.join(cwd, "src.py"))
            runtime.isolated_has_node_modules = os.path.isdir(os.path.join(cwd, "node_modules"))
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )

        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        await run_shadow_replay(
            executor,
            runtime_identity=_identity(),
            execution_id="exec_frugal",
            session_id="sess",
            ac_index=1,
            is_sub_ac=True,
            prompt="do the thing",
            system_prompt="system",
            tools=["Read"],
            decomposition_trustworthy=True,
        )

        assert len(built) == 1
        baseline = built[0]
        # Built at the PARENT tier (standard → sonnet-x), same backend.
        assert baseline.model == "sonnet-x"
        assert baseline.backend == "claude"
        # Ran against the ISOLATED copy (not the live cwd), which held the source
        # but excluded node_modules, and was a fresh session.
        assert baseline.cwd != str(tmp_path)
        assert baseline.isolated_has_src is True
        assert baseline.isolated_has_node_modules is False
        assert baseline.received_resume_handle is None
        # The isolated copy is cleaned up after the replay.
        assert not os.path.exists(baseline.cwd)

        # One shadow_replay event carrying the measured baseline spend + provenance.
        shadow = _shadow_events(events)
        assert len(shadow) == 1
        data = shadow[0].data
        assert data["baseline_token_spend"] == pytest.approx(100)
        assert data["baseline_mode"] == "shadow_replay"
        assert data["baseline_model"] == "sonnet-x"
        assert data["baseline_tier"] == "standard"
        assert data["decomposition_trustworthy"] is True
        assert data["is_decomposed_child"] is True

    @pytest.mark.asyncio
    async def test_usage_less_baseline_emits_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sr_module, "is_git_repo", lambda _cwd: False)

        def _fake_factory(*, backend, model, cwd):
            # Baseline that reports NO usage telemetry.
            return _FakeBaselineRuntime(
                backend=backend, model=model, cwd=cwd, messages=[_usage_result(None)]
            )

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )

        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        await run_shadow_replay(
            executor,
            runtime_identity=_identity(),
            execution_id="exec_frugal",
            session_id="sess",
            ac_index=1,
            is_sub_ac=True,
            prompt="p",
            system_prompt="s",
            tools=["Read"],
            decomposition_trustworthy=True,
        )

        # Missing is missing: no baseline spend measured → no event fabricated.
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_isolation_failure_skips_replay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib

        @contextlib.contextmanager
        def _failed_isolation(_cwd):
            yield None

        monkeypatch.setattr(sr_module, "isolated_workspace", _failed_isolation)
        factory = MagicMock()
        monkeypatch.setattr("ouroboros.orchestrator.runtime_factory.create_agent_runtime", factory)

        store, events = _capturing_event_store()
        executor = _executor_with_router(task_cwd=str(tmp_path), store=store)

        await run_shadow_replay(
            executor,
            runtime_identity=_identity(),
            execution_id="exec_frugal",
            session_id="sess",
            ac_index=1,
            is_sub_ac=True,
            prompt="p",
            system_prompt="s",
            tools=["Read"],
            decomposition_trustworthy=True,
        )

        # Never build a runtime, never emit — and above all never run against the
        # live workspace.
        factory.assert_not_called()
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_no_router_skips_replay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory = MagicMock()
        monkeypatch.setattr("ouroboros.orchestrator.runtime_factory.create_agent_runtime", factory)
        store, events = _capturing_event_store()
        adapter = MagicMock()
        adapter.runtime_backend = "claude"
        adapter.working_directory = str(tmp_path)
        executor = ParallelACExecutor(
            adapter=adapter,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=None,  # routing dormant → no parent baseline tier
            task_cwd=str(tmp_path),
        )

        await run_shadow_replay(
            executor,
            runtime_identity=_identity(),
            execution_id="exec_frugal",
            session_id="sess",
            ac_index=1,
            is_sub_ac=True,
            prompt="p",
            system_prompt="s",
            tools=["Read"],
            decomposition_trustworthy=True,
        )

        factory.assert_not_called()
        assert _shadow_events(events) == []


# -- isolated_workspace primitive --------------------------------------------
class TestIsolatedWorkspace:
    def test_copytree_branch_copies_and_cleans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "keep.txt").write_text("data")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "big").write_text("x")
        monkeypatch.setattr(sr_module, "is_git_repo", lambda _cwd: False)

        captured: str | None = None
        with isolated_workspace(str(tmp_path)) as isolated:
            assert isolated is not None
            captured = isolated
            assert os.path.isfile(os.path.join(isolated, "keep.txt"))
            # Heavy, regenerable dirs are excluded from the baseline copy.
            assert not os.path.exists(os.path.join(isolated, ".venv"))
        # Cleaned up on exit.
        assert captured is not None
        assert not os.path.exists(captured)

    def test_git_branch_uses_worktree_add_and_remove(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sr_module, "is_git_repo", lambda _cwd: True)
        calls: list[list[str]] = []

        def _fake_run(args, **kwargs):
            calls.append(args)
            if args[:3] == ["git", "worktree", "add"]:
                # Simulate a successful worktree checkout by creating the target.
                Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(sr_module.subprocess, "run", _fake_run)

        with isolated_workspace(str(tmp_path)) as isolated:
            assert isolated is not None

        add_calls = [c for c in calls if c[:3] == ["git", "worktree", "add"]]
        remove_calls = [c for c in calls if c[:3] == ["git", "worktree", "remove"]]
        assert len(add_calls) == 1
        assert "--detach" in add_calls[0]
        assert len(remove_calls) == 1
        assert "--force" in remove_calls[0]

    def test_git_add_failure_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sr_module, "is_git_repo", lambda _cwd: True)

        def _fake_run(args, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="fatal: boom")

        monkeypatch.setattr(sr_module.subprocess, "run", _fake_run)

        with isolated_workspace(str(tmp_path)) as isolated:
            # Isolation could not be established → caller must skip.
            assert isolated is None


# -- shadow_replay_enabled_from_env ------------------------------------------
class TestEnvFlag:
    @pytest.mark.parametrize("value", ["1", "true", "on", "TRUE", " On "])
    def test_enabled_tokens(self, value: str) -> None:
        assert shadow_replay_enabled_from_env({"OUROBOROS_SHADOW_REPLAY": value}) is True

    @pytest.mark.parametrize("value", ["", "0", "off", "false", "yes", "nope"])
    def test_disabled_tokens(self, value: str) -> None:
        assert shadow_replay_enabled_from_env({"OUROBOROS_SHADOW_REPLAY": value}) is False

    def test_unset_is_disabled(self) -> None:
        assert shadow_replay_enabled_from_env({}) is False


# -- Layer 3: executor wiring — flag OFF is byte-identical --------------------
class _ScriptedRuntime:
    """Advised runtime yielding a scripted success stream (the live child)."""

    def __init__(self, messages: list[AgentMessage]) -> None:
        self._messages = messages

    @property
    def runtime_backend(self) -> str:
        return "claude"

    @property
    def working_directory(self) -> str | None:
        return "/tmp/project"

    @property
    def permission_mode(self) -> str | None:
        return "acceptEdits"

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ):
        for message in self._messages:
            yield replace(message, resume_handle=resume_handle)


async def _run_child_leaf(executor: ParallelACExecutor):
    return await executor._execute_atomic_ac(
        ac_index=1,
        ac_content="Implement a thing",
        session_id="sess_frugal",
        tools=["Read"],
        system_prompt="system",
        seed_goal="Ship it",
        depth=0,
        start_time=datetime.now(UTC),
        execution_id="exec_frugal",
        is_sub_ac=True,
        parent_ac_index=0,
        sub_ac_index=0,
        retry_attempt=0,
    )


class TestExecutorWiring:
    @pytest.mark.asyncio
    async def test_flag_off_never_replays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory = MagicMock()
        monkeypatch.setattr("ouroboros.orchestrator.runtime_factory.create_agent_runtime", factory)
        store, events = _capturing_event_store()
        runtime = _ScriptedRuntime([_usage_result({"input_tokens": 10, "output_tokens": 2})])
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
            # shadow_replay_enabled defaults False
        )

        result = await _run_child_leaf(executor)

        assert result.success is True
        # Flag off → the harness is never entered: no factory build, no event.
        factory.assert_not_called()
        assert _shadow_events(events) == []

    @pytest.mark.asyncio
    async def test_flag_on_replays_successful_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sr_module, "is_git_repo", lambda _cwd: False)
        built: list[_FakeBaselineRuntime] = []

        def _fake_factory(*, backend, model, cwd):
            runtime = _FakeBaselineRuntime(
                backend=backend,
                model=model,
                cwd=cwd,
                messages=[_usage_result({"input_tokens": 90, "output_tokens": 10})],
            )
            built.append(runtime)
            return runtime

        monkeypatch.setattr(
            "ouroboros.orchestrator.runtime_factory.create_agent_runtime", _fake_factory
        )

        store, events = _capturing_event_store()
        runtime = _ScriptedRuntime([_usage_result({"input_tokens": 10, "output_tokens": 2})])
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            model_router=_claude_router(),
            task_cwd=str(tmp_path),
            shadow_replay_enabled=True,
        )

        result = await _run_child_leaf(executor)

        assert result.success is True
        # Flag on → the successful decomposed child is replayed once at the parent
        # tier, emitting a baseline event.
        assert len(built) == 1
        assert built[0].model == "sonnet-x"
        shadow = _shadow_events(events)
        assert len(shadow) == 1
        assert shadow[0].data["baseline_token_spend"] == pytest.approx(100)
