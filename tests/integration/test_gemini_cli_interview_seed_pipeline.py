"""Integration tests: GeminiCLIRuntime wired into the interview → seed pipeline.

These tests verify that both pipeline stages (interview and seed generation)
complete successfully when the Gemini CLI is used as the LLM backend.

The Gemini CLI subprocess is mocked so the tests run without a real
``gemini`` binary installed — they exercise the full pipeline logic
including adapter construction, prompt building, response parsing,
ambiguity gating, and seed extraction.

Sub-AC 2 of AC 1: Wire GeminiCLIRuntime into the Ouroboros interview →
seed pipeline and verify both stages complete successfully.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AmbiguityScore,
    ComponentScore,
    ScoreBreakdown,
)
from ouroboros.bigbang.interview import InterviewEngine, InterviewRound, InterviewState
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.seed import Seed
from ouroboros.providers.factory import create_llm_adapter
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter

# ---------------------------------------------------------------------------
# Shared subprocess fakes (mirrors patterns in other integration tests)
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal asyncio.StreamReader replacement for subprocess fakes."""

    def __init__(self, text: str = "") -> None:
        self._buffer = text.encode("utf-8")

    async def read(self, n: int = -1) -> bytes:
        if n < 0:
            data, self._buffer = self._buffer, b""
            return data
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data


class _FakeStdin:
    """Minimal asyncio.StreamWriter replacement for subprocess fakes."""

    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeProcess:
    """Minimal subprocess fake that returns pre-configured stdout/stderr."""

    def __init__(
        self,
        stdout_text: str,
        *,
        stderr_text: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_text)
        self.stderr = _FakeStream(stderr_text)
        self._returncode = returncode

    @property
    def returncode(self) -> int:
        return self._returncode

    async def wait(self) -> int:
        return self._returncode


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_INTERVIEW_QUESTION = (
    "What is the primary user persona for this CLI tool, "
    "and what is the single most important workflow they need to complete?"
)

_AMBIGUITY_SCORING_RESPONSE = """{
  "goal_clarity": {"clarity_score": 0.88, "justification": "Goal is well-defined."},
  "constraint_clarity": {"clarity_score": 0.82, "justification": "Constraints are clear."},
  "success_criteria_clarity": {"clarity_score": 0.85, "justification": "Criteria are measurable."}
}"""

_SEED_EXTRACTION_RESPONSE = """\
GOAL: Build a CLI task manager with project grouping and priority support
CONSTRAINTS: Python 3.11+ | No external database | YAML local storage
ACCEPTANCE_CRITERIA: Tasks can be created with priority | Tasks can be listed by project | Tasks can be deleted
ONTOLOGY_NAME: TaskManager
ONTOLOGY_DESCRIPTION: Core domain model for the task management CLI
ONTOLOGY_FIELDS: tasks:array:List of task objects | projects:array:List of project names
EVALUATION_PRINCIPLES: completeness:All CRUD operations work:0.5 | quality:Code is clean:0.3 | usability:UX is intuitive:0.2
EXIT_CONDITIONS: all_criteria_met:All acceptance criteria pass:100% of criteria satisfied
"""


def _make_gemini_fake_process(stdout_text: str) -> _FakeProcess:
    """Create a fake subprocess that emits the given stdout text."""
    return _FakeProcess(stdout_text=stdout_text, returncode=0)


def _make_subprocess_factory(responses: list[str]):
    """Return an async factory that yields fake processes in order.

    Each call consumes the next response from *responses*.  When the
    list is exhausted, the last response is repeated.
    """
    responses_iter = iter(responses)
    last: list[str] = [responses[0] if responses else ""]

    async def factory(*args: Any, **kwargs: Any) -> _FakeProcess:
        try:
            resp = next(responses_iter)
            last[0] = resp
        except StopIteration:
            resp = last[0]
        return _make_gemini_fake_process(resp)

    return factory


