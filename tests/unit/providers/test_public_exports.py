"""Regression tests for provider package public exports."""

import ouroboros.providers as providers
from ouroboros.providers.pi_llm_adapter import PiLLMAdapter


def test_pi_llm_adapter_is_package_exported() -> None:
    assert providers.PiLLMAdapter is PiLLMAdapter
    assert "PiLLMAdapter" in providers.__all__
