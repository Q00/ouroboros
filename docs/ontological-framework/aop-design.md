# Ontological Framework - AOP Architecture Design

> Generated: 2026-01-29
> Analysis: zen thinkdeep (Gemini 3 Pro) - 2 rounds
> Confidence: Very High (Validated)
> Version Target: v0.4.0

---

## Expert Analysis Summary (Round 2)

### Key Refinements from Deep Thinking

| Issue | Solution |
|-------|----------|
| **Nested Results** | Type union: `Result[T, OntologicalViolationError \| E]` |
| **LLM Latency** | LRU+TTL cache (cachetools, 5min/100 entries) |
| **Hot Path Escape** | `skip_analysis: bool` parameter |
| **Cache Key Generation** | Strategy-provided, not Aspect-computed |
| **LLM Failure Handling** | `strict_mode` flag (fail_open vs fail_closed) |

### Final Protocol Design

```python
class OntologyStrategy(Protocol[C]):
    """Protocol with cache key delegation."""

    async def analyze(self, context: C) -> AnalysisResult:
        """Perform ontological analysis."""
        ...

    def get_cache_key(self, context: C) -> str:
        """Strategy decides what parts of context matter for caching."""
        ...

    @property
    def join_point(self) -> OntologicalJoinPoint:
        """Which phase this strategy is for."""
        ...
```

---

## Executive Summary

Ouroboros의 Ontological Analysis는 **Cross-Cutting Concern**이다. 3개의 Phase에 걸쳐 동일한 철학적 질문이 적용되며, 이를 AOP(Aspect-Oriented Programming) 패턴으로 중앙화한다.

**선택된 패턴**: Protocol + Strategy + Dependency Injection
- Python 친화적 (no runtime magic)
- Type-safe with full IDE support
- Ouroboros 기존 패턴과 일치 (Protocol, Result, frozen dataclass)

---

## 1. Problem Statement

### Cross-Cutting Concern 식별

```
┌──────────────────────────────────────────────────────────────────┐
│                    ONTOLOGICAL ANALYSIS                          │
│                    (Same Questions, Different Contexts)          │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│   Phase 0                Phase 3                Phase 4          │
│   INTERVIEW              RESILIENCE             CONSENSUS        │
│                                                                  │
│   "What IS this?"        "What are we          "Is this root    │
│                           assuming?"            cause or         │
│   → User Question        → Challenge            symptom?"        │
│                           Assumptions                            │
│                                                → Devil's         │
│                          → CONTRARIAN            Advocate        │
│                           Persona                               │
└──────────────────────────────────────────────────────────────────┘
```

### 현재 문제점

```python
# Phase 0 - interview.py
from ouroboros.core.ontology_questions import build_ontological_prompt
# ... 직접 호출

# Phase 3 - lateral.py
from ouroboros.core.ontology_questions import ONTOLOGICAL_QUESTIONS
# ... 다른 방식으로 사용

# Phase 4 - consensus.py
from ouroboros.core.ontology_questions import build_devil_advocate_prompt
# ... 또 다른 방식으로 사용
```

**문제**: 동일한 ontological logic이 3곳에서 다르게 구현됨

---

## 2. AOP Pattern Selection

### 비교 분석

| Pattern | Pros | Cons | Fit |
|---------|------|------|-----|
| **Decorator-based** | Pythonic, explicit | Limited runtime context | Medium |
| **Protocol + Strategy + DI** | Type-safe, testable, explicit | More boilerplate | **Best** |
| **Event-driven Pointcut** | True AOP, flexible | Runtime magic, hard to debug | Low |

### 선택: Protocol + Strategy + DI

**이유:**
1. Ouroboros 기존 패턴과 일치 (`OntologicalAnalyzer` Protocol 이미 존재)
2. Type-safe with full IDE support
3. Mock으로 쉽게 테스트 가능
4. Runtime magic 없음 - 명시적이고 디버깅 용이
5. Join point별 다른 Strategy 지원

