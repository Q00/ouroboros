"""Tests for HOTL Convergence Accelerator."""

import pytest
from datetime import datetime, timedelta

from ouroboros.gemini3.convergence_accelerator import (
    HOTLConvergenceAccelerator,
    IterationData,
    IterationOutcome,
    ConvergenceState,
    MAX_CONTEXT_TOKENS,
)


@pytest.fixture
def accelerator() -> HOTLConvergenceAccelerator:
    """Create a test accelerator instance."""
    return HOTLConvergenceAccelerator()


@pytest.fixture
def sample_iteration() -> IterationData:
    """Create a sample iteration for testing."""
    return IterationData(
        iteration_id="test_iter_001",
        ac_id="AC_1",
        execution_id="test_exec_001",
        timestamp=datetime.now(),
        outcome=IterationOutcome.SUCCESS,
        artifact="def test(): pass",
        drift_score=0.3,
        confidence=0.8,
        model_used="gemini-2.5-pro",
        token_count=500,
    )


class TestIterationData:
    """Tests for IterationData model."""

    def test_compute_hash_same_content(self, sample_iteration: IterationData) -> None:
        """Same content should produce same hash."""
        iter1 = sample_iteration
        iter2 = IterationData(
            iteration_id="different_id",
            ac_id="AC_2",
            execution_id="different_exec",
            timestamp=datetime.now() + timedelta(hours=1),
            outcome=IterationOutcome.FAILURE,
            artifact=iter1.artifact,  # Same artifact
            error_message=iter1.error_message,  # Same error
            drift_score=0.9,
            confidence=0.2,
        )

        assert iter1.compute_hash() == iter2.compute_hash()

    def test_compute_hash_different_content(self, sample_iteration: IterationData) -> None:
        """Different content should produce different hash."""
        iter1 = sample_iteration
        iter2 = IterationData(
            iteration_id="test_iter_002",
            ac_id="AC_1",
            execution_id="test_exec_001",
            timestamp=datetime.now(),
            outcome=IterationOutcome.SUCCESS,
            artifact="def different(): pass",  # Different artifact
            drift_score=0.3,
        )

        assert iter1.compute_hash() != iter2.compute_hash()

    def test_to_context_string(self, sample_iteration: IterationData) -> None:
        """Context string should contain key information."""
        context = sample_iteration.to_context_string()

        assert sample_iteration.iteration_id in context
        assert sample_iteration.ac_id in context
        assert "SUCCESS" in context
        assert sample_iteration.artifact in context


