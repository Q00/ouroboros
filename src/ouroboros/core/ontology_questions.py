"""Shared Ontological Question Framework.

This module defines the core philosophical questions used across
Interview, Consensus, and Resilience phases for ontological analysis.

The Two Ancient Methods:
1. Socratic Questioning - "Why?", "What if?", "Is it necessary?"
   → Reveals hidden assumptions, exposes contradictions

2. Ontological Analysis - "What IS this?", "Root cause or symptom?"
   → Finds root problems, separates essential from accidental

This framework provides the Ontological Analysis component.

Usage:
    # For building prompts (low-level)
    prompt = build_ontological_prompt(OntologicalQuestionType.ESSENCE)

    # For full analysis with LLM (high-level, centralized)
    insight = await analyze_ontologically(llm_adapter, context, (ROOT_CAUSE, ESSENCE))
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ouroboros.core.errors import ProviderError, ValidationError
    from ouroboros.core.types import Result
    from ouroboros.providers.base import LLMAdapter


class OntologicalQuestionType(StrEnum):
    """Types of ontological questions.

    Each type probes a different aspect of the fundamental nature
    of a problem or solution.
    """

    ESSENCE = "essence"
    ROOT_CAUSE = "root_cause"
    PREREQUISITES = "prerequisites"
    HIDDEN_ASSUMPTIONS = "hidden_assumptions"
    EXISTING_CONTEXT = "existing_context"


@dataclass(frozen=True, slots=True)
class OntologicalQuestion:
    """A single ontological question with metadata.

    Attributes:
        type: The category of ontological question.
        question: The core question to ask.
        purpose: What this question aims to reveal.
        follow_up: A probing follow-up consideration.
    """

    type: OntologicalQuestionType
    question: str
    purpose: str
    follow_up: str


ONTOLOGICAL_QUESTIONS: dict[OntologicalQuestionType, OntologicalQuestion] = {
    OntologicalQuestionType.ESSENCE: OntologicalQuestion(
        type=OntologicalQuestionType.ESSENCE,
        question="What IS this, really?",
        purpose="Identify the true nature of the problem/solution",
        follow_up="Strip away accidental properties - what remains?",
    ),
    OntologicalQuestionType.ROOT_CAUSE: OntologicalQuestion(
        type=OntologicalQuestionType.ROOT_CAUSE,
        question="Is this the root cause or a symptom?",
        purpose="Distinguish fundamental issues from surface manifestations",
        follow_up="If we solve this, does the underlying issue remain?",
    ),
    OntologicalQuestionType.PREREQUISITES: OntologicalQuestion(
        type=OntologicalQuestionType.PREREQUISITES,
        question="What must exist first?",
        purpose="Identify hidden dependencies and foundations",
        follow_up="What assumptions are we making about existing structures?",
    ),
    OntologicalQuestionType.HIDDEN_ASSUMPTIONS: OntologicalQuestion(
        type=OntologicalQuestionType.HIDDEN_ASSUMPTIONS,
        question="What are we assuming?",
        purpose="Surface implicit beliefs that may be wrong",
        follow_up="What if the opposite were true?",
    ),
    OntologicalQuestionType.EXISTING_CONTEXT: OntologicalQuestion(
        type=OntologicalQuestionType.EXISTING_CONTEXT,
        question="What already exists?",
        purpose="Discover existing code, patterns, and constraints that must be respected",
        follow_up="What would break if we ignore what's already built?",
    ),
}


@dataclass(frozen=True, slots=True)
class OntologicalInsight:
    """Result of ontological analysis.

    Attributes:
        essence: The identified essential nature of the subject.
        is_root_problem: Whether this addresses a root cause.
        prerequisites: Things that must exist first.
        hidden_assumptions: Implicit beliefs discovered.
        confidence: Confidence in the analysis (0.0-1.0).
        reasoning: The reasoning process that led to these insights.
    """

    essence: str
    is_root_problem: bool
    prerequisites: tuple[str, ...]
    hidden_assumptions: tuple[str, ...]
    confidence: float
    reasoning: str
    existing_context: tuple[str, ...] = ()


class OntologicalAnalyzer(Protocol):
    """Protocol for components that perform ontological analysis.

    This protocol is implemented by:
    - InterviewOntologyAnalyzer (bigbang/ontology.py)
    - Devil's Advocate in Consensus (evaluation/consensus.py)
    - CONTRARIAN persona in Lateral Thinking (resilience/lateral.py) [future]
    """

    async def analyze_essence(self, subject: str) -> str:
        """Identify the essential nature of a subject.

        Args:
            subject: The problem or solution to analyze.

        Returns:
            A description of the essential nature.
        """
        ...

    async def check_root_cause(
        self,
        problem: str,
        proposed_solution: str,
    ) -> tuple[bool, str]:
        """Check if a solution addresses the root cause.

        Args:
            problem: The problem being solved.
            proposed_solution: The proposed solution.

        Returns:
            Tuple of (is_root_cause, reasoning).
        """
        ...

    async def identify_prerequisites(self, goal: str) -> list[str]:
        """Identify what must exist before pursuing a goal.

        Args:
            goal: The goal to analyze.

        Returns:
            List of prerequisites.
        """
        ...

    async def surface_assumptions(self, context: str) -> list[str]:
        """Surface hidden assumptions in a context.

        Args:
            context: The context to analyze.

        Returns:
            List of hidden assumptions discovered.
        """
        ...


def build_ontological_prompt(question_type: OntologicalQuestionType) -> str:
    """Build a prompt fragment for ontological questioning.

    Args:
        question_type: The type of ontological question.

    Returns:
        A formatted prompt string for LLM use.
    """
    q = ONTOLOGICAL_QUESTIONS[question_type]
    return f"""Apply ontological analysis:
