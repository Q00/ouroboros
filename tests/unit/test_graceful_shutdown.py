"""Unit tests for graceful shutdown (issue #169).

Covers:
- lineage_generation_interrupted event creation
- LineageProjector handles interrupted events
- find_resume_point returns INTERRUPTED for interrupted generations
- EvolutionaryLoop._check_shutdown returns GenerationResult when flag set
- SIGINT handler installation and cleanup
- Reflect output serialization/restore on resume
- Evaluation summary serialization/restore on resume
"""

import signal
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.lineage import EvaluationSummary, GenerationPhase
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import lineage_generation_interrupted
from ouroboros.evolution.loop import EvolutionaryLoop, EvolutionaryLoopConfig
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.reflect import ReflectOutput

LINEAGE_ID = "lin_shutdown_test"


def _make_event(event_type: str, data: dict | None = None) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        aggregate_type="lineage",
        aggregate_id=LINEAGE_ID,
        data=data or {},
    )


# -- Event tests --


class TestInterruptedEvent:
    def test_event_type(self) -> None:
        event = lineage_generation_interrupted(LINEAGE_ID, 3, "wondering")
        assert event.type == "lineage.generation.interrupted"
        assert event.data["generation_number"] == 3
        assert event.data["last_completed_phase"] == "wondering"

    def test_event_with_partial_state(self) -> None:
        event = lineage_generation_interrupted(
            LINEAGE_ID,
            2,
            "reflecting",
            partial_state={"wonder_questions": ["q1", "q2"]},
        )
        assert event.data["partial_state"]["wonder_questions"] == ["q1", "q2"]

    def test_event_without_partial_state(self) -> None:
        event = lineage_generation_interrupted(LINEAGE_ID, 1, "executing")
        assert "partial_state" not in event.data


# -- Projector tests --


class TestProjectorInterrupted:
    def test_project_marks_generation_interrupted(self) -> None:
        projector = LineageProjector()
        events = [
            _make_event("lineage.created", {"goal": "test"}),
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 1,
                    "phase": "executing",
                    "seed_id": "s1",
                },
            ),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 1,
                    "seed_id": "s1",
                    "ontology_snapshot": {"name": "O1", "description": "d", "fields": []},
                },
            ),
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 2,
                    "phase": "wondering",
                },
            ),
            _make_event(
                "lineage.generation.interrupted",
                {
                    "generation_number": 2,
                    "last_completed_phase": "wondering",
                },
            ),
        ]
        lineage = projector.project(events)
        assert lineage is not None
        assert len(lineage.generations) == 2
        assert lineage.generations[0].phase == GenerationPhase.COMPLETED
        assert lineage.generations[1].phase == GenerationPhase.INTERRUPTED

    def test_find_resume_point_returns_interrupted(self) -> None:
        projector = LineageProjector()
        events = [
            _make_event("lineage.created", {"goal": "test"}),
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 1,
                    "phase": "executing",
                },
            ),
            _make_event(
                "lineage.generation.completed",
                {
                    "generation_number": 1,
                },
            ),
            _make_event(
                "lineage.generation.started",
                {
                    "generation_number": 2,
                    "phase": "wondering",
                },
            ),
            _make_event(
                "lineage.generation.interrupted",
                {
                    "generation_number": 2,
                    "last_completed_phase": "reflecting",
                },
            ),
        ]
        gen, phase, interrupted_at = projector.find_resume_point(events)
        assert gen == 2
        assert phase == GenerationPhase.INTERRUPTED
        assert interrupted_at == "reflecting"


# -- Shutdown flag tests --


class TestShutdownFlag:
    def _make_loop(self) -> EvolutionaryLoop:
        event_store = AsyncMock()
        event_store.append = AsyncMock()
        return EvolutionaryLoop(
            event_store=event_store,
            config=EvolutionaryLoopConfig(),
        )

    @pytest.mark.asyncio
    async def test_check_shutdown_returns_none_when_not_requested(self) -> None:
        loop = self._make_loop()
        seed = MagicMock()
        result = await loop._check_shutdown(LINEAGE_ID, 1, "wondering", seed)
        assert result is None

    @pytest.mark.asyncio
    async def test_check_shutdown_returns_interrupted_when_requested(self) -> None:
        loop = self._make_loop()
        loop._shutdown_requested = True
        seed = MagicMock()
        result = await loop._check_shutdown(LINEAGE_ID, 1, "wondering", seed)
        assert result is not None
        assert result.phase == GenerationPhase.INTERRUPTED
        assert result.success is False

    @pytest.mark.asyncio
    async def test_check_shutdown_emits_event(self) -> None:
        loop = self._make_loop()
        loop._shutdown_requested = True
        seed = MagicMock()
        await loop._check_shutdown(LINEAGE_ID, 2, "reflecting", seed)
        loop.event_store.append.assert_called_once()
        event = loop.event_store.append.call_args[0][0]
        assert event.type == "lineage.generation.interrupted"
        assert event.data["last_completed_phase"] == "reflecting"


