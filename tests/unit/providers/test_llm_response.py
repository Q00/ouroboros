"""Tests for the shared oversized-LLM-response guard.

Per #1787 this lived as byte-identical copies in `codex_cli_adapter` and
`copilot_cli_adapter`. A response-size limit is hardening, so two copies mean
a fix can protect one adapter and not the other.
"""

from __future__ import annotations

import importlib

import pytest

from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH
from ouroboros.providers.llm_response import truncate_llm_response_if_oversized


def test_response_within_the_limit_is_returned_unchanged() -> None:
    content = "ok" * 100
    assert truncate_llm_response_if_oversized(content, model="m") == content


def test_oversized_response_is_clamped() -> None:
    content = "x" * (MAX_LLM_RESPONSE_LENGTH + 500)

    result = truncate_llm_response_if_oversized(content, model="m")

    assert len(result) == MAX_LLM_RESPONSE_LENGTH
    assert result == content[:MAX_LLM_RESPONSE_LENGTH]


def test_response_exactly_at_the_limit_is_not_clamped() -> None:
    """`validate_llm_response` rejects only what *exceeds* the maximum."""
    content = "x" * MAX_LLM_RESPONSE_LENGTH
    assert truncate_llm_response_if_oversized(content, model="m") == content


def test_empty_response_passes_through() -> None:
    assert truncate_llm_response_if_oversized("", model="m") == ""


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        pytest.param("codex_cli_adapter", "CodexCliLLMAdapter", id="codex"),
        pytest.param("copilot_cli_adapter", "CopilotCliLLMAdapter", id="copilot"),
    ],
)
class TestAdaptersShareTheGuard:
    @staticmethod
    def _call(module_name: str, class_name: str, content: str) -> str:
        module = importlib.import_module(f"ouroboros.providers.{module_name}")
        cls = getattr(module, class_name)
        result = cls._truncate_if_oversized(content, "some-model")
        assert isinstance(result, str)
        return result

    def test_adapter_clamps_oversized_content(self, module_name: str, class_name: str) -> None:
        content = "x" * (MAX_LLM_RESPONSE_LENGTH + 1)
        assert len(self._call(module_name, class_name, content)) == MAX_LLM_RESPONSE_LENGTH

    def test_adapter_leaves_small_content_alone(self, module_name: str, class_name: str) -> None:
        assert self._call(module_name, class_name, "short") == "short"
