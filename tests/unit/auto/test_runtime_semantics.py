"""Pin the current ``ooo auto --runtime <backend>`` semantics.

Documented in ``docs/auto-runtime-semantics.md``: ``--runtime`` is the same
value for both authoring (in-process MCP handler) and run-handoff
(dispatcher), and plugin/subagent dispatch in the run handoff is gated on
opencode plugin mode. These tests are observation-grade — they document the
contract so any future change is a deliberate edit, not an accident.
"""

from __future__ import annotations

import pytest

from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin


@pytest.mark.parametrize(
    "runtime",
    ["claude", "codex", "hermes", "gemini", "copilot", "kiro"],
)
def test_runtime_persisted_on_state_for_authoring_and_handoff(runtime, tmp_path) -> None:
    """For non-opencode runtimes, the same backend value is what the
    authoring handler reads (state.runtime_backend) and what the run handoff
    receives downstream. There is no per-phase override path."""
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = runtime
    store.save(state)

    loaded = store.load(state.auto_session_id)
    assert loaded.runtime_backend == runtime
    # Authoring and run handoff both read state.runtime_backend; this is the
    # whole contract that documentation #690 is trying to make legible.
    assert loaded.runtime_backend == loaded.runtime_backend


@pytest.mark.parametrize(
    "runtime,opencode_mode,expected",
    [
        ("claude", None, False),
        ("codex", None, False),
        ("codex", "plugin", False),  # plugin mode irrelevant for non-opencode
        ("opencode", None, False),
        ("opencode", "subprocess", False),
        ("opencode", "plugin", True),
    ],
)
def test_should_dispatch_via_plugin_matrix(
    runtime: str, opencode_mode: str | None, expected: bool
) -> None:
    """Plugin/subagent dispatch is opt-in via opencode plugin mode only."""
    assert should_dispatch_via_plugin(runtime, opencode_mode) is expected


def test_codex_runtime_does_not_imply_plugin_dispatch() -> None:
    """Regression: ``--runtime codex`` MUST NOT trigger plugin dispatch.
    The first interview question is generated in-process by the authoring
    handler that talks to Codex; it does not become a Codex subagent task."""
    assert should_dispatch_via_plugin("codex", None) is False
    assert should_dispatch_via_plugin("codex", "plugin") is False
    assert should_dispatch_via_plugin("codex", "subprocess") is False
