"""Unit tests for lateral thinking personas (Story 4.2).

Tests cover:
- ThinkingPersona enum (5 personas)
- PersonaStrategy dataclass
- LateralThinkingResult dataclass
- LateralThinker class:
  - Strategy retrieval
  - Alternative generation
  - Persona suggestion for patterns
- Event classes for lateral thinking
"""

import pytest

from ouroboros.resilience.lateral import (
    AllPersonasExhaustedEvent,
    LateralThinker,
    LateralThinkingActivatedEvent,
    LateralThinkingFailedEvent,
    LateralThinkingResult,
    LateralThinkingSucceededEvent,
    PersonaStrategy,
    ThinkingPersona,
)
from ouroboros.resilience.stagnation import StagnationPattern


# =============================================================================
# ThinkingPersona Enum Tests
# =============================================================================


class TestThinkingPersona:
    """Test the ThinkingPersona enum."""

    def test_all_five_personas_exist(self) -> None:
        """Test that all 5 personas are defined."""
        assert len(ThinkingPersona) == 5
        assert ThinkingPersona.HACKER
        assert ThinkingPersona.RESEARCHER
        assert ThinkingPersona.SIMPLIFIER
        assert ThinkingPersona.ARCHITECT
        assert ThinkingPersona.CONTRARIAN

    def test_persona_values(self) -> None:
        """Test persona enum values."""
        assert ThinkingPersona.HACKER.value == "hacker"
        assert ThinkingPersona.RESEARCHER.value == "researcher"
        assert ThinkingPersona.SIMPLIFIER.value == "simplifier"
        assert ThinkingPersona.ARCHITECT.value == "architect"
        assert ThinkingPersona.CONTRARIAN.value == "contrarian"

    def test_persona_descriptions(self) -> None:
        """Test that each persona has a description."""
        for persona in ThinkingPersona:
            assert persona.description
            assert isinstance(persona.description, str)
            assert len(persona.description) > 10

    def test_hacker_description(self) -> None:
        """Test Hacker persona description."""
        assert "unconventional" in ThinkingPersona.HACKER.description.lower()

    def test_researcher_description(self) -> None:
        """Test Researcher persona description."""
        assert "information" in ThinkingPersona.RESEARCHER.description.lower()

    def test_simplifier_description(self) -> None:
        """Test Simplifier persona description."""
        assert "complexity" in ThinkingPersona.SIMPLIFIER.description.lower()

    def test_architect_description(self) -> None:
        """Test Architect persona description."""
        assert "restructure" in ThinkingPersona.ARCHITECT.description.lower()

    def test_contrarian_description(self) -> None:
        """Test Contrarian persona description."""
        assert "assumption" in ThinkingPersona.CONTRARIAN.description.lower()

    def test_persona_affinity_patterns(self) -> None:
        """Test that each persona has affinity patterns."""
        for persona in ThinkingPersona:
            patterns = persona.affinity_patterns
            assert isinstance(patterns, tuple)
            assert len(patterns) > 0
            assert all(isinstance(p, StagnationPattern) for p in patterns)

    def test_hacker_affinity_spinning(self) -> None:
        """Test Hacker has affinity for Spinning pattern."""
        assert StagnationPattern.SPINNING in ThinkingPersona.HACKER.affinity_patterns

    def test_researcher_affinity_no_drift(self) -> None:
        """Test Researcher has affinity for No Drift pattern."""
        assert StagnationPattern.NO_DRIFT in ThinkingPersona.RESEARCHER.affinity_patterns

    def test_simplifier_affinity_diminishing(self) -> None:
        """Test Simplifier has affinity for Diminishing Returns pattern."""
        assert StagnationPattern.DIMINISHING_RETURNS in ThinkingPersona.SIMPLIFIER.affinity_patterns

    def test_architect_affinity_oscillation(self) -> None:
        """Test Architect has affinity for Oscillation pattern."""
        assert StagnationPattern.OSCILLATION in ThinkingPersona.ARCHITECT.affinity_patterns

    def test_contrarian_affinity_all_patterns(self) -> None:
        """Test Contrarian has affinity for all patterns."""
        contrarian_affinities = ThinkingPersona.CONTRARIAN.affinity_patterns
        assert len(contrarian_affinities) == 4
        for pattern in StagnationPattern:
            assert pattern in contrarian_affinities


