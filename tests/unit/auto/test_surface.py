from __future__ import annotations

from pathlib import Path
import re

import pytest
from typer.testing import CliRunner

from ouroboros.auto.adapters import HandlerInterviewBackend
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.cli.main import app
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import (
    GenerateSeedHandler,
    InterviewHandler,
    _interview_allowed_tools,
)
from ouroboros.mcp.tools.auto_handler import (
    AutoHandler,
    _authoring_interview_handler,
    _authoring_seed_handler,
    _execution_start_handler,
    _resolve_cwd,
    _safe_default_cwd,
)
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult


def test_cli_auto_runtime_enum_matches_supported_backends() -> None:
    from ouroboros.cli.commands.auto import AgentRuntimeBackend

    assert {item.value for item in AgentRuntimeBackend} == {
        "claude",
        "codex",
        "opencode",
        "hermes",
        "gemini",
        "copilot",
        "kiro",
    }


def test_interview_allowed_tools_omits_unsupported_hermes_envelope(monkeypatch) -> None:
    assert _interview_allowed_tools("hermes") is None
    assert _interview_allowed_tools("codex")

    monkeypatch.setattr(
        "ouroboros.mcp.tools.authoring_handlers.resolve_llm_backend",
        lambda *_args, **_kwargs: "hermes",
    )
    assert _interview_allowed_tools(None) is None


def test_cli_auto_help_is_registered() -> None:
    result = CliRunner().invoke(app, ["auto", "--help"])

    assert result.exit_code == 0
    assert "--max-interview-rounds" in result.output
    assert "--skip-run" in result.output


def test_cli_auto_status_prints_persisted_session(monkeypatch, tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "asking interview round 1/12")
    state.interview_session_id = "interview_123"
    state.current_round = 1
    state.pending_question = "Which runtime should be used?"
    state.seed_path = "/tmp/seed.yaml"
    state.last_grade = "B"
    store.save(state)

    monkeypatch.setattr("ouroboros.cli.commands.auto.AutoStore", lambda: store)

    result = CliRunner().invoke(app, ["auto", "--resume", state.auto_session_id, "--status"])

    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert result.exit_code == 0
    assert "Auto session status" in output
    assert state.auto_session_id in output
    assert "Phase:" in output
    assert "interview" in output
    assert "asking interview round 1/12" in output
    assert "Interview session:" in output
    assert "interview_123" in output
    assert "Current interview round:" in output
    assert "Which runtime should be used?" in output
    assert "Seed grade:" in output
    assert "Resume:" in output


def test_cli_auto_status_requires_resume() -> None:
    result = CliRunner().invoke(app, ["auto", "--status"])

    assert result.exit_code == 1
    assert "--status requires --resume auto_<id>" in result.output


def test_auto_skill_frontmatter_dispatches_to_mcp_tool() -> None:
    skill = Path(__file__).parents[3] / "skills" / "auto" / "SKILL.md"
    content = skill.read_text(encoding="utf-8")

    assert "name: auto" in content
    assert "mcp_tool: ouroboros_auto" in content
    assert 'goal: "$goal"' in content
    assert 'resume: "$resume"' in content
    assert 'skip_run: "$skip_run"' in content
    assert 'driver: "$driver"' in content
    assert 'brake: "$brake"' in content
    assert 'max_interview_rounds: "$max_interview_rounds"' in content
    assert "ooo auto --resume" in content
    assert "--driver hermes --brake on" in content
    assert "--show-ledger" in content


def test_auto_handler_schema_contains_hang_safe_options() -> None:
    definition = AutoHandler().definition

    assert definition.name == "ouroboros_auto"
    names = {param.name for param in definition.parameters}
    assert {
        "goal",
        "resume",
        "max_interview_rounds",
        "max_repair_rounds",
        "skip_run",
        "driver",
        "brake",
    } <= names
    params = {param.name: param for param in definition.parameters}
    assert params["max_interview_rounds"].default is None
    assert params["max_repair_rounds"].default is None
    assert "persisted bound" in params["max_interview_rounds"].description
    assert "persisted bound" in params["max_repair_rounds"].description
    assert params["brake"].default is None


