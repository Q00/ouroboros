"""Regression tests for Q00/ouroboros#687.

The MCP ``ouroboros_interview`` subprocess path must persist the freshly-
created interview state and surface the ``session_id`` even when the first
question generation fails (e.g. LLM timeout).  Without this guarantee the
auto pipeline cannot resume a partially-started interview.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.tools.authoring_handlers import (
    InterviewHandler,
    _redact_interview_event_error_text,
)
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter


class _RecoverableProviderError(MCPServerError):
    """Test stand-in for ``ProviderError`` used by the interview engine."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.details: dict[str, object] = {"stderr": "simulated llm timeout"}


@dataclass(slots=True)
class _FakeInterviewEngine:
    """Minimal engine that mirrors the surface used by ``InterviewHandler``.

    ``start_interview`` writes the interview state to ``state_dir`` to mimic
    the real engine after Q00/ouroboros#687, and ``ask_next_question`` always
    fails so the handler must take the recoverable path.
    """

    state_dir: Path
    saved_states: list[InterviewState] = field(default_factory=list)
    states: dict[str, InterviewState] = field(default_factory=dict)
    question_error: Any | None = None

    async def start_interview(
        self, initial_context: str, cwd: str | None = None, interview_id: str | None = None
    ) -> Result[InterviewState, MCPServerError]:
        sid = interview_id or "interview_persistfail_001"
        state = InterviewState(
            interview_id=sid,
            initial_context=initial_context,
            status=InterviewStatus.IN_PROGRESS,
        )
        await self.save_state(state)
        return Result.ok(state)

    async def ask_next_question(self, state: InterviewState) -> Result[str, MCPServerError]:
        if self.question_error is not None:
            return Result.err(self.question_error)
        return Result.err(_RecoverableProviderError("Question generation timed out"))

    async def save_state(self, state: InterviewState) -> Result[Path, MCPServerError]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"interview_{state.interview_id}.json"
        path.write_text(
            json.dumps({"interview_id": state.interview_id}),
            encoding="utf-8",
        )
        self.saved_states.append(state)
        self.states[state.interview_id] = state
        return Result.ok(path)

    async def load_state(self, session_id: str) -> Result[InterviewState, MCPServerError]:
        return Result.ok(self.states[session_id])

    async def record_response(
        self,
        state: InterviewState,
        user_response: str,
        question: str,
    ) -> Result[InterviewState, MCPServerError]:
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=user_response,
            )
        )
        state.mark_updated()
        self.states[state.interview_id] = state
        return Result.ok(state)


@pytest.mark.asyncio
async def test_subprocess_handler_persists_session_id_on_question_failure(tmp_path: Path) -> None:
    engine = _FakeInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})

    assert outcome.is_ok, "handler must surface a recoverable result, not a hard error"
    mcp_result = outcome.value
    assert mcp_result.is_error is True
    meta = mcp_result.meta or {}
    session_id = meta.get("session_id")
    assert isinstance(session_id, str) and session_id, "meta must carry the persisted session_id"
    assert meta.get("recoverable") is True

    persisted = tmp_path / f"interview_{session_id}.json"
    assert persisted.exists(), (
        "interview state file must exist on disk after first-question failure"
    )
    assert engine.saved_states, "engine.save_state must have been invoked"


@pytest.mark.asyncio
async def test_tool_envelope_question_failure_hands_off_to_parent_session(
    tmp_path: Path,
) -> None:
    provider_error = ProviderError(
        "Question generator produced a ToolUseBlockViolation",
        provider="claude_code",
        details={"error_type": "ToolUseBlockViolation", "session_id": "claude-session-1"},
    )
    engine = _FakeInterviewEngine(state_dir=tmp_path, question_error=provider_error)
    mock_store = AsyncMock()
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=mock_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})
    await handler.close()

    assert outcome.is_ok
    mcp_result = outcome.value
    assert mcp_result.is_error is False
    meta = mcp_result.meta or {}
    session_id = meta.get("session_id")
    assert isinstance(session_id, str) and session_id
    assert meta["status"] == "parent_question_required"
    assert meta["recoverable"] is True
    assert meta["retry_mcp"] is False
    assert meta["ask_user_directly"] is True
    assert meta["last_question_required"] is True
    assert meta["reason_code"] == "question_generation_envelope_violation"
    assert meta["question_source"] == "parent_session"
    assert meta["provider_error_type"] == "ToolUseBlockViolation"

    response_text = mcp_result.content[0].text
    assert "Ask the user exactly one natural Socratic clarification question" in response_text
    assert "Do not mention MCP" in response_text
    assert "last_question=<the exact question you asked>" in response_text
    assert (tmp_path / f"interview_{session_id}.json").exists()

    event_types = [call.args[0].type for call in mock_store.append.await_args_list]
    assert "interview.question_generation.parent_handoff" in event_types
    assert "interview.failed" not in event_types


