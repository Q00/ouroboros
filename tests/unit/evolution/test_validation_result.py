"""Validator result normalization preserves absent proof as absent."""

import pytest

from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result
from ouroboros.evolution.validation_result import normalize_validation_result, validation_passed


def test_missing_validation_results_remain_missing() -> None:
    assert normalize_validation_result(None) is None
    assert normalize_validation_result("") == ""
    assert normalize_validation_result(Result.ok(None)) is None
    assert normalize_validation_result(False) == "Validation failed: typed validator returned false"
    assert normalize_validation_result(Result.ok(True)) == (
        "Validation passed: typed validator returned true"
    )


def test_validation_errors_remain_explicit() -> None:
    result = Result.err(ValidationError("validator failed"))

    assert normalize_validation_result(result) == "Validation error: validator failed"


def test_validation_success_requires_an_explicit_pass_receipt() -> None:
    assert validation_passed("Validation passed: typed validator returned true")
    assert validation_passed("Validation passed (attempt 1/3)")
    assert validation_passed("Validation passed after 3 fix attempts")
    assert not validation_passed(None)
    assert not validation_passed("Validation fix failed (attempt 1): timeout")
    assert not validation_passed("Validation: no fixable errors detected (exit code 2)")


@pytest.mark.parametrize(
    "output",
    (
        "Validation passed: false",
        "Validation passed but 3 errors remain",
        "Validation passed\nValidation error: tests failed",
        "Validation passed (attempt 4/3)",
        "Validation passed (attempt 1/4)",
        "Validation passed (attempt 1/3)\nValidation error: tests failed",
        "Validation passed after 4 fix attempts",
        "Validation passed after 3 fix attempts but tests failed",
        " Validation passed (attempt 1/3)",
    ),
)
def test_validation_success_receipt_rejects_ambiguous_or_error_bearing_text(output: str) -> None:
    assert not validation_passed(output)
