"""Tests for shared backend factory dispatch metadata."""

from __future__ import annotations

from ouroboros.backends import (
    backend_factory_specs,
    get_backend_factory_spec,
    llm_backend_choices,
    runtime_backend_choices,
)


def test_factory_registry_covers_every_backend_capability() -> None:
    llm_factory_backends = {
        spec.name for spec in backend_factory_specs() if spec.llm_adapter_factory is not None
    }
    runtime_factory_backends = {
        spec.name for spec in backend_factory_specs() if spec.agent_runtime_factory is not None
    }

    assert set(llm_backend_choices()) <= llm_factory_backends
    assert set(runtime_backend_choices()) <= runtime_factory_backends


def test_factory_registry_resolves_factory_specific_backend_names() -> None:
    assert (
        get_backend_factory_spec("claude_code", kind="llm").llm_adapter_factory
        == "_create_claude_code_adapter"
    )
    assert (
        get_backend_factory_spec("claude", kind="runtime").agent_runtime_factory
        == "_create_claude_runtime"
    )
    assert get_backend_factory_spec("litellm", kind="runtime") is None
    assert get_backend_factory_spec("codex_mcp", kind="llm") is None