class _FakeInterviewHandler:
    async def handle(self, arguments):
        assert arguments == {"session_id": "interview_1"}
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="Session interview_1\n\nPending question?",
                    ),
                ),
                is_error=False,
                meta={"session_id": "interview_1"},
            )
        )


@pytest.mark.asyncio
async def test_handler_interview_backend_resume_fetches_pending_question() -> None:
    turn = await HandlerInterviewBackend(_FakeInterviewHandler(), cwd=".").resume("interview_1")

    assert turn.session_id == "interview_1"
    assert turn.question == "Pending question?"


class _FakeStartInterviewHandler:
    async def handle(self, arguments):
        assert arguments == {"initial_context": "goal", "cwd": "."}
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="Interview started. Session ID: interview_2\n\nWhat should we build?",
                    ),
                ),
                is_error=False,
                meta={"session_id": "interview_2"},
            )
        )


@pytest.mark.asyncio
async def test_handler_interview_backend_start_strips_session_envelope() -> None:
    turn = await HandlerInterviewBackend(_FakeStartInterviewHandler(), cwd=".").start(
        "goal", cwd="."
    )

    assert turn.session_id == "interview_2"
    assert turn.question == "What should we build?"


class _FakeAnswerInterviewHandler:
    async def handle(self, arguments):
        assert arguments == {"session_id": "interview_1", "answer": "Use Codex"}
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="Session interview_1\n\nWhich runtime should be used?",
                    ),
                ),
                is_error=False,
                meta={"session_id": "interview_1"},
            )
        )


@pytest.mark.asyncio
async def test_handler_interview_backend_answer_strips_session_envelope() -> None:
    turn = await HandlerInterviewBackend(_FakeAnswerInterviewHandler(), cwd=".").answer(
        "interview_1", "Use Codex"
    )

    assert turn.session_id == "interview_1"
    assert turn.question == "Which runtime should be used?"


class _NonEnvelopeInterviewHandler:
    async def handle(self, arguments):  # noqa: ARG002
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text="Session planning\n\nWhat handoff should we produce?",
                    ),
                ),
                is_error=False,
                meta={"session_id": "interview_1"},
            )
        )


@pytest.mark.asyncio
async def test_handler_interview_backend_preserves_non_matching_question_text() -> None:
    turn = await HandlerInterviewBackend(_NonEnvelopeInterviewHandler(), cwd=".").resume(
        "interview_1"
    )

    assert turn.question == "Session planning\n\nWhat handoff should we produce?"


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


def test_auto_handler_rebuilds_injected_authoring_handlers_for_persisted_runtime() -> None:
    interview = InterviewHandler(agent_runtime_backend="codex", opencode_mode=None)
    seed = GenerateSeedHandler(agent_runtime_backend="codex", opencode_mode=None)

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
    assert normalized_interview.agent_runtime_backend == "opencode"
    assert normalized_interview.opencode_mode == "subprocess"
    assert normalized_seed.agent_runtime_backend == "opencode"
    assert normalized_seed.opencode_mode == "subprocess"


def test_auto_handler_rebuilds_injected_execution_handler_for_persisted_runtime() -> None:
    adapter = object()
    execute_handler = ExecuteSeedHandler(
        llm_adapter=adapter,
        llm_backend="anthropic",
        agent_runtime_backend="codex",
        opencode_mode=None,
    )
    start = StartExecuteSeedHandler(
        execute_handler=execute_handler,
        agent_runtime_backend="codex",
        opencode_mode=None,
    )
    assert execute_handler.llm_adapter is adapter

    normalized = _execution_start_handler(
        start,
        llm_backend=None,
        agent_runtime_backend="opencode",
        opencode_mode="subprocess",
        mcp_manager=None,
        mcp_tool_prefix="",
    )

    assert normalized is not start
    assert normalized.agent_runtime_backend == "opencode"
    assert normalized.opencode_mode == "subprocess"
    assert normalized.execute_handler is not None
    assert normalized.execute_handler.agent_runtime_backend == "opencode"
    assert normalized.execute_handler.opencode_mode == "subprocess"
    assert normalized.execute_handler.llm_adapter is adapter
    assert normalized.execute_handler.llm_backend == "anthropic"


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


