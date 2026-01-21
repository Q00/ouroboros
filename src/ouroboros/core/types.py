"""Core types for Ouroboros - Result type and type aliases.

This module provides:
- Result[T, E]: A generic type for handling expected failures without exceptions
- Type aliases for common domain types
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class Result[T, E]:
    """A type that represents either success (Ok) or failure (Err).

    Result is used for expected failures (rate limits, API errors, validation failures)
    instead of exceptions. Exceptions are reserved for programming errors (bugs).

    Usage:
        # Construction
        ok_result: Result[int, str] = Result.ok(42)
        err_result: Result[int, str] = Result.err("something went wrong")

        # Pattern matching
        if result.is_ok:
            process(result.value)
        else:
            handle_error(result.error)

        # Transform values
        mapped = result.map(lambda x: x * 2)
        mapped_err = result.map_err(lambda e: CustomError(e))

        # Extract values
        value = result.unwrap()  # Raises if Err
        value = result.unwrap_or(default)  # Returns default if Err
    """

    _value: T | None
    _error: E | None
    _is_ok: bool

    @classmethod
    def ok(cls, value: T) -> Result[T, E]:
        """Create a successful Result containing the given value.

        Args:
            value: The success value to wrap.

        Returns:
            A Result in the Ok state.
        """
        return cls(_value=value, _error=None, _is_ok=True)

    @classmethod
    def err(cls, error: E) -> Result[T, E]:
        """Create a failed Result containing the given error.

        Args:
            error: The error value to wrap.

        Returns:
            A Result in the Err state.
        """
        return cls(_value=None, _error=error, _is_ok=False)

    @property
    def is_ok(self) -> bool:
        """Return True if this Result is Ok (success)."""
        return self._is_ok

    @property
    def is_err(self) -> bool:
        """Return True if this Result is Err (failure)."""
        return not self._is_ok

    def __repr__(self) -> str:
        """Return a semantic string representation of the Result.

        Returns:
            'Ok(value)' for success, 'Err(error)' for failure.
        """
        if self._is_ok:
            return f"Ok({self._value!r})"
        return f"Err({self._error!r})"

    @property
    def value(self) -> T:
        """Return the Ok value.

        Note: Only access this when is_ok is True.
        For safe access, use unwrap() or unwrap_or().
        """
        if not self._is_ok:
            msg = "Cannot access value on Err result"
            raise ValueError(msg)
        return cast(T, self._value)

    @property
    def error(self) -> E:
        """Return the Err value.

        Note: Only access this when is_err is True.
        """
        if self._is_ok:
            msg = "Cannot access error on Ok result"
            raise ValueError(msg)
        return cast(E, self._error)

    def unwrap(self) -> T:
        """Return the Ok value or raise ValueError if Err.

        Raises:
            ValueError: If this Result is Err, with the error as the message.

        Returns:
            The success value.
        """
        if self._is_ok:
            return cast(T, self._value)
        raise ValueError(str(self._error))

    def unwrap_or(self, default: T) -> T:
        """Return the Ok value or the provided default if Err.

        Args:
            default: Value to return if this Result is Err.

        Returns:
            The success value if Ok, otherwise the default.
        """
        if self._is_ok:
            return cast(T, self._value)
        return default

    def map[U](self, fn: Callable[[T], U]) -> Result[U, E]:
        """Transform the Ok value using the given function.

        If this Result is Ok, apply fn to the value and return Ok(fn(value)).
        If this Result is Err, return Err unchanged.

        Args:
            fn: Function to apply to the Ok value.

        Returns:
            A new Result with the transformed value or the original error.
        """
        if self._is_ok:
            return Result.ok(fn(cast(T, self._value)))
        return Result.err(cast(E, self._error))

    def map_err[F](self, fn: Callable[[E], F]) -> Result[T, F]:
        """Transform the Err value using the given function.

        If this Result is Err, apply fn to the error and return Err(fn(error)).
        If this Result is Ok, return Ok unchanged.

        Args:
            fn: Function to apply to the Err value.

        Returns:
            A new Result with the original value or the transformed error.
        """
        if self._is_ok:
            return Result.ok(cast(T, self._value))
        return Result.err(fn(cast(E, self._error)))

    def and_then[U](self, fn: Callable[[T], "Result[U, E]"]) -> "Result[U, E]":
        """Chain Result-producing operations (flatMap/bind).

        If this Result is Ok, apply fn to the value and return the result.
        If this Result is Err, return Err unchanged.

        This is useful for chaining operations that may fail.

        Args:
            fn: Function that takes the Ok value and returns a new Result.

        Returns:
            The result of fn if Ok, or the original Err.

        Example:
            def divide(a: int, b: int) -> Result[int, str]:
                if b == 0:
                    return Result.err("division by zero")
                return Result.ok(a // b)

            result = Result.ok(10).and_then(lambda x: divide(x, 2))
            # Returns Ok(5)
        """
        if self._is_ok:
            return fn(cast(T, self._value))
        return Result.err(cast(E, self._error))


# Type aliases for common domain types
EventPayload = dict[str, Any]
"""Type alias for event payload data - arbitrary JSON-serializable dict."""

CostUnits = int
"""Type alias for cost tracking - integer units (e.g., token counts)."""

DriftScore = float
"""Type alias for drift measurement - float between 0.0 and 1.0."""
