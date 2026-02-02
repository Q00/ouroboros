"""AOP Framework for Ontological Analysis.

This module provides Aspect-Oriented Programming capabilities for
applying ontological analysis as a cross-cutting concern across
Interview, Consensus, and Resilience phases.

Pattern: Protocol + Strategy + Dependency Injection
- OntologicalAspect: Central weaver (Around Advice)
- OntologyStrategy: Protocol for join-point-specific analysis
- TTLCache: Performance optimization for LLM calls

Usage:
    # Create aspect with strategy
    aspect = OntologicalAspect(
        strategy=DevilAdvocateStrategy(llm_adapter),
    )

    # Execute with ontological analysis
    result = await aspect.execute(
        context=consensus_context,
        core_operation=lambda ctx: consensus.deliberate(ctx),
    )

Design Reference: docs/ontological-framework/aop-design.md
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import logging
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar

from cachetools import TTLCache

from ouroboros.core.errors import OuroborosError
from ouroboros.core.types import Result

if TYPE_CHECKING:
    from ouroboros.events.base import BaseEvent

log = logging.getLogger(__name__)


# =============================================================================
# Core Types
# =============================================================================


class OntologicalJoinPoint(StrEnum):
    """Join points where ontological analysis is applied.

    These correspond to Ouroboros phases where cross-cutting
    ontological concerns are relevant.
    """

    INTERVIEW = "interview"
    """Phase 0: Requirement clarification - ensures user asks about root problem."""

    RESILIENCE = "resilience"
    """Phase 3: Stagnation recovery - CONTRARIAN challenges assumptions."""

    CONSENSUS = "consensus"
    """Phase 4: Result evaluation - Devil's Advocate checks root vs symptom."""


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Result of ontological analysis by a Strategy.

    This represents the AOP verdict (valid/invalid) rather than
    the full ontological insight content.

    Attributes:
        is_valid: Whether the subject passes ontological scrutiny.
        confidence: Analysis confidence (0.0-1.0).
        reasoning: Explanation of the verdict.
        suggestions: Refinement suggestions if invalid.
    """

    is_valid: bool
    confidence: float
    reasoning: tuple[str, ...]
    suggestions: tuple[str, ...]

    @property
    def needs_refinement(self) -> bool:
        """True if invalid and has actionable suggestions."""
        return not self.is_valid and len(self.suggestions) > 0

    @classmethod
    def valid(
        cls,
        confidence: float = 1.0,
        reasoning: tuple[str, ...] | list[str] = (),
    ) -> AnalysisResult:
        """Create a passing result."""
        return cls(
            is_valid=True,
            confidence=confidence,
            reasoning=tuple(reasoning) if isinstance(reasoning, list) else reasoning,
            suggestions=(),
        )

    @classmethod
    def invalid(
        cls,
        reasoning: tuple[str, ...] | list[str],
        suggestions: tuple[str, ...] | list[str] = (),
        confidence: float = 0.8,
    ) -> AnalysisResult:
        """Create a failing result."""
        return cls(
            is_valid=False,
            confidence=confidence,
            reasoning=tuple(reasoning) if isinstance(reasoning, list) else reasoning,
            suggestions=tuple(suggestions) if isinstance(suggestions, list) else suggestions,
        )


class OntologicalViolationError(OuroborosError):
    """Error raised when ontological analysis blocks execution.

    This error contains the analysis result explaining why
    the operation was halted.

    Attributes:
        result: The AnalysisResult that caused the violation.
        join_point: Which phase triggered the violation.
    """

    def __init__(
        self,
        result: AnalysisResult,
        *,
        join_point: OntologicalJoinPoint | None = None,
    ) -> None:
        """Initialize violation error.

        Args:
            result: The analysis result that caused the violation.
            join_point: Which phase triggered this error.
        """
        self.result = result
        self.join_point = join_point
        super().__init__(
            message="Ontological violation: analysis blocked execution",
            details={
                "is_valid": result.is_valid,
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "suggestions": result.suggestions,
                "join_point": join_point.value if join_point else None,
            },
        )


# =============================================================================
# Strategy Protocol
# =============================================================================

C = TypeVar("C", contravariant=True)  # Context type (input)


class OntologyStrategy(Protocol[C]):
    """Protocol for join-point-specific ontological analysis.

    Each Strategy implements phase-specific logic:
    - InterviewOntologyStrategy: Checks if user asks about root problem
    - DevilAdvocateStrategy: Validates solution addresses root cause
    - ContrarianStrategy: Challenges assumptions when stuck

    Key Design Decision:
        Strategy provides get_cache_key(), not Aspect.
        This allows fine-grained control over what matters for caching.
    """

    @property
    def join_point(self) -> OntologicalJoinPoint:
        """Which phase this strategy is for."""
        ...

    def get_cache_key(self, context: C) -> str:
        """Return cache key for this context.

        Strategy decides which parts of context are relevant for caching.
        Example: Consensus only cares about artifact hash, not full state.

        Args:
            context: The analysis context.

        Returns:
            A string cache key.
        """
        ...

    async def analyze(self, context: C) -> AnalysisResult:
        """Perform ontological analysis on the given context.

        Args:
            context: Phase-specific context.

        Returns:
            AnalysisResult with validity, confidence, and reasoning.
        """
        ...


# =============================================================================
# Ontological Aspect (Weaver)
# =============================================================================

T = TypeVar("T")  # Result type (output)
E = TypeVar("E", bound=OuroborosError)  # Error type


@dataclass
class OntologicalAspect(Generic[C, T, E]):
    """Central AOP Weaver for Ontological Analysis.

    Implements the "Around Advice" pattern:
    1. Pre-execution: Run ontological analysis
    2. Decision: Proceed or halt based on analysis
    3. Execution: Run core operation if valid
    4. Post-execution: Emit events

    Type Parameters:
        C: Context type passed to strategy
        T: Success type from core operation
        E: Error type from core operation (must extend OuroborosError)

    Configuration:
        halt_on_violation: Return error on ontological failure (default: True)
        strict_mode: Fail closed on LLM errors (default: True)
        cache_ttl: Cache TTL in seconds (default: 300)
        cache_maxsize: Max cached entries (default: 100)

    Example:
        aspect = OntologicalAspect(
            strategy=DevilAdvocateStrategy(llm),
            event_emitter=event_store.emit,
        )

        result = await aspect.execute(
            context=consensus_context,
            core_operation=lambda ctx: consensus.deliberate(ctx),
        )

        if result.is_err:
            if isinstance(result.error, OntologicalViolationError):
                # Handle ontological violation
                print(result.error.result.suggestions)
    """

    strategy: OntologyStrategy[C]
    event_emitter: Callable[[BaseEvent], Awaitable[None]] | None = None
    halt_on_violation: bool = True
    strict_mode: bool = True  # fail_closed by default
    cache_ttl: int = 300  # 5 minutes
    cache_maxsize: int = 100
    _cache: TTLCache[str, AnalysisResult] = field(
        default_factory=lambda: TTLCache(maxsize=100, ttl=300),
        repr=False,
    )

    def __post_init__(self) -> None:
        """Initialize cache with configured TTL and maxsize."""
        # Recreate cache with actual config values if they differ from defaults
        if self.cache_ttl != 300 or self.cache_maxsize != 100:
            object.__setattr__(
                self,
                "_cache",
                TTLCache(maxsize=self.cache_maxsize, ttl=self.cache_ttl),
            )

    async def execute(
        self,
        context: C,
        core_operation: Callable[[C], Awaitable[Result[T, E]]],
        *,
        skip_analysis: bool = False,
    ) -> Result[T, OntologicalViolationError | E]:
        """Execute with ontological analysis (Around Advice).

        Args:
            context: Phase-specific context passed to strategy.
            core_operation: The operation returning Result[T, E].
            skip_analysis: Skip ontological check (for known-safe hot paths).

        Returns:
            Result with union error type:
            - Ok(T) if analysis passes and operation succeeds
            - Err(OntologicalViolationError) if analysis fails and halt_on_violation
            - Err(E) if core operation fails
        """
        # Escape hatch for hot paths
        if skip_analysis:
            return await core_operation(context)

        # Get cache key from Strategy (not self-computed)
        cache_key = self.strategy.get_cache_key(context)

        # Check cache
        if cache_key in self._cache:
            analysis = self._cache[cache_key]
            log.debug(
                "ontology.analysis.cache_hit",
                extra={
                    "join_point": self.strategy.join_point,
                    "cache_key": cache_key[:16],
                },
            )
        else:
            try:
                analysis = await self.strategy.analyze(context)
                self._cache[cache_key] = analysis
                log.debug(
                    "ontology.analysis.completed",
                    extra={
                        "join_point": self.strategy.join_point,
                        "is_valid": analysis.is_valid,
                        "confidence": analysis.confidence,
                    },
                )
            except Exception as e:
                # LLM provider failure
                if self.strict_mode:
                    # fail_closed: propagate error
                    log.error(
                        "ontology.analysis.failed_closed",
                        extra={
                            "join_point": self.strategy.join_point,
                            "error": str(e),
                        },
                    )
                    raise
                else:
                    # fail_open: log warning, proceed without analysis
                    log.warning(
                        "ontology.analysis.failed_open",
                        extra={
                            "join_point": self.strategy.join_point,
                            "error": str(e),
                        },
                    )
                    return await core_operation(context)

        # Handle violation
        if not analysis.is_valid:
            await self._emit_violation_event(analysis)

            if self.halt_on_violation:
                return Result.err(
                    OntologicalViolationError(
                        analysis,
                        join_point=self.strategy.join_point,
                    )
                )
            # else: log and continue (non-halting mode)
            log.warning(
                "ontology.analysis.violation_ignored",
                extra={
                    "join_point": self.strategy.join_point,
                    "reasoning": analysis.reasoning,
                },
            )

        # Handle valid
        if analysis.is_valid:
            await self._emit_passed_event(analysis)

        # Execute core operation (returns Result[T, E])
        return await core_operation(context)

    async def _emit_violation_event(self, analysis: AnalysisResult) -> None:
        """Emit ontological violation event."""
        if self.event_emitter:
            from ouroboros.events.ontology import OntologicalViolationEvent

            event = OntologicalViolationEvent(
                join_point=self.strategy.join_point,
                confidence=analysis.confidence,
                reasoning=analysis.reasoning,
                suggestions=analysis.suggestions,
            )
            await self.event_emitter(event)

    async def _emit_passed_event(self, analysis: AnalysisResult) -> None:
        """Emit ontological passed event."""
        if self.event_emitter:
            from ouroboros.events.ontology import OntologicalPassedEvent

            event = OntologicalPassedEvent(
                join_point=self.strategy.join_point,
                confidence=analysis.confidence,
            )
            await self.event_emitter(event)


# =============================================================================
# Factory Function
# =============================================================================


def create_ontology_aspect(
    strategy: OntologyStrategy[Any],
    event_emitter: Callable[[BaseEvent], Awaitable[None]] | None = None,
    *,
    halt_on_violation: bool = True,
    strict_mode: bool = True,
    cache_ttl: int = 300,
    cache_maxsize: int = 100,
) -> OntologicalAspect[Any, Any, Any]:
    """Factory to create configured ontological aspect.

    Args:
        strategy: The OntologyStrategy to use.
        event_emitter: Optional event emission callback.
        halt_on_violation: Return error on violation (default: True).
        strict_mode: Fail closed on LLM errors (default: True).
        cache_ttl: Cache TTL in seconds (default: 300).
        cache_maxsize: Max cached entries (default: 100).

    Returns:
        Configured OntologicalAspect instance.

    Example:
        aspect = create_ontology_aspect(
            strategy=DevilAdvocateStrategy(llm_adapter),
            event_emitter=event_store.emit,
        )
    """
    return OntologicalAspect(
        strategy=strategy,
        event_emitter=event_emitter,
        halt_on_violation=halt_on_violation,
        strict_mode=strict_mode,
        cache_ttl=cache_ttl,
        cache_maxsize=cache_maxsize,
    )


__all__ = [
    # Types
    "OntologicalJoinPoint",
    "AnalysisResult",
    "OntologicalViolationError",
    # Protocol
    "OntologyStrategy",
    # Weaver
    "OntologicalAspect",
    # Factory
    "create_ontology_aspect",
]