---

## 3. Architecture Design

### 3.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          core/ontology_aspect.py                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────────────┐      ┌─────────────────────────────────────────┐   │
│  │  OntologicalJoinPoint│      │  OntologyStrategy (Protocol)           │   │
│  │  (Enum)              │      │                                         │   │
│  │                      │      │  + analyze(context) -> AnalysisResult  │   │
│  │  - INTERVIEW         │      └─────────────────────────────────────────┘   │
│  │  - RESILIENCE        │                       ▲                            │
│  │  - CONSENSUS         │                       │ implements                 │
│  └─────────────────────┘       ┌────────────────┼────────────────┐           │
│                                │                │                │           │
│  ┌─────────────────────────────┴──┐ ┌──────────┴────────┐ ┌─────┴────────┐  │
│  │  InterviewOntologyStrategy     │ │ContrarianStrategy │ │DevilStrategy │  │
│  │                                │ │                   │ │              │  │
│  │  Focus: User clarification     │ │Focus: Assumption  │ │Focus: Root   │  │
│  │  Output: Question to ask       │ │challenge          │ │cause check   │  │
│  └────────────────────────────────┘ └───────────────────┘ └──────────────┘  │
│                                                                              │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  OntologicalAspect                                                     │  │
│  │                                                                         │  │
│  │  + __init__(strategies: dict[JoinPoint, Strategy])                     │  │
│  │  + execute(join_point, context, core_operation) -> Result              │  │
│  │                                                                         │  │
│  │  Internal flow:                                                         │  │
│  │    1. Pre-analysis (Strategy.analyze)                                  │  │
│  │    2. Validation (is_valid check)                                      │  │
│  │    3. Core execution (if valid)                                        │  │
│  │    4. Post-processing (event emission)                                 │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │  create_default_ontology_aspect() -> OntologicalAspect                 │  │
│  │                                                                         │  │
│  │  Factory function that creates pre-configured aspect with all          │  │
│  │  default strategies                                                     │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 Integration Pattern (Interceptor)

**핵심 통찰 (from Expert Analysis):**
> Component에 Aspect를 주입하는 대신, Component를 Aspect로 감싸라.

```
Before (High Coupling):
┌──────────────────┐     ┌─────────────────┐
│   Controller     │ ──> │   Component     │
└──────────────────┘     │   (knows about  │
                         │    Aspect)      │
                         └─────────────────┘

After (Low Coupling - Interceptor Pattern):
┌──────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   Controller     │ ──> │   Aspect        │ ──> │   Component     │
└──────────────────┘     │   (Interceptor) │     │   (unchanged)   │
                         └─────────────────┘     └─────────────────┘
```

---

## 4. Detailed Design

### 4.1 Core Protocols

```python
# core/ontology_aspect.py

from typing import Protocol, TypeVar, Generic, Callable, Awaitable, Any
from dataclasses import dataclass
from enum import StrEnum

from ouroboros.core.types import Result
from ouroboros.core.errors import OuroborosError


class OntologicalJoinPoint(StrEnum):
    """Where ontological analysis is applied."""
    INTERVIEW = "interview"      # Phase 0: Requirement clarification
    RESILIENCE = "resilience"    # Phase 3: Stagnation recovery
    CONSENSUS = "consensus"      # Phase 4: Result evaluation


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Standardized result from any ontological analysis."""
    is_valid: bool              # Passes ontological check?
    confidence: float           # 0.0 - 1.0
    reasoning: tuple[str, ...]  # Why this conclusion?
    suggestions: tuple[str, ...]  # Refinements if invalid

    @property
    def needs_refinement(self) -> bool:
        return not self.is_valid and len(self.suggestions) > 0


class OntologicalViolationError(OuroborosError):
    """Raised when ontological analysis blocks execution."""
    def __init__(self, result: AnalysisResult):
        self.result = result
        super().__init__(
            message="Ontological violation detected",
            details={
                "confidence": result.confidence,
                "reasoning": result.reasoning,
                "suggestions": result.suggestions,
            }
        )


# Context type variable for generic strategy
C = TypeVar("C", contravariant=True)


class OntologyStrategy(Protocol[C]):
    """Protocol for join-point-specific ontological analysis.

    Each strategy implements the same interface but applies
    different logic based on the phase context.
    """

    async def analyze(self, context: C) -> AnalysisResult:
        """Perform ontological analysis on the given context.

        Args:
            context: Phase-specific context (InterviewContext, etc.)

        Returns:
            AnalysisResult with validity, confidence, and reasoning
        """
        ...
```

