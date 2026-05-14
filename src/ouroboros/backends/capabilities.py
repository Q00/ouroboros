"""Canonical backend capability registry.

The same backend names show up in CLI help, config validation, provider
factory selection, and runtime construction.  Keep those names and aliases in
one place so adding a backend does not require updating several independent
sets.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SkillExecutionCapability:
    """Runtime-specific guidance for one abstract skill capability."""

    name: str
    guidance: str


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
    skill_execution_capabilities: tuple[SkillExecutionCapability, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Canonical name plus accepted aliases."""
        return (self.name, *self.aliases)


_CODEX_SKILL_EXECUTION_CAPABILITIES: tuple[SkillExecutionCapability, ...] = (
    SkillExecutionCapability(
        name="ask_user",
        guidance=(
            "Ask directly in the main Codex conversation. Use `request_user_input` "
            "only when that structured input tool is available; otherwise ask one "
            "concise question and wait for the answer."
        ),
    ),
    SkillExecutionCapability(
        name="inspect_code",
        guidance=(
            "Use local repository inspection with `rg`, `find`, `sed`, `cat`, and "
            "`exec_command`; prefer reading exact files over guessing from memory."
        ),
    ),
    SkillExecutionCapability(
        name="call_mcp",
        guidance=(
            "Use available Ouroboros MCP tools directly when exposed, and use "
            "Codex tool discovery only when a deferred MCP surface must be loaded. "
            "Do not rely on Claude-specific `ToolSearch` names."
        ),
    ),
    SkillExecutionCapability(
        name="web_research",
        guidance=(
            "Use Codex web/search tooling only when current external evidence is "
            "required; cite sources and keep repo-local facts grounded in local files."
        ),
    ),
    SkillExecutionCapability(
        name="run_shell",
        guidance=(
            "Use `exec_command` for safe local shell commands, keep commands scoped, "
            "and avoid destructive or external-production actions unless explicitly authorized."
        ),
    ),
    SkillExecutionCapability(
        name="refine_answer",
        guidance=(
            "When a free-text answer carries scope, constraints, or decisions, restate "
            "the structured interpretation and get confirmation before treating it as settled."
        ),
    ),
    SkillExecutionCapability(
        name="maintain_ledger",
        guidance=(
            "Keep the ambiguity or acceptance ledger visible in concise updates; do not "
            "hide unresolved gates inside MCP state alone."
        ),
    ),
    SkillExecutionCapability(
        name="run_closure_gate",
        guidance=(
            "Treat MCP `seed-ready` as permission to audit closure in the main session, "
            "not as completion by itself. Verify non-goals, constraints, acceptance criteria, "
            "and unresolved decisions before moving on."
        ),
    ),
    SkillExecutionCapability(
        name="restate_goal",
        guidance=(
            "Before suggesting or running seed generation, restate the goal, non-goals, "
            "constraints, and acceptance criteria, then require explicit user approval."
        ),
    ),
)

_GENERIC_SKILL_EXECUTION_CAPABILITIES: tuple[SkillExecutionCapability, ...] = (
    SkillExecutionCapability(
        name="ask_user",
        guidance="Use the runtime's native structured question surface when available; otherwise ask one concise question and wait.",
    ),
    SkillExecutionCapability(
        name="inspect_code",
        guidance="Use the runtime's local file search/read tools and prefer exact repository evidence over inference.",
    ),
    SkillExecutionCapability(
        name="call_mcp",
        guidance="Call available Ouroboros MCP tools through the runtime's MCP/tool surface instead of emulating MCP workflows manually.",
    ),
    SkillExecutionCapability(
        name="web_research",
        guidance="Use the runtime's web/search capability only when current external facts are required, and cite the sources used.",
    ),
    SkillExecutionCapability(
        name="run_shell",
        guidance="Use the runtime's bounded local shell capability for safe repository/version checks; avoid destructive commands unless explicitly authorized.",
    ),
    SkillExecutionCapability(
        name="refine_answer",
        guidance="Confirm structured interpretations of free-text decisions before forwarding them to workflow state.",
    ),
    SkillExecutionCapability(
        name="maintain_ledger",
        guidance="Keep ambiguity, gates, and unresolved decisions visible in the main session rather than hiding them only in tool state.",
    ),
    SkillExecutionCapability(
        name="run_closure_gate",
        guidance="Audit required client-side gates even when an MCP response says the workflow is ready to proceed.",
    ),
    SkillExecutionCapability(
        name="restate_goal",
        guidance="Restate the goal and require explicit approval before irreversible workflow transitions such as seed generation.",
    ),
)

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
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
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
        skill_execution_capabilities=_CODEX_SKILL_EXECUTION_CAPABILITIES,
    ),
    BackendCapability(
        name="copilot",
        aliases=("copilot_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        cli_name="copilot",
        cli_config_key="copilot_cli_path",
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
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
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
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
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
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
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
    ),
    BackendCapability(
        name="opencode",
        aliases=("opencode_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        cli_name="opencode",
        cli_config_key="opencode_cli_path",
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
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


def render_backend_skill_capability_guide(name: str) -> str:
    """Render runtime-specific guidance for abstract skill capabilities as Markdown."""
    capability = get_backend_capability(name)
    if capability is None:
        msg = f"Unsupported backend: {name.strip().lower()}"
        raise ValueError(msg)

    lines = [f"## Ouroboros Skill Capability Guide: {capability.name.title()}", ""]
    if not capability.skill_execution_capabilities:
        lines.append("No backend-specific skill execution capability guidance is registered yet.")
        return "\n".join(lines).rstrip() + "\n"

    for skill_capability in capability.skill_execution_capabilities:
        lines.extend(
            (
                f"### When a skill requires `{skill_capability.name}`",
                skill_capability.guidance,
                "",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


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
    "SkillExecutionCapability",
    "backend_supports_tool_envelope",
    "get_backend_capability",
    "interview_driver_backend_choices",
    "llm_backend_choices",
    "resolve_backend_alias",
    "render_backend_skill_capability_guide",
    "resolve_interview_driver_backend",
    "resolve_llm_backend_name",
    "resolve_runtime_backend_name",
    "runtime_backend_choices",
    "soft_tool_enforcement_backends",
]
