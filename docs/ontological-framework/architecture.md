# Ontological Question Framework Architecture

> Generated: 2026-01-29
> Version: v0.4.0
> Status: Design

## Overview

Ouroboros에 "본질을 묻는" 철학적 프레임워크 추가. Socratic Questioning과 함께 Ontological Analysis를 통해 요구사항의 근본적 타당성을 검증.

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                    Ontological Question Framework                    │
│                    (core/ontology_questions.py)                      │
│                                                                       │
│  ONTOLOGICAL_QUESTIONS = {                                           │
│    "essence": "What IS this, really?",                               │
│    "root_cause": "Is this the root cause or a symptom?",             │
│    "prerequisites": "What must exist first?",                        │
│    "hidden_assumptions": "What are we assuming?"                     │
│  }                                                                   │
│                                                                       │
│  OntologicalInsight (frozen dataclass)                               │
│  OntologicalAnalyzer (Protocol)                                      │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          │                 │                 │
          ▼                 ▼                 ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│   Interview     │ │   Consensus     │ │   (Future)      │
│   Ontological   │ │   Deliberative  │ │   CONTRARIAN    │
│   Analysis      │ │   (Devil)       │ │   Persona       │
├─────────────────┤ ├─────────────────┤ ├─────────────────┤
│ bigbang/        │ │ evaluation/     │ │ resilience/     │
│ ontology.py     │ │ consensus.py    │ │ lateral.py      │
└─────────────────┘ └─────────────────┘ └─────────────────┘
```

## Data Flow

### 1. Interview Phase (Phase 0)

```
User Input
    │
    ▼
┌─────────────────────────────────────────────────────┐
│                  InterviewEngine                     │
│                                                      │
│  Round 1: Socratic Question                         │
│  Round 2: Socratic Question                         │
│  Round 3: Ontological Question  ← 번갈아 사용        │
│  Round 4: Socratic Question                         │
│  Round 5: Ontological Question                      │
│  ...                                                │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│               AmbiguityScorer (Extended)             │
│                                                      │
│  Goal Clarity:        35%  (was 40%)                │
│  Constraint Clarity:  25%  (was 30%)                │
│  Success Criteria:    25%  (was 30%)                │
│  Ontology Clarity:    15%  ← NEW                    │
│                                                      │
│  Total = 100%                                       │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
                   Seed Generation
```

### 2. Consensus Phase (Phase 4)

```
Artifact (Code/Output)
    │
    ▼
┌─────────────────────────────────────────────────────┐
│           DeliberativeConsensus                      │
│                                                      │
│  ┌─────────────────────────────────────────────┐   │
│  │              ROUND 1: 입장 제시               │   │
│  │                                              │   │
│  │  ADVOCATE (Model 1)                         │   │
│  │  "이 해결책의 강점은... 승인 권장"            │   │
│  │                                              │   │
│  │  DEVIL'S ADVOCATE (Model 2)                 │   │
│  │  "하지만 이건 증상 치료일 뿐..."             │   │
│  │  (Ontological Questions 적용)               │   │
│  └─────────────────────────────────────────────┘   │
│                       │                             │
│                       ▼                             │
│  ┌─────────────────────────────────────────────┐   │
│  │              ROUND 2: 판결                   │   │
│  │                                              │   │
│  │  JUDGE (Model 3)                            │   │
│  │  "양측 의견을 검토한 결과..."                 │   │
│  │  Final Verdict: APPROVED / REJECTED         │   │
│  │  Conditions: [if any]                       │   │
│  └─────────────────────────────────────────────┘   │
│                                                      │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
            ConsensusResult (with reasoning)
```

## Components

### A. core/ontology_questions.py (New)

```python
"""Shared Ontological Question Framework.

This module defines the core philosophical questions used across
Interview, Consensus, and Resilience phases.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

class OntologicalQuestionType(StrEnum):
    """Types of ontological questions."""
    ESSENCE = "essence"
    ROOT_CAUSE = "root_cause"
    PREREQUISITES = "prerequisites"
    HIDDEN_ASSUMPTIONS = "hidden_assumptions"

@dataclass(frozen=True, slots=True)
class OntologicalQuestion:
    """A single ontological question with metadata."""
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
}

@dataclass(frozen=True, slots=True)
class OntologicalInsight:
    """Result of ontological analysis."""
    essence: str
    is_root_problem: bool
    prerequisites: tuple[str, ...]
    hidden_assumptions: tuple[str, ...]
    confidence: float  # 0.0 - 1.0
    reasoning: str

class OntologicalAnalyzer(Protocol):
    """Protocol for components that perform ontological analysis."""

    async def analyze_essence(self, subject: str) -> str:
        """Identify the essential nature of a subject."""
        ...

    async def check_root_cause(
        self, problem: str, proposed_solution: str
    ) -> tuple[bool, str]:
        """Check if solution addresses root cause. Returns (is_root, reasoning)."""
        ...

    async def identify_prerequisites(self, goal: str) -> list[str]:
        """Identify what must exist before pursuing a goal."""
        ...

    async def surface_assumptions(self, context: str) -> list[str]:
        """Surface hidden assumptions in a context."""
        ...


def build_ontological_prompt(question_type: OntologicalQuestionType) -> str:
    """Build a prompt fragment for ontological questioning."""
    q = ONTOLOGICAL_QUESTIONS[question_type]
    return f"""
