"""Tests for PR #442 round-5 reviewer fixes.

Issue #1: start_* plugin-mode does NOT create fake JobManager record.
          Returns job_id=None, status=delegated_to_plugin.
Issue #2: get_ouroboros_tools wires all plugin-capable handlers.
Issue extra: lateral_think single persona dispatches as subagent in plugin mode.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.persistence.event_store import EventStore

# ---------------------------------------------------------------------------
# Shared mock helpers for plugin I/O
# ---------------------------------------------------------------------------


async def _noop_save(state_dir: Path, state: InterviewState) -> Result[Path, str]:
    """Mock ``_plugin_save_state`` — mirrors real signature, no disk I/O.

    Returns a realistic path built from *state_dir* + *interview_id* so
    callers that inspect the result get a plausible ``Path`` object rather
    than a hard-coded ``/tmp/fake``.
    """
    return Result.ok(state_dir / f"interview_{state.interview_id}.json")


def _make_capturing_save(
    capture_list: list[InterviewState],
):
    """Factory: returns a ``_plugin_save_state`` mock that records every state.

    The returned coroutine appends *state* to *capture_list* before
    returning ``Result.ok`` so tests can assert on persisted state without
    touching the filesystem.
    """

    async def _save(state_dir: Path, state: InterviewState) -> Result[Path, str]:
        capture_list.append(state)
        return Result.ok(state_dir / f"interview_{state.interview_id}.json")

    return _save


# ---------------------------------------------------------------------------
# Issue #1: StartExecuteSeedHandler plugin-mode — no fake job
# ---------------------------------------------------------------------------


class TestStartExecuteSeedPluginJobId:
    """start_execute_seed in plugin mode returns delegation receipt, no fake job."""

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store):
        from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler

        return StartExecuteSeedHandler(
            execute_handler=MagicMock(),
            event_store=event_store,
            job_manager=None,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    async def test_returns_none_job_id(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is None

    async def test_status_is_delegated_to_plugin(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        assert result.value.meta["status"] == "delegated_to_plugin"

    async def test_subagent_payload_still_present(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        assert "_subagent" in result.value.meta
        assert result.value.meta["_subagent"]["tool_name"] == "ouroboros_execute_seed"

    async def test_dispatch_mode_is_plugin(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        assert result.value.meta["dispatch_mode"] == "plugin"


# ---------------------------------------------------------------------------
# Issue #1: StartEvolveStepHandler plugin-mode — no fake job
# ---------------------------------------------------------------------------


class TestStartEvolveStepPluginJobId:
    """start_evolve_step in plugin mode returns delegation receipt, no fake job."""

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store):
        from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler

        return StartEvolveStepHandler(
            evolve_handler=MagicMock(),
            event_store=event_store,
            job_manager=None,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    async def test_returns_none_job_id(self, handler) -> None:
        result = await handler.handle({"lineage_id": "lin-abc"})
        assert result.is_ok
        meta = result.value.meta
        assert meta["job_id"] is None

    async def test_status_is_delegated_to_plugin(self, handler) -> None:
        result = await handler.handle({"lineage_id": "lin-abc"})
        assert result.value.meta["status"] == "delegated_to_plugin"

    async def test_subagent_payload_still_present(self, handler) -> None:
        result = await handler.handle({"lineage_id": "lin-abc"})
        assert "_subagent" in result.value.meta
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_evolve_step"

    async def test_lineage_id_in_response(self, handler) -> None:
        result = await handler.handle({"lineage_id": "lin-abc"})
        assert result.value.meta["lineage_id"] == "lin-abc"


# ---------------------------------------------------------------------------
# Issue #2: get_ouroboros_tools wires plugin-capable handlers
# ---------------------------------------------------------------------------


class TestGetOuroborosToolsPluginWiring:
    """get_ouroboros_tools threads runtime/mode to all plugin handlers."""

    def test_lateral_think_handler_wired(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler

        tools = get_ouroboros_tools(runtime_backend="opencode", opencode_mode="plugin")
        h = next(t for t in tools if isinstance(t, LateralThinkHandler))
        assert h.agent_runtime_backend == "opencode"
        assert h.opencode_mode == "plugin"

    def test_evolve_step_handler_wired(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler

        tools = get_ouroboros_tools(runtime_backend="opencode", opencode_mode="plugin")
        h = next(t for t in tools if isinstance(t, EvolveStepHandler))
        assert h.agent_runtime_backend == "opencode"
        assert h.opencode_mode == "plugin"

    def test_start_evolve_step_handler_wired(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler

        tools = get_ouroboros_tools(runtime_backend="opencode", opencode_mode="plugin")
        h = next(t for t in tools if isinstance(t, StartEvolveStepHandler))
        assert h.agent_runtime_backend == "opencode"
        assert h.opencode_mode == "plugin"

    def test_start_evolve_inner_handler_also_wired(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler

        tools = get_ouroboros_tools(runtime_backend="opencode", opencode_mode="plugin")
        h = next(t for t in tools if isinstance(t, StartEvolveStepHandler))
        inner = h._evolve_handler
        assert inner.agent_runtime_backend == "opencode"
        assert inner.opencode_mode == "plugin"

    def test_factory_fns_accept_kwargs(self) -> None:
        from ouroboros.mcp.tools.definitions import (
            evolve_step_handler,
            lateral_think_handler,
            start_evolve_step_handler,
        )

        lt = lateral_think_handler(runtime_backend="opencode", opencode_mode="plugin")
        assert lt.agent_runtime_backend == "opencode"
        assert lt.opencode_mode == "plugin"

        ev = evolve_step_handler(runtime_backend="opencode", opencode_mode="plugin")
        assert ev.agent_runtime_backend == "opencode"
        assert ev.opencode_mode == "plugin"

        sev = start_evolve_step_handler(runtime_backend="opencode", opencode_mode="plugin")
        assert sev.agent_runtime_backend == "opencode"
        assert sev.opencode_mode == "plugin"

    def test_total_tool_count_unchanged(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools

        tools = get_ouroboros_tools()
        assert len(tools) == 24


# ---------------------------------------------------------------------------
# Lateral think: single persona dispatches as subagent in plugin mode
# ---------------------------------------------------------------------------


class TestLateralThinkSinglePersonaDispatch:
    """Single-persona lateral_think dispatches subagent when plugin mode."""

    @pytest.fixture
    def handler(self):
        from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler

        return LateralThinkHandler(
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    @pytest.fixture
    def handler_subprocess(self):
        from ouroboros.mcp.tools.evaluation_handlers import LateralThinkHandler

        return LateralThinkHandler(
            agent_runtime_backend="opencode",
            opencode_mode="subprocess",
        )

    @pytest.mark.asyncio
    async def test_single_persona_dispatches_in_plugin_mode(self, handler) -> None:
        import json

        r = await handler.handle(
            {
                "problem_context": "test problem",
                "current_approach": "test approach",
                "persona": "hacker",
            }
        )
        assert r.is_ok
        data = json.loads(r.value.content[0].text)
        assert "_subagent" in data
        assert data["persona"] == "hacker"
        assert data["dispatch_mode"] == "plugin"
        assert data["status"] == "delegated_to_subagent"

    @pytest.mark.asyncio
    async def test_single_persona_inline_in_subprocess_mode(self, handler_subprocess) -> None:
        r = await handler_subprocess.handle(
            {
                "problem_context": "test problem",
                "current_approach": "test approach",
                "persona": "hacker",
            }
        )
        assert r.is_ok
        # Inline mode returns text, not JSON with _subagent
        text = r.value.content[0].text
        assert "Lateral Thinking" in text
        assert r.value.meta.get("persona") == "hacker"

    @pytest.mark.asyncio
    async def test_single_persona_payload_has_correct_tool(self, handler) -> None:
        import json

        r = await handler.handle(
            {
                "problem_context": "stuck on auth",
                "current_approach": "JWT tokens",
                "persona": "contrarian",
            }
        )
        data = json.loads(r.value.content[0].text)
        assert data["_subagent"]["tool_name"] == "ouroboros_lateral_think"
        assert data["_subagent"]["title"] == "Lateral (contrarian)"

    @pytest.mark.asyncio
    async def test_all_five_personas_dispatch_single(self, handler) -> None:
        """Each single persona value dispatches as subagent."""
        import json

        for persona in ("hacker", "researcher", "simplifier", "architect", "contrarian"):
            r = await handler.handle(
                {
                    "problem_context": "test",
                    "current_approach": "test",
                    "persona": persona,
                }
            )
            assert r.is_ok, f"{persona} failed: {r.error}"
            data = json.loads(r.value.content[0].text)
            assert "_subagent" in data, f"{persona} missing _subagent"
            assert data["persona"] == persona


# ---------------------------------------------------------------------------
# Blocker #1 (actual Round 5): InterviewHandler validates before plugin dispatch
# ---------------------------------------------------------------------------


class TestInterviewHandlerValidationBeforeDispatch:
    """Empty args or answer-without-session_id must error, not dispatch."""

    @pytest.fixture(autouse=True)
    def mock_plugin_io(self, monkeypatch):
        """Mock _plugin_load/save so plugin path doesn't need real state files."""

        async def _fake_load(state_dir: Path, session_id: str) -> Result[InterviewState, str]:
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                rounds=[InterviewRound(round_number=1, question="Q?", user_response=None)],
            )
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _fake_load)
        monkeypatch.setattr(ah, "_plugin_save_state", _noop_save)

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store):
        from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

        return InterviewHandler(
            llm_backend="openai",
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    async def test_empty_args_returns_error(self, handler) -> None:
        """Empty {} must NOT dispatch — returns validation error."""
        result = await handler.handle({})
        assert result.is_err
        assert (
            "initial_context" in str(result.error).lower()
            or "session_id" in str(result.error).lower()
        )

    async def test_answer_without_session_id_returns_error(self, handler) -> None:
        """answer='foo' without session_id must NOT dispatch."""
        result = await handler.handle({"answer": "some answer"})
        assert result.is_err
        assert (
            "session_id" in str(result.error).lower()
            or "initial_context" in str(result.error).lower()
        )

    async def test_valid_start_dispatches(self, handler) -> None:
        """initial_context present → should dispatch (not error)."""
        import json

        result = await handler.handle({"initial_context": "build a CLI tool"})
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        assert "_subagent" in data

    async def test_valid_resume_dispatches(self, handler) -> None:
        """session_id present → should dispatch (not error)."""
        import json

        result = await handler.handle({"session_id": "ses_abc123"})
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        assert "_subagent" in data

    async def test_valid_answer_dispatches(self, handler) -> None:
        """session_id + answer → should dispatch (not error)."""
        import json

        result = await handler.handle({"session_id": "ses_abc123", "answer": "yes"})
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        assert "_subagent" in data


# ---------------------------------------------------------------------------
# Blocker #2 (actual Round 5): PMInterviewHandler validates before plugin dispatch
# ---------------------------------------------------------------------------


class TestPMInterviewHandlerValidationBeforeDispatch:
    """Invalid action+args combos must error, not dispatch."""

    @pytest.fixture(autouse=True)
    def mock_plugin_io(self, monkeypatch):
        """Mock plugin I/O + pm_meta so plugin path doesn't need real state files."""

        async def _fake_load(state_dir: Path, session_id: str) -> Result[InterviewState, str]:
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                status=InterviewStatus.COMPLETED,
                rounds=[InterviewRound(round_number=1, question="Q?", user_response=None)],
            )
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah
        import ouroboros.mcp.tools.pm_handler as pmh

        monkeypatch.setattr(ah, "_plugin_load_state", _fake_load)
        monkeypatch.setattr(ah, "_plugin_save_state", _noop_save)
        # Mock pm_meta persistence (no disk needed in tests)
        monkeypatch.setattr(pmh, "_save_pm_meta", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            pmh, "_load_pm_meta", lambda *_a, **_kw: {"initial_context": "test", "cwd": "/tmp"}
        )

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store):
        from ouroboros.mcp.tools.pm_handler import PMInterviewHandler

        return PMInterviewHandler(
            llm_backend="openai",
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    async def test_empty_args_returns_error(self, handler) -> None:
        """Empty {} must NOT dispatch — returns validation error."""
        result = await handler.handle({})
        assert result.is_err
        assert (
            "initial_context" in str(result.error).lower()
            or "session_id" in str(result.error).lower()
        )

    async def test_generate_without_session_id_returns_error(self, handler) -> None:
        """action=generate without session_id must error."""
        result = await handler.handle({"action": "generate"})
        assert result.is_err

    async def test_resume_without_session_id_returns_error(self, handler) -> None:
        """action=resume (explicit) without session_id must error."""
        result = await handler.handle({"action": "resume"})
        assert result.is_err

    async def test_valid_start_dispatches(self, handler) -> None:
        """initial_context present → should dispatch."""
        import json

        result = await handler.handle({"initial_context": "invoice SaaS app"})
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        assert "_subagent" in data

    async def test_valid_resume_dispatches(self, handler) -> None:
        """session_id present → should dispatch."""
        import json

        result = await handler.handle({"session_id": "ses_pm_123"})
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        assert "_subagent" in data

    async def test_valid_generate_dispatches(self, handler) -> None:
        """action=generate + session_id → should dispatch."""
        import json

        result = await handler.handle({"action": "generate", "session_id": "ses_pm_123"})
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        assert "_subagent" in data


# ---------------------------------------------------------------------------
# Regression: PM brownfield repos survive across plugin turns
# ---------------------------------------------------------------------------


class TestPMBrownfieldReposPersistence:
    """Brownfield repos set at start/select_repos must be present in resume/generate."""

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store, monkeypatch, tmp_path):
        """PM handler with real pm_meta persistence but mocked plugin I/O."""
        from ouroboros.mcp.tools.pm_handler import PMInterviewHandler

        # Use real _save_pm_meta / _load_pm_meta but pointed at tmp_path
        h = PMInterviewHandler(
            llm_backend="openai",
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
            data_dir=tmp_path,
        )

        # Mock plugin state I/O (no real state files)
        async def _fake_load(state_dir: Path, session_id: str) -> Result[InterviewState, str]:
            state = InterviewState(
                interview_id=session_id,
                initial_context="test brownfield app",
                status=InterviewStatus.COMPLETED,
                rounds=[
                    InterviewRound(round_number=1, question="Q?", user_response="A1"),
                ],
            )
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _fake_load)
        monkeypatch.setattr(ah, "_plugin_save_state", _noop_save)

        return h

    async def test_1step_start_persists_selected_repos(self, handler, monkeypatch) -> None:
        """1-step start (initial_context + selected_repos) persists repos in pm_meta."""
        import json

        monkeypatch.setattr(
            "ouroboros.mcp.tools.pm_handler.resolve_initial_context_input",
            lambda ctx, cwd="": Result.ok(ctx),  # noqa: ARG005
        )
        monkeypatch.setattr(
            "ouroboros.core.security.InputValidator.validate_initial_context",
            staticmethod(lambda ctx: (True, None)),  # noqa: ARG005
        )

        repos = ["/home/user/project-a", "/home/user/project-b"]
        result = await handler.handle(
            {
                "initial_context": "brownfield app",
                "selected_repos": repos,
            }
        )
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        session_id = data.get("session_id")
        assert session_id

        # Verify pm_meta persisted the caller's selected_repos
        from ouroboros.mcp.tools.pm_handler import _load_pm_meta

        meta = _load_pm_meta(session_id, data_dir=handler.data_dir)
        assert meta is not None
        assert meta["brownfield_repos"] == repos

    async def test_resume_restores_repos_from_meta(self, handler, monkeypatch) -> None:
        """Resume without selected_repos still gets repos from pm_meta."""
        import json

        from ouroboros.mcp.tools.pm_handler import _save_pm_meta

        session_id = "ses_brownfield_test"
        repos = ["/repo/alpha", "/repo/beta"]

        # Pre-persist pm_meta with repos
        _save_pm_meta(
            session_id,
            engine=None,
            cwd="/tmp",
            data_dir=handler.data_dir,
            extra={"initial_context": "test", "brownfield_repos": repos},
        )

        # Resume WITHOUT passing selected_repos
        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "yes, those repos",
            }
        )
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        subagent = data.get("_subagent", {})
        context = subagent.get("context", {})
        assert context.get("selected_repos") == repos

    async def test_generate_restores_repos_from_meta(self, handler, monkeypatch) -> None:
        """Generate without selected_repos still gets repos from pm_meta."""
        import json

        from ouroboros.mcp.tools.pm_handler import _save_pm_meta

        session_id = "ses_brownfield_gen"
        repos = ["/repo/gamma"]

        _save_pm_meta(
            session_id,
            engine=None,
            cwd="/tmp",
            data_dir=handler.data_dir,
            extra={"initial_context": "test", "brownfield_repos": repos},
        )

        result = await handler.handle(
            {
                "action": "generate",
                "session_id": session_id,
            }
        )
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        subagent = data.get("_subagent", {})
        context = subagent.get("context", {})
        assert context.get("selected_repos") == repos


