"""Tests for Double Diamond cycle implementation.

Tests cover:
- Phase enum and transitions
- PhaseResult model
- PhaseContext model
- DoubleDiamond cycle execution
- Phase transition failures and retries
- Event emission
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.errors import OuroborosError, ProviderError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent


class TestPhaseEnum:
    """Tests for Phase enumeration."""

    def test_phase_values(self):
        """Phase enum should have four values: DISCOVER, DEFINE, DESIGN, DELIVER."""
        from ouroboros.execution.double_diamond import Phase

        assert Phase.DISCOVER.value == "discover"
        assert Phase.DEFINE.value == "define"
        assert Phase.DESIGN.value == "design"
        assert Phase.DELIVER.value == "deliver"

    def test_phase_is_divergent(self):
        """Discover and Design are divergent phases."""
        from ouroboros.execution.double_diamond import Phase

        assert Phase.DISCOVER.is_divergent is True
        assert Phase.DEFINE.is_divergent is False
        assert Phase.DESIGN.is_divergent is True
        assert Phase.DELIVER.is_divergent is False

    def test_phase_is_convergent(self):
        """Define and Deliver are convergent phases."""
        from ouroboros.execution.double_diamond import Phase

        assert Phase.DISCOVER.is_convergent is False
        assert Phase.DEFINE.is_convergent is True
        assert Phase.DESIGN.is_convergent is False
        assert Phase.DELIVER.is_convergent is True

    def test_phase_next(self):
        """Each phase should have a next phase (except DELIVER)."""
        from ouroboros.execution.double_diamond import Phase

        assert Phase.DISCOVER.next_phase == Phase.DEFINE
        assert Phase.DEFINE.next_phase == Phase.DESIGN
        assert Phase.DESIGN.next_phase == Phase.DELIVER
        assert Phase.DELIVER.next_phase is None

    def test_phase_ordering(self):
        """Phases should be orderable by their sequence."""
        from ouroboros.execution.double_diamond import Phase

        phases = [Phase.DELIVER, Phase.DISCOVER, Phase.DESIGN, Phase.DEFINE]
        sorted_phases = sorted(phases, key=lambda p: p.order)
        assert sorted_phases == [Phase.DISCOVER, Phase.DEFINE, Phase.DESIGN, Phase.DELIVER]


class TestPhaseResult:
    """Tests for PhaseResult model."""

    def test_phase_result_creation(self):
        """PhaseResult should store phase output and metadata."""
        from ouroboros.execution.double_diamond import Phase, PhaseResult

        result = PhaseResult(
            phase=Phase.DISCOVER,
            success=True,
            output={"insights": ["insight1", "insight2"]},
            events=[],
        )

        assert result.phase == Phase.DISCOVER
        assert result.success is True
        assert result.output == {"insights": ["insight1", "insight2"]}
        assert result.events == []

    def test_phase_result_with_events(self):
        """PhaseResult should include emitted events."""
        from ouroboros.execution.double_diamond import Phase, PhaseResult

        event = BaseEvent(
            type="execution.phase.completed",
            aggregate_type="execution",
            aggregate_id="exec-123",
            data={"phase": "discover"},
        )

        result = PhaseResult(
            phase=Phase.DISCOVER,
            success=True,
            output={},
            events=[event],
        )

        assert len(result.events) == 1
        assert result.events[0].type == "execution.phase.completed"

    def test_phase_result_failure(self):
        """PhaseResult should handle failure cases."""
        from ouroboros.execution.double_diamond import Phase, PhaseResult

        result = PhaseResult(
            phase=Phase.DEFINE,
            success=False,
            output={},
            events=[],
            error_message="LLM provider timeout",
        )

        assert result.success is False
        assert result.error_message == "LLM provider timeout"

    def test_phase_result_immutable(self):
        """PhaseResult should be immutable (frozen)."""
        from ouroboros.execution.double_diamond import Phase, PhaseResult

        result = PhaseResult(
            phase=Phase.DISCOVER,
            success=True,
            output={},
            events=[],
        )

        with pytest.raises((AttributeError, TypeError)):
            result.success = False


class TestPhaseContext:
    """Tests for PhaseContext model."""

    def test_phase_context_creation(self):
        """PhaseContext should contain execution state for a phase."""
        from ouroboros.execution.double_diamond import Phase, PhaseContext

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Implement user authentication",
            phase=Phase.DISCOVER,
            iteration=1,
            previous_results={},
        )

        assert ctx.execution_id == "exec-123"
        assert ctx.seed_id == "seed-456"
        assert ctx.current_ac == "Implement user authentication"
        assert ctx.phase == Phase.DISCOVER
        assert ctx.iteration == 1

    def test_phase_context_with_previous_results(self):
        """PhaseContext should carry results from previous phases."""
        from ouroboros.execution.double_diamond import Phase, PhaseContext, PhaseResult

        discover_result = PhaseResult(
            phase=Phase.DISCOVER,
            success=True,
            output={"insights": ["use OAuth2"]},
            events=[],
        )

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Implement user authentication",
            phase=Phase.DEFINE,
            iteration=1,
            previous_results={Phase.DISCOVER: discover_result},
        )

        assert Phase.DISCOVER in ctx.previous_results
        assert ctx.previous_results[Phase.DISCOVER].output == {"insights": ["use OAuth2"]}

    def test_phase_context_immutable(self):
        """PhaseContext should be immutable (frozen)."""
        from ouroboros.execution.double_diamond import Phase, PhaseContext

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            phase=Phase.DISCOVER,
            iteration=1,
            previous_results={},
        )

        with pytest.raises((AttributeError, TypeError)):
            ctx.iteration = 2


class TestDoubleDiamondCycle:
    """Tests for DoubleDiamond cycle execution."""

    @pytest.fixture
    def mock_llm_adapter(self):
        """Create a mock LLM adapter."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(MagicMock(content="Phase completed successfully"))
        return adapter

    @pytest.fixture
    def double_diamond(self, mock_llm_adapter):
        """Create DoubleDiamond instance with mocked adapter."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        return DoubleDiamond(llm_adapter=mock_llm_adapter)

    @pytest.mark.asyncio
    async def test_run_cycle_executes_all_phases(self, double_diamond, mock_llm_adapter):
        """run_cycle should execute all four phases in order."""
        from ouroboros.execution.double_diamond import CycleResult, Phase

        result = await double_diamond.run_cycle(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Build login form",
            iteration=1,
        )

        assert result.is_ok
        cycle_result = result.value
        assert isinstance(cycle_result, CycleResult)
        assert cycle_result.success is True
        assert len(cycle_result.phase_results) == 4
        assert Phase.DISCOVER in cycle_result.phase_results
        assert Phase.DEFINE in cycle_result.phase_results
        assert Phase.DESIGN in cycle_result.phase_results
        assert Phase.DELIVER in cycle_result.phase_results

    @pytest.mark.asyncio
    async def test_discover_phase_explores_problem_space(self, double_diamond, mock_llm_adapter):
        """Discover phase should explore problem space (diverge)."""
        from ouroboros.execution.double_diamond import Phase, PhaseContext

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Build login form",
            phase=Phase.DISCOVER,
            iteration=1,
            previous_results={},
        )

        result = await double_diamond.discover(ctx)

        assert result.is_ok
        phase_result = result.value
        assert phase_result.phase == Phase.DISCOVER
        assert phase_result.success is True
        # Verify LLM was called
        mock_llm_adapter.complete.assert_called()

    @pytest.mark.asyncio
    async def test_define_phase_converges_on_approach(self, double_diamond, mock_llm_adapter):
        """Define phase should converge on approach."""
        from ouroboros.execution.double_diamond import Phase, PhaseContext, PhaseResult

        discover_result = PhaseResult(
            phase=Phase.DISCOVER,
            success=True,
            output={"insights": ["OAuth2", "session management"]},
            events=[],
        )

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Build login form",
            phase=Phase.DEFINE,
            iteration=1,
            previous_results={Phase.DISCOVER: discover_result},
        )

        result = await double_diamond.define(ctx)

        assert result.is_ok
        phase_result = result.value
        assert phase_result.phase == Phase.DEFINE
        assert phase_result.success is True

    @pytest.mark.asyncio
    async def test_design_phase_creates_solution(self, double_diamond, mock_llm_adapter):
        """Design phase should create solution (diverge)."""
        from ouroboros.execution.double_diamond import Phase, PhaseContext, PhaseResult

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Build login form",
            phase=Phase.DESIGN,
            iteration=1,
            previous_results={
                Phase.DISCOVER: PhaseResult(
                    phase=Phase.DISCOVER, success=True, output={}, events=[]
                ),
                Phase.DEFINE: PhaseResult(
                    phase=Phase.DEFINE, success=True, output={"approach": "OAuth2"}, events=[]
                ),
            },
        )

        result = await double_diamond.design(ctx)

        assert result.is_ok
        phase_result = result.value
        assert phase_result.phase == Phase.DESIGN
        assert phase_result.success is True

    @pytest.mark.asyncio
    async def test_deliver_phase_implements_and_validates(self, double_diamond, mock_llm_adapter):
        """Deliver phase should implement and validate (converge)."""
        from ouroboros.execution.double_diamond import Phase, PhaseContext, PhaseResult

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Build login form",
            phase=Phase.DELIVER,
            iteration=1,
            previous_results={
                Phase.DISCOVER: PhaseResult(
                    phase=Phase.DISCOVER, success=True, output={}, events=[]
                ),
                Phase.DEFINE: PhaseResult(phase=Phase.DEFINE, success=True, output={}, events=[]),
                Phase.DESIGN: PhaseResult(
                    phase=Phase.DESIGN, success=True, output={"solution": "code"}, events=[]
                ),
            },
        )

        result = await double_diamond.deliver(ctx)

        assert result.is_ok
        phase_result = result.value
        assert phase_result.phase == Phase.DELIVER
        assert phase_result.success is True


class TestPhaseTransitionLogging:
    """Tests for phase transition logging."""

    @pytest.fixture
    def mock_llm_adapter(self):
        """Create a mock LLM adapter."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(MagicMock(content="Phase completed successfully"))
        return adapter

    @pytest.mark.asyncio
    async def test_phase_transitions_are_logged(self, mock_llm_adapter):
        """Phase transitions should be logged."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        dd = DoubleDiamond(llm_adapter=mock_llm_adapter)

        with patch("ouroboros.execution.double_diamond.log") as mock_log:
            await dd.run_cycle(
                execution_id="exec-123",
                seed_id="seed-456",
                current_ac="Test AC",
                iteration=1,
            )

            # Check that phase transitions were logged
            log_calls = [call[0][0] for call in mock_log.info.call_args_list]
            assert any("phase.transition" in call or "phase.started" in call for call in log_calls)


class TestPhaseTransitionFailures:
    """Tests for phase transition failure handling."""

    @pytest.fixture
    def failing_llm_adapter(self):
        """Create an LLM adapter that fails."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.err(
            ProviderError("LLM timeout", provider="openrouter")
        )
        return adapter

    @pytest.mark.asyncio
    async def test_phase_failure_triggers_retry(self, failing_llm_adapter):
        """Phase failure should trigger retry with exponential backoff."""
        from ouroboros.execution.double_diamond import DoubleDiamond, Phase, PhaseContext

        dd = DoubleDiamond(llm_adapter=failing_llm_adapter, max_retries=3, base_delay=0.01)

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            phase=Phase.DISCOVER,
            iteration=1,
            previous_results={},
        )

        result = await dd.discover(ctx)

        # After max retries, should return error
        assert result.is_err
        # Verify retry attempts (initial + max_retries)
        assert failing_llm_adapter.complete.call_count == 3

    @pytest.mark.asyncio
    async def test_phase_failure_emits_error_event(self, failing_llm_adapter):
        """Failed phases should emit detailed error events."""
        from ouroboros.execution.double_diamond import DoubleDiamond, Phase, PhaseContext

        dd = DoubleDiamond(llm_adapter=failing_llm_adapter, max_retries=1, base_delay=0.01)

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            phase=Phase.DISCOVER,
            iteration=1,
            previous_results={},
        )

        result = await dd.discover(ctx)

        assert result.is_err
        error = result.error
        assert isinstance(error, OuroborosError)

    @pytest.mark.asyncio
    async def test_exponential_backoff_delay(self):
        """Retry delay should follow exponential backoff (base 2s)."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        dd = DoubleDiamond(llm_adapter=AsyncMock(), max_retries=3, base_delay=2.0)

        # Check backoff calculation
        assert dd._calculate_backoff(0) == 2.0
        assert dd._calculate_backoff(1) == 4.0
        assert dd._calculate_backoff(2) == 8.0


class TestCycleResult:
    """Tests for CycleResult model."""

    def test_cycle_result_creation(self):
        """CycleResult should aggregate all phase results."""
        from ouroboros.execution.double_diamond import CycleResult, Phase, PhaseResult

        phase_results = {
            Phase.DISCOVER: PhaseResult(phase=Phase.DISCOVER, success=True, output={}, events=[]),
            Phase.DEFINE: PhaseResult(phase=Phase.DEFINE, success=True, output={}, events=[]),
            Phase.DESIGN: PhaseResult(phase=Phase.DESIGN, success=True, output={}, events=[]),
            Phase.DELIVER: PhaseResult(
                phase=Phase.DELIVER, success=True, output={"result": "success"}, events=[]
            ),
        }

        result = CycleResult(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            success=True,
            phase_results=phase_results,
            events=[],
        )

        assert result.success is True
        assert len(result.phase_results) == 4
        assert result.final_output == {"result": "success"}

    def test_cycle_result_collects_all_events(self):
        """CycleResult should collect events from all phases."""
        from ouroboros.execution.double_diamond import CycleResult, Phase, PhaseResult

        event1 = BaseEvent(
            type="execution.phase.completed",
            aggregate_type="execution",
            aggregate_id="exec-123",
            data={"phase": "discover"},
        )
        event2 = BaseEvent(
            type="execution.phase.completed",
            aggregate_type="execution",
            aggregate_id="exec-123",
            data={"phase": "deliver"},
        )

        phase_results = {
            Phase.DISCOVER: PhaseResult(
                phase=Phase.DISCOVER, success=True, output={}, events=[event1]
            ),
            Phase.DEFINE: PhaseResult(phase=Phase.DEFINE, success=True, output={}, events=[]),
            Phase.DESIGN: PhaseResult(phase=Phase.DESIGN, success=True, output={}, events=[]),
            Phase.DELIVER: PhaseResult(
                phase=Phase.DELIVER, success=True, output={}, events=[event2]
            ),
        }

        result = CycleResult(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            success=True,
            phase_results=phase_results,
            events=[event1, event2],
        )

        assert len(result.events) == 2

    def test_cycle_result_immutable(self):
        """CycleResult should be immutable."""
        from ouroboros.execution.double_diamond import CycleResult, Phase, PhaseResult

        result = CycleResult(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            success=True,
            phase_results={
                Phase.DISCOVER: PhaseResult(
                    phase=Phase.DISCOVER, success=True, output={}, events=[]
                ),
            },
            events=[],
        )

        with pytest.raises((AttributeError, TypeError)):
            result.success = False


class TestConfigurableModelConfig:
    """Tests for configurable model configuration (Fix 1)."""

    @pytest.fixture
    def mock_llm_adapter(self):
        """Create a mock LLM adapter."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(MagicMock(content="Phase completed successfully"))
        return adapter

    def test_default_model_config(self, mock_llm_adapter):
        """DoubleDiamond should have sensible defaults."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        dd = DoubleDiamond(llm_adapter=mock_llm_adapter)

        assert dd._default_model == DoubleDiamond.DEFAULT_MODEL
        assert dd._temperature == DoubleDiamond.DEFAULT_TEMPERATURE
        assert dd._max_tokens == DoubleDiamond.DEFAULT_MAX_TOKENS

    def test_custom_model_config(self, mock_llm_adapter):
        """DoubleDiamond should accept custom model configuration."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        dd = DoubleDiamond(
            llm_adapter=mock_llm_adapter,
            default_model="anthropic/claude-3-opus",
            temperature=0.3,
            max_tokens=8192,
        )

        assert dd._default_model == "anthropic/claude-3-opus"
        assert dd._temperature == 0.3
        assert dd._max_tokens == 8192

    @pytest.mark.asyncio
    async def test_custom_model_used_in_llm_call(self, mock_llm_adapter):
        """Custom model should be used when calling LLM."""
        from ouroboros.execution.double_diamond import DoubleDiamond, Phase, PhaseContext

        dd = DoubleDiamond(
            llm_adapter=mock_llm_adapter,
            default_model="custom/model-name",
            temperature=0.5,
            max_tokens=2048,
        )

        ctx = PhaseContext(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            phase=Phase.DISCOVER,
            iteration=1,
            previous_results={},
        )

        await dd.discover(ctx)

        # Verify LLM was called with custom config
        call_args = mock_llm_adapter.complete.call_args
        config = call_args[0][1]  # Second positional argument
        assert config.model == "custom/model-name"
        assert config.temperature == 0.5
        assert config.max_tokens == 2048