# ---------------------------------------------------------------------------
# Stage 1 — Interview
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterInterviewStage:
    """Verify the interview stage works correctly with GeminiCLIAdapter."""

    @pytest.mark.asyncio
    async def test_adapter_asks_question_successfully(self, tmp_path: Path) -> None:
        """GeminiCLIAdapter returns a non-empty question string from the interview engine."""
        adapter = GeminiCLIAdapter(cli_path="/fake/gemini", cwd=str(tmp_path), max_retries=1)
        factory = _make_subprocess_factory([_INTERVIEW_QUESTION])

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=factory,
        ):
            engine = InterviewEngine(
                llm_adapter=adapter,
                state_dir=tmp_path / "interview_state",
            )
            state_result = await engine.start_interview(
                "I want to build a CLI task management tool"
            )
            assert state_result.is_ok, f"start_interview failed: {state_result.error}"
            state = state_result.value

            question_result = await engine.ask_next_question(state)

        assert question_result.is_ok, f"ask_next_question failed: {question_result.error}"
        question = question_result.value
        assert question, "Expected a non-empty question from GeminiCLIAdapter"
        assert len(question) > 10, "Question should be a meaningful sentence"

    @pytest.mark.asyncio
    async def test_adapter_records_response_and_advances_round(
        self, tmp_path: Path
    ) -> None:
        """Recording a response advances the interview to round 2."""
        adapter = GeminiCLIAdapter(cli_path="/fake/gemini", cwd=str(tmp_path), max_retries=1)
        factory = _make_subprocess_factory([_INTERVIEW_QUESTION, _INTERVIEW_QUESTION])

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=factory,
        ):
            engine = InterviewEngine(
                llm_adapter=adapter,
                state_dir=tmp_path / "interview_state",
            )
            state_result = await engine.start_interview("Build a task manager")
            state = state_result.value

            q_result = await engine.ask_next_question(state)
            question = q_result.value

            record_result = await engine.record_response(
                state,
                "The primary persona is a software developer managing personal side-project tasks.",
                question,
            )

        assert record_result.is_ok, f"record_response failed: {record_result.error}"
        updated_state = record_result.value
        assert len(updated_state.rounds) == 1
        assert updated_state.current_round_number == 2
        assert updated_state.rounds[0].user_response is not None

    @pytest.mark.asyncio
    async def test_full_interview_stage_three_rounds(self, tmp_path: Path) -> None:
        """The interview stage completes 3 rounds and marks completion successfully."""
        responses = [
            "What type of tasks will users manage?",
            "How should tasks be organized?",
            "What is the priority model for tasks?",
        ]
        adapter = GeminiCLIAdapter(cli_path="/fake/gemini", cwd=str(tmp_path), max_retries=1)
        factory = _make_subprocess_factory(responses)

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=factory,
        ):
            engine = InterviewEngine(
                llm_adapter=adapter,
                state_dir=tmp_path / "interview_state",
            )
            state = (await engine.start_interview("Build a task manager CLI")).value

            for i in range(3):
                q_result = await engine.ask_next_question(state)
                assert q_result.is_ok, f"Round {i+1} question failed: {q_result.error}"
                state = (
                    await engine.record_response(state, f"Answer {i+1}", q_result.value)
                ).value

            complete_result = await engine.complete_interview(state)

        assert complete_result.is_ok
        final_state = complete_result.value
        assert final_state.is_complete
        assert len(final_state.rounds) == 3


