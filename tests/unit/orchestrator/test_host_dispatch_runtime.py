"""Host-driven execution: the ``host`` backend, bridge, and re-entry routing.

Covers the P1/P2 contracts from HANDOFF-host-dispatch.md:

- backend registration (capability, factory, dispatch-mode resolution)
- execute_task parks a durable dispatch, heartbeats while parked, and completes
  on a host submission delivered through ``submit_fanout_results``
- stale-dispatch poisoning (a late submission never wakes a finished attempt)
- the grading contract never enters a serialized payload (leak sweep)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ouroboros.backends.capabilities import (
    SubagentDispatchMode,
    resolve_subagent_dispatch,
)
from ouroboros.mcp.tools.fanout import FanoutRegistry, submit_fanout_results
from ouroboros.mcp.tools.fanout_handler import SubmitFanoutResultsHandler
from ouroboros.orchestrator.adapter import SubagentOrchestration
from ouroboros.orchestrator.host_dispatch import (
    FANOUT_KIND_HOST_EXECUTION,
    HOST_EXECUTION_CORRELATION_KEY,
    HOST_EXECUTION_RESULT_KEY,
    HostDispatchBridge,
    HostDispatchNotBoundError,
    HostDispatchRuntime,
    bind_host_dispatch_bridge,
)
from ouroboros.orchestrator.runtime_factory import (
    create_agent_runtime,
    resolve_agent_runtime_backend,
)


@pytest.fixture
def registry(tmp_path) -> FanoutRegistry:
    return FanoutRegistry(tmp_path / "fanout")


@pytest.fixture
def bridge(registry: FanoutRegistry) -> HostDispatchBridge:
    return HostDispatchBridge(registry)


def _runtime(bridge: HostDispatchBridge | None = None) -> HostDispatchRuntime:
    return HostDispatchRuntime(
        bridge=bridge,
        cwd="/tmp/project",
        # Fast cadence so parked-loop behavior is observable in a unit test.
        heartbeat_interval_seconds=0.02,
        dispatch_deadline_seconds=5.0,
    )


class TestBackendRegistration:
    def test_host_resolves_as_runtime_backend(self) -> None:
        assert resolve_agent_runtime_backend("host") == "host"
        assert resolve_agent_runtime_backend("host_dispatch") == "host"

    def test_factory_builds_host_runtime(self) -> None:
        runtime = create_agent_runtime(backend="host", cwd="/tmp")
        assert isinstance(runtime, HostDispatchRuntime)
        assert runtime.runtime_backend == "host"

    def test_host_dispatch_mode_is_host_driven(self) -> None:
        assert resolve_subagent_dispatch("host", None) is SubagentDispatchMode.HOST_DRIVEN

    def test_host_declares_no_subagent_orchestration(self) -> None:
        # Landmine 3: joining the passive-bridge / leader-driven orchestration
        # sets would trip the plugin dispatch gate.
        runtime = _runtime()
        assert runtime.capabilities.subagent_orchestration is SubagentOrchestration.NONE

    @pytest.mark.asyncio
    async def test_execute_without_bridge_raises_typed_error(self) -> None:
        runtime = _runtime(bridge=None)
        with pytest.raises(HostDispatchNotBoundError):
            async for _ in runtime.execute_task("do the thing"):
                pass

    def test_bind_helper_is_noop_for_other_runtimes(self, bridge: HostDispatchBridge) -> None:
        sentinel = object()
        bind_host_dispatch_bridge(sentinel, bridge)  # must not raise or mutate

    def test_bind_helper_attaches_bridge_and_scope(self, bridge: HostDispatchBridge) -> None:
        runtime = _runtime()
        bind_host_dispatch_bridge(runtime, bridge, session_id="sess_1", execution_id="exec_1")
        assert runtime._bridge is bridge
        assert runtime._session_id == "sess_1"
        assert runtime._execution_id == "exec_1"


class TestDispatchRoundTrip:
    @pytest.mark.asyncio
    async def test_park_heartbeat_submit_completes(self, bridge: HostDispatchBridge) -> None:
        runtime = _runtime(bridge)
        runtime.bind_dispatch_scope(session_id="sess_rt", execution_id="exec_rt")
        messages: list[Any] = []

        async def host_side() -> None:
            # Wait until the dispatch surfaces exactly the way job_wait shows it.
            pending: list[dict[str, Any]] = []
            while not pending:
                await asyncio.sleep(0.01)
                pending = bridge.pending_for_session("sess_rt")
            entry = pending[0]
            assert entry["host_action"] == "spawn_subagents"
            assert entry["result_correlation_key"] == HOST_EXECUTION_RESULT_KEY
            assert "SYSTEM RULES" in entry["subagents"][0]["prompt"]
            outcome = bridge.submit(
                entry["fanout_id"], {HOST_EXECUTION_RESULT_KEY: "worker finished: files written"}
            )
            assert outcome["status"] == "complete"

        async def worker_side() -> None:
            async for message in runtime.execute_task(
                "write the file", system_prompt="SYSTEM RULES"
            ):
                messages.append(message)

        await asyncio.gather(worker_side(), host_side())

        final = messages[-1]
        assert final.is_final and not final.is_error
        assert final.content == "worker finished: files written"
        # At least one synthetic heartbeat kept the stall scope alive while parked.
        assert any(message.data.get("subtype") == "host_dispatch_heartbeat" for message in messages)
        # The attempt cleaned up after itself: nothing is left pending.
        assert bridge.pending_for_session("sess_rt") == []

    @pytest.mark.asyncio
    async def test_execute_task_to_result_collects_success(
        self, bridge: HostDispatchBridge
    ) -> None:
        runtime = _runtime(bridge)
        runtime.bind_dispatch_scope(session_id="sess_tr")

        async def host_side() -> None:
            pending: list[dict[str, Any]] = []
            while not pending:
                await asyncio.sleep(0.01)
                pending = bridge.pending_for_session("sess_tr")
            bridge.submit(pending[0]["fanout_id"], {HOST_EXECUTION_RESULT_KEY: "done"})

        task = asyncio.create_task(host_side())
        result = await runtime.execute_task_to_result("do it")
        await task
        assert result.is_ok
        assert result.value.success
        assert result.value.final_message == "done"

    @pytest.mark.asyncio
    async def test_host_reported_failure_yields_error_result(
        self, bridge: HostDispatchBridge
    ) -> None:
        runtime = _runtime(bridge)
        runtime.bind_dispatch_scope(session_id="sess_fail")

        async def host_side() -> None:
            pending: list[dict[str, Any]] = []
            while not pending:
                await asyncio.sleep(0.01)
                pending = bridge.pending_for_session("sess_fail")
            bridge.submit(
                pending[0]["fanout_id"],
                {HOST_EXECUTION_RESULT_KEY: {"success": False, "summary": "could not build"}},
            )

        task = asyncio.create_task(host_side())
        result = await runtime.execute_task_to_result("do it")
        await task
        assert result.is_ok
        assert not result.value.success

    @pytest.mark.asyncio
    async def test_deadline_fails_the_attempt(self, bridge: HostDispatchBridge) -> None:
        runtime = HostDispatchRuntime(
            bridge=bridge,
            heartbeat_interval_seconds=0.01,
            dispatch_deadline_seconds=0.05,
        )
        runtime.bind_dispatch_scope(session_id="sess_dl")
        messages = [message async for message in runtime.execute_task("never answered")]
        final = messages[-1]
        assert final.is_final and final.is_error
        assert final.data.get("error_code") == "host_dispatch_deadline_exceeded"
        assert bridge.pending_for_session("sess_dl") == []

    @pytest.mark.asyncio
    async def test_missing_session_scope_fails_closed(self, bridge: HostDispatchBridge) -> None:
        # A sessionless dispatch would bind no submit guard and surface in no
        # job poll; the attempt must fail instead of parking invisibly.
        runtime = _runtime(bridge)
        messages = [message async for message in runtime.execute_task("no scope")]
        final = messages[-1]
        assert final.is_final and final.is_error
        assert final.data.get("error_code") == "host_dispatch_scope_missing"

    @pytest.mark.asyncio
    async def test_discard_wakes_parked_waiter_as_cancelled(
        self, bridge: HostDispatchBridge
    ) -> None:
        # Regression (adversarial review): a stale local reference kept a
        # discarded dispatch parked until its deadline instead of failing fast.
        runtime = HostDispatchRuntime(
            bridge=bridge,
            heartbeat_interval_seconds=0.01,
            dispatch_deadline_seconds=30.0,
        )
        runtime.bind_dispatch_scope(session_id="sess_cx")

        async def canceller() -> None:
            pending: list[dict[str, Any]] = []
            while not pending:
                await asyncio.sleep(0.01)
                pending = bridge.pending_for_session("sess_cx")
            bridge.discard(pending[0]["fanout_id"])

        task = asyncio.create_task(canceller())
        started = asyncio.get_running_loop().time()
        messages = [message async for message in runtime.execute_task("to be cancelled")]
        await task
        elapsed = asyncio.get_running_loop().time() - started
        final = messages[-1]
        assert final.is_final and final.is_error
        assert final.data.get("error_code") == "host_dispatch_cancelled"
        assert elapsed < 5.0  # woke on discard, not on the 30s deadline

    def test_pending_without_session_scope_returns_nothing(
        self, bridge: HostDispatchBridge
    ) -> None:
        bridge.park(session_id="sess_scope", execution_id=None, payload={"prompt": "p"})
        assert bridge.pending_for_session(None) == []
        assert bridge.pending_for_session("other") == []
        assert len(bridge.pending_for_session("sess_scope")) == 1

    def test_announce_once_suppresses_relisting(self, bridge: HostDispatchBridge) -> None:
        # Regression (adversarial review): re-listing on every ~5s poll made a
        # compliant host spawn duplicate workers into the same cwd.
        dispatch_id = bridge.park(session_id="sess_an", execution_id=None, payload={"prompt": "p"})
        assert dispatch_id is not None
        first = bridge.pending_for_session("sess_an")
        assert len(first) == 1 and first[0]["reannounce"] is False
        assert bridge.pending_for_session("sess_an") == []
        # After the TTL the dispatch is re-announced, flagged as such.
        bridge._pending[dispatch_id].announced_at -= 10_000.0
        again = bridge.pending_for_session("sess_an")
        assert len(again) == 1 and again[0]["reannounce"] is True

    def test_park_refuses_empty_session(self, bridge: HostDispatchBridge) -> None:
        with pytest.raises(ValueError):
            bridge.park(session_id="", execution_id=None, payload={"prompt": "p"})

    @pytest.mark.asyncio
    async def test_prompt_carries_mandatory_working_directory(
        self, bridge: HostDispatchBridge
    ) -> None:
        # The verify gate re-runs in the task worktree; the payload prompt
        # itself must steer the host subagent there.
        runtime = _runtime(bridge)
        runtime.bind_dispatch_scope(session_id="sess_wd")

        async def host_side() -> None:
            pending: list[dict[str, Any]] = []
            while not pending:
                await asyncio.sleep(0.01)
                pending = bridge.pending_for_session("sess_wd")
            prompt = pending[0]["subagents"][0]["prompt"]
            assert "WORKING DIRECTORY (mandatory)" in prompt
            assert "/tmp/project" in prompt
            bridge.submit(pending[0]["fanout_id"], {HOST_EXECUTION_RESULT_KEY: "ok"})

        task = asyncio.create_task(host_side())
        result = await runtime.execute_task_to_result("do it")
        await task
        assert result.is_ok


class TestStaleDispatch:
    def test_submit_unknown_id_is_stale(self, bridge: HostDispatchBridge) -> None:
        outcome = bridge.submit("fanout_missing", {HOST_EXECUTION_RESULT_KEY: "late"})
        assert outcome["status"] == "stale_dispatch"

    def test_discard_poisons_late_submission(self, bridge: HostDispatchBridge) -> None:
        dispatch_id = bridge.park(session_id="sess_p", execution_id=None, payload={"prompt": "p"})
        assert dispatch_id is not None
        bridge.discard(dispatch_id)
        outcome = bridge.submit(dispatch_id, {HOST_EXECUTION_RESULT_KEY: "late"})
        assert outcome["status"] == "stale_dispatch"

    def test_double_submit_is_stale(self, bridge: HostDispatchBridge) -> None:
        dispatch_id = bridge.park(session_id="sess_d", execution_id=None, payload={"prompt": "p"})
        assert dispatch_id is not None
        first = bridge.submit(dispatch_id, {HOST_EXECUTION_RESULT_KEY: "one"})
        second = bridge.submit(dispatch_id, {HOST_EXECUTION_RESULT_KEY: "two"})
        assert first["status"] == "complete"
        assert second["status"] == "stale_dispatch"


class TestFanoutReentry:
    @pytest.mark.asyncio
    async def test_submit_fanout_results_routes_to_bridge(
        self, registry: FanoutRegistry, bridge: HostDispatchBridge
    ) -> None:
        handler = SubmitFanoutResultsHandler(fanout_registry=registry, host_dispatch_bridge=bridge)
        dispatch_id = bridge.park(session_id="sess_h", execution_id=None, payload={"prompt": "p"})
        assert dispatch_id is not None
        result = await handler.handle(
            {
                "fanout_id": dispatch_id,
                "session_id": "sess_h",
                "correlation_key": HOST_EXECUTION_CORRELATION_KEY,
                "results": [{"key": HOST_EXECUTION_RESULT_KEY, "content": "host worker output"}],
            }
        )
        assert result.is_ok
        assert result.value.meta["status"] == "complete"
        assert bridge._pending[dispatch_id].result == "host worker output"

    @pytest.mark.asyncio
    async def test_session_mismatch_fails_closed(
        self, registry: FanoutRegistry, bridge: HostDispatchBridge
    ) -> None:
        handler = SubmitFanoutResultsHandler(fanout_registry=registry, host_dispatch_bridge=bridge)
        dispatch_id = bridge.park(
            session_id="sess_owner", execution_id=None, payload={"prompt": "p"}
        )
        assert dispatch_id is not None
        result = await handler.handle(
            {
                "fanout_id": dispatch_id,
                "session_id": "sess_other",
                "correlation_key": HOST_EXECUTION_CORRELATION_KEY,
                "results": [{"key": HOST_EXECUTION_RESULT_KEY, "content": "spoofed"}],
            }
        )
        assert result.is_ok
        assert result.value.meta["status"] == "correlation_mismatch"
        assert not bridge._pending[dispatch_id].submitted

    @pytest.mark.asyncio
    async def test_execution_kind_without_bridge_fails_closed(
        self, registry: FanoutRegistry, bridge: HostDispatchBridge
    ) -> None:
        dispatch_id = bridge.park(session_id="sess_nb", execution_id=None, payload={"prompt": "p"})
        assert dispatch_id is not None
        handler = SubmitFanoutResultsHandler(fanout_registry=registry)
        result = await handler.handle(
            {
                "fanout_id": dispatch_id,
                "session_id": "sess_nb",
                "correlation_key": HOST_EXECUTION_CORRELATION_KEY,
                "results": [{"key": HOST_EXECUTION_RESULT_KEY, "content": "x"}],
            }
        )
        assert result.is_err

    def test_plain_function_identity_path(
        self, registry: FanoutRegistry, bridge: HostDispatchBridge
    ) -> None:
        # Direct callers of submit_fanout_results (no handler, no bridge) get
        # the identity aggregation instead of unknown_kind — the durable record
        # is a real registered kind either way.
        dispatch_id = bridge.park(session_id="sess_fn", execution_id=None, payload={"prompt": "p"})
        assert dispatch_id is not None
        record = registry.load(dispatch_id)
        assert record is not None and record.kind == FANOUT_KIND_HOST_EXECUTION
        outcome = submit_fanout_results(
            registry,
            session_id="sess_fn",
            correlation_key=HOST_EXECUTION_CORRELATION_KEY,
            results=[{"key": HOST_EXECUTION_RESULT_KEY, "content": "x"}],
            fanout_id=dispatch_id,
        )
        assert outcome["status"] == "complete"


class TestGradingContractLeakSweep:
    @pytest.mark.asyncio
    async def test_serialized_payload_carries_no_hidden_contract(
        self, bridge: HostDispatchBridge
    ) -> None:
        # Landmine 4: the payload must carry only the assertion-free worker
        # prompt. The runtime builds its own context; nothing upstream can
        # smuggle the seed's verify_command/output_assertion into it.
        runtime = _runtime(bridge)
        runtime.bind_dispatch_scope(session_id="sess_leak")

        async def host_side() -> None:
            pending: list[dict[str, Any]] = []
            while not pending:
                await asyncio.sleep(0.01)
                pending = bridge.pending_for_session("sess_leak")
            serialized = json.dumps(pending[0], ensure_ascii=False)
            for forbidden in ("verify_command", "output_assertion", "ac_spec"):
                assert forbidden not in serialized
            bridge.submit(pending[0]["fanout_id"], {HOST_EXECUTION_RESULT_KEY: "ok"})

        task = asyncio.create_task(host_side())
        result = await runtime.execute_task_to_result("implement the feature")
        await task
        assert result.is_ok