### 4.2 The Aspect (Interceptor/Weaver)

```python
T_Result = TypeVar("T_Result")


@dataclass
class OntologicalAspect(Generic[C, T_Result]):
    """Central Weaver: Intercepts execution to apply ontological analysis.

    Implements the "Around Advice" pattern:
    1. Pre-execution: Run ontological analysis
    2. Decision: Proceed or halt based on analysis
    3. Execution: Run core operation if valid
    4. Post-execution: Emit events

    Example:
        aspect = OntologicalAspect(
            strategy=DevilAdvocateStrategy(llm),
            on_violation=lambda ctx, r: emit_violation_event(ctx, r),
        )

        result = await aspect.execute(
            context=evaluation_context,
            core_operation=lambda ctx: consensus.evaluate(ctx),
        )
    """

    strategy: OntologyStrategy[C]
    on_violation: Callable[[C, AnalysisResult], Awaitable[None]] | None = None
    on_valid: Callable[[C, AnalysisResult], Awaitable[None]] | None = None
    halt_on_violation: bool = True  # Raise error or continue with warning?

    async def execute(
        self,
        context: C,
        core_operation: Callable[[C], Awaitable[T_Result]],
    ) -> Result[T_Result, OntologicalViolationError]:
        """Execute with ontological analysis (Around Advice).

        Args:
            context: Phase-specific context
            core_operation: The actual operation to execute

        Returns:
            Result containing operation result or violation error
        """
        # 1. Pre-analysis
        analysis = await self.strategy.analyze(context)

        # 2. Handle violation
        if not analysis.is_valid:
            if self.on_violation:
                await self.on_violation(context, analysis)

            if self.halt_on_violation:
                return Result.err(OntologicalViolationError(analysis))
            # else: log warning and continue

        # 3. Handle valid
        if analysis.is_valid and self.on_valid:
            await self.on_valid(context, analysis)

        # 4. Execute core operation
        try:
            result = await core_operation(context)
            return Result.ok(result)
        except Exception as e:
            # Re-wrap if needed
            raise
```

### 4.3 Strategy Implementations

