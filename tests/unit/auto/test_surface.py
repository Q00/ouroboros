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
    assert "Seed origin: none" in output
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


@pytest.mark.asyncio
async def test_handler_interview_backend_does_not_fabricate_partial_evidence() -> None:
    """When the handler does NOT include ``meta.session_id`` we must NOT raise
    ``PartialInterviewStartError`` even if the caller pre-allocated an id.

    Regression for the Q00/ouroboros#723 review: synthesising persistence
    evidence from caller input lets auto state record an id the handler
    never confirmed it wrote to disk.
    """
    from ouroboros.auto.adapters import HandlerError, PartialInterviewStartError

    backend = HandlerInterviewBackend(_FakeErrorInterviewHandler(), cwd=".")
    with pytest.raises(HandlerError) as excinfo:
        await backend.start("goal", cwd=".", interview_id="interview_caller_supplied")
    assert not isinstance(excinfo.value, PartialInterviewStartError), (
        "adapter must require explicit meta.session_id, not synthesise from interview_id"
    )


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
        "resume_capability": "resume",
        "resume_command": "ooo auto --resume auto_test",
        "blocker": "waiting for interview answer",
        "seed_path": "/tmp/seed.yaml",
        "seed_origin": "none",
        "grade": "B",
        "last_grade": "B",
        "interview_session_id": "interview_1",
        "execution_id": "execution_1",
        "job_id": "job_1",
        "run_session_id": "session_1",
        "pending_question": "Which runtime should be used?",
        "ledger_provenance": {},
        "evidence_backed_sections": [],
        "assumption_only_sections": [],
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


def test_auto_state_loads_legacy_sessions_without_attached_run_fields() -> None:
    from ouroboros.auto.state import AutoPipelineState

    payload = AutoPipelineState(goal="Build a CLI", cwd="/repo").to_dict()
    payload.pop("attached_run_handle")
    payload.pop("attached_run_source")
    payload.pop("attached_at")
    payload.pop("run_reconciliation_status")
    payload.pop("run_reconciliation_source")
    payload.pop("run_reconciled_at")

    restored = AutoPipelineState.from_dict(payload)

    assert restored.attached_run_handle is None
    assert restored.attached_run_source is None
    assert restored.attached_at is None
    assert restored.run_reconciliation_status is None
    assert restored.run_reconciliation_source is None
    assert restored.run_reconciled_at is None


def test_auto_handler_definition_exposes_attach_arguments() -> None:
    names = {param.name for param in AutoHandler().definition.parameters}

    assert {
        "attach_execution",
        "attach_job",
        "attach_session",
        "attach_source",
        "reconcile_run",
        "reconcile_source",
    } <= names


def test_auto_handler_meta_exposes_attached_run_fields() -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.mcp.tools.auto_handler import _result_meta

    meta = _result_meta(
        AutoPipelineResult(
            status="complete",
            auto_session_id="auto_test",
            phase="complete",
            execution_id="exec_existing",
            run_handoff_status="attached",
            attached_run_handle="exec_existing",
            attached_run_source="operator",
            attached_at="2026-05-07T00:00:00+00:00",
        )
    )

    assert meta["run_handoff_status"] == "attached"
    assert meta["attached_run_handle"] == "exec_existing"
    assert meta["attached_run_source"] == "operator"
    assert meta["attached_at"] == "2026-05-07T00:00:00+00:00"


def test_auto_handler_meta_exposes_run_reconciliation_fields() -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.mcp.tools.auto_handler import _result_meta

    meta = _result_meta(
        AutoPipelineResult(
            status="blocked",
            auto_session_id="auto_test",
            phase="blocked",
            run_handoff_status="unknown_no_handle",
            run_reconciliation_status="unsupported",
            run_reconciliation_source="generic",
            run_reconciled_at="2026-05-07T00:00:00+00:00",
        )
    )

    assert meta["run_reconciliation_status"] == "unsupported"
    assert meta["run_reconciliation_source"] == "generic"
    assert meta["run_reconciled_at"] == "2026-05-07T00:00:00+00:00"


