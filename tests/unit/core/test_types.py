"""Unit tests for ouroboros.core.types module."""

import pytest

from ouroboros.core.types import Result


class TestResultConstruction:
    """Test Result type construction via ok() and err() class methods."""

    def test_result_ok_creates_success_result(self) -> None:
        """Result.ok(value) creates a result with is_ok=True."""
        result: Result[int, str] = Result.ok(42)

        assert result.is_ok is True
        assert result.is_err is False

    def test_result_err_creates_error_result(self) -> None:
        """Result.err(error) creates a result with is_err=True."""
        result: Result[int, str] = Result.err("something went wrong")

        assert result.is_err is True
        assert result.is_ok is False

    def test_result_ok_stores_value(self) -> None:
        """Result.ok(value) stores the value internally."""
        result: Result[str, int] = Result.ok("hello")

        assert result.value == "hello"

    def test_result_err_stores_error(self) -> None:
        """Result.err(error) stores the error internally."""
        result: Result[str, int] = Result.err(404)

        assert result.error == 404


class TestResultUnwrap:
    """Test Result.unwrap() method."""

    def test_unwrap_returns_value_on_ok(self) -> None:
        """unwrap() returns the value when result is Ok."""
        result: Result[int, str] = Result.ok(100)

        assert result.unwrap() == 100

    def test_unwrap_raises_on_err(self) -> None:
        """unwrap() raises ValueError when result is Err."""
        result: Result[int, str] = Result.err("error message")

        with pytest.raises(ValueError, match="error message"):
            result.unwrap()


class TestResultUnwrapOr:
    """Test Result.unwrap_or(default) method."""

    def test_unwrap_or_returns_value_on_ok(self) -> None:
        """unwrap_or(default) returns value when result is Ok."""
        result: Result[int, str] = Result.ok(50)

        assert result.unwrap_or(0) == 50

    def test_unwrap_or_returns_default_on_err(self) -> None:
        """unwrap_or(default) returns default when result is Err."""
        result: Result[int, str] = Result.err("failed")

        assert result.unwrap_or(0) == 0


class TestResultMap:
    """Test Result.map(fn) method for transforming Ok values."""

    def test_map_transforms_ok_value(self) -> None:
        """map(fn) applies fn to value when result is Ok."""
        result: Result[int, str] = Result.ok(10)

        mapped: Result[str, str] = result.map(lambda x: f"value: {x}")

        assert mapped.is_ok
        assert mapped.unwrap() == "value: 10"

    def test_map_preserves_error(self) -> None:
        """map(fn) preserves error when result is Err."""
        result: Result[int, str] = Result.err("oops")

        mapped: Result[str, str] = result.map(lambda x: f"value: {x}")

        assert mapped.is_err
        assert mapped.error == "oops"


class TestResultMapErr:
    """Test Result.map_err(fn) method for transforming Err values."""

    def test_map_err_transforms_error(self) -> None:
        """map_err(fn) applies fn to error when result is Err."""
        result: Result[int, str] = Result.err("error")

        mapped: Result[int, int] = result.map_err(lambda e: len(e))

        assert mapped.is_err
        assert mapped.error == 5  # len("error") == 5

    def test_map_err_preserves_ok(self) -> None:
        """map_err(fn) preserves value when result is Ok."""
        result: Result[int, str] = Result.ok(42)

        mapped: Result[int, int] = result.map_err(lambda e: len(e))

        assert mapped.is_ok
        assert mapped.unwrap() == 42


class TestResultPropertyAccess:
    """Test Result value/error property access edge cases."""

    def test_value_raises_on_err_result(self) -> None:
        """Accessing value property on Err raises ValueError."""
        result: Result[int, str] = Result.err("error")

        with pytest.raises(ValueError, match="Cannot access value on Err result"):
            _ = result.value

    def test_error_raises_on_ok_result(self) -> None:
        """Accessing error property on Ok raises ValueError."""
        result: Result[int, str] = Result.ok(42)

        with pytest.raises(ValueError, match="Cannot access error on Ok result"):
            _ = result.error


class TestResultTypeAliases:
    """Test that type aliases are defined in the types module."""

    def test_event_payload_alias_exists(self) -> None:
        """EventPayload type alias is defined."""
        from ouroboros.core.types import EventPayload

        # EventPayload should be dict[str, Any]
        payload: EventPayload = {"key": "value", "count": 42}
        assert isinstance(payload, dict)

    def test_cost_units_alias_exists(self) -> None:
        """CostUnits type alias is defined."""
        from ouroboros.core.types import CostUnits

        # CostUnits should be int
        cost: CostUnits = 100
        assert isinstance(cost, int)

    def test_drift_score_alias_exists(self) -> None:
        """DriftScore type alias is defined."""
        from ouroboros.core.types import DriftScore

        # DriftScore should be float
        score: DriftScore = 0.75
        assert isinstance(score, float)