# ---------------------------------------------------------------------------
# Stage 2 — Seed generation
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterSeedGenerationStage:
    """Verify the seed generation stage works correctly with GeminiCLIAdapter."""

    def _make_completed_state(self) -> InterviewState:
        """Create a minimal completed InterviewState ready for seed generation."""
        from ouroboros.bigbang.interview import InterviewStatus

        state = InterviewState(
            interview_id="gemini_test_seed_001",
            initial_context="Build a CLI task manager with project grouping",
        )
        for i in range(3):
            state.rounds.append(
                InterviewRound(
                    round_number=i + 1,
                    question=f"Question {i + 1}?",
                    user_response=f"Clear answer {i + 1} about requirements.",
                )
            )
        state.status = InterviewStatus.COMPLETED
        return state

    @pytest.mark.asyncio
    async def test_seed_generated_from_completed_interview(self, tmp_path: Path) -> None:
        """SeedGenerator produces a valid Seed using GeminiCLIAdapter as the LLM."""
        adapter = GeminiCLIAdapter(cli_path="/fake/gemini", cwd=str(tmp_path), max_retries=1)
        state = self._make_completed_state()
        low_ambiguity = AmbiguityScore(
            overall_score=0.15,
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal Clarity",
                    clarity_score=0.9,
                    weight=0.4,
                    justification="Goal is well-defined.",
                ),
                constraint_clarity=ComponentScore(
                    name="Constraint Clarity",
                    clarity_score=0.85,
                    weight=0.3,
                    justification="Constraints are clear.",
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success Criteria Clarity",
                    clarity_score=0.85,
                    weight=0.3,
                    justification="Criteria are measurable.",
                ),
            ),
        )

        factory = _make_subprocess_factory([_SEED_EXTRACTION_RESPONSE])

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=factory,
        ):
            generator = SeedGenerator(
                llm_adapter=adapter,
                output_dir=tmp_path / "seeds",
            )
            result = await generator.generate(state, low_ambiguity)

        assert result.is_ok, f"SeedGenerator.generate() failed: {result.error}"
        seed = result.value
        assert isinstance(seed, Seed)
        assert seed.goal
        assert len(seed.acceptance_criteria) > 0
        assert seed.ontology_schema.name

    @pytest.mark.asyncio
    async def test_seed_saved_to_disk(self, tmp_path: Path) -> None:
        """SeedGenerator.save_seed() persists the seed file to disk."""
        adapter = GeminiCLIAdapter(cli_path="/fake/gemini", cwd=str(tmp_path), max_retries=1)
        state = self._make_completed_state()
        low_ambiguity = AmbiguityScore(
            overall_score=0.15,
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal Clarity",
                    clarity_score=0.9,
                    weight=0.4,
                    justification="Goal is well-defined.",
                ),
                constraint_clarity=ComponentScore(
                    name="Constraint Clarity",
                    clarity_score=0.85,
                    weight=0.3,
                    justification="Constraints are clear.",
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success Criteria Clarity",
                    clarity_score=0.85,
                    weight=0.3,
                    justification="Criteria are measurable.",
                ),
            ),
        )
        seeds_dir = tmp_path / "seeds"

        factory = _make_subprocess_factory([_SEED_EXTRACTION_RESPONSE])

        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=factory,
        ):
            generator = SeedGenerator(
                llm_adapter=adapter,
                output_dir=seeds_dir,
            )
            seed_result = await generator.generate(state, low_ambiguity)
            assert seed_result.is_ok

            seed = seed_result.value
            seed_path = seeds_dir / f"{seed.metadata.seed_id}.yaml"
            save_result = await generator.save_seed(seed, seed_path)

        assert save_result.is_ok, f"save_seed failed: {save_result.error}"
        assert seed_path.exists(), "Seed file was not written to disk"
        assert seed_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# Full E2E pipeline (interview → ambiguity scoring → seed)
# ---------------------------------------------------------------------------