# ---------------------------------------------------------------------------
# Bot R11: last_question roundtrip — InterviewHandler
# ---------------------------------------------------------------------------


class TestInterviewLastQuestionRoundtrip:
    """Verify last_question param persists real question text in interview rounds.

    Bot R11 finding: round 2+ child sessions receive transcripts with placeholder
    question ``(continued from subagent)`` instead of the real question the child
    session asked.  The ``last_question`` parameter lets the parent LLM relay the
    child's question back so it's persisted correctly.
    """

    @pytest.fixture(autouse=True)
    def capture_save(self, monkeypatch):
        """Mock plugin I/O and capture saved state for assertions."""
        self.saved_states: list[InterviewState] = []

        async def _fake_load(state_dir: Path, session_id: str) -> Result[InterviewState, str]:
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                rounds=[],  # No rounds yet — simulates first answer after start
            )
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _fake_load)
        monkeypatch.setattr(ah, "_plugin_save_state", _make_capturing_save(self.saved_states))

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store):
        from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

        return InterviewHandler(
            llm_backend="openai",
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    async def test_last_question_persisted_in_new_round(self, handler) -> None:
        """answer + last_question → round question uses real text, not placeholder."""
        result = await handler.handle(
            {
                "session_id": "ses_lq_1",
                "answer": "I want a REST API",
                "last_question": "What kind of interface does your app need?",
            }
        )
        assert result.is_ok
        assert len(self.saved_states) == 1
        state = self.saved_states[0]
        assert len(state.rounds) == 1
        assert state.rounds[0].question == "What kind of interface does your app need?"
        assert state.rounds[0].user_response == "I want a REST API"

    async def test_no_last_question_uses_placeholder(self, handler) -> None:
        """answer without last_question → falls back to placeholder (backward compat)."""
        result = await handler.handle(
            {
                "session_id": "ses_lq_2",
                "answer": "Python please",
            }
        )
        assert result.is_ok
        assert len(self.saved_states) == 1
        state = self.saved_states[0]
        assert len(state.rounds) == 1
        assert state.rounds[0].question == "(continued from subagent)"
        assert state.rounds[0].user_response == "Python please"

    async def test_last_question_updates_existing_round(self, monkeypatch, handler) -> None:
        """When round exists with question but no answer, last_question updates it."""

        # Override _fake_load to return state with an unanswered round
        async def _load_with_round(state_dir, session_id):
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                rounds=[
                    InterviewRound(
                        round_number=1,
                        question="(continued from subagent)",
                        user_response=None,
                    )
                ],
            )
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _load_with_round)

        result = await handler.handle(
            {
                "session_id": "ses_lq_3",
                "answer": "yes, that's right",
                "last_question": "Do you want database persistence?",
            }
        )
        assert result.is_ok
        assert len(self.saved_states) == 1
        state = self.saved_states[0]
        assert len(state.rounds) == 1
        # Question text should be updated from placeholder to real text
        assert state.rounds[0].question == "Do you want database persistence?"
        assert state.rounds[0].user_response == "yes, that's right"

    async def test_response_includes_next_turn_hint(self, handler) -> None:
        """Plugin response includes next_turn_hint telling parent to pass last_question."""
        import json

        result = await handler.handle(
            {
                "session_id": "ses_lq_hint",
                "answer": "test answer",
            }
        )
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        # response_shape keys are merged at top level of body (see build_subagent_result)
        assert "last_question" in data.get("next_turn_hint", "")


