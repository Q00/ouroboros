"""Tests for handler subagent wiring.

Verifies that ALL LLM-requiring handlers return _subagent dispatch payloads
instead of calling LLMs directly. Each handler.handle() should:
1. Still validate required arguments (return errors for missing args)
2. Return Result.ok(MCPToolResult) with meta["_subagent"] for valid args
3. Include correct tool_name in the payload
4. Include original arguments in context for round-trip
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result

# ---------------------------------------------------------------------------
# QAHandler
# ---------------------------------------------------------------------------


class TestQAHandlerSubagentDispatch:
    """QAHandler.handle() returns _subagent payload."""

    @pytest.fixture
    def handler(self):
        from ouroboros.mcp.tools.qa import QAHandler

        return QAHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    async def test_returns_subagent_for_valid_args(self, handler) -> None:
        result = await handler.handle(
            {
                "artifact": "def foo(): pass",
                "quality_bar": "All functions have docstrings",
            }
        )
        assert result.is_ok
        mcp_result = result.value
        assert "_subagent" in mcp_result.meta
        assert mcp_result.meta["_subagent"]["tool_name"] == "ouroboros_qa"

    async def test_still_validates_missing_artifact(self, handler) -> None:
        result = await handler.handle({"quality_bar": "good"})
        assert result.is_err

    async def test_still_validates_missing_quality_bar(self, handler) -> None:
        result = await handler.handle({"artifact": "code"})
        assert result.is_err

    async def test_context_includes_arguments(self, handler) -> None:
        result = await handler.handle(
            {
                "artifact": "my code",
                "quality_bar": "no bugs",
                "artifact_type": "document",
            }
        )
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["artifact"] == "my code"
        assert ctx["quality_bar"] == "no bugs"
        assert ctx["artifact_type"] == "document"

    async def test_no_llm_adapter_called(self, handler) -> None:
        """Verify no LLM adapter is created or called."""
        with patch("ouroboros.mcp.tools.qa.create_llm_adapter") as mock_create:
            result = await handler.handle(
                {
                    "artifact": "code",
                    "quality_bar": "good",
                }
            )
            assert result.is_ok
            mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# GenerateSeedHandler
# ---------------------------------------------------------------------------


class TestGenerateSeedHandlerSubagentDispatch:
    """GenerateSeedHandler.handle() returns _subagent payload."""

    @pytest.fixture
    def handler(self):
        from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler

        return GenerateSeedHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    async def test_returns_subagent_for_valid_args(self, handler) -> None:
        result = await handler.handle({"session_id": "sess-123"})
        assert result.is_ok
        assert "_subagent" in result.value.meta
        assert result.value.meta["_subagent"]["tool_name"] == "ouroboros_generate_seed"

    async def test_still_validates_missing_session_id(self, handler) -> None:
        result = await handler.handle({})
        assert result.is_err

    async def test_context_has_session_id(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-456",
                "ambiguity_score": 0.15,
            }
        )
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["session_id"] == "sess-456"
        assert ctx["ambiguity_score"] == 0.15


# ---------------------------------------------------------------------------
# InterviewHandler
# ---------------------------------------------------------------------------


class TestInterviewHandlerSubagentDispatch:
    """InterviewHandler.handle() returns _subagent payload."""

    @pytest.fixture(autouse=True)
    def mock_engine_io(self, monkeypatch):
        """Mock load/save so plugin path doesn't need real state files."""

        async def _fake_load(self, session_id):
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                rounds=[InterviewRound(round_number=1, question="Q?", user_response=None)],
            )
            return Result.ok(state)

        async def _fake_save(self, state):
            pass

        from ouroboros.bigbang.interview import InterviewEngine

        monkeypatch.setattr(InterviewEngine, "load_state", _fake_load)
        monkeypatch.setattr(InterviewEngine, "save_state", _fake_save)

    @pytest.fixture
    def handler(self):
        from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

        return InterviewHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    async def test_start_returns_subagent(self, handler) -> None:
        result = await handler.handle(
            {
                "initial_context": "Build a web app",
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_interview"
        assert "Build a web app" in payload["prompt"]

    async def test_answer_returns_subagent(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-123",
                "answer": "Use Python",
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_interview"
        assert "Use Python" in payload["prompt"]

    async def test_resume_returns_subagent(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-123",
            }
        )
        assert result.is_ok
        assert result.value.meta["_subagent"]["tool_name"] == "ouroboros_interview"


# ---------------------------------------------------------------------------
# EvaluateHandler
# ---------------------------------------------------------------------------


class TestEvaluateHandlerSubagentDispatch:
    """EvaluateHandler.handle() returns _subagent payload."""

    @pytest.fixture
    def handler(self):
        from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler

        return EvaluateHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    async def test_returns_subagent_for_valid_args(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-123",
                "artifact": "def main(): pass",
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_evaluate"

    async def test_still_validates_missing_session_id(self, handler) -> None:
        result = await handler.handle({"artifact": "code"})
        assert result.is_err

    async def test_still_validates_missing_artifact(self, handler) -> None:
        result = await handler.handle({"session_id": "sess-123"})
        assert result.is_err

    async def test_context_includes_all_args(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-123",
                "artifact": "code",
                "seed_content": "goal: test",
                "trigger_consensus": True,
            }
        )
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["session_id"] == "sess-123"
        assert ctx["seed_content"] == "goal: test"
        assert ctx["trigger_consensus"] is True


# ---------------------------------------------------------------------------
# ExecuteSeedHandler
# ---------------------------------------------------------------------------


class TestExecuteSeedHandlerSubagentDispatch:
    """ExecuteSeedHandler.handle() returns _subagent payload."""

    @pytest.fixture
    def handler(self):
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler

        return ExecuteSeedHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    async def test_returns_subagent_for_valid_args(self, handler) -> None:
        result = await handler.handle(
            {
                "seed_content": "goal: build it\nconstraints: []\nacceptance_criteria: [tests pass]",
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_execute_seed"

    async def test_still_validates_missing_seed(self, handler) -> None:
        result = await handler.handle({})
        assert result.is_err

    async def test_context_has_execution_args(self, handler) -> None:
        result = await handler.handle(
            {
                "seed_content": "goal: test",
                "max_iterations": 5,
                "skip_qa": True,
            }
        )
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["max_iterations"] == 5
        assert ctx["skip_qa"] is True


# ---------------------------------------------------------------------------
# StartExecuteSeedHandler
# ---------------------------------------------------------------------------


class TestStartExecuteSeedHandlerSubagentDispatch:
    """StartExecuteSeedHandler.handle() returns _subagent payload."""

    @pytest.fixture
    async def handler(self):
        from ouroboros.mcp.job_manager import JobManager
        from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
        from ouroboros.persistence.event_store import EventStore

        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        jm = JobManager(store)
        handler = StartExecuteSeedHandler(
            execute_handler=MagicMock(),
            event_store=store,
            job_manager=jm,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        yield handler
        await store.close()

    async def test_returns_subagent_for_valid_args(self, handler) -> None:
        result = await handler.handle(
            {
                "seed_content": "goal: build it",
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_execute_seed"

    async def test_still_validates_missing_seed(self, handler) -> None:
        result = await handler.handle({})
        assert result.is_err

    async def test_plugin_mode_returns_no_job_id(self, handler) -> None:
        """Plugin path delegates to host — no fake job_id."""
        result = await handler.handle({"seed_content": "goal: test"})
        assert result.is_ok
        assert result.value.meta["job_id"] is None
        assert result.value.meta["status"] == "delegated_to_plugin"


# ---------------------------------------------------------------------------
# PMInterviewHandler
# ---------------------------------------------------------------------------


class TestPMInterviewHandlerSubagentDispatch:
    """PMInterviewHandler.handle() returns _subagent payload."""

    @pytest.fixture(autouse=True)
    def mock_engine_io(self, monkeypatch):
        """Mock load/save so plugin path doesn't need real state files."""

        async def _fake_load(self, session_id):
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                rounds=[InterviewRound(round_number=1, question="Q?", user_response=None)],
            )
            return Result.ok(state)

        async def _fake_save(self, state):
            pass

        from ouroboros.bigbang.interview import InterviewEngine

        monkeypatch.setattr(InterviewEngine, "load_state", _fake_load)
        monkeypatch.setattr(InterviewEngine, "save_state", _fake_save)

    @pytest.fixture
    def handler(self):
        from ouroboros.mcp.tools.pm_handler import PMInterviewHandler

        return PMInterviewHandler(agent_runtime_backend="opencode", opencode_mode="plugin")

    async def test_start_returns_subagent(self, handler) -> None:
        result = await handler.handle(
            {
                "initial_context": "E-commerce site",
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_pm_interview"

    async def test_resume_with_answer_returns_subagent(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-123",
                "answer": "React + Node.js",
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert "React + Node.js" in payload["prompt"]

    async def test_generate_returns_subagent(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-123",
                "action": "generate",
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_pm_interview"

    async def test_context_preserves_selected_repos(self, handler) -> None:
        result = await handler.handle(
            {
                "initial_context": "site",
                "selected_repos": ["/repo1", "/repo2"],
            }
        )
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["selected_repos"] == ["/repo1", "/repo2"]