def test_auto_handler_rebuilds_matching_execution_handler_for_bridge_context() -> None:
    manager = object()
    start = StartExecuteSeedHandler(
        execute_handler=ExecuteSeedHandler(
            agent_runtime_backend="codex",
            opencode_mode=None,
        ),
        agent_runtime_backend="codex",
        opencode_mode=None,
    )

    normalized = _execution_start_handler(
        start,
        llm_backend=None,
        agent_runtime_backend="codex",
        opencode_mode=None,
        mcp_manager=manager,
        mcp_tool_prefix="bridge__",
    )

    assert normalized is not start
    assert normalized.execute_handler is not None
    assert normalized.execute_handler.mcp_manager is manager
    assert normalized.execute_handler.mcp_tool_prefix == "bridge__"


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


@pytest.mark.asyncio
async def test_auto_handler_meta_exposes_auto_progress_fields(monkeypatch) -> None:
    async def fake_run(self, arguments):  # noqa: ARG001
        from ouroboros.auto.pipeline import AutoPipelineResult

        return AutoPipelineResult(
            status="blocked",
            auto_session_id="auto_test",
            phase="interview",
            grade="B",
            seed_path="/tmp/seed.yaml",
            interview_session_id="interview_1",
            execution_id="execution_1",
            job_id="job_1",
            run_session_id="session_1",
            current_round=2,
            pending_question="Which runtime should be used?",
            last_progress_message="asking interview round 2/12",
            last_progress_at="2026-05-01T12:00:00+00:00",
            last_grade="B",
            blocker="waiting for interview answer",
        )

    monkeypatch.setattr(AutoHandler, "_run", fake_run)

    result = await AutoHandler().handle({"goal": "Build a CLI"})

    assert result.is_ok
    assert result.value.is_error is True
    assert result.value.meta == {
        "status": "blocked",
        "auto_session_id": "auto_test",
        "phase": "interview",
        "current_round": 2,
        "last_progress_message": "asking interview round 2/12",
        "last_progress_at": "2026-05-01T12:00:00+00:00",
        "resume_command": "ooo auto --resume auto_test",
        "blocker": "waiting for interview answer",
        "seed_path": "/tmp/seed.yaml",
        "grade": "B",
        "last_grade": "B",
        "interview_session_id": "interview_1",
        "execution_id": "execution_1",
        "job_id": "job_1",
        "run_session_id": "session_1",
        "pending_question": "Which runtime should be used?",
    }