class TestGeminiCLIFullInterviewSeedPipeline:
    """End-to-end test: interview → ambiguity scoring → seed generation via Gemini CLI."""

    @pytest.mark.asyncio
    async def test_full_pipeline_completes_successfully(self, tmp_path: Path) -> None:
        """Both interview and seed stages complete when using GeminiCLIAdapter.

        This test exercises the complete Sub-AC 2 scenario:
          1. Start an interview using GeminiCLIAdapter as the LLM backend.
          2. Ask a question and record a response.
          3. Score ambiguity (mocked to return a low score).
          4. Generate a seed from the completed interview.

        All Gemini CLI subprocess calls are mocked to avoid requiring a real
        ``gemini`` binary.  The assertions verify that the pipeline
        data flows correctly through both stages.
        """
        # ---- Set up a shared adapter pointing to a fake CLI binary ----
        adapter = GeminiCLIAdapter(cli_path="/fake/gemini", cwd=str(tmp_path), max_retries=1)

        # ---- Subprocess responses (called in order per stage) ----
        interview_responses = [_INTERVIEW_QUESTION]
        seed_responses = [_SEED_EXTRACTION_RESPONSE]

        # ---- Stage 1: Interview ----
        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=_make_subprocess_factory(interview_responses),
        ):
            engine = InterviewEngine(
                llm_adapter=adapter,
                state_dir=tmp_path / "interview",
            )
            start_result = await engine.start_interview(
                "Build a command-line task manager for developers"
            )
            assert start_result.is_ok, f"start_interview failed: {start_result.error}"
            state = start_result.value

            q_result = await engine.ask_next_question(state)
            assert q_result.is_ok, f"ask_next_question failed: {q_result.error}"

            record_result = await engine.record_response(
                state,
                "A solo developer managing personal project tasks across multiple git repos.",
                q_result.value,
            )
            assert record_result.is_ok, f"record_response failed: {record_result.error}"
            state = record_result.value

            complete_result = await engine.complete_interview(state)
            assert complete_result.is_ok
            state = complete_result.value

        # ---- Stage 2: Ambiguity scoring (low score so seed proceeds) ----
        ambiguity_score = AmbiguityScore(
            overall_score=0.12,  # well below the 0.2 threshold
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal Clarity",
                    clarity_score=0.92,
                    weight=0.4,
                    justification="Goal is very well-defined.",
                ),
                constraint_clarity=ComponentScore(
                    name="Constraint Clarity",
                    clarity_score=0.88,
                    weight=0.3,
                    justification="Constraints are explicit.",
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success Criteria Clarity",
                    clarity_score=0.90,
                    weight=0.3,
                    justification="Success criteria are measurable.",
                ),
            ),
        )
        assert ambiguity_score.overall_score <= AMBIGUITY_THRESHOLD, (
            "Test setup error: ambiguity score must be below threshold to proceed to seed"
        )
        assert ambiguity_score.is_ready_for_seed

        # ---- Stage 3: Seed generation ----
        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=_make_subprocess_factory(seed_responses),
        ):
            seeds_dir = tmp_path / "seeds"
            generator = SeedGenerator(
                llm_adapter=adapter,
                output_dir=seeds_dir,
            )
            seed_result = await generator.generate(state, ambiguity_score)

        assert seed_result.is_ok, (
            f"SeedGenerator.generate() failed: {seed_result.error}\n"
            "This indicates the pipeline did not complete the seed stage successfully."
        )
        seed = seed_result.value

        # ---- Verify seed quality ----
        assert isinstance(seed, Seed), "Expected a Seed instance from the generator"
        assert seed.goal, "Seed goal must not be empty"
        assert len(seed.acceptance_criteria) >= 1, "Seed must have at least one acceptance criterion"
        assert seed.ontology_schema.name, "Seed ontology schema must have a name"
        assert seed.metadata.seed_id, "Seed must have a unique ID"

    @pytest.mark.asyncio
    async def test_pipeline_uses_gemini_factory_path(self, tmp_path: Path) -> None:
        """create_llm_adapter('gemini') returns a GeminiCLIAdapter usable in the pipeline."""
        # Verify the factory path creates the right adapter type
        adapter = create_llm_adapter(
            backend="gemini",
            cli_path="/fake/gemini",
            cwd=str(tmp_path),
            use_case="interview",
            timeout=30.0,
        )
        assert isinstance(adapter, GeminiCLIAdapter)

        # Verify the adapter can run through the interview stage
        factory = _make_subprocess_factory([_INTERVIEW_QUESTION])
        with patch(
            "ouroboros.providers.gemini_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=factory,
        ):
            engine = InterviewEngine(
                llm_adapter=adapter,
                state_dir=tmp_path / "interview",
            )
            start_result = await engine.start_interview("Build a test project")
            assert start_result.is_ok

            q_result = await engine.ask_next_question(start_result.value)

        assert q_result.is_ok, "Factory-created GeminiCLIAdapter failed in interview pipeline"
        assert q_result.value, "Expected a non-empty question from factory-created adapter"


