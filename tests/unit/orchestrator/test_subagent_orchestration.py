"""Capability-gate for sub-agent orchestration.

Encodes the empirically verified reality (probed via `codex mcp-server`, which
exposes only `codex`/`codex-reply` — no external multi-agent surface) and the
OpenCode plugin bridge being the one external-orchestration path. Locks the
dispatch gate to the capability SSOT instead of a backend-name set, and proves
the gate's behavior is unchanged.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin
from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    RuntimeCapabilities,
    SubagentOrchestration,
    is_host_bridge_dispatch,
    is_leader_driven_worker,
    subagent_orchestration_for_backend,
)


class TestSubagentOrchestrationSSOT:
    @pytest.mark.parametrize(
        ("backend", "expected"),
        [
            ("opencode", SubagentOrchestration.EXTERNAL_HOST_BRIDGE),
            ("opencode_cli", SubagentOrchestration.EXTERNAL_HOST_BRIDGE),
            ("OpenCode", SubagentOrchestration.EXTERNAL_HOST_BRIDGE),  # case-insensitive
            ("codex", SubagentOrchestration.INTERNAL),
            ("codex_cli", SubagentOrchestration.INTERNAL),
            # `codex_mcp` (the leader-driven worker-pool runtime) is DELIBERATELY
            # absent from the name-map → NONE. Its EXTERNAL orchestration is
            # leader-driven (ouroboros calls codex/codex-reply itself), NOT a
            # host-bridge `_subagent` envelope, so it must never make the plugin
            # gate fire. Adding it here would emit envelopes into a void and
            # break execute/evaluate/qa/auto. See
            # TestLeaderDrivenSeamIsProviderNeutral below.
            ("codex_mcp", SubagentOrchestration.NONE),
            ("claude", SubagentOrchestration.NONE),
            ("gemini_cli", SubagentOrchestration.NONE),
            ("", SubagentOrchestration.NONE),
            (None, SubagentOrchestration.NONE),
        ],
    )
    def test_mapping(self, backend: str | None, expected: SubagentOrchestration) -> None:
        assert subagent_orchestration_for_backend(backend) is expected

    def test_default_capability_is_none(self) -> None:
        caps = RuntimeCapabilities(
            skill_dispatch=True, targeted_resume=True, structured_output=True
        )
        assert caps.subagent_orchestration is SubagentOrchestration.NONE
        assert FULL_CAPABILITIES.subagent_orchestration is SubagentOrchestration.NONE


class TestRuntimeDeclarationsMatchSSOT:
    """Each runtime's declared capability must match the SSOT for its backend."""

    def test_codex_runtime_declares_internal(self) -> None:
        from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

        runtime = CodexCliRuntime(cli_path="codex", cwd="/tmp/project")
        caps = runtime.capabilities
        assert caps.subagent_orchestration is SubagentOrchestration.INTERNAL
        assert caps.subagent_orchestration is subagent_orchestration_for_backend(
            runtime.runtime_backend
        )

    def test_opencode_runtime_declares_host_bridge(self) -> None:
        from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        caps = runtime.capabilities
        assert caps.subagent_orchestration is SubagentOrchestration.EXTERNAL_HOST_BRIDGE
        assert caps.subagent_orchestration is subagent_orchestration_for_backend(
            runtime.runtime_backend
        )


class TestLeaderDrivenSeamIsProviderNeutral:
    """The worker-pool seam is read via capability, NOT backend name — so Claude,
    Codex-mcp, Gemini, or any resumable runtime plug in identically."""

    def test_leader_driven_predicate_is_backend_agnostic(self) -> None:
        # A fabricated runtime of ANY backend that declares EXTERNAL_LEADER_DRIVEN
        # is worker-pool eligible — no backend-name special-casing.
        caps = replace(
            FULL_CAPABILITIES,
            subagent_orchestration=SubagentOrchestration.EXTERNAL_LEADER_DRIVEN,
        )
        assert is_leader_driven_worker(caps) is True
        assert is_host_bridge_dispatch(caps) is False

    def test_host_bridge_is_not_leader_driven(self) -> None:
        # OpenCode's host-bridge envelope path is a DIFFERENT mechanism — not
        # leader-driven (ouroboros does not drive that child directly).
        caps = replace(
            FULL_CAPABILITIES,
            subagent_orchestration=SubagentOrchestration.EXTERNAL_HOST_BRIDGE,
        )
        assert is_leader_driven_worker(caps) is False
        assert is_host_bridge_dispatch(caps) is True

    def test_none_and_internal_are_neither(self) -> None:
        for mode in (SubagentOrchestration.NONE, SubagentOrchestration.INTERNAL):
            caps = replace(FULL_CAPABILITIES, subagent_orchestration=mode)
            assert is_leader_driven_worker(caps) is False
            assert is_host_bridge_dispatch(caps) is False

    def test_leader_driven_worker_never_triggers_host_bridge_gate(self) -> None:
        # A leader-driven worker drives its child directly; it must never emit a
        # `_subagent` envelope. The backend-name map keeps such runtimes at NONE
        # so the plugin gate stays False even under opencode_mode=plugin.
        for backend in ("codex_mcp", "claude_mcp", "claude", "gemini_cli"):
            assert subagent_orchestration_for_backend(backend) is SubagentOrchestration.NONE
            assert should_dispatch_via_plugin(backend, "plugin") is False


class TestDispatchGateBehaviorUnchanged:
    """Routing the gate through the capability must preserve the exact decisions."""

    @pytest.mark.parametrize(
        ("backend", "mode", "expected"),
        [
            ("opencode", "plugin", True),
            ("opencode_cli", "plugin", True),
            ("opencode", "subprocess", False),
            ("opencode", None, False),
            ("opencode", "", False),
            # INTERNAL backend must NOT dispatch externally even with plugin mode.
            ("codex", "plugin", False),
            ("codex_cli", "plugin", False),
            # The leader-driven codex worker-pool runtime must NEVER reach the
            # host-bridge plugin gate: it drives codex/codex-reply itself. Pinned
            # so a future "add codex_mcp to the name-map" change fails loudly here
            # instead of emitting _subagent envelopes into a void.
            ("codex_mcp", "plugin", False),
            ("codex_mcp", "subprocess", False),
            # NONE backends never dispatch.
            ("claude", "plugin", False),
            (None, "plugin", False),
        ],
    )
    def test_matrix(self, backend: str | None, mode: str | None, expected: bool) -> None:
        assert should_dispatch_via_plugin(backend, mode) is expected