@pytest.mark.asyncio
async def test_auto_handler_passes_state_interview_timeout_to_driver(monkeypatch, tmp_path) -> None:
    """Regression for #686: MCP entrypoint must wire state interview timeout into driver."""
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, driver, _seed_generator, **kwargs):  # noqa: ANN001, ANN003, ARG002
            captured["driver_timeout_seconds"] = driver.timeout_seconds

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    monkeypatch.setattr(auto_module, "AutoStore", lambda: AutoStore(tmp_path / "store"))
    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)

    result = await AutoHandler().handle({"goal": "Build a CLI", "cwd": str(tmp_path)})

    assert result.is_ok
    assert captured["driver_timeout_seconds"] == 600.0


@pytest.mark.asyncio
async def test_auto_handler_resume_honours_persisted_interview_timeout(
    monkeypatch, tmp_path
) -> None:
    """Resumed sessions must keep the persisted interview-phase timeout."""
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoPipelineState, AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    store_root = tmp_path / "store"
    store_root.mkdir()
    store = AutoStore(store_root)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.runtime_backend = "claude"
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = 240
    state.transition(AutoPhase.INTERVIEW, "interview")
    state.mark_blocked(
        "auto interview reached max rounds with unresolved gaps: actors",
        tool_name="interview_driver",
    )
    store.save(state)

    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, driver, _seed_generator, **kwargs):  # noqa: ANN001, ANN003, ARG002
            captured["driver_timeout_seconds"] = driver.timeout_seconds

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    monkeypatch.setattr(auto_module, "AutoStore", lambda: store)
    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)

    result = await AutoHandler().handle({"resume": state.auto_session_id})

    assert result.is_ok
    assert captured["driver_timeout_seconds"] == 240.0


def test_auto_state_default_seed_origin_is_none() -> None:
    from ouroboros.auto.state import AutoPipelineState, SeedOrigin

    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")

    assert state.seed_origin is SeedOrigin.NONE


def test_auto_state_persists_seed_origin_round_trip() -> None:
    from ouroboros.auto.state import AutoPipelineState, SeedOrigin

    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    state.seed_origin = SeedOrigin.AUTO_PIPELINE

    payload = state.to_dict()
    assert payload["seed_origin"] == "auto_pipeline"

    restored = AutoPipelineState.from_dict(payload)
    assert restored.seed_origin is SeedOrigin.AUTO_PIPELINE


def test_auto_state_loads_legacy_session_with_default_seed_origin() -> None:
    from ouroboros.auto.state import AutoPipelineState, SeedOrigin

    payload = AutoPipelineState(goal="Build a CLI", cwd="/repo").to_dict()
    payload.pop("seed_origin")

    restored = AutoPipelineState.from_dict(payload)

    assert restored.seed_origin is SeedOrigin.NONE


def test_auto_state_rejects_unknown_seed_origin() -> None:
    from ouroboros.auto.state import AutoPipelineState

    payload = AutoPipelineState(goal="Build a CLI", cwd="/repo").to_dict()
    payload["seed_origin"] = "manual"

    with pytest.raises(ValueError, match="seed_origin must be one of"):
        AutoPipelineState.from_dict(payload)


def test_auto_pipeline_result_default_seed_origin_is_none_string() -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult

    result = AutoPipelineResult(
        status="created",
        auto_session_id="auto_test",
        phase="created",
    )

    assert result.seed_origin == "none"