# -- SIGINT handler tests --


class TestSIGINTHandler:
    def _make_loop(self) -> EvolutionaryLoop:
        event_store = AsyncMock()
        return EvolutionaryLoop(
            event_store=event_store,
            config=EvolutionaryLoopConfig(),
        )

    def test_install_sets_shutdown_flag_on_sigint(self) -> None:
        loop = self._make_loop()
        loop._install_sigint_handler()
        try:
            assert not loop._shutdown_requested
            # Simulate SIGINT
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)
            assert loop._shutdown_requested
        finally:
            loop._uninstall_sigint_handler()

    def test_second_sigint_raises_keyboard_interrupt(self) -> None:
        loop = self._make_loop()
        loop._install_sigint_handler()
        try:
            handler = signal.getsignal(signal.SIGINT)
            handler(signal.SIGINT, None)  # First: sets flag
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGINT, None)  # Second: force exit
        finally:
            loop._uninstall_sigint_handler()

    def test_uninstall_restores_original_handler(self) -> None:
        loop = self._make_loop()
        original = signal.getsignal(signal.SIGINT)
        loop._install_sigint_handler()
        loop._uninstall_sigint_handler()
        assert signal.getsignal(signal.SIGINT) is original


# -- Reflect/Evaluate serialization tests --


class TestCheckShutdownSerialization:
    """Test that _check_shutdown serializes reflect_output and evaluation_summary."""

    def _make_loop(self) -> EvolutionaryLoop:
        event_store = AsyncMock()
        event_store.append = AsyncMock()
        return EvolutionaryLoop(
            event_store=event_store,
            config=EvolutionaryLoopConfig(),
        )

    @pytest.mark.asyncio
    async def test_reflect_output_serialized_in_partial_state(self) -> None:
        loop = self._make_loop()
        loop._shutdown_requested = True
        seed = MagicMock()
        seed.to_dict.return_value = {"goal": "test"}

        reflect_out = ReflectOutput(
            refined_goal="better goal",
            refined_constraints=("c1",),
            refined_acs=("ac1",),
            ontology_mutations=(),
            reasoning="because reasons",
        )

        result = await loop._check_shutdown(
            LINEAGE_ID, 2, "reflecting", seed, reflect_output=reflect_out
        )
        assert result is not None

        # Check event was emitted with reflect_output in partial_state
        event = loop.event_store.append.call_args[0][0]
        ps = event.data["partial_state"]
        assert "reflect_output" in ps
        assert ps["reflect_output"]["refined_goal"] == "better goal"
        assert ps["reflect_output"]["reasoning"] == "because reasons"

    @pytest.mark.asyncio
    async def test_evaluation_summary_serialized_in_partial_state(self) -> None:
        loop = self._make_loop()
        loop._shutdown_requested = True
        seed = MagicMock()
        seed.to_dict.return_value = {"goal": "test"}

        eval_summary = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=0.95,
        )

        result = await loop._check_shutdown(
            LINEAGE_ID,
            2,
            "evaluating",
            seed,
            execution_output="output",
            evaluation_summary=eval_summary,
        )
        assert result is not None
        assert result.evaluation_summary == eval_summary

        # Check event was emitted with evaluation_summary in partial_state
        event = loop.event_store.append.call_args[0][0]
        ps = event.data["partial_state"]
        assert "evaluation_summary" in ps
        assert ps["evaluation_summary"]["final_approved"] is True
        assert ps["evaluation_summary"]["score"] == 0.95

    @pytest.mark.asyncio
    async def test_all_outputs_serialized_together(self) -> None:
        loop = self._make_loop()
        loop._shutdown_requested = True
        seed = MagicMock()
        seed.to_dict.return_value = {"goal": "test"}

        from ouroboros.evolution.wonder import WonderOutput

        wonder_out = WonderOutput(questions=("q1", "q2"), should_continue=True)
        reflect_out = ReflectOutput(
            refined_goal="g",
            ontology_mutations=(),
            reasoning="r",
        )
        eval_summary = EvaluationSummary(final_approved=False, highest_stage_passed=2, score=0.6)

        result = await loop._check_shutdown(
            LINEAGE_ID,
            3,
            "evaluating",
            seed,
            wonder_output=wonder_out,
            reflect_output=reflect_out,
            execution_output="exec output",
            evaluation_summary=eval_summary,
        )
        assert result is not None

        event = loop.event_store.append.call_args[0][0]
        ps = event.data["partial_state"]
        assert ps["wonder_questions"] == ["q1", "q2"]
        assert ps["reflect_output"]["refined_goal"] == "g"
        assert ps["execution_output"] == "exec output"
        assert ps["evaluation_summary"]["score"] == 0.6


# -- Resume restore tests --