# =============================================================================
# PersonaStrategy Tests
# =============================================================================


class TestPersonaStrategy:
    """Test the PersonaStrategy dataclass."""

    def test_strategy_creation(self) -> None:
        """Test creating a PersonaStrategy."""
        strategy = PersonaStrategy(
            persona=ThinkingPersona.HACKER,
            system_prompt="Test prompt",
            approach_instructions=("Step 1", "Step 2"),
            question_templates=("Question 1?",),
        )

        assert strategy.persona == ThinkingPersona.HACKER
        assert strategy.system_prompt == "Test prompt"
        assert strategy.approach_instructions == ("Step 1", "Step 2")
        assert strategy.question_templates == ("Question 1?",)

    def test_strategy_immutability(self) -> None:
        """Test that PersonaStrategy is frozen."""
        strategy = PersonaStrategy(
            persona=ThinkingPersona.HACKER,
            system_prompt="Test",
            approach_instructions=("Step 1",),
        )

        with pytest.raises(AttributeError):
            strategy.system_prompt = "Modified"  # type: ignore

    def test_default_question_templates(self) -> None:
        """Test default empty question templates."""
        strategy = PersonaStrategy(
            persona=ThinkingPersona.HACKER,
            system_prompt="Test",
            approach_instructions=("Step 1",),
        )

        assert strategy.question_templates == ()


# =============================================================================
# LateralThinkingResult Tests
# =============================================================================


class TestLateralThinkingResult:
    """Test the LateralThinkingResult dataclass."""

    def test_result_creation(self) -> None:
        """Test creating a LateralThinkingResult."""
        result = LateralThinkingResult(
            persona=ThinkingPersona.RESEARCHER,
            prompt="Test prompt",
            approach_summary="Summary",
            questions=("Q1?", "Q2?"),
        )

        assert result.persona == ThinkingPersona.RESEARCHER
        assert result.prompt == "Test prompt"
        assert result.approach_summary == "Summary"
        assert result.questions == ("Q1?", "Q2?")

    def test_result_immutability(self) -> None:
        """Test that LateralThinkingResult is frozen."""
        result = LateralThinkingResult(
            persona=ThinkingPersona.RESEARCHER,
            prompt="Test",
            approach_summary="Summary",
        )

        with pytest.raises(AttributeError):
            result.prompt = "Modified"  # type: ignore

    def test_default_empty_questions(self) -> None:
        """Test default empty questions tuple."""
        result = LateralThinkingResult(
            persona=ThinkingPersona.RESEARCHER,
            prompt="Test",
            approach_summary="Summary",
        )

        assert result.questions == ()


# =============================================================================
# LateralThinker Tests
# =============================================================================


class TestLateralThinker:
    """Test the LateralThinker class."""

    def test_initialization(self) -> None:
        """Test LateralThinker initialization."""
        thinker = LateralThinker()

        # Should have strategies for all personas
        for persona in ThinkingPersona:
            strategy = thinker.get_strategy(persona)
            assert strategy.persona == persona

    def test_get_all_personas(self) -> None:
        """Test getting all personas."""
        thinker = LateralThinker()
        personas = thinker.get_all_personas()

        assert len(personas) == 5
        assert set(personas) == set(ThinkingPersona)

    def test_custom_strategy_override(self) -> None:
        """Test custom strategy override."""
        custom = PersonaStrategy(
            persona=ThinkingPersona.HACKER,
            system_prompt="Custom hacker prompt",
            approach_instructions=("Custom step",),
        )

        thinker = LateralThinker(custom_strategies={ThinkingPersona.HACKER: custom})

        assert thinker.get_strategy(ThinkingPersona.HACKER).system_prompt == "Custom hacker prompt"
        # Other strategies should be unchanged
        assert thinker.get_strategy(ThinkingPersona.RESEARCHER).system_prompt != "Custom hacker prompt"