@pytest.mark.asyncio
async def test_first_question_parent_handoff_resume_records_last_question(
    tmp_path: Path,
) -> None:
    provider_error = ProviderError(
        "Question generator produced a ToolUseBlockViolation",
        provider="claude_code",
        details={"error_type": "ToolUseBlockViolation"},
    )
    engine = _FakeInterviewEngine(state_dir=tmp_path, question_error=provider_error)
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=AsyncMock(),
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    start = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})
    session_id = start.value.meta["session_id"]
    answer = await handler.handle(
        {
            "session_id": session_id,
            "answer": "It should scaffold plugin manifests.",
            "last_question": "What should the CLI do first?",
        }
    )
    await handler.close()

    assert answer.is_ok
    persisted_state = engine.states[session_id]
    assert persisted_state.rounds[-1].question == "What should the CLI do first?"
    assert persisted_state.rounds[-1].user_response == "It should scaffold plugin manifests."


@pytest.mark.asyncio
async def test_tool_envelope_failure_after_answer_hands_off_without_mcp_error(
    tmp_path: Path,
) -> None:
    provider_error = ProviderError(
        "Question generator produced a ToolUseBlockViolation",
        provider="claude_code",
        details={"error_type": "ToolUseBlockViolation"},
    )
    session_id = "interview_0123456789abcdef"
    state = InterviewState(
        interview_id=session_id,
        initial_context="Build a CLI",
        status=InterviewStatus.IN_PROGRESS,
    )
    state.rounds.append(
        InterviewRound(
            round_number=1,
            question="What should this CLI do first?",
            user_response="It should scaffold plugins.",
        )
    )
    engine = _FakeInterviewEngine(
        state_dir=tmp_path,
        question_error=provider_error,
        states={session_id: state},
    )
    await engine.save_state(state)
    mock_store = AsyncMock()
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=mock_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle(
        {
            "session_id": session_id,
            "answer": "Use the existing plugin manifest format.",
            "last_question": "Which manifest format should the CLI use?",
        }
    )
    await handler.close()

    assert outcome.is_ok
    mcp_result = outcome.value
    assert mcp_result.is_error is False
    meta = mcp_result.meta or {}
    assert meta["status"] == "parent_question_required"
    assert meta["ask_user_directly"] is True
    assert meta["last_question_required"] is True
    assert meta["retry_mcp"] is False
    assert meta["question_source"] == "parent_session"
    assert meta["interview_reasoning"]["phase"] == "next_question_parent_handoff"

    persisted_state = engine.states[session_id]
    assert persisted_state.rounds[-1].question == "Which manifest format should the CLI use?"
    assert persisted_state.rounds[-1].user_response == "Use the existing plugin manifest format."

    event_types = [call.args[0].type for call in mock_store.append.await_args_list]
    assert "interview.question_generation.parent_handoff" in event_types
    assert "interview.failed" not in event_types


