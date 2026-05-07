"""Pin the per-runtime authoring path that ``ooo auto`` selects (#690).

`--runtime <X>` chooses the run-handoff backend only. The interview and
seed authoring handlers always run in-process inside the Ouroboros MCP
server unless the caller is an OpenCode bridge plugin session that can
intercept the dispatch envelope. The ``ooo auto`` CLI shim itself
demotes ``opencode-mode plugin`` to ``subprocess`` because the CLI
process is not running inside an OpenCode session.

These tests pin both rules so a future change cannot silently start
dispatching authoring envelopes from ``ooo auto`` (which would simply
hang — there would be no plugin on the receiving end).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.auto.pipeline import AutoPipelineResult
from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.cli.commands.auto import _run_auto
from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin


def _capture_handler_construction() -> dict[str, MagicMock]:
    """Patch the four handler classes constructed by ``_run_auto``.

    Returns a dict of ``MagicMock`` instances so tests can assert the
    exact constructor kwargs ``_run_auto`` passed to each handler.
    """
    interview_mock = MagicMock(name="InterviewHandler")
    generate_mock = MagicMock(name="GenerateSeedHandler")
    execute_mock = MagicMock(name="ExecuteSeedHandler")
    start_execute_mock = MagicMock(name="StartExecuteSeedHandler")
    return {
        "interview": interview_mock,
        "generate": generate_mock,
        "execute": execute_mock,
        "start_execute": start_execute_mock,
    }


async def _noop_run(self, state):  # noqa: ARG001
    return AutoPipelineResult(
        status="complete",
        auto_session_id=state.auto_session_id,
        phase="complete",
        grade="A",
    )


@pytest.mark.parametrize(
    "runtime",
    ["claude", "codex", "hermes", "gemini", "kiro", "copilot"],
)
def test_run_auto_keeps_authoring_in_process_for_non_opencode_runtimes(
    runtime: str,
) -> None:
    """Authoring handlers must be in-process for every non-OpenCode runtime."""
    handlers = _capture_handler_construction()

    with (
        patch("ouroboros.cli.commands.auto.InterviewHandler", handlers["interview"]),
        patch("ouroboros.cli.commands.auto.GenerateSeedHandler", handlers["generate"]),
        patch("ouroboros.cli.commands.auto.ExecuteSeedHandler", handlers["execute"]),
        patch("ouroboros.cli.commands.auto.StartExecuteSeedHandler", handlers["start_execute"]),
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=_noop_run),
    ):
        asyncio.run(
            _run_auto(
                goal="safe goal",
                resume=None,
                runtime=runtime,
                max_interview_rounds=2,
                max_repair_rounds=1,
                skip_run=True,
            )
        )

    interview_kwargs = handlers["interview"].call_args.kwargs
    generate_kwargs = handlers["generate"].call_args.kwargs
    execute_kwargs = handlers["execute"].call_args.kwargs
    start_kwargs = handlers["start_execute"].call_args.kwargs

    assert interview_kwargs == {"agent_runtime_backend": runtime, "opencode_mode": None}
    assert generate_kwargs == {"agent_runtime_backend": runtime, "opencode_mode": None}
    assert execute_kwargs == {"agent_runtime_backend": runtime, "opencode_mode": None}
    assert start_kwargs.get("agent_runtime_backend") == runtime
    assert start_kwargs.get("opencode_mode") is None

    # Cross-check: the dispatch gate would also refuse to dispatch.
    assert should_dispatch_via_plugin(runtime, None) is False


def test_run_auto_demotes_persisted_opencode_plugin_to_subprocess(tmp_path) -> None:
    """`ooo auto` resume with persisted opencode-plugin mode must demote to subprocess.

    Background: the bridge plugin only exists inside an OpenCode session.
    The `ouroboros auto` CLI shim runs as a standalone process, so
    dispatching a `_subagent` envelope would hang with no receiver.
    `_run_auto` therefore demotes `opencode_mode="plugin"` to
    `"subprocess"` before constructing handlers.
    """
    state = AutoPipelineState(goal="resume goal", cwd=str(tmp_path))
    state.runtime_backend = "opencode"
    state.opencode_mode = "plugin"
    state.skip_run = True
    state.max_interview_rounds = 2
    state.max_repair_rounds = 1
    store = AutoStore(tmp_path)
    store.save(state)

    handlers = _capture_handler_construction()

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.InterviewHandler", handlers["interview"]),
        patch("ouroboros.cli.commands.auto.GenerateSeedHandler", handlers["generate"]),
        patch("ouroboros.cli.commands.auto.ExecuteSeedHandler", handlers["execute"]),
        patch("ouroboros.cli.commands.auto.StartExecuteSeedHandler", handlers["start_execute"]),
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=_noop_run),
    ):
        store_cls.return_value = store
        asyncio.run(
            _run_auto(
                goal=None,
                resume=state.auto_session_id,
                runtime=None,
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=False,
            )
        )

    interview_kwargs = handlers["interview"].call_args.kwargs
    generate_kwargs = handlers["generate"].call_args.kwargs
    execute_kwargs = handlers["execute"].call_args.kwargs
    start_kwargs = handlers["start_execute"].call_args.kwargs

    assert interview_kwargs["agent_runtime_backend"] == "opencode"
    assert interview_kwargs["opencode_mode"] == "subprocess"
    assert generate_kwargs["opencode_mode"] == "subprocess"
    assert execute_kwargs["opencode_mode"] == "subprocess"
    assert start_kwargs["opencode_mode"] == "subprocess"

    # The demoted args also fail the dispatch gate, so nothing is sent
    # through the bridge plugin envelope from the auto CLI shim.
    assert should_dispatch_via_plugin("opencode", "subprocess") is False


def test_run_auto_demotes_fresh_session_opencode_plugin_to_subprocess(tmp_path) -> None:
    """Fresh `--runtime opencode` session with `get_opencode_mode() == "plugin"` is demoted.

    This pins the *fresh-session* branch of the OpenCode plugin
    demotion. `_run_auto()` reads the persisted opencode_mode for
    resumes but falls back to ``get_opencode_mode()`` (the configured
    default) for new sessions; both paths must demote ``"plugin"`` to
    ``"subprocess"`` for both authoring and run-handoff handlers,
    because the standalone CLI process cannot host the OpenCode bridge
    plugin envelope.
    """
    handlers = _capture_handler_construction()

    with (
        patch("ouroboros.cli.commands.auto.get_opencode_mode", return_value="plugin"),
        patch("ouroboros.cli.commands.auto.InterviewHandler", handlers["interview"]),
        patch("ouroboros.cli.commands.auto.GenerateSeedHandler", handlers["generate"]),
        patch("ouroboros.cli.commands.auto.ExecuteSeedHandler", handlers["execute"]),
        patch("ouroboros.cli.commands.auto.StartExecuteSeedHandler", handlers["start_execute"]),
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=_noop_run),
    ):
        asyncio.run(
            _run_auto(
                goal="safe goal",
                resume=None,
                runtime="opencode",
                max_interview_rounds=2,
                max_repair_rounds=1,
                skip_run=True,
            )
        )

    interview_kwargs = handlers["interview"].call_args.kwargs
    generate_kwargs = handlers["generate"].call_args.kwargs
    execute_kwargs = handlers["execute"].call_args.kwargs
    start_kwargs = handlers["start_execute"].call_args.kwargs

    assert interview_kwargs["agent_runtime_backend"] == "opencode"
    assert interview_kwargs["opencode_mode"] == "subprocess"
    assert generate_kwargs["opencode_mode"] == "subprocess"
    assert execute_kwargs["opencode_mode"] == "subprocess"
    assert start_kwargs["opencode_mode"] == "subprocess"

    assert should_dispatch_via_plugin("opencode", "subprocess") is False


def test_run_auto_keeps_opencode_subprocess_in_process(tmp_path) -> None:
    """OpenCode in subprocess mode (no persisted plugin) stays in-process."""
    state = AutoPipelineState(goal="resume goal", cwd=str(tmp_path))
    state.runtime_backend = "opencode"
    state.opencode_mode = "subprocess"
    state.skip_run = True
    state.max_interview_rounds = 2
    state.max_repair_rounds = 1
    store = AutoStore(tmp_path)
    store.save(state)

    handlers = _capture_handler_construction()

    with (
        patch("ouroboros.cli.commands.auto.AutoStore") as store_cls,
        patch("ouroboros.cli.commands.auto.InterviewHandler", handlers["interview"]),
        patch("ouroboros.cli.commands.auto.GenerateSeedHandler", handlers["generate"]),
        patch("ouroboros.cli.commands.auto.ExecuteSeedHandler", handlers["execute"]),
        patch("ouroboros.cli.commands.auto.StartExecuteSeedHandler", handlers["start_execute"]),
        patch("ouroboros.cli.commands.auto.AutoPipeline.run", new=_noop_run),
    ):
        store_cls.return_value = store
        asyncio.run(
            _run_auto(
                goal=None,
                resume=state.auto_session_id,
                runtime=None,
                max_interview_rounds=None,
                max_repair_rounds=None,
                skip_run=False,
            )
        )

    for handler in ("interview", "generate", "execute", "start_execute"):
        kwargs = handlers[handler].call_args.kwargs
        assert kwargs["agent_runtime_backend"] == "opencode"
        assert kwargs["opencode_mode"] == "subprocess"

    assert should_dispatch_via_plugin("opencode", "subprocess") is False
