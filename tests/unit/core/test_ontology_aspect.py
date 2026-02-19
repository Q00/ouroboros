"""Tests for core/ontology_aspect.py - AOP Framework."""

import pytest

from ouroboros.core.ontology_aspect import (
    AnalysisResult,
    OntologicalAspect,
    OntologicalJoinPoint,
    OntologicalViolationError,
    create_ontology_aspect,
)
from ouroboros.core.types import Result


class MockContext:
    """Mock context for testing."""

    def __init__(self, value: str = "test"):
        self.value = value


class MockStrategy:
    """Mock strategy for testing."""

    def __init__(
        self,
        *,
        is_valid: bool = True,
        confidence: float = 0.9,
        reasoning: tuple[str, ...] = ("Test reasoning",),
        suggestions: tuple[str, ...] = (),
        raise_exception: bool = False,
    ):
        self._is_valid = is_valid
        self._confidence = confidence
        self._reasoning = reasoning
        self._suggestions = suggestions
        self._raise_exception = raise_exception
        self.analyze_called = False

    @property
    def join_point(self) -> OntologicalJoinPoint:
        return OntologicalJoinPoint.CONSENSUS

    def get_cache_key(self, context: MockContext) -> str:
        return f"mock_{context.value}"

    async def analyze(self, context: MockContext) -> AnalysisResult:
        self.analyze_called = True
        if self._raise_exception:
            raise RuntimeError("Mock LLM failure")

        if self._is_valid:
            return AnalysisResult.valid(
                confidence=self._confidence,
                reasoning=self._reasoning,
            )
        else:
            return AnalysisResult.invalid(
                reasoning=self._reasoning,
                suggestions=self._suggestions,
                confidence=self._confidence,
            )


class TestAnalysisResult:
    """Test AnalysisResult dataclass."""

    def test_valid_factory(self) -> None:
        """Test AnalysisResult.valid() factory."""
        result = AnalysisResult.valid(
            confidence=0.95,
            reasoning=["This is valid"],
        )
        assert result.is_valid is True
        assert result.confidence == 0.95
        assert result.reasoning == ("This is valid",)
        assert result.suggestions == ()
        assert result.needs_refinement is False

    def test_invalid_factory(self) -> None:
        """Test AnalysisResult.invalid() factory."""
        result = AnalysisResult.invalid(
            reasoning=["Root cause not addressed"],
            suggestions=["Focus on the underlying issue"],
            confidence=0.8,
        )
        assert result.is_valid is False
        assert result.confidence == 0.8
        assert result.reasoning == ("Root cause not addressed",)
        assert result.suggestions == ("Focus on the underlying issue",)
        assert result.needs_refinement is True

    def test_needs_refinement_no_suggestions(self) -> None:
        """needs_refinement is False if no suggestions even when invalid."""
        result = AnalysisResult.invalid(
            reasoning=["Invalid but no suggestions"],
            suggestions=(),
        )
        assert result.is_valid is False
        assert result.needs_refinement is False


class TestOntologicalViolationError:
    """Test OntologicalViolationError."""

    def test_error_contains_result(self) -> None:
        """Error should contain the analysis result."""
        result = AnalysisResult.invalid(
            reasoning=["Test"],
            suggestions=["Fix it"],
        )
        error = OntologicalViolationError(
            result,
            join_point=OntologicalJoinPoint.CONSENSUS,
        )

        assert error.result is result
        assert error.join_point == OntologicalJoinPoint.CONSENSUS
        assert "Ontological violation" in str(error)
        assert error.details["is_valid"] is False


