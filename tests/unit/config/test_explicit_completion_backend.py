"""Explicit overrides must pass the same completion guard as configured routes."""

import os
from unittest.mock import patch

import pytest

from ouroboros.backends import get_backend_capability
from ouroboros.config.loader import get_llm_backend_for_role, get_llm_backend_for_stage
from ouroboros.config.models import LLMConfig, OuroborosConfig
from ouroboros.core.errors import ConfigError


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


@pytest.mark.parametrize(
    "resolve,key", [(get_llm_backend_for_role, "qa"), (get_llm_backend_for_stage, "evaluate")]
)
@pytest.mark.parametrize("environment_backend", ["antigravity", "grok"])
@pytest.mark.parametrize("explicit_backend", ["antigravity", "grok"])
def test_runtime_only_environment_fallback_uses_configured_completion_backend(
    resolve, key, environment_backend, explicit_backend
):
    config = OuroborosConfig(llm=LLMConfig(backend="codex"))
    with (
        patch.dict(os.environ, {"OUROBOROS_LLM_BACKEND": environment_backend}, clear=True),
        patch("ouroboros.config.loader.load_config", return_value=config),
    ):
        selected = resolve(key, explicit_backend=explicit_backend)
    assert selected == "codex"
    capability = get_backend_capability(selected)
    assert capability is not None and capability.supports_llm


def test_runtime_only_environment_fallback_uses_default_when_config_is_unavailable():
    with (
        patch.dict(os.environ, {"OUROBOROS_LLM_BACKEND": "antigravity"}, clear=True),
        patch("ouroboros.config.loader.load_config", side_effect=ConfigError("missing config")),
    ):
        for resolve, key in (
            (get_llm_backend_for_role, "qa"),
            (get_llm_backend_for_stage, "evaluate"),
        ):
            selected = resolve(key, explicit_backend="grok")
            assert selected == "claude_code"
            capability = get_backend_capability(selected)
            assert capability is not None and capability.supports_llm
