"""Devil's Advocate Strategy for Consensus Phase.

This strategy implements ontological analysis for the Consensus phase (Phase 4).
The Devil's Advocate critically examines whether a solution addresses the
ROOT CAUSE or merely treats SYMPTOMS.

Usage:
    strategy = DevilAdvocateStrategy(llm_adapter)
    aspect = OntologicalAspect(strategy=strategy)

    result = await aspect.execute(
        context=ConsensusContext(artifact=..., goal=...),
        core_operation=lambda ctx: consensus.deliberate(ctx),
    )

Reference: docs/ontological-framework/aop-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING

from ouroboros.core.ontology_aspect import (
    AnalysisResult,
    OntologicalJoinPoint,
)
from ouroboros.core.ontology_questions import (
    OntologicalQuestionType,
    analyze_ontologically,
)

if TYPE_CHECKING:
    from ouroboros.providers.base import LLMAdapter


@dataclass(frozen=True, slots=True)
class ConsensusContext:
    """Context for consensus phase ontological analysis.

    Attributes:
        artifact: The artifact/solution being evaluated.
        goal: The original goal or problem statement.
        current_ac: Current acceptance criteria.
        constraints: Any constraints on the solution.
    """

    artifact: str
    goal: str
    current_ac: str = ""
    constraints: tuple[str, ...] = ()


@dataclass
class DevilAdvocateStrategy:
    """Strategy for Consensus phase (Phase 4).

    The Devil's Advocate role: Critically examine whether
    the solution addresses the ROOT CAUSE or just symptoms.

    This strategy uses the centralized analyze_ontologically() function
    from ontology_questions.py, focusing on ROOT_CAUSE and ESSENCE questions.

    Attributes:
        llm_adapter: LLM adapter for analysis.
        model: Model to use (default: gemini-2.0-flash).
        confidence_threshold: Minimum confidence to pass (default: 0.7).
        temperature: Sampling temperature for LLM (default: 0.3).
        max_tokens: Maximum tokens for LLM response (default: 2048).
    """

    llm_adapter: LLMAdapter
    model: str = "claude-opus-4-6"
    confidence_threshold: float = 0.7
    temperature: float = 0.3
    max_tokens: int = 2048

    @property
    def join_point(self) -> OntologicalJoinPoint:
        """This strategy is for the Consensus phase."""
        return OntologicalJoinPoint.CONSENSUS

    def get_cache_key(self, context: ConsensusContext) -> str:
        """Compute cache key from artifact and goal hash.

        Only artifact content and goal matter for caching -
        the same solution for the same problem should get the same result.

        Args:
            context: The consensus context.

        Returns:
            SHA256 hash of artifact + goal (first 16 chars).
        """
        cache_data = {
            "artifact": context.artifact,
            "goal": context.goal,
        }
        return hashlib.sha256(json.dumps(cache_data, sort_keys=True).encode()).hexdigest()[:16]

    async def analyze(self, context: ConsensusContext) -> AnalysisResult:
        """Analyze solution using Devil's Advocate lens.

        Applies ontological analysis focusing on:
        - Is this solving the root cause or just a symptom?
        - What is the essential nature of the problem?

        Args:
            context: The consensus context with artifact and goal.

        Returns:
            AnalysisResult with validity based on is_root_problem.
        """
        # Build analysis context string
        analysis_context = self._build_analysis_context(context)

        # Use centralized ontological analysis
        insight_result = await analyze_ontologically(
            llm_adapter=self.llm_adapter,
            context=analysis_context,
            question_types=(
                OntologicalQuestionType.ROOT_CAUSE,
                OntologicalQuestionType.ESSENCE,
            ),
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        # Handle LLM failure
        if insight_result.is_err:
            # Return invalid with error info - let Aspect handle retry/fail
            return AnalysisResult.invalid(
                reasoning=[f"Analysis failed: {insight_result.error.message}"],
                suggestions=["Retry the analysis", "Check LLM provider status"],
                confidence=0.0,
            )

        insight = insight_result.value

        # Convert OntologicalInsight to AnalysisResult
        if insight.is_root_problem and insight.confidence >= self.confidence_threshold:
            return AnalysisResult.valid(
                confidence=insight.confidence,
                reasoning=[insight.reasoning],
            )
        else:
            suggestions = list(insight.hidden_assumptions)
            if not insight.is_root_problem:
                suggestions.insert(0, "This appears to treat symptoms, not root cause")
            if insight.confidence < self.confidence_threshold:
                suggestions.append(
                    f"Low confidence ({insight.confidence:.2f}). Consider more analysis."
                )

            return AnalysisResult.invalid(
                reasoning=[insight.reasoning],
                suggestions=tuple(suggestions),
                confidence=insight.confidence,
            )

    def _build_analysis_context(self, context: ConsensusContext) -> str:
        """Build the context string for ontological analysis.

        Args:
            context: The consensus context.

        Returns:
            Formatted context string for analysis.
        """
        parts = [
            "## Goal/Problem",
            context.goal,
            "",
            "## Proposed Solution/Artifact",
            context.artifact,
        ]

        if context.current_ac:
            parts.extend(["", "## Acceptance Criteria", context.current_ac])

        if context.constraints:
            parts.extend(["", "## Constraints", *[f"- {c}" for c in context.constraints]])

        return "\n".join(parts)


__all__ = [
    "ConsensusContext",
    "DevilAdvocateStrategy",
]
