"""Lateral thinking personas for stagnation recovery.

This module implements Story 4.2: Lateral Thinking Personas.

Provides 5 thinking personas to break through stagnation:
1. Hacker: Unconventional, finds workarounds
2. Researcher: Seeks additional information
3. Simplifier: Reduces complexity, removes assumptions
4. Architect: Restructures the approach fundamentally
5. Contrarian: Challenges assumptions, inverts the problem

Design:
- Stateless thinking: Personas generate prompts, not solutions
- Pattern-aware: Selection hints based on stagnation type
- Event emission: Each persona activation emits events

Usage:
    from ouroboros.resilience.lateral import (
        LateralThinker,
        ThinkingPersona,
    )

    thinker = LateralThinker()
    result = thinker.generate_alternative(
        persona=ThinkingPersona.HACKER,
        problem_context="Failing to parse XML",
        current_approach="Using regex to parse",
    )
    print(result.value.prompt)  # Get the alternative thinking prompt
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger
from ouroboros.resilience.stagnation import StagnationPattern

log = get_logger(__name__)


# =============================================================================
# Enums and Data Models
# =============================================================================


class ThinkingPersona(str, Enum):
    """Five lateral thinking personas for breaking through stagnation.

    Each persona approaches problems from a fundamentally different angle,
    providing diverse strategies for escaping stuck states.

    Attributes:
        HACKER: Unconventional, bypasses obstacles, finds workarounds
        RESEARCHER: Seeks more information, explores context
        SIMPLIFIER: Reduces complexity, challenges assumptions
        ARCHITECT: Restructures fundamentally, changes perspective
        CONTRARIAN: Inverts assumptions, questions everything
    """

    HACKER = "hacker"
    RESEARCHER = "researcher"
    SIMPLIFIER = "simplifier"
    ARCHITECT = "architect"
    CONTRARIAN = "contrarian"

    @property
    def description(self) -> str:
        """Return human-readable description of persona."""
        descriptions = {
            ThinkingPersona.HACKER: "Finds unconventional workarounds",
            ThinkingPersona.RESEARCHER: "Seeks additional information",
            ThinkingPersona.SIMPLIFIER: "Reduces complexity",
            ThinkingPersona.ARCHITECT: "Restructures the approach",
            ThinkingPersona.CONTRARIAN: "Challenges assumptions",
        }
        return descriptions[self]

    @property
    def affinity_patterns(self) -> tuple[StagnationPattern, ...]:
        """Return stagnation patterns this persona handles well.

        Each persona has affinity for certain stagnation patterns:
        - HACKER: Good for Spinning (same error repeated)
        - RESEARCHER: Good for No Drift (needs more info)
        - SIMPLIFIER: Good for Diminishing Returns (overcomplicated)
        - ARCHITECT: Good for Oscillation (structural problem)
        - CONTRARIAN: Good for all patterns (challenges everything)
        """
        affinities: dict[ThinkingPersona, tuple[StagnationPattern, ...]] = {
            ThinkingPersona.HACKER: (StagnationPattern.SPINNING,),
            ThinkingPersona.RESEARCHER: (
                StagnationPattern.NO_DRIFT,
                StagnationPattern.DIMINISHING_RETURNS,
            ),
            ThinkingPersona.SIMPLIFIER: (
                StagnationPattern.DIMINISHING_RETURNS,
                StagnationPattern.OSCILLATION,
            ),
            ThinkingPersona.ARCHITECT: (
                StagnationPattern.OSCILLATION,
                StagnationPattern.NO_DRIFT,
            ),
            ThinkingPersona.CONTRARIAN: (
                StagnationPattern.SPINNING,
                StagnationPattern.OSCILLATION,
                StagnationPattern.NO_DRIFT,
                StagnationPattern.DIMINISHING_RETURNS,
            ),
        }
        return affinities[self]


@dataclass(frozen=True, slots=True)
class PersonaStrategy:
    """Strategy configuration for a thinking persona.

    Describes how a persona approaches problem-solving.

    Attributes:
        persona: The thinking persona.
        system_prompt: System-level prompt defining persona behavior.
        approach_instructions: Step-by-step thinking instructions.
        question_templates: Templates for probing questions.
    """

    persona: ThinkingPersona
    system_prompt: str
    approach_instructions: tuple[str, ...]
    question_templates: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class LateralThinkingResult:
    """Result of applying lateral thinking to a problem.

    Attributes:
        persona: The persona that generated this result.
        prompt: Complete prompt for LLM to think laterally.
        approach_summary: Brief summary of the thinking approach.
        questions: Probing questions to consider.
    """

    persona: ThinkingPersona
    prompt: str
    approach_summary: str
    questions: tuple[str, ...] = field(default_factory=tuple)


# =============================================================================
# Persona Strategies
# =============================================================================

_PERSONA_STRATEGIES: dict[ThinkingPersona, PersonaStrategy] | None = None


def _get_persona_strategies() -> dict[ThinkingPersona, PersonaStrategy]:
    """Lazy-load persona strategies from canonical .md files."""
    global _PERSONA_STRATEGIES
    if _PERSONA_STRATEGIES is None:
        _PERSONA_STRATEGIES = _load_persona_strategies_from_md()
    return _PERSONA_STRATEGIES


def _load_persona_strategies_from_md() -> dict[ThinkingPersona, PersonaStrategy]:
    """Load all persona strategies from agent .md files."""
    from ouroboros.agents.loader import load_persona_prompt_data

    mapping = {
        ThinkingPersona.HACKER: "hacker",
        ThinkingPersona.RESEARCHER: "researcher",
        ThinkingPersona.SIMPLIFIER: "simplifier",
        ThinkingPersona.ARCHITECT: "architect",
        ThinkingPersona.CONTRARIAN: "contrarian",
    }
    return {
        persona: PersonaStrategy(
            persona=persona,
            system_prompt=data.system_prompt,
            approach_instructions=data.approach_instructions,
            question_templates=data.question_templates,
        )
        for persona, filename in mapping.items()
        for data in [load_persona_prompt_data(filename)]
    }


# =============================================================================
# Lateral Thinker
# =============================================================================


class LateralThinker:
    """Generates alternative thinking approaches using personas.

    Stateless generator that creates prompts for LLM to think laterally.
    Each persona provides a different perspective on the problem.

    Attributes:
        strategies: Mapping of personas to their strategies.
    """

    def __init__(
        self,
        *,
        custom_strategies: dict[ThinkingPersona, PersonaStrategy] | None = None,
    ) -> None:
        """Initialize LateralThinker with optional custom strategies.

        Args:
            custom_strategies: Optional overrides for persona strategies.
        """
        self._strategies = {**_get_persona_strategies()}
        if custom_strategies:
            self._strategies.update(custom_strategies)

    def get_strategy(self, persona: ThinkingPersona) -> PersonaStrategy:
        """Get the strategy for a specific persona.

        Args:
            persona: The thinking persona.

        Returns:
            PersonaStrategy for the given persona.
        """
        return self._strategies[persona]

    def generate_alternative(
        self,
        persona: ThinkingPersona,
        problem_context: str,
        current_approach: str,
        *,
        failed_attempts: tuple[str, ...] = (),
    ) -> Result[LateralThinkingResult, str]:
        """Generate an alternative thinking approach using a persona.

        Combines persona strategy with problem context to create a prompt
        that guides LLM thinking from a different perspective.

        Args:
            persona: The thinking persona to use.
            problem_context: Description of the problem.
            current_approach: What has been tried so far.
            failed_attempts: Previous approaches that failed.

        Returns:
            Result containing LateralThinkingResult or error message.
        """
        log.debug(
            "resilience.lateral.generating",
            persona=persona.value,
            problem_length=len(problem_context),
            approach_length=len(current_approach),
            failed_count=len(failed_attempts),
        )

        strategy = self._strategies[persona]

        # Build the prompt
        prompt_parts = [
            f"## Persona: {persona.value.title()}",
            f"_{strategy.system_prompt}_",
            "",
            "## Problem Context",
            problem_context,
            "",
            "## Current Approach (Not Working)",
            current_approach,
            "",
        ]

        if failed_attempts:
            prompt_parts.extend([
                "## Previous Failed Attempts",
                *[f"- {attempt}" for attempt in failed_attempts],
                "",
            ])

        prompt_parts.extend([
            "## Lateral Thinking Instructions",
            *[f"{instr}" for instr in strategy.approach_instructions],
            "",
            "## Questions to Consider",
            *[f"- {q}" for q in strategy.question_templates],
            "",
            "## Your Alternative Approach",
            "Based on the above, propose a fundamentally different approach:",
        ])

        prompt = "\n".join(prompt_parts)

        # Generate questions specific to this problem
        questions = tuple(
            q.format(
                obstacle="the current blocker",
                domain="this problem domain",
            )
            for q in strategy.question_templates
        )

        result = LateralThinkingResult(
            persona=persona,
            prompt=prompt,
            approach_summary=f"{persona.value.title()}: {persona.description}",
            questions=questions,
        )

        log.info(
            "resilience.lateral.generated",
            persona=persona.value,
            prompt_length=len(prompt),
            questions_count=len(questions),
        )

        return Result.ok(result)

    def suggest_persona_for_pattern(
        self,
        pattern: StagnationPattern,
        *,
        exclude_personas: tuple[ThinkingPersona, ...] = (),
    ) -> ThinkingPersona | None:
        """Suggest the best persona for a given stagnation pattern.

        Considers persona affinities and excludes already-tried personas.

        Args:
            pattern: The detected stagnation pattern.
            exclude_personas: Personas to exclude from consideration.

        Returns:
            Best matching persona, or None if all excluded.
        """
        # Find personas with affinity for this pattern
        candidates = [
            persona
            for persona in ThinkingPersona
            if pattern in persona.affinity_patterns and persona not in exclude_personas
        ]

        if candidates:
            # Return first (highest affinity since we defined them in priority order)
            return candidates[0]

        # Fall back to any non-excluded persona
        remaining = [p for p in ThinkingPersona if p not in exclude_personas]
        return remaining[0] if remaining else None

    def get_all_personas(self) -> tuple[ThinkingPersona, ...]:
        """Get all available thinking personas.

        Returns:
            Tuple of all ThinkingPersona values.
        """
        return tuple(ThinkingPersona)


# =============================================================================
# Event Classes
# =============================================================================


class LateralThinkingActivatedEvent(BaseEvent):
    """Event emitted when lateral thinking is activated.

    Indicates a persona has been selected to address stagnation.
    """

    def __init__(
        self,
        execution_id: str,
        persona: ThinkingPersona,
        stagnation_pattern: StagnationPattern | None,
        *,
        seed_id: str | None = None,
        iteration: int = 0,
        reason: str = "",
    ) -> None:
        """Create LateralThinkingActivatedEvent.

        Args:
            execution_id: Execution identifier.
            persona: The selected thinking persona.
            stagnation_pattern: Pattern that triggered activation (if any).
            seed_id: Optional seed identifier.
            iteration: Current iteration number.
            reason: Human-readable reason for activation.
        """
        super().__init__(
            type="resilience.lateral.activated",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "persona": persona.value,
                "stagnation_pattern": stagnation_pattern.value if stagnation_pattern else None,
                "seed_id": seed_id,
                "iteration": iteration,
                "reason": reason,
            },
        )


class LateralThinkingSucceededEvent(BaseEvent):
    """Event emitted when lateral thinking breaks through stagnation.

    Indicates a persona successfully produced a working alternative.
    """

    def __init__(
        self,
        execution_id: str,
        persona: ThinkingPersona,
        *,
        seed_id: str | None = None,
        iteration: int = 0,
        breakthrough_summary: str = "",
    ) -> None:
        """Create LateralThinkingSucceededEvent.

        Args:
            execution_id: Execution identifier.
            persona: The persona that succeeded.
            seed_id: Optional seed identifier.
            iteration: Current iteration number.
            breakthrough_summary: Brief description of the breakthrough.
        """
        super().__init__(
            type="resilience.lateral.succeeded",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "persona": persona.value,
                "seed_id": seed_id,
                "iteration": iteration,
                "breakthrough_summary": breakthrough_summary[:500],
            },
        )


class LateralThinkingFailedEvent(BaseEvent):
    """Event emitted when a lateral thinking attempt fails.

    Indicates a persona did not produce a working alternative.
    """

    def __init__(
        self,
        execution_id: str,
        persona: ThinkingPersona,
        *,
        seed_id: str | None = None,
        iteration: int = 0,
        failure_reason: str = "",
    ) -> None:
        """Create LateralThinkingFailedEvent.

        Args:
            execution_id: Execution identifier.
            persona: The persona that failed.
            seed_id: Optional seed identifier.
            iteration: Current iteration number.
            failure_reason: Reason the persona's approach failed.
        """
        super().__init__(
            type="resilience.lateral.failed",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "persona": persona.value,
                "seed_id": seed_id,
                "iteration": iteration,
                "failure_reason": failure_reason[:500],
            },
        )


class AllPersonasExhaustedEvent(BaseEvent):
    """Event emitted when all personas have been tried without success.

    Indicates resilience has exhausted lateral thinking options.
    """

    def __init__(
        self,
        execution_id: str,
        tried_personas: tuple[ThinkingPersona, ...],
        *,
        seed_id: str | None = None,
        iteration: int = 0,
    ) -> None:
        """Create AllPersonasExhaustedEvent.

        Args:
            execution_id: Execution identifier.
            tried_personas: All personas that were attempted.
            seed_id: Optional seed identifier.
            iteration: Current iteration number.
        """
        super().__init__(
            type="resilience.lateral.exhausted",
            aggregate_type="execution",
            aggregate_id=execution_id,
            data={
                "tried_personas": [p.value for p in tried_personas],
                "total_personas": len(ThinkingPersona),
                "seed_id": seed_id,
                "iteration": iteration,
            },
        )