@pytest.mark.asyncio
async def test_auto_handler_meta_uses_pipeline_state_progress(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

        async def run(self, run_state):  # noqa: ANN001
            run_state.transition(AutoPhase.INTERVIEW, "asking interview round 3/12")
            run_state.current_round = 3
            run_state.pending_question = "What should the command output?"
            run_state.last_progress_message = "asking interview round 3/12"
            run_state.last_progress_at = "2026-05-01T12:30:00+00:00"
            run_state.seed_path = "/tmp/seed.yaml"
            run_state.last_grade = "A"
            captured["auto_session_id"] = run_state.auto_session_id
            return AutoPipelineResult(
                status=run_state.phase.value,
                auto_session_id=run_state.auto_session_id,
                phase=run_state.phase.value,
                seed_path=run_state.seed_path,
                current_round=run_state.current_round,
                pending_question=run_state.pending_question,
                last_progress_message=run_state.last_progress_message,
                last_progress_at=run_state.last_progress_at,
                last_grade=run_state.last_grade,
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.setattr(auto_module, "AutoStore", lambda: AutoStore(tmp_path / "store"))
    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)

    result = await AutoHandler().handle({"goal": "Build a CLI", "cwd": str(tmp_path)})

    assert result.is_ok
    assert result.value.meta["auto_session_id"] == captured["auto_session_id"]
    assert result.value.meta["phase"] == "interview"
    assert result.value.meta["current_round"] == 3
    assert result.value.meta["pending_question"] == "What should the command output?"
    assert result.value.meta["last_progress_message"] == "asking interview round 3/12"
    assert result.value.meta["last_progress_at"] == "2026-05-01T12:30:00+00:00"
    assert result.value.meta["seed_path"] == "/tmp/seed.yaml"
    assert result.value.meta["last_grade"] == "A"
    assert result.value.meta["resume_command"] == (
        f"ooo auto --resume {captured['auto_session_id']}"
    )


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


def test_auto_handler_default_cwd_rejects_non_writable_project(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    monkeypatch.setattr("ouroboros.mcp.tools.auto_handler.os.access", lambda *_args: False)

    with pytest.raises(ValueError, match="not writable"):
        _safe_default_cwd()


def test_auto_handler_explicit_cwd_rejects_non_writable_project(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ouroboros.mcp.tools.auto_handler.os.access", lambda *_args: False)

    with pytest.raises(ValueError, match="not writable"):
        _resolve_cwd(str(tmp_path))


def test_auto_handler_explicit_cwd_rejects_non_searchable_project(monkeypatch, tmp_path) -> None:
    from ouroboros.mcp.tools import auto_handler as auto_module

    monkeypatch.setattr(auto_module.os, "access", lambda _path, mode: mode == auto_module.os.W_OK)

    with pytest.raises(ValueError, match="not writable"):
        _resolve_cwd(str(tmp_path))


def test_auto_handler_explicit_relative_cwd_is_persisted_as_absolute(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "project").mkdir()

    assert _resolve_cwd("project") == tmp_path / "project"


def test_auto_handler_explicit_cwd_rejects_regular_file(tmp_path) -> None:
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("not a project root", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        _resolve_cwd(str(file_path))


@pytest.mark.asyncio
async def test_cli_resume_replays_persisted_runtime_and_skip_run(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.cli.commands import auto as auto_command

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "codex"
    state.skip_run = True
    state.max_interview_rounds = 2
    state.max_repair_rounds = 3
    store.save(state)
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            captured["skip_run"] = kwargs.get("skip_run")
            captured["driver_rounds"] = args[0].max_rounds
            captured["repair_rounds"] = kwargs["repairer"].max_repair_rounds

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
        max_interview_rounds=None,
        max_repair_rounds=None,
        skip_run=False,
    )

    assert result.status == "complete"
    assert captured["state_runtime"] == "codex"
    assert captured["state_skip_run"] is True
    assert captured["skip_run"] is True
    assert captured["driver_rounds"] == 2
    assert captured["repair_rounds"] == 3
    assert captured["runtimes"] == ["codex", "codex", "codex", "codex"]


@pytest.mark.asyncio
async def test_cli_resume_migrates_legacy_session_without_runtime_backend(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.cli.commands import auto as auto_command

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = None
    store.save(state)
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

        async def run(self, run_state):  # noqa: ANN001
            captured["state_runtime"] = run_state.runtime_backend
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, **kwargs):
            captured.setdefault("runtimes", []).append(kwargs.get("agent_runtime_backend"))

    monkeypatch.setattr(auto_command, "AutoStore", lambda: store)
    monkeypatch.setattr(
        auto_command, "resolve_agent_runtime_backend", lambda value=None: value or "codex"
    )
    monkeypatch.setattr(auto_command, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_command, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "StartExecuteSeedHandler", FakeHandler)

    result = await auto_command._run_auto(
        goal=None,
        resume=state.auto_session_id,
        runtime=None,
        max_interview_rounds=None,
        max_repair_rounds=None,
        skip_run=False,
    )

    assert result.status == "complete"
    assert captured["state_runtime"] == "codex"
    assert captured["runtimes"] == ["codex", "codex", "codex", "codex"]


@pytest.mark.asyncio
async def test_cli_resume_infers_opencode_for_legacy_session_with_opencode_mode(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.cli.commands import auto as auto_command

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = None
    state.opencode_mode = "subprocess"
    store.save(state)
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

        async def run(self, run_state):  # noqa: ANN001
            captured["state_runtime"] = run_state.runtime_backend
            captured["state_mode"] = run_state.opencode_mode
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, **kwargs):
            captured.setdefault("runtimes", []).append(kwargs.get("agent_runtime_backend"))
            captured.setdefault("modes", []).append(kwargs.get("opencode_mode"))

    monkeypatch.setattr(auto_command, "AutoStore", lambda: store)
    monkeypatch.setattr(
        auto_command, "resolve_agent_runtime_backend", lambda value=None: value or "codex"
    )
    monkeypatch.setattr(auto_command, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_command, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "StartExecuteSeedHandler", FakeHandler)

    result = await auto_command._run_auto(
        goal=None,
        resume=state.auto_session_id,
        runtime=None,
        max_interview_rounds=None,
        max_repair_rounds=None,
        skip_run=False,
    )

    assert result.status == "complete"
    assert captured["state_runtime"] == "opencode"
    assert captured["state_mode"] == "subprocess"
    assert captured["runtimes"] == ["opencode", "opencode", "opencode", "opencode"]
    assert captured["modes"] == ["subprocess", "subprocess", "subprocess", "subprocess"]


@pytest.mark.asyncio
async def test_cli_resume_rejects_legacy_opencode_runtime_mismatch(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.cli.commands import auto as auto_command

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = None
    state.opencode_mode = "subprocess"
    store.save(state)
    monkeypatch.setattr(auto_command, "AutoStore", lambda: store)

    with pytest.raises(ValueError, match="runtime mismatch"):
        await auto_command._run_auto(
            goal=None,
            resume=state.auto_session_id,
            runtime="codex",
            max_interview_rounds=1,
            max_repair_rounds=1,
            skip_run=False,
        )


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


@pytest.mark.asyncio
async def test_cli_fresh_auto_rejects_blank_goal() -> None:
    from ouroboros.cli.commands import auto as auto_command

    with pytest.raises(ValueError, match="goal is required"):
        await auto_command._run_auto(
            goal="   ",
            resume=None,
            runtime=None,
            max_interview_rounds=1,
            max_repair_rounds=1,
            skip_run=False,
        )


def test_cli_default_cwd_rejects_non_searchable_project(monkeypatch, tmp_path) -> None:
    from ouroboros.cli.commands import auto as auto_command

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    monkeypatch.setattr(auto_command.os, "access", lambda _path, mode: mode == auto_command.os.W_OK)

    with pytest.raises(ValueError, match="not writable"):
        auto_command._safe_default_cwd()


@pytest.mark.asyncio
async def test_cli_fresh_auto_rejects_non_writable_project(monkeypatch, tmp_path) -> None:
    from ouroboros.cli.commands import auto as auto_command

    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    monkeypatch.setattr(auto_command.os, "access", lambda *_args: False)

    with pytest.raises(ValueError, match="not writable"):
        await auto_command._run_auto(
            goal="Build a CLI",
            resume=None,
            runtime=None,
            max_interview_rounds=1,
            max_repair_rounds=1,
            skip_run=False,
        )


@pytest.mark.asyncio
async def test_cli_fresh_auto_uses_safe_default_cwd(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoStore
    from ouroboros.cli.commands import auto as auto_command

    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            captured["skip_run"] = kwargs.get("skip_run")
            captured["driver_rounds"] = args[0].max_rounds
            captured["repair_rounds"] = kwargs["repairer"].max_repair_rounds

        async def run(self, run_state):  # noqa: ANN001
            captured["cwd"] = run_state.cwd
            captured["runtime"] = run_state.runtime_backend
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, **kwargs):  # noqa: ARG002
            pass

    monkeypatch.setattr(Path, "cwd", lambda: Path("/"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(auto_command, "AutoStore", lambda: AutoStore(tmp_path))
    monkeypatch.setattr(
        auto_command, "resolve_agent_runtime_backend", lambda value=None: value or "codex"
    )
    monkeypatch.setattr(auto_command, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_command, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_command, "StartExecuteSeedHandler", FakeHandler)

    result = await auto_command._run_auto(
        goal="Build a CLI",
        resume=None,
        runtime=None,
        max_interview_rounds=1,
        max_repair_rounds=1,
        skip_run=False,
    )

    assert result.status == "complete"
    assert captured["cwd"] == str(tmp_path)
    assert captured["runtime"] == "codex"


def test_static_ouroboros_tools_exports_auto_handler() -> None:
    from ouroboros.mcp.tools.definitions import OUROBOROS_TOOLS

    names = {handler.definition.name for handler in OUROBOROS_TOOLS}

    assert "ouroboros_auto" in names


@pytest.mark.asyncio
async def test_auto_handler_preserves_plugin_mode_for_execution_handoff(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, driver, _seed_generator, **kwargs):  # noqa: ANN001, ANN003
            run_starter = kwargs["run_starter"]
            captured["authoring_mode"] = driver.backend.handler.opencode_mode
            captured["run_mode"] = run_starter.handler.opencode_mode
            captured["execute_mode"] = run_starter.handler.execute_handler.opencode_mode

        async def run(self, run_state):  # noqa: ANN001
            captured["state_mode"] = run_state.opencode_mode
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    monkeypatch.setattr(auto_module, "AutoStore", lambda: AutoStore(tmp_path / "store"))
    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)

    result = await AutoHandler(agent_runtime_backend="opencode", opencode_mode="plugin").handle(
        {"goal": "Build a CLI", "cwd": str(tmp_path)}
    )

    assert result.is_ok
    assert captured == {
        "authoring_mode": "subprocess",
        "run_mode": "plugin",
        "execute_mode": "plugin",
        "state_mode": "plugin",
    }


@pytest.mark.asyncio
async def test_auto_handler_fresh_session_persists_resolved_runtime(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

        async def run(self, run_state):  # noqa: ANN001
            captured["runtime"] = run_state.runtime_backend
            captured["opencode_mode"] = run_state.opencode_mode
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.setattr(auto_module, "AutoStore", lambda: AutoStore(tmp_path / "store"))
    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(
        auto_module, "resolve_agent_runtime_backend", lambda value=None: value or "codex"
    )

    result = await AutoHandler().handle({"goal": "Build a CLI", "cwd": str(tmp_path)})

    assert result.is_ok
    assert captured == {"runtime": "codex", "opencode_mode": None}


@pytest.mark.asyncio
async def test_auto_handler_fresh_relative_cwd_persists_absolute_project(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    (tmp_path / "project").mkdir()
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

        async def run(self, run_state):  # noqa: ANN001
            captured["cwd"] = run_state.cwd
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auto_module, "AutoStore", lambda: AutoStore(tmp_path / "store"))
    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)

    result = await AutoHandler().handle({"goal": "Build a CLI", "cwd": "project"})

    assert result.is_ok
    assert captured["cwd"] == str(tmp_path / "project")


@pytest.mark.asyncio
async def test_auto_handler_resume_rebuilds_injected_handlers_for_persisted_runtime(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path / "project"))
    state.runtime_backend = "opencode"
    state.opencode_mode = "subprocess"
    store.save(state)
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, driver, seed_generator, **kwargs):  # noqa: ANN001, ANN003
            captured["interview_runtime"] = driver.backend.handler.agent_runtime_backend
            captured["interview_mode"] = driver.backend.handler.opencode_mode
            captured["seed_runtime"] = seed_generator.handler.agent_runtime_backend
            captured["seed_mode"] = seed_generator.handler.opencode_mode
            run_starter = kwargs["run_starter"]
            captured["run_runtime"] = run_starter.handler.agent_runtime_backend
            captured["run_mode"] = run_starter.handler.opencode_mode
            captured["run_adapter"] = run_starter.handler.execute_handler.llm_adapter
            captured["run_llm_backend"] = run_starter.handler.execute_handler.llm_backend
            captured["run_mcp_manager"] = run_starter.handler.execute_handler.mcp_manager
            captured["run_mcp_prefix"] = run_starter.handler.execute_handler.mcp_tool_prefix

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)

    adapter = object()
    manager = object()
    result = await AutoHandler(
        store=store,
        interview_handler=InterviewHandler(agent_runtime_backend="codex", opencode_mode=None),
        generate_seed_handler=GenerateSeedHandler(
            agent_runtime_backend="codex", opencode_mode=None
        ),
        start_execute_seed_handler=StartExecuteSeedHandler(
            execute_handler=ExecuteSeedHandler(
                llm_adapter=adapter,
                llm_backend="anthropic",
                agent_runtime_backend="codex",
                opencode_mode=None,
            ),
            agent_runtime_backend="codex",
            opencode_mode=None,
        ),
        agent_runtime_backend="codex",
        opencode_mode=None,
        mcp_manager=manager,
        mcp_tool_prefix="bridge__",
    ).handle({"resume": state.auto_session_id})

    assert result.is_ok
    assert captured == {
        "interview_runtime": "opencode",
        "interview_mode": "subprocess",
        "seed_runtime": "opencode",
        "seed_mode": "subprocess",
        "run_runtime": "opencode",
        "run_mode": "subprocess",
        "run_adapter": adapter,
        "run_llm_backend": "anthropic",
        "run_mcp_manager": manager,
        "run_mcp_prefix": "bridge__",
    }


@pytest.mark.asyncio
async def test_auto_handler_resume_uses_persisted_cwd_without_revalidating_server_cwd(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path / "project"))
    state.runtime_backend = "codex"
    state.max_interview_rounds = 2
    state.max_repair_rounds = 3
    store.save(state)
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            captured["skip_run"] = kwargs.get("skip_run")
            captured["driver_rounds"] = args[0].max_rounds
            captured["repair_rounds"] = kwargs["repairer"].max_repair_rounds

        async def run(self, run_state):  # noqa: ANN001
            captured["cwd"] = run_state.cwd
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.setattr(auto_module.Path, "cwd", lambda: tmp_path / "server")
    monkeypatch.setattr(auto_module.os, "access", lambda *_args: False)
    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)

    result = await AutoHandler(store=store).handle({"resume": state.auto_session_id})

    assert result.is_ok
    assert captured["cwd"] == str(tmp_path / "project")
    assert captured["driver_rounds"] == 2
    assert captured["repair_rounds"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_interview_rounds", "expected_repair_rounds"),
    [
        ({}, 4, 6),
        ({"max_interview_rounds": 4, "max_repair_rounds": 6}, 4, 6),
        ({"max_interview_rounds": 7, "max_repair_rounds": 8}, 7, 8),
        ({"max_interview_rounds": 7}, 7, 6),
        ({"max_repair_rounds": 8}, 4, 8),
    ],
)
async def test_auto_handler_resume_resolves_effective_loop_bounds(
    monkeypatch,
    tmp_path,
    arguments,
    expected_interview_rounds,
    expected_repair_rounds,
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path / "project"))
    state.runtime_backend = "codex"
    state.max_interview_rounds = 4
    state.max_repair_rounds = 6
    store.save(state)
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            captured["driver_rounds"] = args[0].max_rounds
            captured["repair_rounds"] = kwargs["repairer"].max_repair_rounds

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
                max_interview_rounds=run_state.max_interview_rounds,
                max_repair_rounds=run_state.max_repair_rounds,
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)

    result = await AutoHandler(store=store).handle({"resume": state.auto_session_id, **arguments})

    assert result.is_ok
    assert captured["driver_rounds"] == expected_interview_rounds
    assert captured["repair_rounds"] == expected_repair_rounds
    assert result.value.meta["max_interview_rounds"] == expected_interview_rounds
    assert result.value.meta["max_repair_rounds"] == expected_repair_rounds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "lower_value"),
    [("max_interview_rounds", 3), ("max_repair_rounds", 5)],
)
async def test_auto_handler_resume_rejects_lower_loop_bounds_without_persisting(
    tmp_path, field_name, lower_value
) -> None:
    from ouroboros.auto.state import AutoPipelineState, AutoStore

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path / "project"))
    state.runtime_backend = "codex"
    state.max_interview_rounds = 4
    state.max_repair_rounds = 6
    store.save(state)

    result = await AutoHandler(store=store).handle(
        {"resume": state.auto_session_id, field_name: lower_value}
    )

    restored = store.load(state.auto_session_id)
    assert result.is_err
    assert field_name in str(result.error)
    assert "lower than the persisted bound" in str(result.error)
    assert restored.max_interview_rounds == 4
    assert restored.max_repair_rounds == 6