- Core Question: {q.question}
- Purpose: {q.purpose}
- Follow-up consideration: {q.follow_up}
"""


def build_devil_advocate_prompt() -> str:
    """Build the Devil's Advocate prompt using all ontological questions.

    This prompt is used in the Deliberative Consensus phase to ensure
    solutions address root problems rather than symptoms.

    Returns:
        A formatted prompt string for the Devil's Advocate role.
    """
    questions = "\n".join(f"- {q.question} ({q.purpose})" for q in ONTOLOGICAL_QUESTIONS.values())
    return f"""You are the DEVIL'S ADVOCATE. Your role is to critically examine
this solution using ONTOLOGICAL ANALYSIS.

Apply these fundamental questions:
{questions}

Your goal is NOT to reject everything, but to ensure we're solving
the ROOT problem, not just treating SYMPTOMS.

Guidelines:
- If you find fundamental issues, explain WHY this is symptom treatment
- If the solution is sound, acknowledge its validity with clear reasoning
- Focus on the ESSENCE of the problem - is it being addressed?
- Challenge hidden ASSUMPTIONS respectfully but firmly
- Consider what PREREQUISITES might be missing

Be rigorous but fair. A good solution deserves recognition.
A symptomatic treatment deserves honest critique.
"""


def get_all_questions() -> list[OntologicalQuestion]:
    """Get all ontological questions as a list.

    Returns:
        List of all OntologicalQuestion instances.
    """
    return list(ONTOLOGICAL_QUESTIONS.values())


def get_question(question_type: OntologicalQuestionType) -> OntologicalQuestion:
    """Get a specific ontological question by type.

    Args:
        question_type: The type of question to retrieve.

    Returns:
        The corresponding OntologicalQuestion.
    """
    return ONTOLOGICAL_QUESTIONS[question_type]


# ============================================================================
# Centralized Ontological Analysis
# ============================================================================
#
# This is the SINGLE PLACE where philosophical interpretation happens.
# All phases (Interview, Consensus, Resilience) should use this function
# to ensure consistent ontological analysis across the system.
#
# Philosophical Interpretation Criteria:
# - ROOT solution indicators: "fundamental", "core nature", "essential"
# - SYMPTOM treatment indicators: "surface", "temporary", "workaround"
# ============================================================================


def _get_ontology_analysis_system_prompt() -> str:
    """Lazy-load ontology analysis system prompt to avoid import-time I/O."""
    from ouroboros.agents.loader import load_agent_prompt

    return load_agent_prompt("ontology-analyst")


def _build_analysis_prompt(
    context: str,
    question_types: tuple[OntologicalQuestionType, ...],
) -> str:
    """Build the user prompt for ontological analysis.

    Args:
        context: The subject to analyze
        question_types: Which questions to emphasize (all if empty)

    Returns:
        Formatted prompt string
    """
    if question_types:
        questions_text = "\n".join(
            f"- {ONTOLOGICAL_QUESTIONS[qt].question}: {ONTOLOGICAL_QUESTIONS[qt].purpose}"
            for qt in question_types
        )
        focus = f"\n\nFocus especially on:\n{questions_text}"
    else:
        questions_text = "\n".join(
            f"- {q.question}: {q.purpose}" for q in ONTOLOGICAL_QUESTIONS.values()
        )
        focus = f"\n\nApply all ontological questions:\n{questions_text}"

    return f"""Analyze the following using ontological inquiry:

## Subject
{context}
{focus}

