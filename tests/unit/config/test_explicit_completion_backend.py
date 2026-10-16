"""Explicit overrides must pass the same completion guard as configured routes."""

import os
from unittest.mock import patch

import pytest

from ouroboros.backends import get_backend_capability
from ouroboros.config.loader import get_llm_backend_for_role, get_llm_backend_for_stage
from ouroboros.config.models import LLMConfig, OuroborosConfig


@pytest.mark.parametrize(
    "resolve,key", [(get_llm_backend_for_role, "qa"), (get_llm_backend_for_stage, "evaluate")]
)
@pytest.mark.parametrize(
    "backend,expected",
    [("opencode", "opencode"), ("gemini", "gemini"), ("antigravity", "codex"), ("grok", "codex")],
)
def test_explicit_backend_selection_requires_completion_capability(resolve, key, backend, expected):
    config = OuroborosConfig(llm=LLMConfig(backend="codex"))
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("ouroboros.config.loader.load_config", return_value=config),
    ):
        selected = resolve(key, explicit_backend=backend)
    assert selected == expected
    capability = get_backend_capability(selected)
    assert capability is not None and capability.supports_llm