```python
# strategies/interview_strategy.py

@dataclass
class InterviewContext:
    """Context for interview phase ontological analysis."""
    initial_context: str
    rounds: list[InterviewRound]
    current_round: int


@dataclass
class InterviewOntologyStrategy:
    """Strategy for Interview phase (Phase 0).

    Focuses on:
    - Is the user asking about the ROOT problem?
    - Are there hidden assumptions in the request?
    - What prerequisites are being assumed?
    """

    llm_adapter: LLMAdapter
    model: str = "openrouter/google/gemini-2.0-flash-001"

    async def analyze(self, context: InterviewContext) -> AnalysisResult:
        # Build analysis prompt using shared ontological questions
        prompt = self._build_analysis_prompt(context)

        result = await self.llm_adapter.complete(
            messages=[Message(role=MessageRole.USER, content=prompt)],
            config=CompletionConfig(model=self.model, temperature=0.3),
        )

        return self._parse_result(result.value.content)

    def _build_analysis_prompt(self, context: InterviewContext) -> str:
        from ouroboros.core.ontology_questions import ONTOLOGICAL_QUESTIONS

        questions = "\n".join(
            f"- {q.question}: {q.purpose}"
            for q in ONTOLOGICAL_QUESTIONS.values()
        )

        return f"""Analyze this requirement using ontological questions:

Context: {context.initial_context}
Rounds completed: {context.current_round}

Apply these questions:
{questions}

Respond with JSON:
{{
    "is_root_problem": true/false,
    "confidence": 0.0-1.0,
    "reasoning": ["..."],
    "suggestions": ["..."]  // if not root problem
}}"""


# strategies/devil_advocate_strategy.py

@dataclass
class ConsensusContext:
    """Context for consensus phase ontological analysis."""
    artifact: str
    current_ac: str
    goal: str
    constraints: tuple[str, ...]


@dataclass
class DevilAdvocateStrategy:
    """Strategy for Consensus phase (Phase 4).

    The Devil's Advocate role: Critically examine whether
    the solution addresses the ROOT CAUSE or just symptoms.
    """

    llm_adapter: LLMAdapter
    model: str = "openrouter/anthropic/claude-sonnet-4-20250514"

    async def analyze(self, context: ConsensusContext) -> AnalysisResult:
        from ouroboros.core.ontology_questions import build_devil_advocate_prompt

        system_prompt = build_devil_advocate_prompt()
        user_prompt = self._build_evaluation_prompt(context)

        result = await self.llm_adapter.complete(
            messages=[
                Message(role=MessageRole.SYSTEM, content=system_prompt),
                Message(role=MessageRole.USER, content=user_prompt),
            ],
            config=CompletionConfig(model=self.model, temperature=0.3),
        )

        return self._parse_result(result.value.content)


# strategies/contrarian_strategy.py

@dataclass
class ResilienceContext:
    """Context for resilience phase ontological analysis."""
    problem: str
    failed_approaches: list[str]
    stagnation_pattern: str


@dataclass
class ContrarianStrategy:
    """Strategy for Resilience phase (Phase 3).

    The CONTRARIAN persona: Challenge all assumptions
    when other approaches have failed.
    """

    llm_adapter: LLMAdapter
    model: str = "openrouter/openai/gpt-4o"

    async def analyze(self, context: ResilienceContext) -> AnalysisResult:
        from ouroboros.core.ontology_questions import (
            OntologicalQuestionType,
            build_ontological_prompt,
        )

        # Focus on hidden assumptions when stuck
        assumption_prompt = build_ontological_prompt(
            OntologicalQuestionType.HIDDEN_ASSUMPTIONS
        )

        # ... implementation
```

### 4.4 Factory Function

```python
def create_default_ontology_aspect(
    llm_adapter: LLMAdapter,
    join_point: OntologicalJoinPoint,
    event_emitter: Callable[[BaseEvent], Awaitable[None]] | None = None,
) -> OntologicalAspect:
    """Factory to create pre-configured ontological aspect.

    Args:
        llm_adapter: LLM adapter for analysis
        join_point: Which phase this aspect is for
        event_emitter: Optional event emission callback

    Returns:
        Configured OntologicalAspect for the specified join point

    Example:
        aspect = create_default_ontology_aspect(
            llm_adapter=adapter,
            join_point=OntologicalJoinPoint.CONSENSUS,
        )
    """
    strategies: dict[OntologicalJoinPoint, OntologyStrategy] = {
        OntologicalJoinPoint.INTERVIEW: InterviewOntologyStrategy(llm_adapter),
        OntologicalJoinPoint.RESILIENCE: ContrarianStrategy(llm_adapter),
        OntologicalJoinPoint.CONSENSUS: DevilAdvocateStrategy(llm_adapter),
    }

    async def emit_violation(ctx, result: AnalysisResult):
        if event_emitter:
            event = create_ontological_violation_event(
                join_point=join_point,
                reasoning=result.reasoning,
                suggestions=result.suggestions,
            )
            await event_emitter(event)

    return OntologicalAspect(
        strategy=strategies[join_point],
        on_violation=emit_violation,
    )
```

---

## 5. Integration Examples