@pytest.mark.asyncio
async def test_auto_handler_without_driver_keeps_deterministic_answerer(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.answerer import AutoAnswerer
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.mcp.tools import auto_handler as auto_module

    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, driver, _seed_generator, **kwargs):  # noqa: ANN001, ANN003
            captured["answerer_type"] = type(driver.answerer)

        async def run(self, run_state):  # noqa: ANN001
            captured["state_driver"] = run_state.interview_driver_backend
            captured["state_brake"] = run_state.brake.value
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)

    result = await AutoHandler(llm_backend="codex_cli").handle(
        {"goal": "Build a CLI", "cwd": str(tmp_path)}
    )

    assert result.is_ok
    assert captured == {
        "answerer_type": AutoAnswerer,
        "state_driver": None,
        "state_brake": "on",
    }


@pytest.mark.asyncio
async def test_auto_handler_resume_preserves_driver_and_brake_when_unspecified(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoBrakeMode, AutoPipelineState, AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path / "project"))
    state.runtime_backend = "codex"
    state.interview_driver_backend = "opencode"
    state.brake = AutoBrakeMode.OFF
    store.save(state)
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, driver, _seed_generator, **kwargs):  # noqa: ANN001, ANN003
            captured["answerer_backend"] = driver.answerer.backend
            captured["answerer_brake"] = driver.answerer.brake.value

        async def run(self, run_state):  # noqa: ANN001
            captured["state_driver"] = run_state.interview_driver_backend
            captured["state_brake"] = run_state.brake.value
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)

    result = await AutoHandler(store=store).handle({"resume": state.auto_session_id})

    assert result.is_ok
    assert captured == {
        "answerer_backend": "opencode",
        "answerer_brake": "off",
        "state_driver": "opencode",
        "state_brake": "off",
    }


