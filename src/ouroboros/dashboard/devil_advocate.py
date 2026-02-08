"""Enhanced Devil's Advocate with Gemini 3 1M Context.

This module extends the Devil's Advocate strategy to leverage Gemini 3's
1M token context window for deeper ontological analysis using full
iteration history.

Key Features:
1. Full iteration history context (up to 1M tokens)
2. Pattern-aware challenging using detected patterns
3. Dependency-aware root cause analysis
4. Progressive deepening of Socratic questions
5. Cross-AC insight generation

Design Philosophy:
- Socratic method: Progressive questioning to find root causes
- Ontological analysis: Distinguish essence from symptoms
- HOTL convergence: Use history to accelerate insight
- Devils Advocate: Challenge assumptions, question foundations

Usage:
    from ouroboros.dashboard.devil_advocate import EnhancedDevilAdvocate

    devil = EnhancedDevilAdvocate(llm_adapter)

    # Analyze with full history context
    challenge = await devil.challenge(
        artifact=solution,
        goal=goal,
        iteration_history=accelerator.build_context_string(),
        patterns=detected_patterns,
    )
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

# Python 3.11+ has StrEnum, for earlier versions use string mixin
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    class StrEnum(str, Enum):
        """String enum for Python < 3.11 compatibility."""
        pass

# Python 3.10+ supports slots=True in dataclass
DATACLASS_SLOTS = {"slots": True} if sys.version_info >= (3, 10) else {}

if TYPE_CHECKING:
    from ouroboros.dashboard.pattern_analyzer import FailurePattern
    from ouroboros.providers.base import LLMAdapter

try:
    from ouroboros.observability.logging import get_logger
    log = get_logger(__name__)
except ImportError:
    import logging
    log = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Maximum context for enhanced analysis
MAX_ENHANCED_CONTEXT_TOKENS = 500_000  # Use up to 500K for analysis

# Temperature for deep analysis
DEEP_ANALYSIS_TEMPERATURE = 0.2

# Gemini 3 model for enhanced analysis
GEMINI_3_MODEL = "gemini-2.5-pro-preview-05-06"

# Challenge depth levels
MAX_CHALLENGE_DEPTH = 5


# =============================================================================
# Enums and Data Models
# =============================================================================


class ChallengeType(StrEnum):
    """Types of Devil's Advocate challenges.

    Attributes:
        ROOT_CAUSE: Is this addressing root cause or symptom?
        ASSUMPTION: What hidden assumptions are being made?
        ALTERNATIVE: Is there a fundamentally different approach?
        SCOPE: Is the problem properly scoped?
        DEPENDENCY: Are dependencies correctly identified?
        REGRESSION: Will this fix cause other issues?
        COMPLETENESS: Is the solution complete?
    """

    ROOT_CAUSE = "root_cause"
    ASSUMPTION = "assumption"
    ALTERNATIVE = "alternative"
    SCOPE = "scope"
    DEPENDENCY = "dependency"
    REGRESSION = "regression"
    COMPLETENESS = "completeness"


class ChallengeIntensity(StrEnum):
    """Intensity of the challenge.

    Attributes:
        GENTLE: Exploratory questions
        MODERATE: Probing questions
        INTENSE: Fundamental challenges
        CRITICAL: Existential challenges to approach
    """

    GENTLE = "gentle"
    MODERATE = "moderate"
    INTENSE = "intense"
    CRITICAL = "critical"


@dataclass(frozen=True, **DATACLASS_SLOTS)
class DeepChallenge:
    """A deep challenge from the Enhanced Devil's Advocate.

    Attributes:
        challenge_id: Unique identifier
        challenge_type: Type of challenge
        intensity: How intense the challenge is
        question: The Socratic question being asked
        reasoning: Why this challenge is being raised
        evidence: Evidence from iteration history
        root_cause_hypothesis: Hypothesized root cause
        alternative_approaches: Suggested alternatives
        patterns_leveraged: Patterns used to generate challenge
        confidence: Confidence in the challenge (0-1)
        depth: Depth of questioning (1-5)
        follow_up_questions: Deeper questions to explore
        metadata: Additional challenge data
    """

    challenge_id: str
    challenge_type: ChallengeType
    intensity: ChallengeIntensity
    question: str
    reasoning: str = ""
    evidence: tuple[str, ...] = ()
    root_cause_hypothesis: str = ""
    alternative_approaches: tuple[str, ...] = ()
    patterns_leveraged: tuple[str, ...] = ()
    confidence: float = 0.5
    depth: int = 1
    follow_up_questions: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "challenge_id": self.challenge_id,
            "challenge_type": self.challenge_type.value,
            "intensity": self.intensity.value,
            "question": self.question,
            "reasoning": self.reasoning,
            "evidence": list(self.evidence),
            "root_cause_hypothesis": self.root_cause_hypothesis,
            "alternative_approaches": list(self.alternative_approaches),
            "patterns_leveraged": list(self.patterns_leveraged),
            "confidence": self.confidence,
            "depth": self.depth,
            "follow_up_questions": list(self.follow_up_questions),
        }


@dataclass
class ChallengeResult:
    """Result of enhanced Devil's Advocate analysis.

    Attributes:
        challenges: List of generated challenges
        is_root_solution: Whether solution addresses root cause
        overall_confidence: Confidence in analysis (0-1)
        recommendation: Final recommendation
        context_tokens_used: Tokens used from context
    """

    challenges: list[DeepChallenge] = field(default_factory=list)
    is_root_solution: bool = False
    overall_confidence: float = 0.5
    recommendation: str = ""
    context_tokens_used: int = 0

    @property
    def primary_challenge(self) -> DeepChallenge | None:
        """Get the most important challenge."""
        if not self.challenges:
            return None
        return max(self.challenges, key=lambda c: c.confidence)


# =============================================================================
# Enhanced Devil's Advocate
# =============================================================================


class EnhancedDevilAdvocate:
    """Enhanced Devil's Advocate with Gemini 3 1M Context.

    Uses full iteration history and pattern analysis to generate
    deeper, more insightful challenges that probe root causes.

    Attributes:
        llm_adapter: LLM adapter for Gemini 3
        model: Model to use (default: gemini-2.5-pro)
        max_context_tokens: Maximum context tokens to use
    """

    def __init__(
        self,
        llm_adapter: LLMAdapter | None = None,
        *,
        model: str = GEMINI_3_MODEL,
        max_context_tokens: int = MAX_ENHANCED_CONTEXT_TOKENS,
        temperature: float = DEEP_ANALYSIS_TEMPERATURE,
    ) -> None:
        """Initialize Enhanced Devil's Advocate.

        Args:
            llm_adapter: LLM adapter for completions
            model: Model to use (default: gemini-2.5-pro)
            max_context_tokens: Max context tokens (default: 500K)
            temperature: Sampling temperature (default: 0.2)
        """
        self._llm_adapter = llm_adapter
        self._model = model
        self._max_context_tokens = max_context_tokens
        self._temperature = temperature

        # Challenge history for progressive deepening
        self._previous_challenges: list[DeepChallenge] = []

    async def challenge(
        self,
        artifact: str,
        goal: str,
        iteration_history: str = "",
        patterns: list[FailurePattern] | None = None,
        current_ac: str = "",
        constraints: tuple[str, ...] = (),
        depth: int = 1,
    ) -> ChallengeResult:
        """Generate deep challenges using full context.

        Analyzes the artifact using iteration history and patterns
        to generate insightful challenges that probe root causes.

        Args:
            artifact: The solution/artifact to challenge
            goal: The original goal
            iteration_history: Full iteration history context
            patterns: Detected failure patterns
            current_ac: Current acceptance criterion
            constraints: Any constraints
            depth: Depth of questioning (1-5)

        Returns:
            ChallengeResult with generated challenges
        """
        patterns = patterns or []
        depth = min(MAX_CHALLENGE_DEPTH, max(1, depth))

        # Build comprehensive context
        context = self._build_challenge_context(
            artifact=artifact,
            goal=goal,
            iteration_history=iteration_history,
            patterns=patterns,
            current_ac=current_ac,
            constraints=constraints,
            depth=depth,
        )

        # If no LLM adapter, use heuristic analysis
        if self._llm_adapter is None:
            return self._heuristic_challenge(
                artifact=artifact,
                goal=goal,
                patterns=patterns,
                depth=depth,
            )

        # Generate challenges using Gemini 3
        challenges = await self._generate_llm_challenges(context, depth)

        # Calculate overall assessment
        is_root_solution = self._assess_root_cause(challenges)
        overall_confidence = self._calculate_confidence(challenges)

        # Generate recommendation
        recommendation = self._generate_recommendation(
            challenges=challenges,
            is_root_solution=is_root_solution,
            patterns=patterns,
        )

        # Track challenges for progressive deepening
        self._previous_challenges.extend(challenges)

        return ChallengeResult(
            challenges=challenges,
            is_root_solution=is_root_solution,
            overall_confidence=overall_confidence,
            recommendation=recommendation,
            context_tokens_used=len(context) // 4,  # Rough estimate
        )

    def get_progressive_questions(
        self,
        challenge: DeepChallenge,
        depth: int = 1,
    ) -> list[str]:
        """Generate progressively deeper questions from a challenge.

        Implements Socratic method by asking deeper "why" questions.

        Args:
            challenge: The challenge to deepen
            depth: How many levels deeper to go

        Returns:
            List of progressively deeper questions
        """
        questions = [challenge.question]

        # Base follow-up templates by type
        templates = {
            ChallengeType.ROOT_CAUSE: [
                "But why does this root cause exist?",
                "What systemic issue allows this to occur?",
                "If we fix this, what other root causes remain?",
            ],
            ChallengeType.ASSUMPTION: [
                "What if the opposite assumption were true?",
                "How did this assumption become embedded?",
                "What evidence would invalidate this assumption?",
            ],
            ChallengeType.ALTERNATIVE: [
                "Why hasn't this alternative been tried before?",
                "What would make this alternative fail?",
                "Is there a synthesis of approaches possible?",
            ],
            ChallengeType.DEPENDENCY: [
                "Can this dependency be inverted?",
                "What would a dependency-free solution look like?",
                "Is the dependency essential or accidental?",
            ],
        }

        base_questions = templates.get(challenge.challenge_type, [
            "Why is this the case?",
            "What would change if this weren't true?",
        ])

        for i in range(min(depth, len(base_questions))):
            questions.append(base_questions[i])

        return questions

    # =========================================================================
    # Context Building
    # =========================================================================

    def _build_challenge_context(
        self,
        artifact: str,
        goal: str,
        iteration_history: str,
        patterns: list[FailurePattern],
        current_ac: str,
        constraints: tuple[str, ...],
        depth: int,
    ) -> str:
        """Build comprehensive context for challenge generation."""
        parts = [
            "# Enhanced Devil's Advocate Analysis",
            "",
            "You are the Devil's Advocate in an AI quality system.",
            "Your role is to critically examine whether solutions address ROOT CAUSES or just SYMPTOMS.",
            "",
            f"Analysis Depth Level: {depth}/5",
            "- Level 1: Surface-level questioning",
            "- Level 2: Probing assumptions",
            "- Level 3: Challenging fundamentals",
            "- Level 4: Existential questioning",
            "- Level 5: First-principles analysis",
            "",
            "## Goal/Problem",
            goal,
            "",
        ]

        if current_ac:
            parts.extend([
                "## Current Acceptance Criterion",
                current_ac,
                "",
            ])

        if constraints:
            parts.extend([
                "## Constraints",
                *[f"- {c}" for c in constraints],
                "",
            ])

        parts.extend([
            "## Proposed Solution/Artifact",
            "```",
            artifact[:50000] if len(artifact) > 50000 else artifact,  # Truncate if needed
            "```",
            "",
        ])

        # Add pattern context
        if patterns:
            parts.extend([
                "## Detected Failure Patterns (Historical)",
                "",
            ])
            for p in patterns[:10]:  # Top 10 patterns
                parts.extend([
                    f"### Pattern: {p.category.value.upper()}",
                    f"Description: {p.description}",
                    f"Occurrences: {p.occurrence_count}",
                    f"Root Cause Hypothesis: {p.root_cause_hypothesis}",
                    "",
                ])

        # Add iteration history (truncated to fit context)
        if iteration_history:
            # Calculate available space
            current_tokens = len("\n".join(parts)) // 4
            available_tokens = self._max_context_tokens - current_tokens - 10000  # Reserve for response

            if available_tokens > 10000:
                history_chars = available_tokens * 4
                truncated_history = iteration_history[:history_chars]
                if len(iteration_history) > history_chars:
                    truncated_history += "\n... [earlier history truncated]"

                parts.extend([
                    "## Iteration History",
                    truncated_history,
                    "",
                ])

        # Add previous challenges for progressive deepening
        if self._previous_challenges:
            parts.extend([
                "## Previous Challenges (for progressive deepening)",
                "",
            ])
            for c in self._previous_challenges[-5:]:  # Last 5 challenges
                parts.append(f"- [{c.challenge_type.value}] {c.question}")
            parts.append("")

        # Add analysis instructions
        parts.extend([
            "## Your Task",
            "",
            "Analyze this solution and generate challenges. For each challenge:",
            "1. Identify if it treats SYMPTOMS or ROOT CAUSES",
            "2. Question hidden ASSUMPTIONS",
            "3. Consider ALTERNATIVE approaches",
            "4. Check DEPENDENCY issues",
            "5. Evaluate COMPLETENESS",
            "",
            "Use the iteration history and patterns to inform your analysis.",
            "Be specific - reference actual patterns and iterations when possible.",
            "",
            "Respond with JSON in this format:",
            "```json",
            "{",
            '  "challenges": [',
            "    {",
            '      "type": "root_cause|assumption|alternative|scope|dependency|regression|completeness",',
            '      "intensity": "gentle|moderate|intense|critical",',
            '      "question": "The Socratic question",',
            '      "reasoning": "Why this challenge matters",',
            '      "evidence": ["Evidence from history"],',
            '      "root_cause_hypothesis": "What the real root cause might be",',
            '      "alternative_approaches": ["Alternative approaches to consider"],',
            '      "confidence": 0.0-1.0',
            "    }",
            "  ],",
            '  "is_root_solution": true|false,',
            '  "overall_assessment": "Summary assessment"',
            "}",
            "```",
        ])

        return "\n".join(parts)

    # =========================================================================
    # Challenge Generation
    # =========================================================================

    async def _generate_llm_challenges(
        self,
        context: str,
        depth: int,
    ) -> list[DeepChallenge]:
        """Generate challenges using LLM."""
        from ouroboros.providers.base import Message, MessageRole, CompletionConfig

        messages = [
            Message(role=MessageRole.SYSTEM, content="You are the Devil's Advocate in a Socratic AI system."),
            Message(role=MessageRole.USER, content=context),
        ]

        config = CompletionConfig(
            model=self._model,
            temperature=self._temperature,
            max_tokens=4096,
        )

        result = await self._llm_adapter.complete(messages, config)

        if result.is_err:
            log.warning(
                "devil.llm_failed",
                extra={"error": result.error.message if hasattr(result.error, 'message') else str(result.error)},
            )
            return []

        # Parse response
        return self._parse_challenges(result.value.content, depth)

    def _parse_challenges(
        self,
        response: str,
        depth: int,
    ) -> list[DeepChallenge]:
        """Parse challenges from LLM response."""
        challenges: list[DeepChallenge] = []

        try:
            # Extract JSON from response
            json_start = response.find("{")
            json_end = response.rfind("}") + 1

            if json_start >= 0 and json_end > json_start:
                json_str = response[json_start:json_end]
                data = json.loads(json_str)

                for i, c in enumerate(data.get("challenges", [])):
                    challenge_id = hashlib.md5(
                        f"{c.get('question', '')}:{i}".encode()
                    ).hexdigest()[:12]

                    # Parse type
                    type_str = c.get("type", "root_cause").lower()
                    try:
                        challenge_type = ChallengeType(type_str)
                    except ValueError:
                        challenge_type = ChallengeType.ROOT_CAUSE

                    # Parse intensity
                    intensity_str = c.get("intensity", "moderate").lower()
                    try:
                        intensity = ChallengeIntensity(intensity_str)
                    except ValueError:
                        intensity = ChallengeIntensity.MODERATE

                    challenges.append(
                        DeepChallenge(
                            challenge_id=challenge_id,
                            challenge_type=challenge_type,
                            intensity=intensity,
                            question=c.get("question", ""),
                            reasoning=c.get("reasoning", ""),
                            evidence=tuple(c.get("evidence", [])),
                            root_cause_hypothesis=c.get("root_cause_hypothesis", ""),
                            alternative_approaches=tuple(c.get("alternative_approaches", [])),
                            confidence=float(c.get("confidence", 0.5)),
                            depth=depth,
                        )
                    )

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.warning(
                "devil.parse_failed",
                extra={"error": str(e)},
            )

        return challenges

    def _heuristic_challenge(
        self,
        artifact: str,
        goal: str,
        patterns: list[FailurePattern],
        depth: int,
    ) -> ChallengeResult:
        """Generate challenges using heuristics when LLM unavailable."""
        challenges: list[DeepChallenge] = []

        # Challenge based on patterns
        for pattern in patterns[:3]:
            challenge_id = f"heuristic_{pattern.pattern_id}"

            if pattern.category.value == "spinning":
                challenges.append(
                    DeepChallenge(
                        challenge_id=challenge_id,
                        challenge_type=ChallengeType.ROOT_CAUSE,
                        intensity=ChallengeIntensity.INTENSE,
                        question=f"The same error repeated {pattern.occurrence_count} times. Is this solution actually different from previous attempts?",
                        reasoning=f"Spinning pattern detected: {pattern.description}",
                        root_cause_hypothesis=pattern.root_cause_hypothesis,
                        confidence=0.7,
                        depth=depth,
                    )
                )

            elif pattern.category.value == "oscillation":
                challenges.append(
                    DeepChallenge(
                        challenge_id=challenge_id,
                        challenge_type=ChallengeType.ALTERNATIVE,
                        intensity=ChallengeIntensity.INTENSE,
                        question="Solutions are oscillating between two states. Is there a third approach that avoids both failure modes?",
                        reasoning=f"Oscillation pattern detected: {pattern.description}",
                        alternative_approaches=(
                            "Consider a fundamentally different architecture",
                            "Look for underlying constraint conflicts",
                        ),
                        confidence=0.75,
                        depth=depth,
                    )
                )

            elif pattern.category.value == "dependency":
                challenges.append(
                    DeepChallenge(
                        challenge_id=challenge_id,
                        challenge_type=ChallengeType.DEPENDENCY,
                        intensity=ChallengeIntensity.CRITICAL,
                        question="Dependency blocking detected. Is the current approach respecting prerequisite ordering?",
                        reasoning=f"Dependency pattern detected: {pattern.description}",
                        root_cause_hypothesis="Task execution order may need restructuring",
                        confidence=0.8,
                        depth=depth,
                    )
                )

        # Default challenge if no patterns
        if not challenges:
            challenges.append(
                DeepChallenge(
                    challenge_id="heuristic_default",
                    challenge_type=ChallengeType.ROOT_CAUSE,
                    intensity=ChallengeIntensity.MODERATE,
                    question="Is this solution addressing the fundamental problem, or just the immediate symptoms?",
                    reasoning="Standard Devil's Advocate challenge",
                    confidence=0.5,
                    depth=depth,
                )
            )

        return ChallengeResult(
            challenges=challenges,
            is_root_solution=len(patterns) == 0,
            overall_confidence=0.5,
            recommendation="Heuristic analysis - consider deeper investigation with LLM",
        )

    # =========================================================================
    # Assessment Methods
    # =========================================================================

    def _assess_root_cause(self, challenges: list[DeepChallenge]) -> bool:
        """Assess if solution addresses root cause based on challenges."""
        if not challenges:
            return True  # No challenges = probably OK

        # Count critical challenges
        critical_count = sum(
            1 for c in challenges
            if c.intensity in (ChallengeIntensity.CRITICAL, ChallengeIntensity.INTENSE)
            and c.confidence > 0.6
        )

        # Count root cause challenges
        root_cause_challenges = sum(
            1 for c in challenges
            if c.challenge_type == ChallengeType.ROOT_CAUSE
            and c.confidence > 0.7
        )

        # If few critical challenges and low root cause concerns, probably OK
        return critical_count < 2 and root_cause_challenges < 2

    def _calculate_confidence(self, challenges: list[DeepChallenge]) -> float:
        """Calculate overall confidence in analysis."""
        if not challenges:
            return 0.5

        confidences = [c.confidence for c in challenges]
        return sum(confidences) / len(confidences)

    def _generate_recommendation(
        self,
        challenges: list[DeepChallenge],
        is_root_solution: bool,
        patterns: list[FailurePattern],
    ) -> str:
        """Generate final recommendation based on analysis."""
        if is_root_solution:
            return "Solution appears to address root causes. Proceed with confidence."

        if not challenges:
            return "Unable to generate specific challenges. Manual review recommended."

        # Build recommendation from challenges
        parts = ["Solution may treat symptoms rather than root causes.\n"]

        # Add top challenges
        for c in sorted(challenges, key=lambda x: x.confidence, reverse=True)[:3]:
            parts.append(f"- {c.challenge_type.value.upper()}: {c.question}")

        # Add pattern context if relevant
        if patterns:
            parts.append(f"\nDetected {len(patterns)} historical failure patterns that inform this analysis.")

        return "\n".join(parts)


__all__ = [
    "EnhancedDevilAdvocate",
    "DeepChallenge",
    "ChallengeType",
    "ChallengeIntensity",
    "ChallengeResult",
]
