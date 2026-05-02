from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ouroboros.auto.adapters import HandlerInterviewBackend
from ouroboros.cli.main import app
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
from ouroboros.mcp.tools.auto_handler import (
    AutoHandler,
    _authoring_interview_handler,
    _authoring_seed_handler,
    _execution_start_handler,
    _safe_default_cwd,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult


def test_cli_auto_help_is_registered() -> None:
    result = CliRunner().invoke(app, ["auto", "--help"])

    assert result.exit_code == 0
    assert "--max-interview-rounds" in result.output
    assert "--skip-run" in result.output


def test_auto_skill_frontmatter_dispatches_to_mcp_tool() -> None:
    skill = Path(__file__).parents[3] / "skills" / "auto" / "SKILL.md"
    content = skill.read_text(encoding="utf-8")

    assert "name: auto" in content
    assert "mcp_tool: ouroboros_auto" in content
    assert 'goal: "$1"' in content
    assert 'resume: "$resume"' in content
    assert 'skip_run: "$skip_run"' in content
    assert 'max_interview_rounds: "$max_interview_rounds"' in content
    assert "ooo auto --resume" in content
    assert "--show-ledger" in content


def test_auto_handler_schema_contains_hang_safe_options() -> None:
    definition = AutoHandler().definition

    assert definition.name == "ouroboros_auto"
    names = {param.name for param in definition.parameters}
    assert {"goal", "resume", "max_interview_rounds", "max_repair_rounds", "skip_run"} <= names


class _FakeInterviewHandler:
    async def handle(self, arguments):
        assert arguments == {"session_id": "interview_1"}
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="Pending question?"),),
                is_error=False,
                meta={"session_id": "interview_1"},
            )
        )


@pytest.mark.asyncio
async def test_handler_interview_backend_resume_fetches_pending_question() -> None:
    turn = await HandlerInterviewBackend(_FakeInterviewHandler(), cwd=".").resume("interview_1")

    assert turn.session_id == "interview_1"
    assert turn.question == "Pending question?"


class _FakeErrorInterviewHandler:
    async def handle(self, arguments):  # noqa: ARG002
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="recoverable failure"),),
                is_error=True,
                meta={"recoverable": True},
            )
        )


@pytest.mark.asyncio
async def test_handler_interview_backend_rejects_mcp_error_payloads() -> None:
    with pytest.raises(RuntimeError, match="recoverable failure"):
        await HandlerInterviewBackend(_FakeErrorInterviewHandler(), cwd=".").start("goal", cwd=".")


def test_auto_handler_uses_synchronous_authoring_mode_for_opencode_plugin() -> None:
    handler = AutoHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    assert handler.agent_runtime_backend == "opencode"
    assert handler.opencode_mode == "plugin"


def test_get_ouroboros_tools_includes_auto_for_runtime_dispatch() -> None:
    from ouroboros.mcp.tools.definitions import get_ouroboros_tools

    names = {handler.definition.name for handler in get_ouroboros_tools()}

    assert "ouroboros_auto" in names


def test_auto_handler_normalizes_injected_plugin_authoring_handlers() -> None:
    interview = InterviewHandler(agent_runtime_backend="opencode", opencode_mode="plugin")
    seed = GenerateSeedHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    normalized_interview = _authoring_interview_handler(
        interview,
        llm_backend=None,
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )
    normalized_seed = _authoring_seed_handler(
        seed,
        llm_backend=None,
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
    )

    assert normalized_interview is not interview
    assert normalized_seed is not seed
    assert normalized_interview.opencode_mode == "subprocess"
    assert normalized_seed.opencode_mode == "subprocess"
    assert normalized_interview.agent_runtime_backend == "opencode"
    assert normalized_seed.agent_runtime_backend == "opencode"


def test_auto_handler_fresh_execution_preserves_bridge_wiring() -> None:
    manager = object()

    start = _execution_start_handler(
        None,
        llm_backend="anthropic",
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
        mcp_manager=manager,
        mcp_tool_prefix="bridge__",
    )

    assert start.execute_handler is not None
    assert start.execute_handler.mcp_manager is manager
    assert start.execute_handler.mcp_tool_prefix == "bridge__"


def test_get_ouroboros_tools_forwards_bridge_wiring_to_auto_handler() -> None:
    from ouroboros.mcp.tools.definitions import get_ouroboros_tools

    manager = object()
    handlers = get_ouroboros_tools(mcp_manager=manager, mcp_tool_prefix="bridge__")
    auto = next(handler for handler in handlers if handler.definition.name == "ouroboros_auto")

    assert isinstance(auto, AutoHandler)
    assert auto.mcp_manager is manager
    assert auto.mcp_tool_prefix == "bridge__"


@pytest.mark.asyncio
async def test_auto_handler_forwards_run_subagent_envelope(monkeypatch) -> None:
    async def fake_run(self, arguments):  # noqa: ARG001
        from ouroboros.auto.pipeline import AutoPipelineResult

        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_test",
            phase="complete",
            run_session_id="session_1",
            run_subagent={"tool_name": "ouroboros_execute_seed", "context": {"x": "y"}},
        )

    monkeypatch.setattr(AutoHandler, "_run", fake_run)

    result = await AutoHandler().handle({"goal": "Build a CLI"})

    assert result.is_ok
    assert result.value.meta["_subagent"]["tool_name"] == "ouroboros_execute_seed"
    assert '"_subagent"' in result.value.content[0].text


