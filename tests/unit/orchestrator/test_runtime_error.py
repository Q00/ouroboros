"""Unit tests for runtime subprocess-failure classification.

These cover the typed-error contract that lets the orchestrator's recoverable-
failure classifier recognize provider usage-limit windows surfaced by CLI
runtimes (hermes, gemini-cli, ...) as a non-zero process exit plus free-form
text. See ``docs/rca-june-2026.md`` (P0).
"""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.runtime_error import classify_subprocess_failure

# The exact failure text that hard-failed the June 2026 run (issue 1.1).
_ZAI_429_TEXT = (
    "API call failed after 3 retries:\n"
    "HTTP 429: Usage limit reached for 5 hour.\n"
    "Your limit will reset at 2026-06-07 21:41:18"
)


class TestClassifySubprocessFailure:
    """Behaviour of ``classify_subprocess_failure``."""

    def test_preserves_subtype_and_exit_code(self) -> None:
        # Arrange / Act
        metadata = classify_subprocess_failure("boom", exit_code=2)

        # Assert
        assert metadata["subtype"] == "error"
        assert metadata["exit_code"] == 2

    def test_zai_usage_limit_429_is_typed_as_rate_limit(self) -> None:
        # Act
        metadata = classify_subprocess_failure(_ZAI_429_TEXT, exit_code=1)

        # Assert
        assert metadata["error_type"] == "RateLimitError"
        assert metadata["http_status"] == 429

    def test_always_sets_error_type_so_failure_has_runtime_shape(self) -> None:
        # A plain failure must still carry a typed error_type so the
        # orchestrator recognizes it as runtime-owned (the bug was the
        # absence of any shape key).
        metadata = classify_subprocess_failure(
            "Hermes exited with code 1",
            exit_code=1,
        )

        assert metadata["error_type"] == "RuntimeExecutionError"

    def test_non_rate_limit_failure_has_no_http_status(self) -> None:
        metadata = classify_subprocess_failure(
            "Traceback: ValueError: bad config",
            exit_code=1,
        )

        assert "http_status" not in metadata
        assert metadata["error_type"] == "RuntimeExecutionError"

    def test_detects_rate_limit_phrase_without_status_code(self) -> None:
        metadata = classify_subprocess_failure(
            "Error: rate limit exceeded, slow down",
            exit_code=1,
        )

        assert metadata["error_type"] == "RateLimitError"

    def test_extracts_http_500(self) -> None:
        metadata = classify_subprocess_failure(
            "HTTP 500: internal server error",
            exit_code=1,
        )

        assert metadata["http_status"] == 500
        assert metadata["error_type"] == "RuntimeExecutionError"

    def test_does_not_mistake_timestamps_or_durations_for_status(self) -> None:
        # "5 hour" and "21:41:18" must not be read as HTTP status codes.
        metadata = classify_subprocess_failure(
            "Please try again in 5 hour, around 21:41:18",
            exit_code=1,
        )

        assert "http_status" not in metadata

    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_empty_text_still_yields_typed_metadata(self, text: str) -> None:
        metadata = classify_subprocess_failure(text, exit_code=137)

        assert metadata["subtype"] == "error"
        assert metadata["exit_code"] == 137
        assert metadata["error_type"] == "RuntimeExecutionError"
