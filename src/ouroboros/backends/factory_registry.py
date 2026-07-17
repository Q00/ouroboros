"""Shared backend-to-factory dispatch registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class BackendFactorySpec:
    """Factory dispatch metadata for one backend family."""

    name: str
    llm_backend: str | None = None
    runtime_backend: str | None = None
    llm_adapter_factory: str | None = None
    agent_runtime_factory: str | None = None


_FACTORY_SPECS: tuple[BackendFactorySpec, ...] = (
    BackendFactorySpec(
        name="claude",
        llm_backend="claude_code",
        runtime_backend="claude",
        llm_adapter_factory="_create_claude_code_adapter",
        agent_runtime_factory="_create_claude_runtime",
    ),
    BackendFactorySpec(
        name="codex",
        llm_backend="codex",
        runtime_backend="codex",
        llm_adapter_factory="_create_codex_adapter",
        agent_runtime_factory="_create_codex_runtime",
    ),
    BackendFactorySpec(
        name="codex_mcp",
        runtime_backend="codex_mcp",
        agent_runtime_factory="_create_codex_mcp_runtime",
    ),
    BackendFactorySpec(
        name="claude_mcp",
        runtime_backend="claude_mcp",
        agent_runtime_factory="_create_claude_mcp_runtime",
    ),
    BackendFactorySpec(
        name="copilot",
        llm_backend="copilot",
        runtime_backend="copilot",
        llm_adapter_factory="_create_copilot_adapter",
        agent_runtime_factory="_create_copilot_runtime",
    ),
    BackendFactorySpec(
        name="gemini",
        llm_backend="gemini",
        runtime_backend="gemini",
        llm_adapter_factory="_create_gemini_adapter",
        agent_runtime_factory="_create_gemini_runtime",
    ),
    BackendFactorySpec(
        name="hermes",
        llm_backend="hermes",
        runtime_backend="hermes",
        llm_adapter_factory="_create_hermes_adapter",
        agent_runtime_factory="_create_hermes_runtime",
    ),
    BackendFactorySpec(
        name="kiro",
        llm_backend="kiro",
        runtime_backend="kiro",
        llm_adapter_factory="_create_kiro_adapter",
        agent_runtime_factory="_create_kiro_runtime",
    ),
    BackendFactorySpec(
        name="opencode",
        llm_backend="opencode",
        runtime_backend="opencode",
        llm_adapter_factory="_create_opencode_adapter",
        agent_runtime_factory="_create_opencode_runtime",
    ),
    BackendFactorySpec(
        name="goose",
        llm_backend="goose",
        runtime_backend="goose",
        llm_adapter_factory="_create_goose_adapter",
        agent_runtime_factory="_create_goose_runtime",
    ),
    BackendFactorySpec(
        name="pi",
        llm_backend="pi",
        runtime_backend="pi",
        llm_adapter_factory="_create_pi_adapter",
        agent_runtime_factory="_create_pi_runtime",
    ),
    BackendFactorySpec(
        name="gjc",
        llm_backend="gjc",
        runtime_backend="gjc",
        llm_adapter_factory="_create_gjc_adapter",
        agent_runtime_factory="_create_gjc_runtime",
    ),
    BackendFactorySpec(
        name="antigravity",
        runtime_backend="antigravity",
        agent_runtime_factory="_create_antigravity_runtime",
    ),
    BackendFactorySpec(
        name="grok",
        runtime_backend="grok",
        agent_runtime_factory="_create_grok_runtime",
    ),
    BackendFactorySpec(
        name="zcode",
        llm_backend="zcode",
        runtime_backend="zcode",
        llm_adapter_factory="_create_zcode_adapter",
        agent_runtime_factory="_create_zcode_runtime",
    ),
    BackendFactorySpec(
        name="ourocode",
        llm_backend="ourocode",
        llm_adapter_factory="_create_ourocode_adapter",
    ),
    BackendFactorySpec(
        name="litellm",
        llm_backend="litellm",
        llm_adapter_factory="_create_litellm_adapter",
    ),
)

_LLM_FACTORY_BY_BACKEND = {
    spec.llm_backend: spec for spec in _FACTORY_SPECS if spec.llm_backend is not None
}
_RUNTIME_FACTORY_BY_BACKEND = {
    spec.runtime_backend: spec for spec in _FACTORY_SPECS if spec.runtime_backend is not None
}


def get_backend_factory_spec(
    backend: str,
    *,
    kind: Literal["llm", "runtime"],
) -> BackendFactorySpec | None:
    """Return factory metadata for a resolved backend name."""
    if kind == "llm":
        return _LLM_FACTORY_BY_BACKEND.get(backend)
    return _RUNTIME_FACTORY_BY_BACKEND.get(backend)


def backend_factory_specs() -> tuple[BackendFactorySpec, ...]:
    """Return all factory dispatch specs."""
    return _FACTORY_SPECS


__all__ = [
    "BackendFactorySpec",
    "backend_factory_specs",
    "get_backend_factory_spec",
]