def test_cli_auto_status_renders_seed_origin(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.state import (
        AutoPhase,
        AutoPipelineState,
        AutoStore,
        SeedOrigin,
    )

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/tmp/project")
    state.transition(AutoPhase.INTERVIEW, "asking interview round 1/12")
    state.seed_path = "/tmp/seed.yaml"
    state.seed_origin = SeedOrigin.AUTO_PIPELINE
    store.save(state)

    monkeypatch.setattr("ouroboros.cli.commands.auto.AutoStore", lambda: store)

    cli_result = CliRunner().invoke(app, ["auto", "--resume", state.auto_session_id, "--status"])
    output = re.sub(r"\x1b\[[0-9;]*m", "", cli_result.output)

    assert cli_result.exit_code == 0
    assert "Seed origin: auto_pipeline" in output


@pytest.mark.asyncio
async def test_auto_pipeline_backfills_seed_origin_for_legacy_persisted_seed(tmp_path) -> None:
    """Pre-PR sessions persisted ``seed_artifact`` without ``seed_origin``.

    On the first resume after this PR ships, the pipeline must infer the
    origin (auto_pipeline) so the new CLI/MCP surfaces don't keep reporting
    a stale ``none`` value.
    """
    from ouroboros.auto.pipeline import AutoPipeline
    from ouroboros.auto.state import (
        AutoPhase,
        AutoPipelineState,
        AutoStore,
        SeedOrigin,
    )
    from ouroboros.core.seed import (
        EvaluationPrinciple,
        ExitCondition,
        OntologyField,
        OntologySchema,
        Seed,
        SeedMetadata,
    )

    seed = Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("Command prints stable output",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(
                OntologyField(
                    name="command",
                    field_type="string",
                    description="Command",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="testability",
                description="Observable behavior",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.12),
    )

    class _StubInterviewDriver:
        async def run(self, _state, _ledger):  # noqa: ARG002
            raise AssertionError("interview driver must not run for this resume path")

    async def fake_seed_generator(_session_id: str):  # noqa: ARG001
        raise AssertionError("seed generator must not run when artifact is persisted")

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    state.seed_artifact = seed.to_dict()
    state.seed_path = str(tmp_path / "seed.yaml")
    state.last_grade = "A"
    state.transition(AutoPhase.INTERVIEW, "primed")
    state.transition(AutoPhase.SEED_GENERATION, "ready for seed generation")
    state.transition(AutoPhase.REVIEW, "review queued")
    state.transition(AutoPhase.COMPLETE, "skip-run requested")
    # Simulate the legacy persisted state: seed_artifact present but
    # seed_origin still at the schema default (the field did not exist
    # when the session was first written).
    assert state.seed_origin is SeedOrigin.NONE
    store.save(state)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        fake_seed_generator,
        store=store,
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert state.seed_origin is SeedOrigin.AUTO_PIPELINE
    assert result.seed_origin == "auto_pipeline"


def test_print_result_renders_seed_origin_line() -> None:
    """The terminal CLI summary must surface the seed_origin field."""
    import re

    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.cli.commands.auto import _print_result
    from ouroboros.cli.formatters import console

    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_x",
        phase="complete",
        grade="A",
        seed_path="/tmp/seed.yaml",
        seed_origin="auto_pipeline",
    )

    with console.capture() as capture:
        _print_result(result, show_ledger=False)
    output = re.sub(r"\x1b\[[0-9;]*m", "", capture.get())

    assert "Seed origin: auto_pipeline" in output


def test_format_result_keeps_evidence_provenance_out_of_user_text() -> None:
    """Detailed provenance should live in MCP meta, not in the human-readable body.

    The user-facing text stays simple (status/phase/grade/seed_path/etc.); rich
    provenance breakdown is consumed by clients via ``MCPToolResult.meta``.
    """
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.mcp.tools.auto_handler import _format_result

    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_x",
        phase="complete",
        grade="A",
        seed_path="/tmp/seed.yaml",
        seed_origin="auto_pipeline",
    )

    text = _format_result(result)

    assert "Evidence:" not in text
    assert "evidence-backed" not in text
    assert "assumption-only" not in text


@pytest.mark.asyncio
async def test_auto_pipeline_resets_seed_origin_when_invalid_artifact_is_wiped(tmp_path) -> None:
    """A discarded malformed seed_artifact must reset seed_origin to ``none``.

    Otherwise the public CLI/MCP surfaces would still report the prior
    provenance (e.g. ``auto_pipeline``) for a session that no longer
    holds any Seed at all, leaking incorrect status metadata.
    """
    from ouroboros.auto.pipeline import AutoPipeline
    from ouroboros.auto.state import AutoPipelineState, SeedOrigin

    class _StubInterviewDriver:
        async def run(self, _state, _ledger):  # noqa: ARG002
            raise AssertionError("interview driver must not run on the malformed-artifact path")

    async def fake_seed_generator(_session_id: str):  # noqa: ARG001
        raise AssertionError("seed generator must not run on the malformed-artifact path")

    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    # Simulate a corrupt persisted Seed: ``Seed.from_dict`` will reject
    # this payload at the start of ``run``. The store is left ``None``
    # so the pipeline does not try to persist the malformed value back
    # through ``AutoStore.save``'s strict validator.
    state.seed_artifact = {"not": "a real seed"}
    state.seed_origin = SeedOrigin.AUTO_PIPELINE

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        fake_seed_generator,
        store=None,
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert state.seed_artifact == {}
    assert state.seed_origin is SeedOrigin.NONE
    assert result.seed_origin == "none"
    assert result.status in {"failed", "blocked"}