class TestResumeRestore:
    """Test that _run_generation_phases restores reflect_output and evaluation on resume."""

    def _make_loop(self) -> EvolutionaryLoop:
        event_store = AsyncMock()
        event_store.append = AsyncMock()
        return EvolutionaryLoop(
            event_store=event_store,
            config=EvolutionaryLoopConfig(),
        )

    def _make_lineage_with_interrupted_gen(
        self, partial_state: dict, last_completed_phase: str = "reflecting"
    ):
        """Create a mock lineage with an interrupted generation."""
        from ouroboros.core.lineage import OntologyLineage

        gen = MagicMock()
        gen.phase = GenerationPhase.INTERRUPTED
        gen.partial_state = partial_state
        gen.evaluation_summary = None
        gen.execution_output = None
        gen.seed_json = None

        prev_gen = MagicMock()
        prev_gen.phase = GenerationPhase.COMPLETED
        prev_gen.evaluation_summary = EvaluationSummary(
            final_approved=False, highest_stage_passed=1, score=0.5
        )
        prev_gen.execution_output = "prev output"

        lineage = MagicMock(spec=OntologyLineage)
        lineage.lineage_id = LINEAGE_ID
        lineage.generations = [prev_gen, gen]
        return lineage

    @pytest.mark.asyncio
    async def test_reflect_output_restored_on_resume(self) -> None:
        """Interrupt after Reflect → resume restores reflect_output (skips re-running Reflect)."""
        reflect_data = ReflectOutput(
            refined_goal="evolved goal",
            refined_constraints=("c1",),
            refined_acs=("ac1",),
            ontology_mutations=(),
            reasoning="from reflect",
        ).model_dump(mode="json")

        partial_state = {
            "wonder_questions": ["q1"],
            "reflect_output": reflect_data,
        }

        loop = self._make_loop()
        lineage = self._make_lineage_with_interrupted_gen(partial_state, "reflecting")

        seed = MagicMock()
        seed.metadata.seed_id = "s1"
        seed.metadata.parent_seed_id = None
        seed.ontology_schema = MagicMock()
        seed.to_dict.return_value = {"goal": "test"}

        # Mock seed_generator to capture the reflect_output it receives
        captured_reflect = []

        def mock_generate(current_seed, reflect_out):
            captured_reflect.append(reflect_out)
            result = MagicMock()
            result.is_err = False
            result.is_ok = True
            result.value = seed  # Return same seed for simplicity
            return result

        loop.seed_generator = MagicMock()
        loop.seed_generator.generate_from_reflect = mock_generate

        # No executor/evaluator — we just want to verify reflect_output is restored
        result = await loop._run_generation_phases(
            lineage=lineage,
            generation_number=2,
            current_seed=seed,
            execute=False,
            resume_after_phase="reflecting",
        )

        assert result.is_ok
        gen_result = result.value

        # reflect_output should be restored from partial_state
        assert gen_result.reflect_output is not None
        assert gen_result.reflect_output.refined_goal == "evolved goal"

        # seed_generator should have been called with the restored reflect_output
        assert len(captured_reflect) == 1
        assert captured_reflect[0].refined_goal == "evolved goal"

    @pytest.mark.asyncio
    async def test_evaluation_restored_on_resume(self) -> None:
        """Interrupt after Evaluate → resume restores evaluation (skips re-running)."""
        eval_data = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            score=0.92,
        ).model_dump(mode="json")

        partial_state = {
            "wonder_questions": ["q1"],
            "reflect_output": ReflectOutput(
                refined_goal="g", ontology_mutations=(), reasoning="r"
            ).model_dump(mode="json"),
            "execution_output": "exec out",
            "evaluation_summary": eval_data,
        }

        loop = self._make_loop()
        lineage = self._make_lineage_with_interrupted_gen(partial_state, "evaluating")

        seed = MagicMock()
        seed.metadata.seed_id = "s1"
        seed.metadata.parent_seed_id = None
        seed.ontology_schema = MagicMock()
        seed.to_dict.return_value = {"goal": "test"}

        # Set up seed_generator that returns the seed
        gen_result_mock = MagicMock()
        gen_result_mock.is_err = False
        gen_result_mock.is_ok = True
        gen_result_mock.value = seed
        loop.seed_generator = MagicMock()
        loop.seed_generator.generate_from_reflect.return_value = gen_result_mock

        # Set evaluator to track if it gets called (it shouldn't)
        evaluator_called = []

        async def mock_evaluator(s, output):
            evaluator_called.append(True)
            return EvaluationSummary(final_approved=False, highest_stage_passed=1)

        loop.evaluator = mock_evaluator

        result = await loop._run_generation_phases(
            lineage=lineage,
            generation_number=2,
            current_seed=seed,
            execute=True,
            resume_after_phase="evaluating",
        )

        assert result.is_ok
        gen_result = result.value

        # evaluation_summary should be restored, not re-run
        assert gen_result.evaluation_summary is not None
        assert gen_result.evaluation_summary.score == 0.92
        assert gen_result.evaluation_summary.final_approved is True

        # evaluator should NOT have been called
        assert len(evaluator_called) == 0