class TestLateralThinkerGenerate:
    """Test LateralThinker.generate_alternative()."""

    def test_generate_returns_result(self) -> None:
        """Test generate_alternative returns a Result."""
        thinker = LateralThinker()

        result = thinker.generate_alternative(
            persona=ThinkingPersona.HACKER,
            problem_context="Cannot parse XML",
            current_approach="Using regex",
        )

        assert result.is_ok
        assert isinstance(result.value, LateralThinkingResult)

    def test_generate_includes_persona(self) -> None:
        """Test generated result includes correct persona."""
        thinker = LateralThinker()

        result = thinker.generate_alternative(
            persona=ThinkingPersona.RESEARCHER,
            problem_context="Test problem",
            current_approach="Test approach",
        )

        assert result.value.persona == ThinkingPersona.RESEARCHER

    def test_generate_includes_problem_context(self) -> None:
        """Test generated prompt includes problem context."""
        thinker = LateralThinker()

        result = thinker.generate_alternative(
            persona=ThinkingPersona.SIMPLIFIER,
            problem_context="Very specific problem description",
            current_approach="Current approach description",
        )

        assert "Very specific problem description" in result.value.prompt
        assert "Current approach description" in result.value.prompt

    def test_generate_includes_failed_attempts(self) -> None:
        """Test generated prompt includes failed attempts."""
        thinker = LateralThinker()

        result = thinker.generate_alternative(
            persona=ThinkingPersona.ARCHITECT,
            problem_context="Test problem",
            current_approach="Test approach",
            failed_attempts=("First failed attempt", "Second failed attempt"),
        )

        assert "First failed attempt" in result.value.prompt
        assert "Second failed attempt" in result.value.prompt
        assert "Previous Failed Attempts" in result.value.prompt

    def test_generate_has_approach_summary(self) -> None:
        """Test generated result has approach summary."""
        thinker = LateralThinker()

        result = thinker.generate_alternative(
            persona=ThinkingPersona.CONTRARIAN,
            problem_context="Test",
            current_approach="Test",
        )

        assert "Contrarian" in result.value.approach_summary
        assert "assumption" in result.value.approach_summary.lower()

    def test_generate_has_questions(self) -> None:
        """Test generated result has questions."""
        thinker = LateralThinker()

        result = thinker.generate_alternative(
            persona=ThinkingPersona.HACKER,
            problem_context="Test",
            current_approach="Test",
        )

        assert len(result.value.questions) > 0
        assert all(q.endswith("?") for q in result.value.questions)

    def test_generate_all_personas(self) -> None:
        """Test generating alternatives for all personas."""
        thinker = LateralThinker()

        for persona in ThinkingPersona:
            result = thinker.generate_alternative(
                persona=persona,
                problem_context="Generic problem",
                current_approach="Generic approach",
            )

            assert result.is_ok
            assert result.value.persona == persona
            assert len(result.value.prompt) > 0