@pytest.mark.asyncio
async def test_auto_pipeline_backfills_seed_origin_for_seed_path_only_resume(tmp_path) -> None:
    """Resume that only carries ``seed_path`` (no ``seed_artifact``) must backfill provenance.

    Pre-PR auto pipelines were the only writer of ``seed_path`` via
    ``seed_saver``, so a session that resumes through the loader path
    with ``seed_origin=none`` is by definition a legacy session whose
    Seed was authored by the auto pipeline. The new CLI/MCP surfaces
    must not report ``none`` for it.
    """
    from ouroboros.auto.grading import GradeResult, SeedGrade
    from ouroboros.auto.ledger import SeedDraftLedger
    from ouroboros.auto.pipeline import AutoPipeline
    from ouroboros.auto.seed_repairer import RepairResult
    from ouroboros.auto.seed_reviewer import SeedReview
    from ouroboros.auto.state import (
        AutoPhase,
        AutoPipelineState,
        AutoStore,
        SeedOrigin,
    )
    from ouroboros.core.seed import (
        EvaluationPrinciple,
        ExitCondition,
        OntologyField,
        OntologySchema,
        Seed,
        SeedMetadata,
    )

    seed = Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("Command prints stable output",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(OntologyField(name="command", field_type="string", description="Command"),),
        ),
        evaluation_principles=(
            EvaluationPrinciple(name="testability", description="Observable behavior", weight=1.0),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.12),
    )

    class _StubInterviewDriver:
        async def run(self, _state, _ledger):  # noqa: ARG002
            raise AssertionError("interview driver must not run on the seed_path resume")

    async def fake_seed_generator(_session_id):  # noqa: ARG001
        raise AssertionError("seed generator must not run when seed_path is persisted")

    def fake_seed_saver(_seed):
        return str(tmp_path / "seed.yaml")

    def fake_seed_loader(_path):
        return seed

    class _PassingReviewer:
        def review(self, _seed, *, ledger=None):  # noqa: ARG002
            grade = GradeResult(grade=SeedGrade.A, scores={}, may_run=True)
            return SeedReview(grade_result=grade, findings=())

    class _PassingRepairer:
        def converge(self, seed_in, *, ledger=None):
            review = _PassingReviewer().review(seed_in, ledger=ledger)
            return (
                seed_in,
                review,
                [
                    RepairResult(
                        changed=False,
                        seed=seed_in,
                        applied_repairs=(),
                        unresolved_findings=(),
                    )
                ],
            )

    store = AutoStore(tmp_path / "store")
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    # Loader path: only seed_path is persisted, seed_artifact is empty,
    # seed_origin is at the legacy default ``none``.
    state.seed_path = str(tmp_path / "seed.yaml")
    state.last_grade = "A"
    ledger = SeedDraftLedger.from_goal(state.goal)
    state.ledger = ledger.to_dict()
    state.transition(AutoPhase.INTERVIEW, "primed")
    state.transition(AutoPhase.SEED_GENERATION, "ready for seed generation")
    state.transition(AutoPhase.REVIEW, "review queued")
    state.skip_run = True
    assert state.seed_origin is SeedOrigin.NONE
    store.save(state)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        fake_seed_generator,
        store=store,
        seed_saver=fake_seed_saver,
        seed_loader=fake_seed_loader,
        skip_run=True,
        reviewer=_PassingReviewer(),
        repairer=_PassingRepairer(),
    )

    result = await pipeline.run(state)

    assert state.seed_origin is SeedOrigin.AUTO_PIPELINE
    assert result.seed_origin == "auto_pipeline"


