"""Tests for handler subagent wiring.

Verifies that ALL LLM-requiring handlers return _subagent dispatch payloads
instead of calling LLMs directly. Each handler.handle() should:
1. Still validate required arguments (return errors for missing args)
2. Return Result.ok(MCPToolResult) with meta["_subagent"] for valid args
3. Include correct tool_name in the payload
4. Include original arguments in context for round-trip
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.types import Result

# ---------------------------------------------------------------------------
# Shared mock helper for plugin I/O
# ---------------------------------------------------------------------------


async def _noop_save(state_dir: Path, state: InterviewState) -> Result[Path, str]:
    """Mock ``_plugin_save_state`` — mirrors real signature, no disk I/O.

    Returns a realistic path built from *state_dir* + *interview_id* so
    callers that inspect the result get a plausible ``Path`` object rather
    than a hard-coded ``/tmp/fake``.
    """
    return Result.ok(state_dir / f"interview_{state.interview_id}.json")


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

    async def test_subagent_prompt_includes_adversarial_probes(self, handler) -> None:
        result = await handler.handle(
            {
                "artifact": "def foo(): pass",
                "quality_bar": "All functions have docstrings",
            }
        )
        assert result.is_ok
        prompt = result.value.meta["_subagent"]["prompt"]
        assert "Adversarial Probes" in prompt
        assert "malformed_input" in prompt
        assert "prompt_injection" in prompt
        assert "evidence gap" in prompt
        assert "instead of implying you ran it" in prompt

    async def test_still_validates_missing_artifact(self, handler) -> None:
        result = await handler.handle({"quality_bar": "good"})
        assert result.is_err

    async def test_still_validates_non_string_artifact(self, handler) -> None:
        result = await handler.handle({"artifact": [], "quality_bar": "good"})
        assert result.is_err

    async def test_still_validates_missing_quality_bar(self, handler) -> None:
        result = await handler.handle({"artifact": "code"})
        assert result.is_err

    async def test_empty_artifact_reaches_qa_evaluation(self, handler) -> None:
        result = await handler.handle({"artifact": "", "quality_bar": "good"})
        assert result.is_ok
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["artifact"] == ""

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

    @pytest.fixture(autouse=True)
    def mock_plugin_state(self):
        """Mock _plugin_load_state so plugin path can load interview state."""
        from unittest.mock import AsyncMock, patch

        from ouroboros.bigbang.interview import InterviewState, InterviewStatus
        from ouroboros.core.types import Result

        state = InterviewState(
            interview_id="sess-123",
            initial_context="test project",
            status=InterviewStatus.COMPLETED,
            ambiguity_score=0.1,
        )
        mock_load = AsyncMock(return_value=Result.ok(state))
        with patch(
            "ouroboros.mcp.tools.authoring_handlers._plugin_load_state",
            mock_load,
        ):
            self._mock_load = mock_load
            yield

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
        # Plugin path now prefers caller-supplied score over persisted
        assert ctx["ambiguity_score"] == 0.15

    async def test_plugin_context_preserves_client_gate_acknowledgements(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-456",
                "client_gates": ["restate_goal_approved", "seed_ready_acceptance_guard"],
            }
        )

        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["client_gates"] == (
            "restate_goal_approved",
            "seed_ready_acceptance_guard",
        )
        assert result.value.meta["missing_client_gates"] == ()


# ---------------------------------------------------------------------------
# InterviewHandler
# ---------------------------------------------------------------------------


class TestInterviewHandlerSubagentDispatch:
    """InterviewHandler.handle() returns _subagent payload."""

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
        assert "## Question-first Advisory Fanout" in payload["prompt"]
        assert result.value.meta["question_advisory_recommended"] is True
        assert (
            result.value.meta["question_advisory_strategy"]
            == "plugin_child_question_first_advisory"
        )

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

    async def test_plugin_dispatch_carries_advisory_fanout_contract(self, tmp_path) -> None:
        """Plugin transport parity (PR #1703 bot review round 4, B1).

        The OpenCode plugin path previously emitted only a prose mention of
        the advisory lanes — no ``data_policy``, no ``data_evidence_answer.v1``
        contract, no fan-out id — so that supported transport bypassed the
        machine-readable policy and re-entry validation entirely.
        """
        from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
        from ouroboros.mcp.tools.subagent import FanoutRegistry

        registry = FanoutRegistry(tmp_path)
        handler = InterviewHandler(
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
            fanout_registry=registry,
        )
        result = await handler.handle({"initial_context": "Build a web app"})
        assert result.is_ok
        meta = result.value.meta

        contract = meta["question_advisory_fanout"]
        lanes = {lane["lane_id"]: lane for lane in contract["lanes"]}
        assert lanes["data_context"]["data_policy"]["read_only"] is True
        assert lanes["data_context"]["answer_contract"]["contract_id"] == "data_evidence_answer.v1"
        assert meta["question_advisory_result_correlation_key"] == "context.lane_id"

        # The stamped id is redeemable: the registered record carries the data
        # lane's answer contract, so re-entry door validation applies on this
        # transport too.
        record = registry.load(meta["question_advisory_fanout_id"])
        assert record is not None
        contracts = record.synthesizer_input["lane_answer_contracts"]
        assert contracts["data_context"]["contract_id"] == "data_evidence_answer.v1"

        # Round-5 B1: the bridge dispatches ONLY the child prompt, so the
        # ACTUAL fan-out id and the complete data contract ride the prompt
        # itself — field names alone are not a contract.
        prompt = result.value.meta["_subagent"]["prompt"]
        assert meta["question_advisory_fanout_id"] in prompt
        assert "data_evidence_answer.v1" in prompt
        assert '"read_only": true' in prompt
        assert "requires_user_confirmation" in prompt

    async def test_plugin_rendered_recipe_is_redeemable_verbatim(self, tmp_path) -> None:
        """Following the rendered re-entry recipe EXACTLY must succeed.

        Bot-review round-8 probe (PR #1703): the recipe supplied fanout_id
        and correlation_key but omitted session_id, and strict correlation
        then rejected a contract-following child with correlation_mismatch.
        The prompt now renders all three identity values; submitting with
        them verbatim completes.
        """
        import re as re_module

        from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
        from ouroboros.mcp.tools.subagent import FanoutRegistry, submit_fanout_results

        registry = FanoutRegistry(tmp_path)
        handler = InterviewHandler(
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
            fanout_registry=registry,
        )
        result = await handler.handle({"initial_context": "Build a web app"})
        assert result.is_ok
        prompt = result.value.meta["_subagent"]["prompt"]

        fanout_match = re_module.search(r"^fanout_id: (\S+)$", prompt, re_module.MULTILINE)
        session_match = re_module.search(r"^session_id: (\S+)$", prompt, re_module.MULTILINE)
        assert fanout_match, "recipe must render the actual fanout_id"
        assert session_match, "recipe must render the actual session_id"

        out = submit_fanout_results(
            registry,
            session_id=session_match.group(1),
            correlation_key="context.lane_id",
            results=[
                {"key": "ambiguity_contrarian", "content": "contrarian-advice"},
                {"key": "answer_simplifier", "content": "simplifier-advice"},
            ],
            fanout_id=fanout_match.group(1),
        )
        assert out["status"] == "complete"

    async def test_plugin_child_prompt_renders_known_data_tools(
        self, tmp_path, monkeypatch
    ) -> None:
        """Round-6 suggestion: known_data_tools must reach the child prompt —
        parent metadata alone is discarded by the bridge."""
        from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
        from ouroboros.mcp.tools.subagent import FanoutRegistry

        monkeypatch.setenv("OUROBOROS_KNOWN_DATA_TOOLS", "clickhouse_query,metabase_card")
        handler = InterviewHandler(
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
            fanout_registry=FanoutRegistry(tmp_path),
        )
        result = await handler.handle({"initial_context": "Build a web app"})
        assert result.is_ok
        prompt = result.value.meta["_subagent"]["prompt"]
        assert "clickhouse_query" in prompt
        assert "metabase_card" in prompt


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
                "auto_evaluate": False,
            }
        )
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["max_iterations"] == 5
        assert ctx["skip_qa"] is True
        assert ctx["auto_evaluate"] is False
        assert ctx["model_tier"] == "medium"

    async def test_context_preserves_explicit_model_tier(self, handler) -> None:
        result = await handler.handle(
            {
                "seed_content": "goal: test",
                "model_tier": "medium",
            }
        )

        assert result.value.meta["_subagent"]["context"]["model_tier"] == "medium"

    async def test_plugin_payload_includes_resolved_worker_cap(self, handler) -> None:
        """Plugin dispatch must propagate the configured worker cap (#489)."""
        from unittest.mock import patch

        with patch(
            "ouroboros.mcp.tools.execution_handlers.get_max_parallel_workers",
            return_value=7,
        ):
            result = await handler.handle({"seed_content": "goal: test"})
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["max_parallel_workers"] == 7

    async def test_plugin_payload_includes_formal_evaluation_contract(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        ctx = result.value.meta["_subagent"]["context"]
        prompt = result.value.meta["_subagent"]["prompt"]
        assert ctx["auto_evaluate"] is True
        assert "ouroboros_start_evaluate" in prompt
        assert "formal 3-stage evaluation" in prompt

    async def test_plugin_path_surfaces_worker_cap_config_error(self, handler) -> None:
        """Plugin dispatch must fail clearly on invalid worker-cap config (#489)."""
        from unittest.mock import patch

        from ouroboros.core.errors import ConfigError

        with patch(
            "ouroboros.mcp.tools.execution_handlers.get_max_parallel_workers",
            side_effect=ConfigError(
                "orchestrator.max_parallel_workers must be greater than 0",
                config_key="orchestrator.max_parallel_workers",
            ),
        ):
            result = await handler.handle({"seed_content": "goal: test"})
        assert result.is_err
        assert "Execution handler config error" in str(result.error)


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

    async def test_plugin_context_applies_fresh_default_and_preserves_explicit_tier(
        self, handler
    ) -> None:
        omitted = await handler.handle({"seed_content": "goal: test"})
        explicit = await handler.handle({"seed_content": "goal: test", "model_tier": "medium"})

        assert omitted.value.meta["_subagent"]["context"]["model_tier"] == "medium"
        assert explicit.value.meta["_subagent"]["context"]["model_tier"] == "medium"

    async def test_plugin_mode_delegates_formal_evaluation_to_child(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test"})
        assert result.is_ok
        assert result.value.meta["verification_status"] == "evaluation_delegated"
        assert result.value.meta["evaluation_status"] == "delegated_to_plugin"
        assert result.value.meta["formal_evaluation_delegated"] is True
        assert result.value.meta["next_step"] == (
            "wait for delegated plugin task to complete formal evaluation"
        )
        assert result.value.meta["manual_retry_next_step"].startswith("ooo evaluate orch_")
        assert result.value.meta["_subagent"]["context"]["auto_evaluate"] is True

    async def test_plugin_mode_auto_evaluate_false_keeps_legacy_manual_path(self, handler) -> None:
        result = await handler.handle({"seed_content": "goal: test", "auto_evaluate": False})
        assert result.is_ok
        assert result.value.meta["verification_status"] == "delegated_unverified"
        assert result.value.meta["next_step"].startswith("ooo evaluate orch_")
        assert "evaluation_status" not in result.value.meta
        assert result.value.meta["_subagent"]["context"]["auto_evaluate"] is False
        assert (
            "Formal evaluation auto-chain is disabled" in result.value.meta["_subagent"]["prompt"]
        )

    async def test_plugin_payload_includes_resolved_worker_cap(self, handler) -> None:
        """Plugin dispatch must propagate the configured worker cap (#489)."""
        from unittest.mock import patch

        with patch(
            "ouroboros.mcp.tools.execution_handlers.get_max_parallel_workers",
            return_value=7,
        ):
            result = await handler.handle({"seed_content": "goal: test"})
        ctx = result.value.meta["_subagent"]["context"]
        assert ctx["max_parallel_workers"] == 7

    async def test_plugin_path_surfaces_worker_cap_config_error(self, handler) -> None:
        """Plugin dispatch must fail clearly on invalid worker-cap config (#489)."""
        from unittest.mock import patch

        from ouroboros.core.errors import ConfigError

        with patch(
            "ouroboros.mcp.tools.execution_handlers.get_max_parallel_workers",
            side_effect=ConfigError(
                "orchestrator.max_parallel_workers must be greater than 0",
                config_key="orchestrator.max_parallel_workers",
            ),
        ):
            result = await handler.handle({"seed_content": "goal: test"})
        assert result.is_err
        assert "Execution handler config error" in str(result.error)


# ---------------------------------------------------------------------------
# PMInterviewHandler
# ---------------------------------------------------------------------------


class TestPMInterviewHandlerSubagentDispatch:
    """PMInterviewHandler.handle() returns _subagent payload."""

    @pytest.fixture(autouse=True)
    def mock_plugin_io(self, monkeypatch):
        """Mock _plugin_load/save and pm_meta so plugin path doesn't need real state files."""

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
        # PM plugin path now calls _save_pm_meta on start and select_repos
        monkeypatch.setattr(pmh, "_save_pm_meta", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            pmh,
            "_load_pm_meta",
            lambda *_a, **_kw: {
                "initial_context": "test context",
                "brownfield_repos": [],
                "cwd": "/tmp",
                "status": "pending_repo_selection",
            },
        )

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

    async def test_select_repos_returns_subagent(self, handler) -> None:
        """select_repos with session_id dispatches subagent (2-step flow step 2)."""
        result = await handler.handle(
            {
                "session_id": "sess-abc",
                "selected_repos": ["/repo1"],
            }
        )
        assert result.is_ok
        payload = result.value.meta["_subagent"]
        assert payload["tool_name"] == "ouroboros_pm_interview"
        assert payload["context"]["selected_repos"] == ["/repo1"]
        # initial_context recovered from pm_meta and passed in context dict
        assert payload["context"]["initial_context"] == "test context"

    async def test_select_repos_without_session_id_errors(self, handler) -> None:
        """select_repos without session_id returns validation error."""
        import ouroboros.mcp.tools.pm_handler as pmh
        from ouroboros.mcp.tools.pm_handler import PMInterviewHandler

        # Override _load_pm_meta to return None (no session found)
        original = pmh._load_pm_meta
        pmh._load_pm_meta = lambda *_a, **_kw: None
        try:
            h = PMInterviewHandler(agent_runtime_backend="opencode", opencode_mode="plugin")
            result = await h.handle(
                {
                    "selected_repos": ["/repo1"],
                }
            )
            assert result.is_err
            assert "session_id" in str(result.error).lower() or "select_repos" in str(result.error)
        finally:
            pmh._load_pm_meta = original