class TestOntologicalAspect:
    """Test OntologicalAspect weaver."""

    async def test_execute_valid_proceeds(self) -> None:
        """Valid analysis should execute core operation."""
        strategy = MockStrategy(is_valid=True)
        aspect = OntologicalAspect(strategy=strategy)

        async def core_op(ctx: MockContext) -> Result[str, Exception]:
            return Result.ok(f"success_{ctx.value}")

        result = await aspect.execute(
            context=MockContext("test"),
            core_operation=core_op,
        )

        assert result.is_ok
        assert result.value == "success_test"
        assert strategy.analyze_called is True

    async def test_execute_invalid_halts(self) -> None:
        """Invalid analysis should return error when halt_on_violation=True."""
        strategy = MockStrategy(
            is_valid=False,
            suggestions=["Try this instead"],
        )
        aspect = OntologicalAspect(strategy=strategy, halt_on_violation=True)

        async def core_op(ctx: MockContext) -> Result[str, Exception]:
            return Result.ok("should not run")

        result = await aspect.execute(
            context=MockContext(),
            core_operation=core_op,
        )

        assert result.is_err
        assert isinstance(result.error, OntologicalViolationError)
        assert result.error.result.suggestions == ("Try this instead",)

    async def test_execute_invalid_continues_when_not_halting(self) -> None:
        """Invalid analysis should continue when halt_on_violation=False."""
        strategy = MockStrategy(is_valid=False)
        aspect = OntologicalAspect(strategy=strategy, halt_on_violation=False)

        async def core_op(ctx: MockContext) -> Result[str, Exception]:
            return Result.ok("continued")

        result = await aspect.execute(
            context=MockContext(),
            core_operation=core_op,
        )

        assert result.is_ok
        assert result.value == "continued"

    async def test_skip_analysis(self) -> None:
        """skip_analysis=True should bypass ontological analysis."""
        strategy = MockStrategy(is_valid=False)
        aspect = OntologicalAspect(strategy=strategy)

        async def core_op(ctx: MockContext) -> Result[str, Exception]:
            return Result.ok("skipped")

        result = await aspect.execute(
            context=MockContext(),
            core_operation=core_op,
            skip_analysis=True,
        )

        assert result.is_ok
        assert result.value == "skipped"
        assert strategy.analyze_called is False

    async def test_cache_hit(self) -> None:
        """Same context should use cached result."""
        strategy = MockStrategy(is_valid=True)
        aspect = OntologicalAspect(strategy=strategy)

        async def core_op(ctx: MockContext) -> Result[str, Exception]:
            return Result.ok("success")

        # First call - analyze should be called
        await aspect.execute(context=MockContext("same"), core_operation=core_op)
        assert strategy.analyze_called is True

        # Reset flag
        strategy.analyze_called = False

        # Second call with same context - should hit cache
        await aspect.execute(context=MockContext("same"), core_operation=core_op)
        assert strategy.analyze_called is False  # Not called again

    async def test_strict_mode_raises_on_llm_failure(self) -> None:
        """strict_mode=True should raise on LLM failure."""
        strategy = MockStrategy(raise_exception=True)
        aspect = OntologicalAspect(strategy=strategy, strict_mode=True)

        async def core_op(ctx: MockContext) -> Result[str, Exception]:
            return Result.ok("should not reach")

        with pytest.raises(RuntimeError, match="Mock LLM failure"):
            await aspect.execute(context=MockContext(), core_operation=core_op)

    async def test_fail_open_on_llm_failure(self) -> None:
        """strict_mode=False should continue on LLM failure."""
        strategy = MockStrategy(raise_exception=True)
        aspect = OntologicalAspect(strategy=strategy, strict_mode=False)

        async def core_op(ctx: MockContext) -> Result[str, Exception]:
            return Result.ok("continued despite failure")

        result = await aspect.execute(context=MockContext(), core_operation=core_op)

        assert result.is_ok
        assert result.value == "continued despite failure"


class TestCreateOntologyAspect:
    """Test factory function."""

    def test_factory_creates_aspect(self) -> None:
        """Factory should create properly configured aspect."""
        strategy = MockStrategy()
        aspect = create_ontology_aspect(
            strategy=strategy,
            halt_on_violation=False,
            strict_mode=False,
            cache_ttl=600,
            cache_maxsize=200,
        )

        assert aspect.strategy is strategy
        assert aspect.halt_on_violation is False
        assert aspect.strict_mode is False
        assert aspect.cache_ttl == 600
        assert aspect.cache_maxsize == 200