def test_cli_opencode_plugin_uses_subprocess_for_plain_cli(monkeypatch) -> None:
    from ouroboros.cli.commands import auto as auto_command

    captured: dict[str, str | None] = {}

    class FakeInterviewHandler:
        def __init__(self, **kwargs):
            captured["interview_mode"] = kwargs.get("opencode_mode")

    class FakeGenerateSeedHandler:
        def __init__(self, **kwargs):
            captured["seed_mode"] = kwargs.get("opencode_mode")

    class FakeExecuteSeedHandler:
        def __init__(self, **kwargs):
            captured["execute_mode"] = kwargs.get("opencode_mode")

    class FakeStartExecuteSeedHandler:
        def __init__(self, **kwargs):
            captured["start_mode"] = kwargs.get("opencode_mode")

    monkeypatch.setattr(auto_command, "get_opencode_mode", lambda: "plugin")
    monkeypatch.setattr(auto_command, "InterviewHandler", FakeInterviewHandler)
    monkeypatch.setattr(auto_command, "GenerateSeedHandler", FakeGenerateSeedHandler)
    monkeypatch.setattr(auto_command, "ExecuteSeedHandler", FakeExecuteSeedHandler)
    monkeypatch.setattr(auto_command, "StartExecuteSeedHandler", FakeStartExecuteSeedHandler)

    # Instantiate the dependency block without running the whole pipeline.
    opencode_mode = auto_command.get_opencode_mode()
    if opencode_mode == "plugin":
        opencode_mode = "subprocess"
    authoring_opencode_mode = "subprocess" if opencode_mode == "plugin" else opencode_mode
    auto_command.InterviewHandler(
        agent_runtime_backend="opencode", opencode_mode=authoring_opencode_mode
    )
    auto_command.GenerateSeedHandler(
        agent_runtime_backend="opencode", opencode_mode=authoring_opencode_mode
    )
    execute_seed = auto_command.ExecuteSeedHandler(
        agent_runtime_backend="opencode", opencode_mode=opencode_mode
    )
    auto_command.StartExecuteSeedHandler(
        execute_handler=execute_seed, agent_runtime_backend="opencode", opencode_mode=opencode_mode
    )

    assert captured == {
        "interview_mode": "subprocess",
        "seed_mode": "subprocess",
        "execute_mode": "subprocess",
        "start_mode": "subprocess",
    }


def test_auto_handler_default_cwd_avoids_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "cwd", lambda: Path("/"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _safe_default_cwd() == tmp_path


@pytest.mark.asyncio
async def test_cli_resume_replays_persisted_runtime_and_skip_run(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.cli.commands import auto as auto_command

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "codex"
    state.skip_run = True
    store.save(state)
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            captured["skip_run"] = kwargs.get("skip_run")

        async def run(self, run_state):  # noqa: ANN001
            captured["state_runtime"] = run_state.runtime_backend
            captured["state_skip_run"] = run_state.skip_run
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, **kwargs):
            captured.setdefault("runtimes", []).append(kwargs.get("agent_runtime_backend"))
            captured.setdefault("opencode_modes", []).append(kwargs.get("opencode_mode"))

    monkeypatch.setattr(auto_command, "AutoStore", lambda: store)
    monkeypatch.setattr(auto_command, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_command, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "StartExecuteSeedHandler", FakeHandler)

    result = await auto_command._run_auto(
        goal=None,
        resume=state.auto_session_id,
        runtime=None,
        max_interview_rounds=1,
        max_repair_rounds=1,
        skip_run=False,
    )

    assert result.status == "complete"
    assert captured["state_runtime"] == "codex"
    assert captured["state_skip_run"] is True
    assert captured["skip_run"] is True
    assert captured["runtimes"] == ["codex", "codex", "codex", "codex"]


@pytest.mark.asyncio
async def test_cli_resume_rejects_runtime_mismatch(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.cli.commands import auto as auto_command

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "codex"
    store.save(state)
    monkeypatch.setattr(auto_command, "AutoStore", lambda: store)

    with pytest.raises(ValueError, match="runtime mismatch"):
        await auto_command._run_auto(
            goal=None,
            resume=state.auto_session_id,
            runtime="opencode",
            max_interview_rounds=1,
            max_repair_rounds=1,
            skip_run=False,
        )


def test_static_ouroboros_tools_exports_auto_handler() -> None:
    from ouroboros.mcp.tools.definitions import OUROBOROS_TOOLS

    names = {handler.definition.name for handler in OUROBOROS_TOOLS}

    assert "ouroboros_auto" in names


@pytest.mark.asyncio
async def test_auto_handler_rejects_zero_loop_bounds() -> None:
    for field_name in ("max_interview_rounds", "max_repair_rounds"):
        result = await AutoHandler().handle({"goal": "Build a CLI", field_name: 0})

        assert result.is_err
        assert field_name in str(result.error)
        assert ">= 1" in str(result.error)