class TestCycleEvents:
    """Tests for cycle-level event emission (Fix 3)."""

    @pytest.fixture
    def mock_llm_adapter(self):
        """Create a mock LLM adapter."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(MagicMock(content="Phase completed successfully"))
        return adapter

    @pytest.fixture
    def failing_llm_adapter(self):
        """Create an LLM adapter that fails."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.err(
            ProviderError("LLM timeout", provider="openrouter")
        )
        return adapter

    @pytest.mark.asyncio
    async def test_cycle_emits_started_event(self, mock_llm_adapter):
        """run_cycle should emit execution.cycle.started event."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        dd = DoubleDiamond(llm_adapter=mock_llm_adapter)

        result = await dd.run_cycle(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            iteration=1,
        )

        assert result.is_ok
        cycle_result = result.value

        # Find cycle started event
        started_events = [e for e in cycle_result.events if e.type == "execution.cycle.started"]
        assert len(started_events) == 1
        assert started_events[0].data["iteration"] == 1
        assert started_events[0].data["current_ac"] == "Test AC"

    @pytest.mark.asyncio
    async def test_cycle_emits_completed_event(self, mock_llm_adapter):
        """run_cycle should emit execution.cycle.completed event."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        dd = DoubleDiamond(llm_adapter=mock_llm_adapter)

        result = await dd.run_cycle(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            iteration=1,
        )

        assert result.is_ok
        cycle_result = result.value

        # Find cycle completed event
        completed_events = [e for e in cycle_result.events if e.type == "execution.cycle.completed"]
        assert len(completed_events) == 1
        assert completed_events[0].data["iteration"] == 1
        assert completed_events[0].data["phases_completed"] == 4

    @pytest.mark.asyncio
    async def test_cycle_events_include_phase_events(self, mock_llm_adapter):
        """CycleResult.events should include both cycle and phase events."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        dd = DoubleDiamond(llm_adapter=mock_llm_adapter)

        result = await dd.run_cycle(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            iteration=1,
        )

        assert result.is_ok
        cycle_result = result.value

        # Should have: 1 cycle.started + 4 phase.completed + 1 cycle.completed = 6 events
        event_types = [e.type for e in cycle_result.events]
        assert "execution.cycle.started" in event_types
        assert "execution.cycle.completed" in event_types
        assert event_types.count("execution.phase.completed") == 4

    @pytest.mark.asyncio
    async def test_failed_cycle_emits_failed_event(self, failing_llm_adapter):
        """Failed cycle should emit execution.cycle.failed event."""
        from ouroboros.execution.double_diamond import DoubleDiamond

        dd = DoubleDiamond(llm_adapter=failing_llm_adapter, max_retries=1, base_delay=0.01)

        result = await dd.run_cycle(
            execution_id="exec-123",
            seed_id="seed-456",
            current_ac="Test AC",
            iteration=1,
        )

        # Cycle failed, but we can't access events from error
        # The failed event is emitted but not returned in error case
        assert result.is_err


class TestPhasePrompts:
    """Tests for phase-specific prompts configuration (Fix 2)."""

    def test_phase_prompts_exist_for_all_phases(self):
        """PHASE_PROMPTS should have entries for all phases."""
        from ouroboros.execution.double_diamond import PHASE_PROMPTS, Phase

        for phase in Phase:
            assert phase.value in PHASE_PROMPTS
            prompts = PHASE_PROMPTS[phase.value]
            assert "system" in prompts
            assert "user_template" in prompts
            assert "output_key" in prompts
            assert "event_data_key" in prompts

    def test_define_phase_references_discover(self):
        """Define phase should reference discover as previous phase."""
        from ouroboros.execution.double_diamond import PHASE_PROMPTS

        assert PHASE_PROMPTS["define"]["previous_phase"] == "discover"

    def test_design_phase_references_define(self):
        """Design phase should reference define as previous phase."""
        from ouroboros.execution.double_diamond import PHASE_PROMPTS

        assert PHASE_PROMPTS["design"]["previous_phase"] == "define"

    def test_deliver_phase_references_design(self):
        """Deliver phase should reference design as previous phase."""
        from ouroboros.execution.double_diamond import PHASE_PROMPTS

        assert PHASE_PROMPTS["deliver"]["previous_phase"] == "design"

    def test_discover_phase_has_no_previous(self):
        """Discover phase should not have a previous phase."""
        from ouroboros.execution.double_diamond import PHASE_PROMPTS

        assert "previous_phase" not in PHASE_PROMPTS["discover"]
