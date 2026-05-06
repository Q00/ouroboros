"""Canonical backend capability registry.

The same backend names show up in CLI help, config validation, provider
factory selection, and runtime construction.  Keep those names and aliases in
one place so adding a backend does not require updating several independent
sets.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackendCapability:
    """Capabilities and aliases for one canonical backend."""

    name: str
    aliases: tuple[str, ...] = ()
    supports_runtime: bool = False
    supports_llm: bool = False
    supports_interview_driver: bool = False
    switchable_runtime: bool = False
    cli_name: str | None = None
    cli_config_key: str | None = None
    soft_tool_enforcement: bool = False
    supports_tool_envelope: bool = True

    @property
    def names(self) -> tuple[str, ...]:
        """Canonical name plus accepted aliases."""
        return (self.name, *self.aliases)


_CAPABILITIES: tuple[BackendCapability, ...] = (
    BackendCapability(
        name="claude",
        aliases=("claude_code",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        switchable_runtime=True,
        cli_name="claude",
        cli_config_key="cli_path",
    ),
    BackendCapability(
        name="codex",
        aliases=("codex_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        switchable_runtime=True,
        cli_name="codex",
        cli_config_key="codex_cli_path",
    ),
    BackendCapability(
        name="copilot",
        aliases=("copilot_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        cli_name="copilot",
        cli_config_key="copilot_cli_path",
    ),
    BackendCapability(
        name="gemini",
        aliases=("gemini_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        switchable_runtime=True,
        cli_name="gemini",
        cli_config_key="gemini_cli_path",
        soft_tool_enforcement=True,
    ),
    BackendCapability(
        name="hermes",
        aliases=("hermes_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        switchable_runtime=True,
        cli_name="hermes",
        cli_config_key="hermes_cli_path",
        supports_tool_envelope=False,
    ),
    BackendCapability(
        name="kiro",
        aliases=("kiro_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        cli_name="kiro",
        cli_config_key="kiro_cli_path",
    ),
    BackendCapability(
        name="opencode",
        aliases=("opencode_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        cli_name="opencode",
        cli_config_key="opencode_cli_path",
        soft_tool_enforcement=True,
    ),
    BackendCapability(
        name="litellm",
        aliases=("openai", "openrouter"),
        supports_llm=True,
        supports_interview_driver=False,
    ),
)

_BY_NAME: dict[str, BackendCapability] = {
    name: capability for capability in _CAPABILITIES for name in capability.names
}


def get_backend_capability(name: str) -> BackendCapability | None:
    """Return capability metadata for a canonical backend name or alias."""
    return _BY_NAME.get(name.strip().lower())


def resolve_backend_alias(name: str) -> str:
    """Resolve a backend alias to its canonical name."""
    capability = get_backend_capability(name)
    if capability is None:
        msg = f"Unsupported backend: {name.strip().lower()}"
        raise ValueError(msg)
    return capability.name


def _resolve_capable_backend(name: str, *, capability_name: str) -> str:
    candidate = name.strip().lower()
    capability = get_backend_capability(candidate)
    if capability is None or not getattr(capability, capability_name):
        msg = f"Unsupported backend for {capability_name.removeprefix('supports_')}: {candidate}"
        raise ValueError(msg)
    return capability.name


def resolve_runtime_backend_name(name: str) -> str:
    """Resolve and validate a backend that can run agent tasks."""
    return _resolve_capable_backend(name, capability_name="supports_runtime")


def resolve_llm_backend_name(name: str) -> str:
    """Resolve and validate a backend that can produce LLM completions."""
    return _resolve_capable_backend(name, capability_name="supports_llm")


def resolve_interview_driver_backend(name: str) -> str:
    """Resolve and validate a backend usable as an auto interview driver."""
    return _resolve_capable_backend(name, capability_name="supports_interview_driver")


def _choices(*, capability_name: str, include_aliases: bool = False) -> tuple[str, ...]:
    values: list[str] = []
    for capability in _CAPABILITIES:
        if getattr(capability, capability_name):
            values.extend(capability.names if include_aliases else (capability.name,))
    return tuple(values)


def runtime_backend_choices(*, include_aliases: bool = False) -> tuple[str, ...]:
    """Backend names that support orchestrator runtime execution."""
    return _choices(capability_name="supports_runtime", include_aliases=include_aliases)


def llm_backend_choices(*, include_aliases: bool = False) -> tuple[str, ...]:
    """Backend names that support LLM completion."""
    return _choices(capability_name="supports_llm", include_aliases=include_aliases)


def interview_driver_backend_choices(*, include_aliases: bool = False) -> tuple[str, ...]:
    """Backend names that can answer auto interview questions."""
    return _choices(
        capability_name="supports_interview_driver",
        include_aliases=include_aliases,
    )


def soft_tool_enforcement_backends() -> frozenset[str]:
    """Canonical backends whose tool envelope is cooperatively enforced."""
    return frozenset(c.name for c in _CAPABILITIES if c.soft_tool_enforcement)


def backend_supports_tool_envelope(name: str | None) -> bool:
    """Return whether a backend accepts an engine-owned tool envelope."""
    if name is None:
        return True
    capability = get_backend_capability(name)
    return True if capability is None else capability.supports_tool_envelope


__all__ = [
    "BackendCapability",
    "backend_supports_tool_envelope",
    "get_backend_capability",
    "interview_driver_backend_choices",
    "llm_backend_choices",
    "resolve_backend_alias",
    "resolve_interview_driver_backend",
    "resolve_llm_backend_name",
    "resolve_runtime_backend_name",
    "runtime_backend_choices",
    "soft_tool_enforcement_backends",
]