### 5.1 Phase 4: Deliberative Consensus

```python
# evaluation/consensus.py

class DeliberativeConsensus:
    """Two-round deliberative consensus with ontological analysis."""

    def __init__(
        self,
        llm_adapter: LiteLLMAdapter,
        ontology_aspect: OntologicalAspect | None = None,
    ):
        self._llm = llm_adapter
        self._ontology = ontology_aspect or create_default_ontology_aspect(
            llm_adapter, OntologicalJoinPoint.CONSENSUS
        )

    async def deliberate(
        self, context: EvaluationContext
    ) -> Result[DeliberationResult, OuroborosError]:
        """Run 2-round deliberation with ontological Devil's Advocate."""

        # Wrap the evaluation with ontological aspect
        consensus_context = ConsensusContext(
            artifact=context.artifact,
            current_ac=context.current_ac,
            goal=context.goal,
            constraints=context.constraints,
        )

        # Around Advice: Ontological check wraps core deliberation
        return await self._ontology.execute(
            context=consensus_context,
            core_operation=self._run_deliberation,
        )

    async def _run_deliberation(
        self, ctx: ConsensusContext
    ) -> DeliberationResult:
        # Core deliberation logic (Advocate -> Devil -> Judge)
        ...
```

### 5.2 Phase 0: Interview Engine

```python
# bigbang/interview.py

class InterviewEngine:
    """Interview engine with interleaved ontological questioning."""

    def __init__(
        self,
        llm_adapter: LLMAdapter,
        ontology_aspect: OntologicalAspect | None = None,
    ):
        self._llm = llm_adapter
        self._ontology = ontology_aspect or create_default_ontology_aspect(
            llm_adapter, OntologicalJoinPoint.INTERVIEW
        )

    async def ask_next_question(
        self, state: InterviewState
    ) -> Result[str, OuroborosError]:
        if self._should_ask_ontological(state.current_round_number):
            return await self._ask_ontological_question(state)
        else:
            return await self._ask_socratic_question(state)

    async def _ask_ontological_question(
        self, state: InterviewState
    ) -> Result[str, OuroborosError]:
        context = InterviewContext(
            initial_context=state.initial_context,
            rounds=state.rounds,
            current_round=state.current_round_number,
        )

        # Use aspect to analyze and generate question
        analysis_result = await self._ontology.strategy.analyze(context)

        if analysis_result.needs_refinement:
            return Result.ok(self._build_refinement_question(analysis_result))
        else:
            return Result.ok(self._build_deepening_question(analysis_result))
```

---

## 6. Key Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Pattern | Protocol + Strategy + DI | Matches Ouroboros style, type-safe |
| Interceptor vs Injection | Interceptor (Aspect wraps Component) | Lower coupling |
| Error Handling | Result type + specific Error | Consistent with codebase |
| Strategy per Phase | Yes (3 strategies) | Different contexts need different logic |
| Halt on Violation | Configurable (default: true) | Flexibility for different use cases |
| Caching | Inside Strategy | Avoid redundant LLM calls |

---

## 7. Trade-offs

### Pros

| Benefit | Description |
|---------|-------------|
| **Single Source of Truth** | All ontological logic in one module |
| **Consistency** | Same questions applied uniformly |
| **Testability** | Mock strategies for unit testing |
| **Extensibility** | Add new JoinPoints easily |
| **Explicit** | No runtime magic, clear call paths |

### Cons

| Cost | Mitigation |
|------|------------|
| **Boilerplate** | Factory functions reduce it |
| **Indirection** | Clear naming, good docs |
| **Constructor changes** | One-time migration effort |

---

## 8. Implementation Plan

### Phase 1: Core Module (v0.4.0)

```
1. Create core/ontology_aspect.py
   - OntologicalJoinPoint enum
   - AnalysisResult dataclass
   - OntologyStrategy Protocol
   - OntologicalAspect class
   - Factory function

2. Create strategies/
   - interview_strategy.py
   - devil_advocate_strategy.py
   - contrarian_strategy.py (stub for v0.5.0)
```