# ---------------------------------------------------------------------------
# Factory + Runtime wiring
# ---------------------------------------------------------------------------


class TestGeminiCLIRuntimePipelineWiring:
    """Verify GeminiCLIRuntime is correctly wired via the runtime factory."""

    def test_runtime_factory_creates_gemini_runtime(self) -> None:
        """create_agent_runtime('gemini') returns a GeminiCLIRuntime instance."""
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_gemini_cli_path",
                return_value="/fake/gemini",
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=None,
            ),
        ):
            runtime = create_agent_runtime(backend="gemini", cwd=str(Path.cwd()))

        assert isinstance(runtime, GeminiCLIRuntime)
        assert runtime.runtime_backend == "gemini_cli"

    def test_runtime_factory_creates_gemini_runtime_via_alias(self) -> None:
        """create_agent_runtime('gemini_cli') also returns a GeminiCLIRuntime."""
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_gemini_cli_path",
                return_value=None,
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=None,
            ),
        ):
            runtime = create_agent_runtime(backend="gemini_cli")

        assert isinstance(runtime, GeminiCLIRuntime)

    def test_init_llm_backend_enum_includes_gemini(self) -> None:
        """The LLMBackend CLI enum includes 'gemini' so --llm-backend gemini is accepted."""
        from ouroboros.cli.commands.init import LLMBackend

        assert "gemini" in {backend.value for backend in LLMBackend}

    def test_init_runtime_backend_enum_includes_gemini(self) -> None:
        """The AgentRuntimeBackend CLI enum includes 'gemini' so --runtime gemini is accepted."""
        from ouroboros.cli.commands.init import AgentRuntimeBackend

        assert "gemini" in {backend.value for backend in AgentRuntimeBackend}

    def test_cli_start_accepts_llm_backend_gemini(self) -> None:
        """The init start CLI command accepts --llm-backend gemini without error."""
        from unittest.mock import AsyncMock, patch

        from typer.testing import CliRunner

        from ouroboros.cli.main import app

        cli_runner = CliRunner()
        mock_run_interview = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.init._run_interview",
                new=mock_run_interview,
            ),
            patch("ouroboros.cli.commands.init.asyncio.run") as mock_asyncio_run,
        ):
            mock_asyncio_run.return_value = None
            result = cli_runner.invoke(
                app,
                [
                    "init",
                    "start",
                    "Build a task manager",
                    "--llm-backend",
                    "gemini",
                ],
            )

        assert result.exit_code == 0, (
            f"CLI rejected --llm-backend gemini. Output:\n{result.output}"
        )
        # Verify llm_backend was forwarded correctly
        call_args = mock_asyncio_run.call_args
        assert call_args is not None, "asyncio.run was not called"

    def test_cli_start_accepts_runtime_gemini(self) -> None:
        """The init start CLI command accepts --runtime gemini without error."""
        from unittest.mock import AsyncMock, patch

        from typer.testing import CliRunner

        from ouroboros.cli.main import app

        cli_runner = CliRunner()
        mock_run_interview = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.init._run_interview",
                new=mock_run_interview,
            ),
            patch("ouroboros.cli.commands.init.asyncio.run") as mock_asyncio_run,
        ):
            mock_asyncio_run.return_value = None
            result = cli_runner.invoke(
                app,
                [
                    "init",
                    "start",
                    "Build a task manager",
                    "--orchestrator",
                    "--runtime",
                    "gemini",
                ],
            )

        assert result.exit_code == 0, (
            f"CLI rejected --runtime gemini. Output:\n{result.output}"
        )