Apply ontological analysis:
- Question: {q.question}
- Purpose: {q.purpose}
- Follow-up: {q.follow_up}
"""


def build_devil_advocate_prompt() -> str:
    """Build the Devil's Advocate prompt using ontological questions."""
    questions = "\n".join(
        f"- {q.question} ({q.purpose})"
        for q in ONTOLOGICAL_QUESTIONS.values()
    )
    return f"""You are the Devil's Advocate. Your role is to critically examine
this solution using ontological analysis.

Apply these questions:
{questions}

Your goal is NOT to reject everything, but to ensure we're solving
the ROOT problem, not just treating SYMPTOMS.

If you find fundamental issues, explain why this is a symptom treatment.
If the solution is sound, acknowledge its validity with reasoning.
"""
```

### B. bigbang/ontology.py (New)

```python
"""Ontological Analysis for Interview Phase.

Complements Socratic Questioning with questions about the fundamental
nature of problems and solutions.
"""

from dataclasses import dataclass

from ouroboros.core.ontology_questions import (
    ONTOLOGICAL_QUESTIONS,
    OntologicalInsight,
    OntologicalQuestionType,
    build_ontological_prompt,
)
from ouroboros.core.types import Result
from ouroboros.providers.base import LLMAdapter


@dataclass
class InterviewOntologyAnalyzer:
    """Ontological analyzer for interview phase."""

    llm_adapter: LLMAdapter
    model: str = "openrouter/google/gemini-2.0-flash-001"

    def should_ask_ontological_question(self, round_number: int) -> bool:
        """Determine if this round should use ontological questioning.

        Pattern: Every 3rd round starting from round 3.
        Round 1: Socratic
        Round 2: Socratic
        Round 3: Ontological  ←
        Round 4: Socratic
        Round 5: Socratic
        Round 6: Ontological  ←
        """
        return round_number >= 3 and round_number % 3 == 0

    def select_question_type(
        self, round_number: int, context: str
    ) -> OntologicalQuestionType:
        """Select which ontological question to ask based on context."""
        # Simple rotation for now
        types = list(OntologicalQuestionType)
        index = (round_number // 3) % len(types)
        return types[index]

    def build_ontological_system_prompt(
        self,
        round_number: int,
        initial_context: str,
        question_type: OntologicalQuestionType,
    ) -> str:
        """Build system prompt for ontological questioning."""
        q = ONTOLOGICAL_QUESTIONS[question_type]

        return f"""You are an expert ontological analyst examining requirements
for fundamental clarity.

This is Round {round_number}. Your goal is to probe the ESSENTIAL NATURE
of what's being requested.

Initial context: {initial_context}

Your task: Ask ONE question that applies this ontological lens:
- Core Question: {q.question}
- Purpose: {q.purpose}
- Follow-up consideration: {q.follow_up}

Guidelines:
- Ask ONE focused ontological question
- Probe whether this is a ROOT problem or a SYMPTOM
- Challenge hidden assumptions gently
- Keep the question specific to the user's context
- Be respectful but incisive

Generate the next question to reveal the essential nature of the requirement."""
```

### C. bigbang/ambiguity.py (Modifications)

```python
# Add new constant (after line 34)
ONTOLOGY_CLARITY_WEIGHT = 0.15

# Adjust existing weights
GOAL_CLARITY_WEIGHT = 0.35      # was 0.40
CONSTRAINT_CLARITY_WEIGHT = 0.25  # was 0.30
SUCCESS_CRITERIA_CLARITY_WEIGHT = 0.25  # was 0.30

# Add to ScoreBreakdown class (after line 72)
class ScoreBreakdown(BaseModel):
    """Detailed breakdown of ambiguity score with justifications."""

    goal_clarity: ComponentScore
    constraint_clarity: ComponentScore
    success_criteria_clarity: ComponentScore
    ontology_clarity: ComponentScore  # NEW

    @property
    def components(self) -> list[ComponentScore]:
        """Return all component scores as a list."""
        return [
            self.goal_clarity,
            self.constraint_clarity,
            self.success_criteria_clarity,
            self.ontology_clarity,  # NEW
        ]

# Update system prompt (line 309)
SCORING_SYSTEM_PROMPT = """You are an expert requirements analyst...

Evaluate four components:
1. Goal Clarity (35%): Is the goal specific and well-defined?
2. Constraint Clarity (25%): Are constraints and limitations specified?
3. Success Criteria Clarity (25%): Are success criteria measurable?
4. Ontology Clarity (15%): Is this addressing the ROOT problem, not symptoms?

...

Required JSON format:
{
    "goal_clarity_score": 0.0,
    "goal_clarity_justification": "string",
    "constraint_clarity_score": 0.0,
    "constraint_clarity_justification": "string",
    "success_criteria_clarity_score": 0.0,
    "success_criteria_clarity_justification": "string",
    "ontology_clarity_score": 0.0,
    "ontology_clarity_justification": "string"
}"""
```

### D. evaluation/consensus.py (Refactoring)

```python
"""Stage 3: Deliberative Multi-Model Consensus.

Refactored from simple voting to role-based deliberation:
- ADVOCATE: Argues in favor, finds strengths
- DEVIL: Critical perspective, ontological questions
- JUDGE: Weighs both sides, makes final decision
"""

from enum import StrEnum

class VoterRole(StrEnum):
    """Roles in deliberative consensus."""
    ADVOCATE = "advocate"
    DEVIL = "devil"
    JUDGE = "judge"

# Role-specific prompts
ADVOCATE_SYSTEM_PROMPT = """You are the ADVOCATE in a deliberative review.

Your role is to:
- Find and articulate the STRENGTHS of this solution
- Explain why it correctly addresses the acceptance criterion
- Highlight positive aspects of implementation quality
- Provide reasoned support for approval

Be honest - if there are genuine weaknesses, acknowledge them,
but focus on making the case FOR approval if warranted.

Respond with JSON: {"approved": bool, "confidence": 0-1, "reasoning": "..."}
"""

DEVIL_ADVOCATE_SYSTEM_PROMPT = """You are the DEVIL'S ADVOCATE in a deliberative review.

Your role is to critically examine using ONTOLOGICAL ANALYSIS:
- Is this solving the ROOT CAUSE or just a SYMPTOM?
- What's the ESSENCE of the problem? Does this address it?
- Are there PREREQUISITES that should exist first?
- What HIDDEN ASSUMPTIONS does this solution make?

Your goal is NOT to reject everything, but to ensure we're solving
the FUNDAMENTAL problem. If this is symptom treatment, explain why.

Respond with JSON: {"approved": bool, "confidence": 0-1, "reasoning": "..."}
"""

JUDGE_SYSTEM_PROMPT = """You are the JUDGE in a deliberative review.

You will receive:
- The ADVOCATE's position (supporting approval)
- The DEVIL's ADVOCATE position (critical analysis)

Your role is to:
1. Weigh both perspectives fairly
2. Determine if the Devil's concerns are valid
3. Make the FINAL decision on approval

Consider: Is this a symptom treatment or genuine solution?

Respond with JSON: {
    "final_verdict": "approved" | "rejected" | "conditional",
    "confidence": 0-1,
    "reasoning": "...",
    "conditions": [".."] | null
}
"""

@dataclass(frozen=True, slots=True)
class DeliberationResult:
    """Result of deliberative consensus."""
    final_verdict: str  # "approved", "rejected", "conditional"
    advocate_position: Vote
    devil_position: Vote
    judge_reasoning: str
    conditions: tuple[str, ...] | None
    confidence: float

class DeliberativeConsensus:
    """Two-round deliberative consensus evaluator."""

    async def deliberate(
        self, context: EvaluationContext
    ) -> Result[DeliberationResult, ProviderError]:
        """Run 2-round deliberation: positions → judgment."""

        # Round 1: Get positions (parallel)
        advocate_task = self._get_position(context, VoterRole.ADVOCATE)
        devil_task = self._get_position(context, VoterRole.DEVIL)

        advocate_result, devil_result = await asyncio.gather(
            advocate_task, devil_task
        )

        # Round 2: Judge reviews both positions
        judge_result = await self._get_judgment(
            context, advocate_result.value, devil_result.value
        )

        return Result.ok(DeliberationResult(
            final_verdict=judge_result.verdict,
            advocate_position=advocate_result.value,
            devil_position=devil_result.value,
            judge_reasoning=judge_result.reasoning,
            conditions=judge_result.conditions,
            confidence=judge_result.confidence,
        ))
```

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| Shared question framework in `core/` | Single source of truth for philosophical consistency |
| Ontological questions every 3rd round | Balance between thoroughness and interview length |
| 15% weight for Ontology Score | Meaningful but not dominant influence |
| 2-round deliberation | Simpler than 3-round, still captures debate |
| Devil uses ontological questions | Links consensus back to core framework |

## File Changes Summary

| File | Change Type | Lines Affected |
|------|-------------|----------------|
| `core/ontology_questions.py` | New | ~120 lines |
| `bigbang/ontology.py` | New | ~80 lines |
| `bigbang/interview.py` | Modify | +30 lines |
| `bigbang/ambiguity.py` | Modify | +40 lines |
| `evaluation/consensus.py` | Refactor | +100 lines, -50 lines |
| `evaluation/models.py` | Modify | +10 lines |

## Testing Strategy

1. **Unit Tests**
   - `test_ontology_questions.py` - Question framework
   - `test_interview_ontology.py` - Interview integration
   - `test_ambiguity_extended.py` - 4-component scoring
   - `test_deliberative_consensus.py` - Role-based deliberation

2. **Integration Tests**
   - Interview flow with mixed question types
   - Full evaluation pipeline with new consensus

3. **Regression Tests**
   - All existing tests must pass
   - Backward compatibility for old consensus format

## Related Documents

- Requirements: `docs/ontological-framework/requirements.md`
- Implementation: `docs/ontological-framework/implementation.md` (TBD)