class TestHOTLConvergenceAccelerator:
    """Tests for HOTLConvergenceAccelerator."""

    @pytest.mark.asyncio
    async def test_initialize(self, accelerator: HOTLConvergenceAccelerator) -> None:
        """Accelerator should initialize without error."""
        await accelerator.initialize()
        assert accelerator._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, accelerator: HOTLConvergenceAccelerator) -> None:
        """Multiple initialize calls should be safe."""
        await accelerator.initialize()
        await accelerator.initialize()
        await accelerator.initialize()
        assert accelerator._initialized is True

    @pytest.mark.asyncio
    async def test_track_iteration(
        self,
        accelerator: HOTLConvergenceAccelerator,
        sample_iteration: IterationData,
    ) -> None:
        """Should track iterations successfully."""
        result = await accelerator.track_iteration(sample_iteration)

        assert result.is_ok
        assert result.value is True
        assert len(accelerator._iterations) == 1

    @pytest.mark.asyncio
    async def test_track_iteration_deduplication(
        self,
        accelerator: HOTLConvergenceAccelerator,
        sample_iteration: IterationData,
    ) -> None:
        """Should deduplicate identical iterations."""
        await accelerator.track_iteration(sample_iteration)
        result = await accelerator.track_iteration(sample_iteration)

        assert result.is_ok
        assert result.value is False  # Deduplicated
        assert len(accelerator._iterations) == 1

    @pytest.mark.asyncio
    async def test_convergence_state_empty(
        self,
        accelerator: HOTLConvergenceAccelerator,
    ) -> None:
        """Empty accelerator should return default state."""
        state = accelerator.get_convergence_state()

        assert state.total_iterations == 0
        assert state.satisfaction_percentage == 0.0
        # Empty state has convergence_rate=0, so is_converging is False
        assert state.is_converging is False
        assert state.is_stagnant is False

    @pytest.mark.asyncio
    async def test_convergence_state_with_iterations(
        self,
        accelerator: HOTLConvergenceAccelerator,
    ) -> None:
        """State should reflect tracked iterations."""
        # Track some iterations
        for i in range(5):
            outcome = IterationOutcome.SUCCESS if i % 2 == 0 else IterationOutcome.FAILURE
            iteration = IterationData(
                iteration_id=f"iter_{i}",
                ac_id=f"AC_{i % 3}",
                execution_id="test_exec",
                timestamp=datetime.now() + timedelta(minutes=i),
                outcome=outcome,
            )
            await accelerator.track_iteration(iteration, deduplicate=False)

        state = accelerator.get_convergence_state()

        assert state.total_iterations == 5
        assert state.successful_iterations == 3
        assert state.failed_iterations == 2

    @pytest.mark.asyncio
    async def test_get_iteration_history(
        self,
        accelerator: HOTLConvergenceAccelerator,
    ) -> None:
        """Should return iteration history."""
        # Track iterations
        for i in range(10):
            iteration = IterationData(
                iteration_id=f"iter_{i}",
                ac_id="AC_1" if i < 5 else "AC_2",
                execution_id="test_exec",
                timestamp=datetime.now() + timedelta(minutes=i),
                outcome=IterationOutcome.SUCCESS,
            )
            await accelerator.track_iteration(iteration, deduplicate=False)

        # Get all history
        all_history = accelerator.get_iteration_history()
        assert len(all_history) == 10

        # Get filtered by AC
        ac1_history = accelerator.get_iteration_history(ac_id="AC_1")
        assert len(ac1_history) == 5

        # Get with limit
        limited = accelerator.get_iteration_history(limit=3)
        assert len(limited) == 3

    @pytest.mark.asyncio
    async def test_convergence_curve(
        self,
        accelerator: HOTLConvergenceAccelerator,
    ) -> None:
        """Should track convergence curve points."""
        for i in range(5):
            iteration = IterationData(
                iteration_id=f"iter_{i}",
                ac_id=f"AC_{i}",
                execution_id="test_exec",
                timestamp=datetime.now() + timedelta(minutes=i),
                outcome=IterationOutcome.SUCCESS,
            )
            await accelerator.track_iteration(iteration, deduplicate=False)

        curve = accelerator.get_convergence_curve()

        assert len(curve) == 5
        assert curve[0].iteration_number == 1
        assert curve[-1].iteration_number == 5

    @pytest.mark.asyncio
    async def test_context_string_building(
        self,
        accelerator: HOTLConvergenceAccelerator,
    ) -> None:
        """Should build comprehensive context string."""
        for i in range(3):
            iteration = IterationData(
                iteration_id=f"iter_{i}",
                ac_id="AC_1",
                execution_id="test_exec",
                timestamp=datetime.now() + timedelta(minutes=i),
                outcome=IterationOutcome.SUCCESS,
                artifact=f"def func_{i}(): pass",
            )
            await accelerator.track_iteration(iteration, deduplicate=False)

        context = accelerator.build_context_string()

        assert "HOTL Iteration History" in context
        assert "iter_0" in context
        assert "AC_1" in context

    @pytest.mark.asyncio
    async def test_stagnation_detection(
        self,
        accelerator: HOTLConvergenceAccelerator,
    ) -> None:
        """Should detect stagnation patterns."""
        # Track 5 consecutive failures with same drift
        for i in range(5):
            iteration = IterationData(
                iteration_id=f"iter_{i}",
                ac_id="AC_1",
                execution_id="test_exec",
                timestamp=datetime.now() + timedelta(minutes=i),
                outcome=IterationOutcome.FAILURE,
                drift_score=0.5,  # Same drift
            )
            await accelerator.track_iteration(iteration, deduplicate=False)

        state = accelerator.get_convergence_state()
        assert state.is_stagnant is True

    @pytest.mark.asyncio
    async def test_failure_summary(
        self,
        accelerator: HOTLConvergenceAccelerator,
    ) -> None:
        """Should generate failure summary."""
        # Track some failures
        for i in range(5):
            iteration = IterationData(
                iteration_id=f"iter_{i}",
                ac_id=f"AC_{i % 2}",
                execution_id="test_exec",
                timestamp=datetime.now() + timedelta(minutes=i),
                outcome=IterationOutcome.FAILURE if i % 2 == 0 else IterationOutcome.SUCCESS,
                error_message="ImportError: No module" if i % 2 == 0 else "",
            )
            await accelerator.track_iteration(iteration, deduplicate=False)

        summary = accelerator.get_failure_summary()

        assert summary["total_failures"] == 3
        assert "AC_0" in summary["failures_by_ac"]


class TestMaxContextTokens:
    """Tests for context token limits."""

    def test_max_context_tokens_value(self) -> None:
        """MAX_CONTEXT_TOKENS should be 1M."""
        assert MAX_CONTEXT_TOKENS == 1_000_000