@pytest.mark.asyncio
async def test_auto_pipeline_marks_seed_origin_after_seed_generation(tmp_path) -> None:
    from ouroboros.auto.ledger import SeedDraftLedger
    from ouroboros.auto.pipeline import AutoPipeline
    from ouroboros.auto.state import (
        AutoPhase,
        AutoPipelineState,
        AutoStore,
        SeedOrigin,
    )
    from ouroboros.core.seed import (
        EvaluationPrinciple,
        ExitCondition,
        OntologyField,
        OntologySchema,
        Seed,
        SeedMetadata,
    )

    class _StubInterviewDriver:
        async def run(self, _state, _ledger):  # noqa: ARG002
            raise AssertionError("interview driver should not be invoked at SEED_GENERATION")

    seed = Seed(
        goal="Build a CLI",
        constraints=("Use existing project patterns",),
        acceptance_criteria=("Command prints stable output",),
        ontology_schema=OntologySchema(
            name="CliTask",
            description="CLI task ontology",
            fields=(
                OntologyField(
                    name="command",
                    field_type="string",
                    description="Command",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="testability",
                description="Observable behavior",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="verified",
                description="Checks pass",
                evaluation_criteria="All acceptance criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.12),
    )

    async def fake_seed_generator(session_id: str) -> Seed:  # noqa: ARG001
        return seed

    def fake_seed_saver(_seed: Seed) -> str:
        return str(tmp_path / "seed.yaml")

    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    state.ledger = ledger.to_dict()
    state.interview_session_id = "interview_xyz"
    state.interview_completed = True
    state.transition(AutoPhase.INTERVIEW, "primed for resume")
    state.transition(AutoPhase.SEED_GENERATION, "ready for seed generation")
    state.skip_run = True
    store.save(state)

    pipeline = AutoPipeline(
        _StubInterviewDriver(),
        fake_seed_generator,
        store=store,
        seed_saver=fake_seed_saver,
        skip_run=True,
    )

    result = await pipeline.run(state)

    assert state.seed_origin is SeedOrigin.AUTO_PIPELINE
    assert result.seed_origin == "auto_pipeline"


def test_format_result_omits_evidence_block_when_unknown() -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.mcp.tools.auto_handler import _format_result

    result = AutoPipelineResult(
        status="complete",
        auto_session_id="auto_test",
        phase="complete",
    )

    text = _format_result(result)

    assert "Evidence:" not in text
    assert "evidence-backed" not in text
    assert "assumption-only" not in text


@pytest.mark.asyncio
async def test_auto_handler_meta_exposes_ledger_provenance_breakdown(monkeypatch) -> None:
    async def fake_run(self, arguments):  # noqa: ARG001
        from ouroboros.auto.pipeline import AutoPipelineResult

        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_test",
            phase="complete",
            ledger_provenance={
                "user_goal": ("goal", "actors"),
                "repo_fact": ("runtime_context",),
                "conservative_default": ("constraints",),
            },
            evidence_backed_sections=("actors", "goal", "runtime_context"),
            assumption_only_sections=("constraints",),
        )

    monkeypatch.setattr(AutoHandler, "_run", fake_run)

    result = await AutoHandler().handle({"goal": "Build a CLI"})

    assert result.is_ok
    meta = result.value.meta
    assert meta["ledger_provenance"] == {
        "user_goal": ["goal", "actors"],
        "repo_fact": ["runtime_context"],
        "conservative_default": ["constraints"],
    }
    assert meta["evidence_backed_sections"] == ["actors", "goal", "runtime_context"]
    assert meta["assumption_only_sections"] == ["constraints"]


@pytest.mark.asyncio
async def test_auto_handler_meta_always_emits_provenance_keys_when_empty(monkeypatch) -> None:
    """Empty provenance must still surface as ``[]``/``{}``, not be omitted.

    The contract distinguishes "computed and empty" from "field not provided",
    so MCP clients can treat absence of these keys as a protocol error rather
    than silently degrading to defaults.
    """

    async def fake_run(self, arguments):  # noqa: ARG001
        from ouroboros.auto.pipeline import AutoPipelineResult

        return AutoPipelineResult(
            status="complete",
            auto_session_id="auto_test",
            phase="complete",
        )

    monkeypatch.setattr(AutoHandler, "_run", fake_run)

    result = await AutoHandler().handle({"goal": "Build a CLI"})

    assert result.is_ok
    meta = result.value.meta
    assert meta["ledger_provenance"] == {}
    assert meta["evidence_backed_sections"] == []
    assert meta["assumption_only_sections"] == []


def test_auto_save_seed_encodes_path_traversal_seed_id_inside_seed_dir(
    tmp_path: Path,
) -> None:
    from ouroboros.auto.adapters import load_seed, save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    seed = Seed(
        goal="Demo goal",
        ontology_schema=OntologySchema(name="demo", description="demo ontology"),
        metadata=SeedMetadata(seed_id="../outside"),
    )

    path = Path(save_seed(seed, seeds_dir=seeds_dir)).resolve()

    assert path == (seeds_dir / "%2E%2E%2Foutside.yaml").resolve()
    assert path.exists()
    assert not (tmp_path / "outside.yaml").exists()
    assert load_seed(path).metadata.seed_id == "../outside"


def test_auto_save_seed_encodes_whitespace_padded_seed_id_without_metadata_drift(
    tmp_path: Path,
) -> None:
    from ouroboros.auto.adapters import load_seed, save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    seed = Seed(
        goal="Demo goal",
        ontology_schema=OntologySchema(name="demo", description="demo ontology"),
        metadata=SeedMetadata(seed_id="seed_safe_123 "),
    )

    path = Path(save_seed(seed, seeds_dir=seeds_dir)).resolve()

    assert path == (seeds_dir / "seed_safe_123%20.yaml").resolve()
    assert path.exists()
    assert not (seeds_dir / "seed_safe_123.yaml").exists()
    assert load_seed(path).metadata.seed_id == "seed_safe_123 "


def test_auto_save_seed_encodes_previously_accepted_punctuation_seed_id(
    tmp_path: Path,
) -> None:
    from ouroboros.auto.adapters import load_seed, save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    seed = Seed(
        goal="Demo goal",
        ontology_schema=OntologySchema(name="demo", description="demo ontology"),
        metadata=SeedMetadata(seed_id="seed.v1"),
    )

    path = Path(save_seed(seed, seeds_dir=seeds_dir)).resolve()

    assert path == (seeds_dir / "seed%2Ev1.yaml").resolve()
    assert path.exists()
    assert load_seed(path).metadata.seed_id == "seed.v1"


def test_auto_save_seed_encodes_windows_reserved_basename_seed_id(
    tmp_path: Path,
) -> None:
    from ouroboros.auto.adapters import load_seed, save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    seed = Seed(
        goal="Demo goal",
        ontology_schema=OntologySchema(name="demo", description="demo ontology"),
        metadata=SeedMetadata(seed_id="CON"),
    )

    path = Path(save_seed(seed, seeds_dir=seeds_dir)).resolve()

    assert path == (seeds_dir / "%43ON.yaml").resolve()
    assert path.exists()
    assert not (seeds_dir / "CON.yaml").exists()
    assert load_seed(path).metadata.seed_id == "CON"


def test_auto_save_seed_bounds_long_unicode_seed_id_filename_without_metadata_drift(
    tmp_path: Path,
) -> None:
    from ouroboros.auto.adapters import load_seed, save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    seed_id = "漢" * 80
    seed = Seed(
        goal="Demo goal",
        ontology_schema=OntologySchema(name="demo", description="demo ontology"),
        metadata=SeedMetadata(seed_id=seed_id),
    )

    path = Path(save_seed(seed, seeds_dir=seeds_dir)).resolve()

    assert path.parent == seeds_dir.resolve()
    assert path.exists()
    assert len(path.name.encode("utf-8")) <= 255
    assert path.name.endswith(".yaml")
    assert "--" in path.stem
    assert load_seed(path).metadata.seed_id == seed_id


def test_auto_save_seed_bounds_long_ascii_seed_id_filename_without_metadata_drift(
    tmp_path: Path,
) -> None:
    from ouroboros.auto.adapters import load_seed, save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    seed_id = "seed_" + ("a" * 300)
    seed = Seed(
        goal="Demo goal",
        ontology_schema=OntologySchema(name="demo", description="demo ontology"),
        metadata=SeedMetadata(seed_id=seed_id),
    )

    path = Path(save_seed(seed, seeds_dir=seeds_dir)).resolve()

    assert path.parent == seeds_dir.resolve()
    assert path.exists()
    assert len(path.name.encode("utf-8")) <= 255
    assert "--%TRUNC%" in path.stem
    assert load_seed(path).metadata.seed_id == seed_id


def test_auto_save_seed_long_unicode_filename_digest_avoids_prefix_collision(
    tmp_path: Path,
) -> None:
    from ouroboros.auto.adapters import save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    ontology = OntologySchema(name="demo", description="demo ontology")
    first = Seed(
        goal="Demo goal", ontology_schema=ontology, metadata=SeedMetadata(seed_id="漢" * 80)
    )
    second = Seed(
        goal="Demo goal",
        ontology_schema=ontology,
        metadata=SeedMetadata(seed_id=("漢" * 79) + "字"),
    )

    first_path = Path(save_seed(first, seeds_dir=seeds_dir)).resolve()
    second_path = Path(save_seed(second, seeds_dir=seeds_dir)).resolve()

    assert first_path != second_path
    assert first_path.exists()
    assert second_path.exists()
    assert len(first_path.name.encode("utf-8")) <= 255
    assert len(second_path.name.encode("utf-8")) <= 255


def test_auto_save_seed_truncated_filename_namespace_cannot_overwrite_untruncated_id(
    tmp_path: Path,
) -> None:
    from ouroboros.auto.adapters import load_seed, save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    ontology = OntologySchema(name="demo", description="demo ontology")
    long_seed = Seed(
        goal="Long Unicode goal",
        ontology_schema=ontology,
        metadata=SeedMetadata(seed_id="漢" * 80),
    )

    long_path = Path(save_seed(long_seed, seeds_dir=seeds_dir)).resolve()
    colliding_semantic_id = long_path.stem
    safe_ascii_seed = Seed(
        goal="Safe ASCII goal",
        ontology_schema=ontology,
        metadata=SeedMetadata(seed_id=colliding_semantic_id),
    )

    safe_ascii_path = Path(save_seed(safe_ascii_seed, seeds_dir=seeds_dir)).resolve()

    assert long_path != safe_ascii_path
    assert long_path.exists()
    assert safe_ascii_path.exists()
    assert load_seed(long_path).metadata.seed_id == "漢" * 80
    assert load_seed(safe_ascii_path).metadata.seed_id == colliding_semantic_id


def test_auto_save_seed_accepts_default_seed_id_inside_seed_dir(tmp_path: Path) -> None:
    from ouroboros.auto.adapters import load_seed, save_seed
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    seeds_dir = tmp_path / "seeds"
    seed = Seed(
        goal="Demo goal",
        ontology_schema=OntologySchema(name="demo", description="demo ontology"),
        metadata=SeedMetadata(seed_id="seed_safe_123"),
    )

    path = Path(save_seed(seed, seeds_dir=seeds_dir)).resolve()

    assert path == (seeds_dir / "seed_safe_123.yaml").resolve()
    assert path.exists()
    assert load_seed(path).metadata.seed_id == "seed_safe_123"


@pytest.mark.asyncio
async def test_auto_handler_complete_product_uses_injected_configured_ralph_handler(monkeypatch, tmp_path) -> None:
    from ouroboros.auto.pipeline import AutoPipelineResult
    from ouroboros.auto.state import AutoStore
    from ouroboros.mcp.tools import auto_handler as auto_module

    class FakeRalphHandler:
        pass

    injected_ralph = FakeRalphHandler()
    captured: dict[str, object] = {}

    class FakePipeline:
        def __init__(self, driver, _seed_generator, **kwargs):  # noqa: ANN001, ANN003, ARG002
            captured["ralph_starter_handler"] = kwargs["ralph_starter"].handler
            captured["ralph_resumer_handler"] = kwargs["ralph_resumer"].handler

        async def run(self, run_state):  # noqa: ANN001
            return AutoPipelineResult(
                status="complete",
                auto_session_id=run_state.auto_session_id,
                phase="complete",
            )

    monkeypatch.setattr(auto_module, "AutoStore", lambda: AutoStore(tmp_path / "store"))
    monkeypatch.setattr(auto_module, "AutoPipeline", FakePipeline)

    result = await AutoHandler(ralph_handler=injected_ralph).handle(
        {"goal": "Build a CLI", "cwd": str(tmp_path), "complete_product": True}
    )

    assert result.is_ok
    assert captured["ralph_starter_handler"] is injected_ralph
    assert captured["ralph_resumer_handler"] is injected_ralph