# ---------------------------------------------------------------------------
# Bot R11: last_question roundtrip — PMInterviewHandler
# ---------------------------------------------------------------------------


class TestPMInterviewLastQuestionRoundtrip:
    """Verify last_question param persists real question text in PM interview rounds.

    Same issue as InterviewHandler: plugin-mode child sessions generate questions
    but can't write back to server-side state.  last_question lets parent relay.
    """

    @pytest.fixture(autouse=True)
    def capture_save(self, monkeypatch):
        """Mock plugin I/O + pm_meta, capture saved state."""
        self.saved_states: list[InterviewState] = []

        async def _fake_load(state_dir: Path, session_id: str) -> Result[InterviewState, str]:
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                rounds=[],
            )
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah
        import ouroboros.mcp.tools.pm_handler as pmh

        monkeypatch.setattr(ah, "_plugin_load_state", _fake_load)
        monkeypatch.setattr(ah, "_plugin_save_state", _make_capturing_save(self.saved_states))
        monkeypatch.setattr(pmh, "_save_pm_meta", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            pmh,
            "_load_pm_meta",
            lambda *_a, **_kw: {"initial_context": "test", "cwd": "/tmp"},
        )

    @pytest.fixture
    async def event_store(self):
        store = EventStore("sqlite+aiosqlite:///:memory:")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def handler(self, event_store):
        from ouroboros.mcp.tools.pm_handler import PMInterviewHandler

        return PMInterviewHandler(
            llm_backend="openai",
            event_store=event_store,
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )

    async def test_last_question_persisted_in_new_round(self, handler) -> None:
        """answer + last_question → round uses real question text."""
        result = await handler.handle(
            {
                "session_id": "ses_pm_lq_1",
                "answer": "yes, invoice management",
                "last_question": "What is the core domain of your product?",
            }
        )
        assert result.is_ok
        assert len(self.saved_states) == 1
        state = self.saved_states[0]
        assert len(state.rounds) == 1
        assert state.rounds[0].question == "What is the core domain of your product?"
        assert state.rounds[0].user_response == "yes, invoice management"

    async def test_no_last_question_uses_placeholder(self, handler) -> None:
        """answer without last_question → placeholder (backward compat)."""
        result = await handler.handle(
            {
                "session_id": "ses_pm_lq_2",
                "answer": "B2B SaaS",
            }
        )
        assert result.is_ok
        assert len(self.saved_states) == 1
        state = self.saved_states[0]
        assert len(state.rounds) == 1
        assert state.rounds[0].question == "(continued from subagent)"
        assert state.rounds[0].user_response == "B2B SaaS"

    async def test_last_question_updates_existing_round(self, monkeypatch, handler) -> None:
        """Existing unanswered round + last_question → question text updated."""

        async def _load_with_round(state_dir, session_id):
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                rounds=[
                    InterviewRound(
                        round_number=1,
                        question="(continued from subagent)",
                        user_response=None,
                    )
                ],
            )
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _load_with_round)

        result = await handler.handle(
            {
                "session_id": "ses_pm_lq_3",
                "answer": "React frontend",
                "last_question": "What tech stack do you prefer?",
            }
        )
        assert result.is_ok
        assert len(self.saved_states) == 1
        state = self.saved_states[0]
        assert len(state.rounds) == 1
        assert state.rounds[0].question == "What tech stack do you prefer?"
        assert state.rounds[0].user_response == "React frontend"

    async def test_pm_response_includes_next_turn_hint(self, handler) -> None:
        """PM plugin response includes next_turn_hint for last_question."""
        import json

        result = await handler.handle(
            {
                "session_id": "ses_pm_lq_hint",
                "answer": "test",
            }
        )
        assert result.is_ok
        data = json.loads(result.value.content[0].text)
        # response_shape keys are merged at top level (see build_subagent_result)
        assert "last_question" in data.get("next_turn_hint", "")