@pytest.mark.asyncio
async def test_question_failure_event_uses_compact_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted interview failure events must not stringify full auth paths.

    Codex auth diagnostics intentionally keep structured context on the
    ProviderError, but lifecycle event text is a broader persistence/logging
    surface.  Use ``format_details()`` there so local ``CODEX_HOME`` / ``HOME``
    paths do not cross into ``interview.failed`` text.
    """
    home = Path("/Users/alice")
    codex_home = home / ".codex"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    auth_error = "401 Unauthorized from api.openai.com"
    details = CodexCliLLMAdapter._codex_failure_details(
        returncode=1,
        session_id="thread_path",
        stderr="",
        stdout_errors=[auth_error],
        message=auth_error,
    )
    provider_error = ProviderError(auth_error, provider="codex_cli", details=details)

    mock_store = AsyncMock()
    engine = _FakeInterviewEngine(state_dir=tmp_path, question_error=provider_error)
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=mock_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})
    await handler.close()

    assert outcome.is_ok
    failed_events = [
        call.args[0]
        for call in mock_store.append.await_args_list
        if getattr(call.args[0], "type", None) == "interview.failed"
    ]
    assert failed_events, "question-generation failure must emit interview.failed"
    event_error = failed_events[-1].data["error"]
    assert auth_error in event_error
    assert str(codex_home) not in event_error
    assert str(home) not in event_error
    assert "codex_auth_context" not in event_error


@pytest.mark.asyncio
async def test_question_failure_event_excludes_provider_path_diagnostics(tmp_path: Path) -> None:
    """Provider compact diagnostics are not automatically safe for lifecycle events."""
    provider_error = ProviderError(
        "Claude Agent SDK request failed in /Users/alice/workspace/project, see /tmp/project and C:\\Program Files\\Claude\\claude.exe and then https://api.openai.com/v1/responses plus cwd:/tmp/project+secrets(Old):v2; FileNotFoundError: '/tmp/foo'",
        provider="claude_code",
        details={
            "error_type": "RuntimeError",
            "session_id": "claude-session-1",
            "stderr": "trace mentions /Users/alice/.claude/config.json",
            "claudecode_present": True,
            "claude_code_entrypoint": r"C:\\Program Files\\Claude\\claude.exe",
            "configured_cli_path": "/opt/homebrew/bin/claude",
            "cwd": "/tmp",
            "env_override_keys": ["ANTHROPIC_API_KEY"],
        },
    )

    mock_store = AsyncMock()
    engine = _FakeInterviewEngine(state_dir=tmp_path, question_error=provider_error)
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=mock_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})
    await handler.close()

    assert outcome.is_ok
    failed_events = [
        call.args[0]
        for call in mock_store.append.await_args_list
        if getattr(call.args[0], "type", None) == "interview.failed"
    ]
    assert failed_events, "question-generation failure must emit interview.failed"
    event_error = failed_events[-1].data["error"]
    assert "Claude Agent SDK request failed" in event_error
    assert "error_type: RuntimeError" in event_error
    assert "session_id: claude-session-1" in event_error
    assert "/Users/alice" not in event_error
    assert "/opt/homebrew" not in event_error
    assert "/tmp" not in event_error
    assert "C:" not in event_error
    assert "Program Files" not in event_error
    assert r"Files\Claude" not in event_error
    assert "https://api.openai.com/v1/responses" in event_error
    assert "see [redacted path] and [redacted path] and then https://" in event_error
    assert "project+secrets" not in event_error
    assert "/tmp/foo" not in event_error
    assert "configured_cli_path" not in event_error
    assert "stderr" not in event_error
    assert "claude_code_entrypoint" not in event_error


def test_interview_failure_redactor_preserves_non_path_diagnostics() -> None:
    text = (
        "disk ratio 1/2 exceeded; use /api/v1/resource; "
        "failed at C:\\repo\\foo because timeout; "
        "path:'/Users/alice/.codex/config.json'; wrapped=(/tmp/wrapped); "
        "list=[/tmp/listed]; root=/root/.codex/auth.json; "
        "markdown=`/tmp/markdown`; angle=</tmp/angle>; "
        "/srv/app/config.yml /run/user/1000/codex.sock /bin/bash /nix/store/hash; "
        "endpoint https://api.openai.com/v1/responses"
    )

    redacted = _redact_interview_event_error_text(text)

    assert "disk ratio 1/2 exceeded" in redacted
    assert "use /api/v1/resource" in redacted
    assert "because timeout" in redacted
    assert "https://api.openai.com/v1/responses" in redacted
    assert "/Users/alice" not in redacted
    assert ".codex/config.json" not in redacted
    assert "/tmp/wrapped" not in redacted
    assert "/tmp/listed" not in redacted
    assert "/root/.codex/auth.json" not in redacted
    assert "/tmp/markdown" not in redacted
    assert "/tmp/angle" not in redacted
    assert "/srv/app/config.yml" not in redacted
    assert "/run/user/1000/codex.sock" not in redacted
    assert "/bin/bash" not in redacted
    assert "/nix/store/hash" not in redacted
    assert "C:" not in redacted


@pytest.mark.asyncio
async def test_subprocess_handler_honours_caller_supplied_interview_id(tmp_path: Path) -> None:
    """The auto driver pre-allocates an id; the handler must use it verbatim."""

    engine = _FakeInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    # Must match the strict server format ``interview_<16 hex>``.
    caller_id = "interview_0123456789abcdef"
    outcome = await handler.handle(
        {
            "initial_context": "Build a CLI",
            "cwd": str(tmp_path),
            "interview_id": caller_id,
        }
    )

    assert outcome.is_ok
    meta = outcome.value.meta or {}
    assert meta.get("session_id") == caller_id
    assert (tmp_path / f"interview_{caller_id}.json").exists()


@pytest.mark.asyncio
async def test_subprocess_handler_rejects_malformed_interview_id(tmp_path: Path) -> None:
    """A non-matching ``interview_id`` must hard-fail before any side effects."""

    engine = _FakeInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle(
        {
            "initial_context": "Build a CLI",
            "cwd": str(tmp_path),
            "interview_id": "not_in_server_format",
        }
    )

    assert outcome.is_err
    assert "server format" in str(outcome.error)
    assert engine.saved_states == [], "engine must not run when id is rejected"


@pytest.mark.asyncio
async def test_subprocess_handler_rejects_colliding_interview_id(tmp_path: Path) -> None:
    """Re-using an id that already has a state file must be refused."""

    caller_id = "interview_0123456789abcdef"
    # Pre-create the colliding file.
    (tmp_path / f"interview_{caller_id}.json").write_text("{}", encoding="utf-8")

    engine = _FakeInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle(
        {
            "initial_context": "Build a CLI",
            "cwd": str(tmp_path),
            "interview_id": caller_id,
        }
    )

    assert outcome.is_err
    assert "collide" in str(outcome.error)


@pytest.mark.asyncio
async def test_subprocess_handler_rejects_interview_id_on_resume_action(tmp_path: Path) -> None:
    """``interview_id`` is only valid for new interviews; resume must reject it."""

    engine = _FakeInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle(
        {
            "session_id": "interview_existingsession",
            "interview_id": "interview_0123456789abcdef",
        }
    )

    assert outcome.is_err
    assert "only valid for new interviews" in str(outcome.error)


@pytest.mark.asyncio
async def test_collision_check_targets_engine_state_dir_when_injected(tmp_path: Path) -> None:
    """Collision detection must follow the engine's state_dir, not handler.data_dir.

    Models the production wiring where ``create_ouroboros_server`` injects an
    ``InterviewEngine`` with a custom ``state_dir`` while ``handler.data_dir``
    may be unset or stale.  See Q00/ouroboros#723 review.
    """
    engine_dir = tmp_path / "engine"
    handler_data_dir = tmp_path / "handler"
    engine_dir.mkdir()
    handler_data_dir.mkdir()

    caller_id = "interview_0123456789abcdef"
    # Pre-create the colliding file ONLY in the engine directory.
    (engine_dir / f"interview_{caller_id}.json").write_text("{}", encoding="utf-8")

    engine = _FakeInterviewEngine(state_dir=engine_dir)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=handler_data_dir,
    )

    outcome = await handler.handle(
        {
            "initial_context": "Build a CLI",
            "cwd": str(tmp_path),
            "interview_id": caller_id,
        }
    )

    assert outcome.is_err, "collision must be detected against the engine's state_dir"
    assert "collide" in str(outcome.error)


def test_handler_persistence_probe_routes_through_engine_state_dir(tmp_path: Path) -> None:
    """``HandlerInterviewBackend.is_session_persisted`` must use the engine dir."""
    from ouroboros.auto.adapters import HandlerInterviewBackend

    engine_dir = tmp_path / "engine"
    handler_data_dir = tmp_path / "handler"
    engine_dir.mkdir()
    handler_data_dir.mkdir()

    sid = "interview_0123456789abcdef"
    # Persisted only in the engine dir.
    (engine_dir / f"interview_{sid}.json").write_text("{}", encoding="utf-8")

    engine = _FakeInterviewEngine(state_dir=engine_dir)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=handler_data_dir,
    )
    backend = HandlerInterviewBackend(handler, cwd=str(tmp_path))

    assert backend.is_session_persisted(sid) is True
    assert backend.is_session_persisted("interview_aaaaaaaaaaaaaaaa") is False


@pytest.mark.asyncio
async def test_plugin_path_writes_state_into_engine_state_dir(tmp_path: Path, monkeypatch) -> None:
    """Plugin path must persist into the engine's state_dir, not handler.data_dir.

    Regression for the Q00/ouroboros#723 bot review: the plugin path used to
    read/write via ``self.data_dir or _DATA_DIR`` while the collision check
    consulted ``engine.state_dir``.  After routing through
    ``resolved_state_dir`` both sides agree, so a custom-state_dir
    deployment can resume the interview it just started.
    """
    import ouroboros.mcp.tools.authoring_handlers as ah

    engine_dir = tmp_path / "engine"
    handler_data_dir = tmp_path / "handler"
    engine_dir.mkdir()
    handler_data_dir.mkdir()

    saved_paths: list[Path] = []

    async def _capturing_save(state_dir, state):  # type: ignore[no-untyped-def]
        path = state_dir / f"interview_{state.interview_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        saved_paths.append(path)
        return Result.ok(path)

    monkeypatch.setattr(ah, "_plugin_save_state", _capturing_save)

    engine = _FakeInterviewEngine(state_dir=engine_dir)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
        data_dir=handler_data_dir,
    )

    outcome = await handler.handle(
        {"initial_context": "Build a CLI", "cwd": str(tmp_path)},
    )

    assert outcome.is_ok
    assert saved_paths, "plugin path must invoke _plugin_save_state"
    saved = saved_paths[0]
    assert saved.parent == engine_dir, (
        f"plugin save must land in engine.state_dir; saw {saved.parent}"
    )
