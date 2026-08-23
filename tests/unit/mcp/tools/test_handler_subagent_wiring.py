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

    async def test_plugin_payload_hides_harness_contract(self, handler) -> None:
        result = await handler.handle(
            {
                "session_id": "sess-hidden",
                "artifact": "partial artifact HIDDEN_SENTINEL",
                "seed_content": (
                    "goal: Judge the artifact\n"
                    "acceptance_criteria:\n"
                    "  - description: Produce output.json without HIDDEN_SENTINEL\n"
                    "    expected_artifacts: [output.json]\n"
                    "    verify_command: python secret_check.py --token TOP_SECRET\n"
                    "    output_assertion: HIDDEN_SENTINEL\n"
                    "ontology_schema:\n"
                    "  name: HiddenContractArtifact\n"
                    "  description: Artifact with parent-owned verification\n"
                    "metadata:\n"
                    "  ambiguity_score: 0.0\n"
                ),
            }
        )

        assert result.is_ok
        payload = result.value.meta["_subagent"]
        visible = payload["prompt"] + str(payload["context"])
        assert "Produce output.json" in visible
        assert "output.json" in visible
        assert "TOP_SECRET" not in visible
        assert "HIDDEN_SENTINEL" not in visible
        assert "verify_command" not in visible
        assert "output_assertion" not in visible


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
        # An omitted tier preserves the runtime's selected model. The public
        # response calls the automatic choice "medium", but a delegated child
        # must receive None rather than a materialized standard-tier pin.
        assert ctx["model_tier"] is None

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

    async def test_direct_plugin_payload_hides_seed_path_and_preserves_chain_contract(
        self, tmp_path: Path
    ) -> None:
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry

        seed_path = tmp_path / "seed_with_hidden_contract.yaml"
        seed_path.write_text(
            """goal: Build the artifact
acceptance_criteria:
  - description: Produce output.json
    artifacts: [output.json]
    verify_command: python secret_check.py --token TOP_SECRET
    output_assertion:
      contains: HIDDEN_SENTINEL
""",
            encoding="utf-8",
        )
        registry = SeedHandoffRegistry()
        direct_handler = ExecuteSeedHandler(
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
            seed_handoff_registry=registry,
        )

        result = await direct_handler.handle(
            {
                "seed_path": str(seed_path),
                "cwd": str(tmp_path),
                "auto_evolve": False,
            }
        )

        assert result.is_ok
        payload = result.value.meta["_subagent"]
        visible = payload["prompt"] + str(payload["context"])
        assert str(seed_path) not in visible
        assert "TOP_SECRET" not in visible
        assert "HIDDEN_SENTINEL" not in visible
        assert "verify_command" not in visible
        assert "output_assertion" not in visible
        assert payload["context"]["seed_path"] is None
        assert payload["context"]["auto_evolve"] is False
        handoff_id = payload["context"]["seed_handoff_id"]
        assert handoff_id.startswith("seed_handoff_")
        assert registry.resolve(handoff_id, session_id=result.value.meta["session_id"]) is not None

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
        from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry
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
            seed_handoff_registry=SeedHandoffRegistry(),
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

    async def test_plugin_context_preserves_automatic_omission_and_explicit_tier(
        self, handler
    ) -> None:
        omitted = await handler.handle({"seed_content": "goal: test"})
        explicit = await handler.handle({"seed_content": "goal: test", "model_tier": "medium"})

        assert omitted.value.meta["_subagent"]["context"]["model_tier"] is None
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

    async def test_plugin_payload_hides_harness_contract_and_preserves_opt_out(
        self, handler
    ) -> None:
        seed = """goal: Build the artifact
constraints:
  - Never print python secret_check.py --token TOP_SECRET
  - Never print HIDDEN_SENTINEL
acceptance_criteria:
  - description: Produce output.json
    expected_artifacts: [output.json]
    verify_command: python secret_check.py --token TOP_SECRET
    output_assertion: HIDDEN_SENTINEL
ontology_schema:
  name: HiddenContractArtifact
  description: Artifact with parent-owned verification
metadata:
  ambiguity_score: 0.0
"""
        result = await handler.handle({"seed_content": seed, "auto_evolve": False})

        payload = result.value.meta["_subagent"]
        visible = payload["prompt"] + str(payload["context"])
        assert "TOP_SECRET" not in visible
        assert "HIDDEN_SENTINEL" not in visible
        assert "verify_command" not in visible
        assert "output_assertion" not in visible
        assert "Produce output.json" in visible
        assert "output.json" in visible
        assert payload["context"]["auto_evolve"] is False
        assert payload["context"]["seed_handoff_id"].startswith("seed_handoff_")
        assert "auto_evolve: false" in payload["prompt"]
        assert "including unsuccessful AC execution" in payload["prompt"]

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
        # Every answer names its question, on every runtime: a turn persists
        # nothing when it asks (RFC #2222 revision 4), so there is no stored
        # question for the server to file an unnamed answer under.
        result = await handler.handle(
            {
                "session_id": "sess-123",
                "answer": "React + Node.js",
                "last_question": "What stack?",
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

    async def test_generate_withholds_observations_but_resume_keeps_them(
        self, handler, monkeypatch
    ) -> None:
        observed = "competitor-secret-retry-policy"

        async def _load_observation(
            state_dir: Path, session_id: str
        ) -> Result[InterviewState, str]:
            del state_dir
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                status=InterviewStatus.COMPLETED,
            )
            state.record_answer("What exists today?", f"[from-research] {observed}")
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _load_observation)

        resumed = await handler.handle(
            {
                "session_id": "sess-observation",
                "answer": "Keep investigating",
                "last_question": "What should we decide next?",
            }
        )
        assert resumed.is_ok
        assert observed in resumed.value.meta["_subagent"]["prompt"]

        generated = await handler.handle({"session_id": "sess-observation", "action": "generate"})
        assert generated.is_ok
        prompt = generated.value.meta["_subagent"]["prompt"]
        assert observed not in prompt
        assert "observation withheld" in prompt

    async def test_generate_rejects_an_incomplete_observation_only_session(
        self, handler, monkeypatch
    ) -> None:
        """Regression (#1941): a forwarded finding cannot authorise generation.

        A confirmed lane finding occupies a round while being a fact the user
        adopted, and ``generate`` withholds its content three lines later.
        Counting it as interview evidence would let a session where the user
        decided nothing produce a PM seed from an empty transcript, under a
        prompt telling the child the interview is complete.
        """

        async def _load_observation_only(
            state_dir: Path, session_id: str
        ) -> Result[InterviewState, str]:
            del state_dir
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                status=InterviewStatus.IN_PROGRESS,
            )
            state.record_answer("What retry policy exists?", "[from-code] three retries")
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _load_observation_only)

        result = await handler.handle({"session_id": "sess-obs-only", "action": "generate"})

        assert result.is_err
        assert "Continue the interview" in str(result.error)

    async def test_generate_accepts_one_user_decision_beside_an_observation(
        self, handler, monkeypatch
    ) -> None:
        """The gate counts decisions, not provenance-blind rounds.

        The mirror of the case above: once the user has decided once, the
        session is generatable even though an observation shares the
        transcript. The gate must not have become a blanket rejection of
        sessions that carry findings.
        """

        async def _load_mixed(state_dir: Path, session_id: str) -> Result[InterviewState, str]:
            del state_dir
            state = InterviewState(
                interview_id=session_id,
                initial_context="test context",
                status=InterviewStatus.IN_PROGRESS,
            )
            state.record_answer("What retry policy exists?", "[from-code] three retries")
            state.record_answer("What should it be?", "Five retries, capped at 30s")
            return Result.ok(state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _load_mixed)

        result = await handler.handle({"session_id": "sess-mixed", "action": "generate"})

        assert result.is_ok
        assert result.value.meta["_subagent"]["tool_name"] == "ouroboros_pm_interview"

    async def test_plugin_path_records_a_confirmed_finding_like_any_answer(
        self, handler, monkeypatch
    ) -> None:
        """Regression (#1941): one entrance, so the two runtimes cannot drift.

        This runtime used to be handed a second field it never read, and a
        lane's finding silently failed to reach the child that writes the next
        question. There is no second field now: a confirmed finding arrives as
        the answer, ``record_answer`` settles it as an observation, and the
        transcript carries it forward with no branch of its own here.

        Revert the parameter and this still passes — which is why the *absence*
        is pinned separately, in ``test_pm_question_advisory.py``.
        """
        saved: list[InterviewState] = []

        async def _capture_save(state_dir: Path, state: InterviewState) -> Result[Path, str]:
            saved.append(state)
            return await _noop_save(state_dir, state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_save_state", _capture_save)

        result = await handler.handle(
            {
                "session_id": "sess-ev",
                "answer": "[from-code] billing-api: period end",
                "last_question": "Q?",
            }
        )

        assert result.is_ok
        assert "billing-api: period end" in result.value.meta["_subagent"]["prompt"]
        assert [(r.question, r.user_response, r.provenance) for r in saved[-1].rounds] == [
            ("Q?", "[from-code] billing-api: period end", "observation")
        ]

    async def test_plugin_path_opens_the_round_a_finding_lands_on(
        self, handler, monkeypatch
    ) -> None:
        """The other plugin entry: nothing persisted, so the answer opens it.

        Each plugin dispatch is a new child session, so a resumed interview can
        reach here with no round to fill. One round appears, not two.
        """
        saved: list[InterviewState] = []

        async def _load_empty(state_dir: Path, session_id: str) -> Result[InterviewState, str]:
            del state_dir
            return Result.ok(
                InterviewState(interview_id=session_id, initial_context="test context")
            )

        async def _capture_save(state_dir: Path, state: InterviewState) -> Result[Path, str]:
            saved.append(state)
            return await _noop_save(state_dir, state)

        import ouroboros.mcp.tools.authoring_handlers as ah

        monkeypatch.setattr(ah, "_plugin_load_state", _load_empty)
        monkeypatch.setattr(ah, "_plugin_save_state", _capture_save)

        result = await handler.handle(
            {
                "session_id": "sess-ev-fresh",
                "answer": "[from-data] 12,480 cancellations",
                "last_question": "When does the slot reopen?",
            }
        )

        assert result.is_ok
        assert "12,480 cancellations" in result.value.meta["_subagent"]["prompt"]
        assert [(r.question, r.provenance) for r in saved[-1].rounds] == [
            ("When does the slot reopen?", "observation")
        ]

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
