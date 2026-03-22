"""Unit tests for graceful shutdown (issue #169).

Covers:
- lineage_generation_interrupted event creation
- LineageProjector handles interrupted events
- find_resume_point returns INTERRUPTED for interrupted generations
- EvolutionaryLoop._check_shutdown returns GenerationResult when flag set
- SIGINT handler installation and cleanup
"""

import signal
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.lineage import GenerationPhase
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import lineage_generation_interrupted
from ouroboros.evolution.loop import EvolutionaryLoop, EvolutionaryLoopConfig
from ouroboros.evolution.projector import LineageProjector

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
