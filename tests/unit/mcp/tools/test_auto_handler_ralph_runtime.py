"""Pin MCP ``ouroboros_auto`` Ralph-runtime construction across resume.

Q00/ouroboros#782 review-11 BLOCKING #1: when an OpenCode ``--complete-product``
session was originally started by the CLI inside plugin mode, the CLI
demotes ``state.opencode_mode`` to ``"subprocess"`` for the authoring /
run-handoff handlers and stores the un-demoted mode separately as
``state.ralph_opencode_mode`` so the persisted-session contract can rebuild
the plugin Ralph dispatch on resume. The MCP entrypoint must use the same
discipline — otherwise resuming such a session through MCP silently
downgrades Ralph from plugin delegation to in-process job mode and the
caller loses the expected ``_subagent`` child-session handoff.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.mcp.tools.auto_handler import AutoHandler


def test_mcp_resume_passes_undemoted_ralph_opencode_mode_to_ralph_handler(
    tmp_path,
) -> None:
    """MCP resume of a CLI-created plugin session must rebuild Ralph in plugin mode.

    Background: a CLI session originally started with
    ``opencode_mode="plugin"`` persists ``state.opencode_mode == "subprocess"``
    (demoted form for authoring/run-handoff) and
    ``state.ralph_opencode_mode == "plugin"`` (un-demoted form, the actual
    Ralph dispatch mode). The MCP resume path must read the un-demoted form
    when constructing :class:`RalphHandler`, otherwise the plugin
    ``_subagent`` dispatch contract is silently broken on cross-entrypoint
    resume.
    """
    state = AutoPipelineState(goal="ralph plugin resume", cwd=str(tmp_path))
    state.runtime_backend = "opencode"
    # Demoted form persisted by the CLI for authoring/run-handoff.
    state.opencode_mode = "subprocess"
    # Un-demoted form persisted by the CLI specifically for Ralph dispatch.
    state.ralph_opencode_mode = "plugin"
    state.skip_run = True
    state.max_interview_rounds = 2
    state.max_repair_rounds = 1
    state.transition(AutoPhase.INTERVIEW, "interview")
    store = AutoStore(tmp_path)
    store.save(state)

    captured: dict[str, Any] = {}

    class _CapturingRalphHandler:
        def __init__(self, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs
            self._kwargs = kwargs
            self.agent_runtime_backend = kwargs.get("agent_runtime_backend")
            self.opencode_mode = kwargs.get("opencode_mode")

    async def _noop_run(self, state):  # noqa: ARG001
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    handler = AutoHandler(store=store, agent_runtime_backend="opencode")

    with (
        patch(
            "ouroboros.mcp.tools.auto_handler.RalphHandler",
            new=_CapturingRalphHandler,
        ),
        patch(
            "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
            new=_noop_run,
        ),
    ):
        asyncio.run(
            handler.handle(
                {
                    "resume": state.auto_session_id,
                    "complete_product": True,
                }
            )
        )

    assert "kwargs" in captured, (
        "RalphHandler must be constructed when resuming a complete-product session"
    )
    assert captured["kwargs"]["agent_runtime_backend"] == "opencode"
    # The fix: Ralph must see the un-demoted plugin mode, NOT the demoted
    # ``state.opencode_mode == "subprocess"`` used for authoring/run handlers.
    assert captured["kwargs"]["opencode_mode"] == "plugin"


def test_mcp_resume_configured_ralph_factory_preserves_session_runtime_and_mode(
    tmp_path,
) -> None:
    """Configured MCP Ralph factories must keep resume-specific dispatch settings."""
    state = AutoPipelineState(goal="ralph factory resume", cwd=str(tmp_path))
    state.runtime_backend = "opencode"
    state.opencode_mode = "subprocess"
    state.ralph_opencode_mode = "plugin"
    state.complete_product = True
    state.skip_run = True
    state.max_interview_rounds = 2
    state.max_repair_rounds = 1
    state.transition(AutoPhase.INTERVIEW, "interview")
    store = AutoStore(tmp_path)
    store.save(state)

    captured: dict[str, Any] = {}

    class _ConfiguredRalphHandler:
        def __init__(self, runtime_backend: str | None, opencode_mode: str | None) -> None:
            captured["runtime_backend"] = runtime_backend
            captured["opencode_mode"] = opencode_mode

    def _build_ralph_handler(
        runtime_backend: str | None,
        opencode_mode: str | None,
    ) -> _ConfiguredRalphHandler:
        return _ConfiguredRalphHandler(runtime_backend, opencode_mode)

    async def _noop_run(self, state):  # noqa: ARG001
        return AutoPipelineResult(
            status="complete",
            auto_session_id=state.auto_session_id,
            phase="complete",
            grade="A",
            runtime_backend=state.runtime_backend,
            opencode_mode=state.opencode_mode,
        )

    handler = AutoHandler(
        store=store,
        agent_runtime_backend="subprocess",
        ralph_handler_factory=_build_ralph_handler,
    )

    with patch(
        "ouroboros.mcp.tools.auto_handler.AutoPipeline.run",
        new=_noop_run,
    ):
        asyncio.run(handler.handle({"resume": state.auto_session_id}))

    assert captured == {"runtime_backend": "opencode", "opencode_mode": "plugin"}