class TestLateralThinkerSuggest:
    """Test LateralThinker.suggest_persona_for_pattern()."""

    def test_suggest_for_spinning(self) -> None:
        """Test persona suggestion for Spinning pattern."""
        thinker = LateralThinker()

        persona = thinker.suggest_persona_for_pattern(StagnationPattern.SPINNING)

        assert persona is not None
        assert StagnationPattern.SPINNING in persona.affinity_patterns

    def test_suggest_for_oscillation(self) -> None:
        """Test persona suggestion for Oscillation pattern."""
        thinker = LateralThinker()

        persona = thinker.suggest_persona_for_pattern(StagnationPattern.OSCILLATION)

        assert persona is not None
        assert StagnationPattern.OSCILLATION in persona.affinity_patterns

    def test_suggest_for_no_drift(self) -> None:
        """Test persona suggestion for No Drift pattern."""
        thinker = LateralThinker()

        persona = thinker.suggest_persona_for_pattern(StagnationPattern.NO_DRIFT)

        assert persona is not None
        assert StagnationPattern.NO_DRIFT in persona.affinity_patterns

    def test_suggest_for_diminishing_returns(self) -> None:
        """Test persona suggestion for Diminishing Returns pattern."""
        thinker = LateralThinker()

        persona = thinker.suggest_persona_for_pattern(StagnationPattern.DIMINISHING_RETURNS)

        assert persona is not None
        assert StagnationPattern.DIMINISHING_RETURNS in persona.affinity_patterns

    def test_suggest_excludes_tried_personas(self) -> None:
        """Test that suggestion excludes already-tried personas."""
        thinker = LateralThinker()

        # Exclude Hacker (has affinity for Spinning)
        persona = thinker.suggest_persona_for_pattern(
            StagnationPattern.SPINNING,
            exclude_personas=(ThinkingPersona.HACKER,),
        )

        # Should still get a suggestion (Contrarian has affinity for all)
        assert persona is not None
        assert persona != ThinkingPersona.HACKER

    def test_suggest_returns_none_when_all_excluded(self) -> None:
        """Test suggestion returns None when all personas excluded."""
        thinker = LateralThinker()

        persona = thinker.suggest_persona_for_pattern(
            StagnationPattern.SPINNING,
            exclude_personas=tuple(ThinkingPersona),
        )

        assert persona is None

    def test_suggest_fallback_to_any_remaining(self) -> None:
        """Test fallback to any remaining persona if no affinity match."""
        thinker = LateralThinker()

        # Exclude all with Spinning affinity except one
        exclude = tuple(
            p for p in ThinkingPersona
            if StagnationPattern.SPINNING in p.affinity_patterns and p != ThinkingPersona.CONTRARIAN
        )

        persona = thinker.suggest_persona_for_pattern(
            StagnationPattern.SPINNING,
            exclude_personas=exclude,
        )

        # Should still return a persona
        assert persona is not None


# =============================================================================
# LateralThinkingActivatedEvent Tests
# =============================================================================


class TestLateralThinkingActivatedEvent:
    """Test LateralThinkingActivatedEvent creation."""

    def test_event_creation(self) -> None:
        """Test creating LateralThinkingActivatedEvent."""
        event = LateralThinkingActivatedEvent(
            execution_id="exec-123",
            persona=ThinkingPersona.HACKER,
            stagnation_pattern=StagnationPattern.SPINNING,
            seed_id="seed-456",
            iteration=5,
            reason="Spinning detected",
        )

        assert event.type == "resilience.lateral.activated"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec-123"
        assert event.data["persona"] == "hacker"
        assert event.data["stagnation_pattern"] == "spinning"
        assert event.data["seed_id"] == "seed-456"
        assert event.data["iteration"] == 5
        assert event.data["reason"] == "Spinning detected"

    def test_event_without_stagnation_pattern(self) -> None:
        """Test event creation without stagnation pattern."""
        event = LateralThinkingActivatedEvent(
            execution_id="exec-123",
            persona=ThinkingPersona.ARCHITECT,
            stagnation_pattern=None,
        )

        assert event.data["stagnation_pattern"] is None

    def test_all_personas_can_create_event(self) -> None:
        """Test event creation with all personas."""
        for persona in ThinkingPersona:
            event = LateralThinkingActivatedEvent(
                execution_id="exec-123",
                persona=persona,
                stagnation_pattern=StagnationPattern.OSCILLATION,
            )

            assert event.data["persona"] == persona.value


# =============================================================================
# LateralThinkingSucceededEvent Tests
# =============================================================================