@pytest.mark.asyncio
async def test_auto_handler_resume_rejects_brake_mismatch(tmp_path) -> None:
    from ouroboros.auto.state import AutoBrakeMode, AutoPipelineState, AutoStore

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path / "project"))
    state.runtime_backend = "codex"
    state.interview_driver_backend = "codex"
    state.brake = AutoBrakeMode.OFF
    store.save(state)

    result = await AutoHandler(store=store).handle({"resume": state.auto_session_id, "brake": "on"})

    assert result.is_err
    assert "resume brake mismatch" in str(result.error)


@pytest.mark.asyncio
async def test_auto_handler_resume_allows_normalized_driver_alias(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path / "project"))
    state.runtime_backend = "codex"
    state.interview_driver_backend = "codex"
    store.save(state)

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)

    result = await AutoHandler(store=store).handle(
        {"resume": state.auto_session_id, "driver": "codex_cli"}
    )

    assert result.is_ok


@pytest.mark.asyncio
async def test_auto_handler_resume_accepts_claude_alias_for_legacy_claude(
    monkeypatch, tmp_path
) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path / "project"))
    state.runtime_backend = "codex"
    state.interview_driver_backend = "claude"
    store.save(state)

    class FakePipeline:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    class FakeHandler:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ARG002
            pass

    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)
    monkeypatch.setattr(auto_module, "InterviewHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "GenerateSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "ExecuteSeedHandler", FakeHandler)
    monkeypatch.setattr(auto_module, "StartExecuteSeedHandler", FakeHandler)

    result = await AutoHandler(store=store).handle(
        {"resume": state.auto_session_id, "driver": "claude"}
    )

    assert result.is_ok