### Phase 2: Integration (v0.4.0)

```
3. Update evaluation/consensus.py
   - Add OntologicalAspect injection
   - Integrate with DeliberativeConsensus

4. Update bigbang/interview.py
   - Add OntologicalAspect injection
   - Interleave with Socratic questioning
```

### Phase 3: Resilience (v0.5.0)

```
5. Update resilience/lateral.py
   - Connect CONTRARIAN persona to ContrarianStrategy
   - Wire stagnation detection to aspect
```

---

## 9. File Structure

```
src/ouroboros/
├── core/
│   ├── ontology_questions.py    # Existing: questions, types
│   └── ontology_aspect.py       # NEW: AOP framework
│
├── strategies/                   # NEW: Strategy implementations
│   ├── __init__.py
│   ├── interview_strategy.py
│   ├── devil_advocate_strategy.py
│   └── contrarian_strategy.py
│
├── bigbang/
│   ├── interview.py             # Modified: inject aspect
│   └── ontology.py              # Can be simplified or removed
│
├── evaluation/
│   └── consensus.py             # Modified: inject aspect
│
└── resilience/
    └── lateral.py               # Modified: inject aspect (v0.5.0)
```

---

## 10. Testing Strategy

```python
# tests/unit/core/test_ontology_aspect.py

class TestOntologicalAspect:
    """Test the AOP weaver."""

    async def test_execute_valid_proceeds(self):
        """Valid analysis should execute core operation."""
        mock_strategy = MockStrategy(is_valid=True)
        aspect = OntologicalAspect(strategy=mock_strategy)

        result = await aspect.execute(
            context={"test": "context"},
            core_operation=lambda ctx: "success",
        )

        assert result.is_ok
        assert result.value == "success"

    async def test_execute_invalid_halts(self):
        """Invalid analysis should return error."""
        mock_strategy = MockStrategy(
            is_valid=False,
            suggestions=["Try this instead"],
        )
        aspect = OntologicalAspect(strategy=mock_strategy)

        result = await aspect.execute(
            context={"test": "context"},
            core_operation=lambda ctx: "should not run",
        )

        assert result.is_err
        assert isinstance(result.error, OntologicalViolationError)
```

---

## 11. Final Validated Design (from Expert Analysis)

### Complete OntologicalAspect Implementation