Respond with JSON containing: essence, is_root_problem, prerequisites, hidden_assumptions, existing_context, confidence, reasoning."""


def _parse_insight_response(response_text: str) -> OntologicalInsight | None:
    """Parse LLM response into OntologicalInsight.

    Args:
        response_text: Raw LLM response

    Returns:
        OntologicalInsight or None if parsing fails
    """
    # Extract JSON using index-based approach
    start = response_text.find("{")
    end = response_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        data = json.loads(response_text[start : end + 1])
    except json.JSONDecodeError:
        return None

    # Extract and validate fields with defaults
    try:
        prereqs_raw = data.get("prerequisites", [])
        prerequisites = tuple(str(p) for p in prereqs_raw) if isinstance(prereqs_raw, list) else ()

        assumptions_raw = data.get("hidden_assumptions", [])
        hidden_assumptions = (
            tuple(str(a) for a in assumptions_raw) if isinstance(assumptions_raw, list) else ()
        )

        existing_raw = data.get("existing_context", [])
        existing_context = (
            tuple(str(e) for e in existing_raw) if isinstance(existing_raw, list) else ()
        )

        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))

        return OntologicalInsight(
            essence=str(data.get("essence", "Unknown")),
            is_root_problem=bool(data.get("is_root_problem", False)),
            prerequisites=prerequisites,
            hidden_assumptions=hidden_assumptions,
            existing_context=existing_context,
            confidence=confidence,
            reasoning=str(data.get("reasoning", "No reasoning provided")),
        )
    except (TypeError, ValueError):
        return None


async def analyze_ontologically(
    llm_adapter: LLMAdapter,
    context: str,
    question_types: tuple[OntologicalQuestionType, ...] = (),
    model: str = "openrouter/google/gemini-2.0-flash-001",
    temperature: float = 0.3,
    max_tokens: int = 2048,
) -> Result[OntologicalInsight, ProviderError | ValidationError]:
    """Central ontological analysis function.

    This is the SINGLE SOURCE OF TRUTH for ontological analysis.
    All phases (Interview, Consensus, Resilience) should use this function
    to ensure consistent philosophical interpretation across the system.

    The function:
    1. Builds a standardized ontological prompt
    2. Calls the LLM for analysis
    3. Parses the response using centralized criteria
    4. Returns a structured OntologicalInsight

    Args:
        llm_adapter: LLM adapter for analysis
        context: What to analyze (problem, solution, or situation)
        question_types: Which questions to emphasize (empty = all)
        model: Model to use for analysis
        temperature: Sampling temperature (lower = more deterministic)
        max_tokens: Maximum tokens for LLM response

    Returns:
        Result containing OntologicalInsight or error

    Example:
        # For consensus (Devil's Advocate)
        insight = await analyze_ontologically(
            llm, artifact,
            (OntologicalQuestionType.ROOT_CAUSE, OntologicalQuestionType.ESSENCE)
        )
        if insight.is_ok and not insight.value.is_root_problem:
            # Solution treats symptoms, not root cause

        # For interview (surface assumptions)
        insight = await analyze_ontologically(
            llm, user_context,
            (OntologicalQuestionType.HIDDEN_ASSUMPTIONS,)
        )

        # For resilience (CONTRARIAN - challenge everything)
        insight = await analyze_ontologically(llm, stuck_context)  # All questions
    """
    # Import here to avoid circular dependency
    from ouroboros.core.errors import ValidationError
    from ouroboros.core.types import Result
    from ouroboros.providers.base import CompletionConfig, Message, MessageRole

    messages = [
        Message(role=MessageRole.SYSTEM, content=_get_ontology_analysis_system_prompt()),
        Message(
            role=MessageRole.USER,
            content=_build_analysis_prompt(context, question_types),
        ),
    ]

    config = CompletionConfig(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    llm_result = await llm_adapter.complete(messages, config)
    if llm_result.is_err:
        return Result.err(llm_result.error)

    insight = _parse_insight_response(llm_result.value.content)
    if insight is None:
        return Result.err(
            ValidationError(
                "Failed to parse ontological analysis response",
                field="response",
                value=llm_result.value.content[:200],
            )
        )

    return Result.ok(insight)


__all__ = [
    # Types and Constants
    "OntologicalQuestionType",
    "OntologicalQuestion",
    "ONTOLOGICAL_QUESTIONS",
    "OntologicalInsight",
    "OntologicalAnalyzer",
    # Prompt Builders (low-level)
    "build_ontological_prompt",
    "build_devil_advocate_prompt",
    # Question Accessors
    "get_all_questions",
    "get_question",
    # Centralized Analysis (high-level)
    "analyze_ontologically",
]