def test_auto_state_persists_loop_bounds() -> None:
    from ouroboros.auto.state import AutoPipelineState

    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    state.max_interview_rounds = 2
    state.max_repair_rounds = 3

    restored = AutoPipelineState.from_dict(state.to_dict())

    assert restored.max_interview_rounds == 2
    assert restored.max_repair_rounds == 3


def test_auto_state_loads_legacy_sessions_with_default_loop_bounds() -> None:
    from ouroboros.auto.state import AutoPipelineState

    payload = AutoPipelineState(goal="Build a CLI", cwd="/repo").to_dict()
    payload.pop("max_interview_rounds")
    payload.pop("max_repair_rounds")

    restored = AutoPipelineState.from_dict(payload)

    assert restored.max_interview_rounds == 12
    assert restored.max_repair_rounds == 5


def test_auto_state_rejects_invalid_loop_bounds() -> None:
    from ouroboros.auto.state import AutoPipelineState

    payload = AutoPipelineState(goal="Build a CLI", cwd="/repo").to_dict()
    payload["max_interview_rounds"] = True

    with pytest.raises(ValueError, match="max_interview_rounds"):
        AutoPipelineState.from_dict(payload)


@pytest.mark.asyncio
async def test_auto_handler_rejects_zero_loop_bounds() -> None:
    for field_name in ("max_interview_rounds", "max_repair_rounds"):
        result = await AutoHandler().handle({"goal": "Build a CLI", field_name: 0})

        assert result.is_err
        assert field_name in str(result.error)
        assert ">= 1" in str(result.error)