class TestLateralThinkingSucceededEvent:
    """Test LateralThinkingSucceededEvent creation."""

    def test_event_creation(self) -> None:
        """Test creating LateralThinkingSucceededEvent."""
        event = LateralThinkingSucceededEvent(
            execution_id="exec-123",
            persona=ThinkingPersona.SIMPLIFIER,
            seed_id="seed-456",
            iteration=10,
            breakthrough_summary="Simplified by removing abstraction layer",
        )

        assert event.type == "resilience.lateral.succeeded"
        assert event.aggregate_type == "execution"
        assert event.data["persona"] == "simplifier"
        assert event.data["breakthrough_summary"] == "Simplified by removing abstraction layer"

    def test_breakthrough_summary_truncation(self) -> None:
        """Test that breakthrough summary is truncated to 500 chars."""
        long_summary = "x" * 1000
        event = LateralThinkingSucceededEvent(
            execution_id="exec-123",
            persona=ThinkingPersona.RESEARCHER,
            breakthrough_summary=long_summary,
        )

        assert len(event.data["breakthrough_summary"]) == 500


# =============================================================================
# LateralThinkingFailedEvent Tests
# =============================================================================


class TestLateralThinkingFailedEvent:
    """Test LateralThinkingFailedEvent creation."""

    def test_event_creation(self) -> None:
        """Test creating LateralThinkingFailedEvent."""
        event = LateralThinkingFailedEvent(
            execution_id="exec-123",
            persona=ThinkingPersona.CONTRARIAN,
            seed_id="seed-456",
            iteration=3,
            failure_reason="Contrarian approach led to same error",
        )

        assert event.type == "resilience.lateral.failed"
        assert event.aggregate_type == "execution"
        assert event.data["persona"] == "contrarian"
        assert event.data["failure_reason"] == "Contrarian approach led to same error"

    def test_failure_reason_truncation(self) -> None:
        """Test that failure reason is truncated to 500 chars."""
        long_reason = "y" * 1000
        event = LateralThinkingFailedEvent(
            execution_id="exec-123",
            persona=ThinkingPersona.HACKER,
            failure_reason=long_reason,
        )

        assert len(event.data["failure_reason"]) == 500


# =============================================================================
# AllPersonasExhaustedEvent Tests
# =============================================================================


class TestAllPersonasExhaustedEvent:
    """Test AllPersonasExhaustedEvent creation."""

    def test_event_creation(self) -> None:
        """Test creating AllPersonasExhaustedEvent."""
        event = AllPersonasExhaustedEvent(
            execution_id="exec-123",
            tried_personas=(
                ThinkingPersona.HACKER,
                ThinkingPersona.RESEARCHER,
                ThinkingPersona.SIMPLIFIER,
            ),
            seed_id="seed-456",
            iteration=15,
        )

        assert event.type == "resilience.lateral.exhausted"
        assert event.aggregate_type == "execution"
        assert event.data["tried_personas"] == ["hacker", "researcher", "simplifier"]
        assert event.data["total_personas"] == 5

    def test_event_with_all_personas(self) -> None:
        """Test event with all personas exhausted."""
        event = AllPersonasExhaustedEvent(
            execution_id="exec-123",
            tried_personas=tuple(ThinkingPersona),
        )

        assert len(event.data["tried_personas"]) == 5
        assert event.data["total_personas"] == 5


# =============================================================================
# Integration Tests
# =============================================================================


