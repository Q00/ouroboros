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

import pytest

from ouroboros.bigbang.interview import InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


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
        return Result.err(_RecoverableProviderError("Question generation timed out"))

    async def save_state(self, state: InterviewState) -> Result[Path, MCPServerError]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"interview_{state.interview_id}.json"
        path.write_text(
            json.dumps({"interview_id": state.interview_id}),
            encoding="utf-8",
        )
        self.saved_states.append(state)
        return Result.ok(path)

    async def load_state(
        self, session_id: str
    ) -> Result[InterviewState, MCPServerError]:  # pragma: no cover - unused
        raise NotImplementedError


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