```python
# core/ontology_aspect.py - PRODUCTION READY

from typing import TypeVar, Generic, Callable, Awaitable, Any
from dataclasses import dataclass, field
from cachetools import TTLCache

from ouroboros.core.types import Result
from ouroboros.core.errors import OuroborosError
from ouroboros.events.base import BaseEvent

C = TypeVar("C")  # Context type
T = TypeVar("T")  # Result type
E = TypeVar("E", bound=OuroborosError)  # Error type


class OntologyStrategy(Protocol[C]):
    """Protocol for join-point-specific ontological analysis.

    Key: Strategy provides cache_key, not Aspect.
    This allows fine-grained control over what matters for caching.
    """

    async def analyze(self, context: C) -> AnalysisResult:
        """Perform ontological analysis on the given context."""
        ...

    def get_cache_key(self, context: C) -> str:
        """Return cache key for this context.

        Strategy decides which parts of context are relevant.
        Example: Consensus only cares about artifact hash, not full state.
        """
        ...

    @property
    def join_point(self) -> OntologicalJoinPoint:
        """Which phase this strategy is for."""
        ...


@dataclass
class OntologicalAspect(Generic[C, T, E]):
    """
    Central AOP Weaver for Ontological Analysis.

    Production refinements:
    1. Type union for error handling
    2. Strategy-delegated cache keys
    3. skip_analysis escape hatch
    4. strict_mode for LLM failure handling
    5. Event emission integration
    """

    strategy: OntologyStrategy[C]
    event_emitter: Callable[[BaseEvent], Awaitable[None]] | None = None
    halt_on_violation: bool = True
    strict_mode: bool = True  # fail_closed by default
    cache_ttl: int = 300  # 5 minutes
    cache_maxsize: int = 100
    _cache: TTLCache = field(
        default_factory=lambda: TTLCache(maxsize=100, ttl=300),
        repr=False,
    )

    async def execute(
        self,
        context: C,
        core_operation: Callable[[C], Awaitable[Result[T, E]]],
        *,
        skip_analysis: bool = False,
    ) -> Result[T, OntologicalViolationError | E]:
        """
        Execute with ontological analysis (Around Advice).

        Args:
            context: Phase-specific context
            core_operation: The operation returning Result[T, E]
            skip_analysis: Skip ontological check (for known-safe paths)

        Returns:
            Result with union error type
        """
        # Escape hatch for hot paths
        if skip_analysis:
            return await core_operation(context)

        # Get cache key from Strategy (not self-computed)
        cache_key = self.strategy.get_cache_key(context)

        # Check cache
        if cache_key in self._cache:
            analysis = self._cache[cache_key]
        else:
            try:
                analysis = await self.strategy.analyze(context)
                self._cache[cache_key] = analysis
            except Exception as e:
                # LLM provider failure
                if self.strict_mode:
                    # fail_closed: propagate error
                    raise
                else:
                    # fail_open: log warning, proceed
                    log.warning(
                        "ontology.analysis.failed_open",
                        error=str(e),
                        join_point=self.strategy.join_point,
                    )
                    return await core_operation(context)

        # Handle violation
        if not analysis.is_valid:
            if self.event_emitter:
                event = OntologicalViolationEvent(
                    join_point=self.strategy.join_point,
                    confidence=analysis.confidence,
                    reasoning=analysis.reasoning,
                    suggestions=analysis.suggestions,
                )
                await self.event_emitter(event)

            if self.halt_on_violation:
                return Result.err(OntologicalViolationError(analysis))

        # Handle valid
        if analysis.is_valid and self.event_emitter:
            event = OntologicalPassedEvent(
                join_point=self.strategy.join_point,
                confidence=analysis.confidence,
            )
            await self.event_emitter(event)

        # Execute core operation (returns Result[T, E])
        return await core_operation(context)
```

### Configuration Matrix

| Setting | Default | Description |
|---------|---------|-------------|
| `halt_on_violation` | `True` | Return error on ontological failure |
| `strict_mode` | `True` | Fail closed on LLM errors |
| `cache_ttl` | `300` | Cache TTL in seconds |
| `cache_maxsize` | `100` | Max cached entries |

### Implementation Checklist

- [ ] Create `core/ontology_aspect.py`
  - [ ] `OntologicalJoinPoint` enum
  - [ ] `AnalysisResult` dataclass
  - [ ] `OntologyStrategy` Protocol with `get_cache_key()`
  - [ ] `OntologicalAspect` class with caching
  - [ ] Factory function
- [ ] Create `events/ontology.py`
  - [ ] `OntologicalViolationEvent`
  - [ ] `OntologicalPassedEvent`
- [ ] Create `strategies/` module
  - [ ] `InterviewOntologyStrategy`
  - [ ] `DevilAdvocateStrategy`
  - [ ] `ContrarianStrategy` (stub)
- [ ] Update existing components (DI)
  - [ ] `evaluation/consensus.py`
  - [ ] `bigbang/interview.py`
- [ ] Add tests
  - [ ] `test_ontology_aspect.py`
  - [ ] `test_strategies.py`

---

## References

- [Expert Analysis] zen thinkdeep - Gemini 3 Pro (2026-01-29, 2 rounds)
- [Existing Code] `src/ouroboros/core/ontology_questions.py`
- [AOP Concepts] Protocol + Strategy + DI in Python
- [Ouroboros Patterns] Result type, Protocol-based design, Event system
- [Dependencies] `cachetools` for TTLCache