class TestLateralThinkingIntegration:
    """Integration tests for lateral thinking workflow."""

    def test_full_workflow_pattern_to_alternative(self) -> None:
        """Test full workflow from pattern to alternative generation."""
        thinker = LateralThinker()

        # 1. Get suggested persona for pattern
        pattern = StagnationPattern.SPINNING
        persona = thinker.suggest_persona_for_pattern(pattern)
        assert persona is not None

        # 2. Generate alternative using suggested persona
        result = thinker.generate_alternative(
            persona=persona,
            problem_context="API rate limit hit repeatedly",
            current_approach="Retry with same parameters",
        )

        assert result.is_ok
        assert len(result.value.prompt) > 100
        assert len(result.value.questions) > 0

    def test_persona_rotation_on_failure(self) -> None:
        """Test persona rotation when previous attempts fail."""
        thinker = LateralThinker()
        pattern = StagnationPattern.NO_DRIFT
        tried: list[ThinkingPersona] = []

        # Try multiple personas
        for _ in range(3):
            persona = thinker.suggest_persona_for_pattern(
                pattern,
                exclude_personas=tuple(tried),
            )
            if persona is None:
                break

            result = thinker.generate_alternative(
                persona=persona,
                problem_context="Stuck with no progress",
                current_approach="Keep trying same thing",
                failed_attempts=tuple(f"Attempt with {p.value}" for p in tried),
            )

            assert result.is_ok
            tried.append(persona)

        # Should have tried multiple unique personas
        assert len(tried) > 1
        assert len(tried) == len(set(tried))

    def test_each_persona_produces_unique_prompt(self) -> None:
        """Test that different personas produce different prompts."""
        thinker = LateralThinker()
        prompts: dict[ThinkingPersona, str] = {}

        for persona in ThinkingPersona:
            result = thinker.generate_alternative(
                persona=persona,
                problem_context="Same problem",
                current_approach="Same approach",
            )
            prompts[persona] = result.value.prompt

        # All prompts should be unique
        unique_prompts = set(prompts.values())
        assert len(unique_prompts) == 5


# =============================================================================
# Strategy Content Tests
# =============================================================================


class TestStrategyContent:
    """Test that persona strategies have meaningful content."""

    def test_hacker_strategy_content(self) -> None:
        """Test Hacker strategy has appropriate content."""
        thinker = LateralThinker()
        strategy = thinker.get_strategy(ThinkingPersona.HACKER)

        assert "unconventional" in strategy.system_prompt.lower()
        assert len(strategy.approach_instructions) >= 4
        assert any("constraint" in instr.lower() for instr in strategy.approach_instructions)

    def test_researcher_strategy_content(self) -> None:
        """Test Researcher strategy has appropriate content."""
        thinker = LateralThinker()
        strategy = thinker.get_strategy(ThinkingPersona.RESEARCHER)

        assert "information" in strategy.system_prompt.lower()
        assert len(strategy.approach_instructions) >= 4
        assert any("documentation" in instr.lower() for instr in strategy.approach_instructions)

    def test_simplifier_strategy_content(self) -> None:
        """Test Simplifier strategy has appropriate content."""
        thinker = LateralThinker()
        strategy = thinker.get_strategy(ThinkingPersona.SIMPLIFIER)

        assert "complexity" in strategy.system_prompt.lower()
        assert len(strategy.approach_instructions) >= 4
        assert any("simplest" in instr.lower() for instr in strategy.approach_instructions)

    def test_architect_strategy_content(self) -> None:
        """Test Architect strategy has appropriate content."""
        thinker = LateralThinker()
        strategy = thinker.get_strategy(ThinkingPersona.ARCHITECT)

        assert "structural" in strategy.system_prompt.lower()
        assert len(strategy.approach_instructions) >= 4
        assert any("structure" in instr.lower() for instr in strategy.approach_instructions)

    def test_contrarian_strategy_content(self) -> None:
        """Test Contrarian strategy has appropriate content."""
        thinker = LateralThinker()
        strategy = thinker.get_strategy(ThinkingPersona.CONTRARIAN)

        assert "question" in strategy.system_prompt.lower()
        assert len(strategy.approach_instructions) >= 4
        assert any("opposite" in instr.lower() for instr in strategy.approach_instructions)

    def test_all_strategies_have_question_templates(self) -> None:
        """Test all strategies have question templates."""
        thinker = LateralThinker()

        for persona in ThinkingPersona:
            strategy = thinker.get_strategy(persona)
            assert len(strategy.question_templates) >= 3
            assert all(q.endswith("?") for q in strategy.question_templates)
