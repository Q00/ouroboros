"""Canonical backend capability registry.

The same backend names show up in CLI help, config validation, provider
factory selection, and runtime construction.  Keep those names and aliases in
one place so adding a backend does not require updating several independent
sets.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


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
    # ``supports_native_parallel_subagents``: a *passive* bridge receiver auto-
    # consumes ``_subagents`` envelopes (OpenCode plugin). ``supports_host_driven_
    # subagents``: the host *model* spawns subagents from inline payloads via its
    # own native primitive (e.g. Codex Desktop). These are different layers — a
    # backend can have the latter without the former. Do NOT conflate them.
    supports_native_parallel_subagents: bool = False
    supports_host_driven_subagents: bool = False
    host_driven_subagent_mechanism: SubagentSpawnTriggerMechanism | None = None
    host_driven_subagent_requires_explicit_request: bool = False
    host_driven_callable_spawn_tool_name: str | None = None
    prohibited_subagent_spawn_tool_names: tuple[str, ...] = ()
    # Tool discovery axis: how this runtime makes a *deferred* Ouroboros MCP
    # tool callable. ``tool_discovery_mechanism`` is the concrete per-runtime
    # translation of the abstract "load a deferred tool by query" concept;
    # ``tool_discovery_tool_name`` names the callable discovery tool when the
    # runtime exposes one (Claude → ``ToolSearch``). ``None`` mechanism means
    # the runtime exposes tools directly and needs no discovery step.
    tool_discovery_mechanism: ToolDiscoveryMechanism | None = None
    tool_discovery_tool_name: str | None = None
    skill_execution_capabilities: tuple[SkillExecutionCapability, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """Canonical name plus accepted aliases."""
        return (self.name, *self.aliases)


class SubagentDispatchMode(StrEnum):
    """How a handler should surface subagent fan-out for the active runtime.

    Three distinct layers, not a boolean:

    - ``PLUGIN_PASSIVE``: a passive bridge receiver (the OpenCode plugin) auto-
      intercepts the ``_subagents`` envelope and spawns children. The handler
      returns the envelope and skips the real in-process work.
    - ``HOST_DRIVEN``: there is no passive receiver, but the host *model* can
      spawn subagents from inline payloads via its own native primitive (e.g.
      Codex Desktop's multi-agent spawn). The handler returns the inline result
      plus an explicit ``dispatch_mode=host_driven`` / ``host_action`` stamp so
      the host deterministically fans out.
    - ``SEQUENTIAL``: neither a passive receiver nor a native parallel primitive
      is available; the handler runs the in-process / inline sequential path.
    """

    PLUGIN_PASSIVE = "plugin_passive"
    HOST_DRIVEN = "host_driven"
    SEQUENTIAL = "sequential"


class SubagentSpawnTriggerMechanism(StrEnum):
    """Concrete runtime mechanism used after dispatch mode is resolved."""

    PASSIVE_BRIDGE_ENVELOPE = "passive_bridge_envelope"
    CODEX_NATURAL_LANGUAGE_DELEGATION = "codex_natural_language_delegation"
    CLAUDE_TASK_AGENT_TOOL = "claude_task_agent_tool"
    SEQUENTIAL_FALLBACK = "sequential_fallback"


class ToolDiscoveryMechanism(StrEnum):
    """Concrete per-runtime mechanism for loading a *deferred* MCP tool.

    "Tool discovery" is the ubiquitous concept: an Ouroboros MCP tool may ship
    with its schema deferred (not loaded into context), so a runtime must load
    it by a discovery query before it becomes callable. Each runtime translates
    that one concept differently:

    - ``DEFERRED_TOOL_SEARCH``: the runtime exposes a callable discovery tool
      that loads deferred schemas on demand (Claude Code's ``ToolSearch``).
    - ``NATIVE_RUNTIME_DISCOVERY``: the runtime has its own (non-``ToolSearch``)
      discovery surface it must drive when a deferred tool must be loaded
      (Codex).
    - ``DIRECT_EXPOSURE``: tools are always exposed; no discovery step exists
      and calling directly is correct (OpenCode plugin and sequential runtimes).
    """

    DEFERRED_TOOL_SEARCH = "deferred_tool_search"
    NATIVE_RUNTIME_DISCOVERY = "native_runtime_discovery"
    DIRECT_EXPOSURE = "direct_exposure"


@dataclass(frozen=True, slots=True)
class RuntimeSubagentOrchestrationContract:
    """Runtime handling contract for MCP subagent directive metadata."""

    backend_name: str
    supports_native_parallel_subagents: bool
    dispatch_mode: str
    spawn_trigger_mechanism: str
    requires_explicit_spawn_request: bool
    callable_spawn_tool_name: str | None
    prohibited_spawn_tool_names: tuple[str, ...]
    mcp_directive_keys: tuple[str, ...]
    sequential_fallback: Mapping[str, Any]
    runtime_instruction_handling: str
    callable_mcp_tool_capabilities: tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the contract for runtime envelopes and tests."""
        return {
            "backend_name": self.backend_name,
            "supports_native_parallel_subagents": self.supports_native_parallel_subagents,
            "dispatch_mode": self.dispatch_mode,
            "spawn_trigger_mechanism": self.spawn_trigger_mechanism,
            "requires_explicit_spawn_request": self.requires_explicit_spawn_request,
            "callable_spawn_tool_name": self.callable_spawn_tool_name,
            "prohibited_spawn_tool_names": list(self.prohibited_spawn_tool_names),
            "mcp_directive_keys": list(self.mcp_directive_keys),
            "sequential_fallback": dict(self.sequential_fallback),
            "runtime_instruction_handling": self.runtime_instruction_handling,
            "callable_mcp_tool_capabilities": [
                dict(capability) for capability in self.callable_mcp_tool_capabilities
            ],
        }


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
            "Use available Ouroboros MCP tools directly when exposed. When a "
            "deferred Ouroboros tool must be loaded, use Codex's own "
            "tool-discovery surface (not another runtime's discovery tool) with "
            "the `+ouroboros <skill>` query, then call the tool by its full "
            "name. Keep this routing internal: when a tool is already exposed, "
            "call it directly without running discovery, and never narrate the "
            "deferred-tool / tool-discovery plumbing to the user. An empty "
            "discovery result for an already-available tool is an expected "
            "no-op, not a failure, warning, or uncertainty — do not surface it."
        ),
    ),
    SkillExecutionCapability(
        name="run_lateral_review",
        guidance=(
            "When an interview response marks `lateral_review_required=true`, call "
            "`ouroboros_lateral_think` with the supplied `lateral_review_tool_args` "
            "before routing the next interview turn. For direct-answer synthesis, "
            "run a lightweight multi-perspective review with researcher, contrarian, "
            "and simplifier, then present only the actionable options or draft answer."
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
    SkillExecutionCapability(
        name="orchestrate_subagents",
        guidance=(
            "Codex has a native subagent primitive but no passive Ouroboros bridge, "
            "so subagent fan-out is host-driven. When an Ouroboros MCP tool returns "
            "`host_action=spawn_subagents` (via `dispatch_mode=host_driven` or "
            "`question_advisory_dispatch_mode=host_driven` in the result `meta`, or "
            "the `ouroboros-lateral-inline-dispatch-v1` content block), do NOT just "
            "read the inline text: spawn one subagent per entry in the payload array "
            "(`payloads` or `question_advisory_subagents`) using Codex's native "
            "subagent workflow. Codex subagents are triggered by an explicit "
            "natural-language delegation, not by a callable tool name: explicitly "
            "spawn one Codex subagent per payload, give each child the payload's "
            "`prompt`, wait for all children, then summarize the results. Correlate "
            "every child result by the "
            "payload-specific key named in the result `meta` "
            "(`result_correlation_key`): lateral payloads use `context.persona`, "
            "interview advisory payloads use `context.lane_id` (their `persona` is "
            "absent on some lanes). Collect them, then synthesise — preserving the "
            "user-facing content. If you have no parallel primitive available, "
            "process the payloads sequentially instead."
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
        name="run_lateral_review",
        guidance=(
            "When an interview response marks `lateral_review_required=true`, call "
            "`ouroboros_lateral_think` with the supplied `lateral_review_tool_args` "
            "before routing the next interview turn. When directly synthesizing an "
            "answer for the user, run researcher, contrarian, and simplifier "
            "perspectives first, then collapse the result into concise choices or a "
            "recommended draft."
        ),
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
    SkillExecutionCapability(
        name="orchestrate_subagents",
        guidance=(
            "Process each payload in order. Correlate results by the key named "
            "in `result_correlation_key`. Synthesize after the last payload."
        ),
    ),
)
_GENERIC_SKILL_EXECUTION_CAPABILITIES_WITHOUT_SUBAGENTS: tuple[SkillExecutionCapability, ...] = (
    tuple(
        capability
        for capability in _GENERIC_SKILL_EXECUTION_CAPABILITIES
        if capability.name != "orchestrate_subagents"
    )
)

_CLAUDE_SKILL_EXECUTION_CAPABILITIES: tuple[SkillExecutionCapability, ...] = (
    *_GENERIC_SKILL_EXECUTION_CAPABILITIES_WITHOUT_SUBAGENTS,
    SkillExecutionCapability(
        name="orchestrate_subagents",
        guidance=(
            "Claude Code has a native Task/Agent subagent primitive but no "
            "passive Ouroboros bridge, so subagent fan-out is host-driven. "
            "When an Ouroboros MCP tool returns inline payloads stamped with "
            "`dispatch_mode=host_driven` / `host_action=spawn_subagents`, or "
            "when a skill provides spawn-ready payloads such as "
            "`question_advisory_subagents`, spawn one Task/Agent subagent per "
            "payload in one batch, passing each payload's `prompt`. Correlate "
            "results by the payload-specific `result_correlation_key` when "
            "present, then synthesize in the parent session. If the Task/Agent "
            "primitive is unavailable, follow the sequential fallback."
        ),
    ),
)

_OPENCODE_SKILL_EXECUTION_CAPABILITIES: tuple[SkillExecutionCapability, ...] = (
    *_GENERIC_SKILL_EXECUTION_CAPABILITIES_WITHOUT_SUBAGENTS,
    SkillExecutionCapability(
        name="orchestrate_subagents",
        guidance=(
            "Use OpenCode's native task/subagent primitive for parallel-capable "
            "Ouroboros subagent fan-out, including `_subagent` and `_subagents` "
            "MCP directive payloads. Preserve the sequential fallback described "
            "by the MCP capability metadata when native parallel dispatch is unavailable."
        ),
    ),
)

# Zcode (Z.ai GLM desktop CLI) — a complete, measured skill-execution matrix
# rather than the generic defaults. Each capability below maps onto a Zcode
# mechanism verified against `zcode --help`, `zcode skills list`, and captured
# `--prompt --json` summaries. Two generic capabilities are intentionally
# omitted because the runtime cannot truthfully provide them on the headless
# `--prompt` path the orchestrator drives:
#   - ``ask_user``: ``zcode --prompt`` is a headless one-shot with no TTY and
#     no user turn-taking; the TUI exposes interaction, but the orchestrator
#     runtime never enters it. Declaring it would overstate the surface.
#   - ``orchestrate_subagents``: Zcode has no native parallel subagent
#     primitive (``/expert`` is a sequential workflow); subagent dispatch
#     resolves to the sequential contract, so it is not declared as a
#     capability here.
_ZCODE_SKILL_EXECUTION_CAPABILITIES: tuple[SkillExecutionCapability, ...] = (
    SkillExecutionCapability(
        name="inspect_code",
        guidance=(
            "Zcode reads files with its built-in tools under the active `--mode` "
            "(build = read-only, edit = read/write, yolo = full). Prefer its "
            "local file search/read over inference."
        ),
    ),
    SkillExecutionCapability(
        name="call_mcp",
        guidance=(
            "Zcode exposes MCP via the `/mcp` slash surface and the stdio "
            "`app-server`; call Ouroboros MCP tools through that surface rather "
            "than emulating MCP workflows manually."
        ),
    ),
    SkillExecutionCapability(
        name="run_lateral_review",
        guidance=(
            "Within a single `--prompt` turn, run researcher, contrarian, and "
            "simplifier perspectives before collapsing the result into concise "
            "choices or a recommended draft."
        ),
    ),
    SkillExecutionCapability(
        name="web_research",
        guidance=(
            "Zcode reports `webFetchRequests`/`webSearchRequests` in its JSON "
            "summary; use its web capability only when current external facts "
            "are required, and cite the sources used."
        ),
    ),
    SkillExecutionCapability(
        name="run_shell",
        guidance=(
            "Zcode gates shell execution by `--mode`; destructive commands "
            "require `yolo`. Avoid destructive commands unless explicitly "
            "authorized."
        ),
    ),
    SkillExecutionCapability(
        name="refine_answer",
        guidance=(
            "Confirm structured interpretations of free-text decisions before "
            "forwarding them to workflow state."
        ),
    ),
    SkillExecutionCapability(
        name="maintain_ledger",
        guidance=(
            "Keep ambiguity, gates, and unresolved decisions visible in the "
            "session; Zcode's `/compact [instructions]` preserves intent across "
            "compaction."
        ),
    ),
    SkillExecutionCapability(
        name="run_closure_gate",
        guidance=(
            "Audit required client-side gates even when an MCP response says "
            "the workflow is ready to proceed."
        ),
    ),
    SkillExecutionCapability(
        name="restate_goal",
        guidance=(
            "Zcode holds the session goal natively (`--target`, `/goal`) rather "
            "than only in prompt text; restating the goal maps onto that held "
            "goal so Zcode can reference it across the run."
        ),
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
        skill_execution_capabilities=_CLAUDE_SKILL_EXECUTION_CAPABILITIES,
        supports_host_driven_subagents=True,
        host_driven_subagent_mechanism=SubagentSpawnTriggerMechanism.CLAUDE_TASK_AGENT_TOOL,
        host_driven_subagent_requires_explicit_request=True,
        host_driven_callable_spawn_tool_name="Task/Agent",
        tool_discovery_mechanism=ToolDiscoveryMechanism.DEFERRED_TOOL_SEARCH,
        tool_discovery_tool_name="ToolSearch",
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
        supports_host_driven_subagents=True,
        host_driven_subagent_mechanism=(
            SubagentSpawnTriggerMechanism.CODEX_NATURAL_LANGUAGE_DELEGATION
        ),
        host_driven_subagent_requires_explicit_request=True,
        host_driven_callable_spawn_tool_name=None,
        prohibited_subagent_spawn_tool_names=("multi_agent_v1.spawn_agent",),
        tool_discovery_mechanism=ToolDiscoveryMechanism.NATIVE_RUNTIME_DISCOVERY,
    ),
    # Leader-driven worker runtime over ``codex mcp-server`` (codex/codex-reply).
    # Same codex binary, runtime-only (not an LLM/interview backend); declares
    # EXTERNAL_LEADER_DRIVEN at the runtime layer. See orchestrator.codex_mcp_runtime.
    BackendCapability(
        name="codex_mcp",
        aliases=("codex_mcp_server",),
        supports_runtime=True,
        cli_name="codex",
        cli_config_key="codex_cli_path",
        skill_execution_capabilities=_CODEX_SKILL_EXECUTION_CAPABILITIES,
    ),
    # Leader-driven worker runtime over ``claude -p --resume`` (stream-json).
    # Same claude binary, runtime-only; declares EXTERNAL_LEADER_DRIVEN. Proves
    # the worker pool is provider-neutral. See orchestrator.claude_worker_runtime.
    BackendCapability(
        name="claude_mcp",
        aliases=("claude_worker",),
        supports_runtime=True,
        cli_name="claude",
        cli_config_key="cli_path",
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
        # Zcode (Z.ai GLM-5.x desktop agent) — the app-bundle ``zcode.cjs``
        # script runs through ZCode's bundled Electron/Node runtime and emits
        # one JSON summary. The LLM adapter consumes that same measured summary
        # contract for structured completions and interview answering.
        name="zcode",
        aliases=("zcode_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        switchable_runtime=True,
        cli_name="zcode",
        cli_config_key="zcode_cli_path",
        skill_execution_capabilities=_ZCODE_SKILL_EXECUTION_CAPABILITIES,
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
        cli_name="kiro-cli",
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
        skill_execution_capabilities=_OPENCODE_SKILL_EXECUTION_CAPABILITIES,
        soft_tool_enforcement=True,
        supports_native_parallel_subagents=True,
        tool_discovery_mechanism=ToolDiscoveryMechanism.DIRECT_EXPOSURE,
    ),
    BackendCapability(
        name="goose",
        aliases=("goose_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        switchable_runtime=True,
        cli_name="goose",
        cli_config_key="goose_cli_path",
        soft_tool_enforcement=True,
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
    ),
    BackendCapability(
        name="pi",
        aliases=("pi_cli",),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        switchable_runtime=True,
        cli_name="pi",
        cli_config_key="pi_cli_path",
        supports_tool_envelope=False,
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
    ),
    BackendCapability(
        name="gjc",
        aliases=("gajae-code", "gajae_code"),
        supports_runtime=True,
        supports_llm=True,
        supports_interview_driver=True,
        switchable_runtime=True,
        cli_name="gjc",
        cli_config_key="gjc_cli_path",
        supports_tool_envelope=False,
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
    ),
    BackendCapability(
        # Antigravity is Google's successor to the Gemini CLI (the ``agy``
        # binary). Gemini CLI stops serving the Pro/Ultra/free consumer tiers
        # on 2026-06-18 and migrates those users to Antigravity. Runtime-only:
        # ``agy -p`` prints a plain-text response (no ``--output-format``), so
        # it drives the agentic orchestrator runtime but does not back
        # structured LLM completions or auto-interview answering.
        name="antigravity",
        aliases=("agy",),
        supports_runtime=True,
        supports_llm=False,
        supports_interview_driver=False,
        switchable_runtime=True,
        cli_name="agy",
        cli_config_key="antigravity_cli_path",
        supports_tool_envelope=False,
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
    ),
    BackendCapability(
        # Grok Build (xAI) — the ``grok`` binary. Runtime-only: ``grok -p
        # --output-format streaming-json`` drives the agentic orchestrator
        # runtime. Auth is subscription OAuth (``grok login`` with SuperGrok /
        # X Premium+) or ``XAI_API_KEY``. Grok owns its own model catalog
        # (``grok models``), so it is a sentinel-model backend.
        name="grok",
        aliases=("grok_cli", "grok_build"),
        supports_runtime=True,
        supports_llm=False,
        supports_interview_driver=False,
        switchable_runtime=True,
        cli_name="grok",
        cli_config_key="grok_cli_path",
        supports_tool_envelope=False,
        skill_execution_capabilities=_GENERIC_SKILL_EXECUTION_CAPABILITIES,
    ),
    BackendCapability(
        # ourocode is an LLM-completion backend only: it streams a single Claude
        # turn over ACP (`ourocode --acp`, its OAuth `:claude_api`) with no tool
        # use, so it backs completions (interview/seed/qa/evaluate) but NOT the
        # agentic orchestrator runtime or interview driving. SDK-free and
        # `claude -p`-free Claude.
        name="ourocode",
        supports_runtime=False,
        supports_llm=True,
        supports_interview_driver=False,
        switchable_runtime=False,
        cli_name="ourocode",
        cli_config_key="ourocode_cli_path",
        supports_tool_envelope=False,
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


def render_mcp_server_instructions() -> str:
    """Render the provider-neutral 'ubiquitous language' for every MCP client.

    Delivered via the MCP server ``instructions`` field, which Claude Code and
    other MCP hosts inject into the model's context at session start. This is
    the one channel every provider reads symmetrically, so the text stays
    runtime-neutral — it names the *concepts* (tool discovery, subagent fan-out)
    and the shared conventions (the ``+ouroboros <skill>`` discovery query, the
    ``host_action=spawn_subagents`` payload contract), and lets each runtime map
    them onto its own concrete mechanism (``ToolDiscoveryMechanism`` /
    ``SubagentSpawnTriggerMechanism``). Keep it within the host's instructions
    budget (Claude Code truncates at ~2KB) with the load-bearing rules first.
    """
    return (
        "Ouroboros drives interview/seed/run/evaluate/evolve workflows through "
        "MCP tools named `...ouroboros_*`.\n"
        "\n"
        "TOOL DISCOVERY: these tools may be DEFERRED — the tool exists but its "
        "schema is not loaded, so it is not in your immediate tool list. Do NOT "
        "conclude a tool is unavailable from its absence. Load it through your "
        "runtime's own tool-discovery mechanism using the query "
        "`+ouroboros <skill>` (e.g. `+ouroboros evaluate`), then call it by its "
        "full `...ouroboros_*` name. A deferred schema can unload between turns, "
        "so re-run discovery immediately before each call. If a tool is already "
        "exposed, call it directly — discovery is a no-op then, and an empty "
        "discovery result is expected, not a failure. Never surface this "
        "tool-discovery plumbing to the user.\n"
        "\n"
        "SUBAGENT FAN-OUT: when a tool result's `meta` carries "
        "`host_action=spawn_subagents` (or `dispatch_mode=host_driven`) with a "
        "payload array (e.g. `question_advisory_subagents`), spawn ONE subagent "
        "per payload using your runtime's native primitive, give each the "
        "payload `prompt`, await all, correlate results by the "
        "`result_correlation_key` named in `meta`, then synthesize while "
        "preserving the user-facing content. With no parallel primitive, process "
        "the payloads sequentially. Keep any user-facing question visible before "
        "the assistive work it triggers."
    )


def resolve_tool_discovery(
    runtime_backend: str | None,
) -> tuple[ToolDiscoveryMechanism, str | None]:
    """Resolve the (mechanism, callable discovery tool name) for a runtime.

    Mirrors :func:`resolve_subagent_dispatch`: the abstract tool-discovery
    concept resolves to one concrete per-runtime mechanism. Unknown backends or
    backends with no registered mechanism are treated as ``DIRECT_EXPOSURE``
    (tools are always exposed; no discovery step).
    """
    capability = get_backend_capability((runtime_backend or "").strip().lower())
    if capability is None or capability.tool_discovery_mechanism is None:
        return ToolDiscoveryMechanism.DIRECT_EXPOSURE, None
    return capability.tool_discovery_mechanism, capability.tool_discovery_tool_name


def build_runtime_subagent_orchestration_contract(
    name: str,
    *,
    directive_metadata: Mapping[str, Any],
    opencode_mode: str | None = None,
    callable_mcp_tool_capabilities: Sequence[Mapping[str, Any]] = (),
) -> RuntimeSubagentOrchestrationContract:
    """Build runtime-specific handling metadata for MCP subagent directives.

    ``directive_metadata`` is the explicit MCP-side orchestration metadata from
    owned Ouroboros tools, such as the lateral panel contract. Runtime support
    determines whether native parallel subagent dispatch can be used or whether
    the declared sequential fallback must be followed.
    """
    normalized_name = name.strip().lower()
    capability = get_backend_capability(normalized_name)
    if capability is None:
        capability = BackendCapability(name=normalized_name or "unknown")

    sequential_fallback = directive_metadata.get("sequential_fallback", {})
    if not isinstance(sequential_fallback, Mapping):
        sequential_fallback = {}

    mode = resolve_subagent_dispatch(name, opencode_mode)
    # The contract speaks the same vocabulary as the resolver: ``dispatch_mode``
    # is exactly the ``SubagentDispatchMode`` value, so there is one source of
    # truth for the mode string ({plugin_passive | host_driven | sequential}).
    dispatch_mode = mode.value

    if mode is SubagentDispatchMode.PLUGIN_PASSIVE:
        spawn_trigger_mechanism = SubagentSpawnTriggerMechanism.PASSIVE_BRIDGE_ENVELOPE
        requires_explicit_spawn_request = False
        callable_spawn_tool_name = None
        prohibited_spawn_tool_names: tuple[str, ...] = ()
        runtime_instruction_handling = (
            "Consume MCP `_subagent` or `_subagents` directive payloads with the "
            "runtime's native parallel subagent primitive. For OpenCode this "
            "requires the plugin surface (`opencode_mode=plugin`). Keep the "
            "MCP-declared sequential fallback available for downgraded runtime "
            "surfaces."
        )
    elif mode is SubagentDispatchMode.HOST_DRIVEN:
        spawn_trigger_mechanism = (
            capability.host_driven_subagent_mechanism
            or SubagentSpawnTriggerMechanism.SEQUENTIAL_FALLBACK
        )
        requires_explicit_spawn_request = capability.host_driven_subagent_requires_explicit_request
        callable_spawn_tool_name = capability.host_driven_callable_spawn_tool_name
        prohibited_spawn_tool_names = capability.prohibited_subagent_spawn_tool_names
        runtime_instruction_handling = (
            "This runtime has a native subagent primitive but no passive "
            "Ouroboros bridge. Consume the inline `host_driven` dispatch payloads "
            "(the `payloads` array stamped with `host_action=spawn_subagents`) "
            "and spawn each one with the runtime's own subagent primitive, "
            "correlating results by the directive metadata's correlation key. "
            "Fall back to the MCP `sequential_fallback` contract only when no "
            "parallel primitive is available."
        )
    else:
        spawn_trigger_mechanism = SubagentSpawnTriggerMechanism.SEQUENTIAL_FALLBACK
        requires_explicit_spawn_request = False
        callable_spawn_tool_name = None
        prohibited_spawn_tool_names = ()
        runtime_instruction_handling = (
            "This runtime has no native parallel subagent primitive. Follow the "
            "MCP `sequential_fallback` contract and process each structured "
            "subagent payload sequentially, preserving the response correlation "
            "keys declared by the directive metadata."
        )

    return RuntimeSubagentOrchestrationContract(
        backend_name=capability.name,
        # This boolean is the *passive bridge* axis only; ``HOST_DRIVEN`` runtimes
        # report False here and carry their capability via ``dispatch_mode``.
        supports_native_parallel_subagents=mode is SubagentDispatchMode.PLUGIN_PASSIVE,
        dispatch_mode=dispatch_mode,
        spawn_trigger_mechanism=spawn_trigger_mechanism.value,
        requires_explicit_spawn_request=requires_explicit_spawn_request,
        callable_spawn_tool_name=callable_spawn_tool_name,
        prohibited_spawn_tool_names=prohibited_spawn_tool_names,
        mcp_directive_keys=("_subagent", "_subagents"),
        sequential_fallback=dict(sequential_fallback),
        runtime_instruction_handling=runtime_instruction_handling,
        callable_mcp_tool_capabilities=tuple(
            dict(capability) for capability in callable_mcp_tool_capabilities
        ),
    )


def _supports_native_parallel_subagent_surface(
    capability: BackendCapability,
    *,
    opencode_mode: str | None,
) -> bool:
    """Return whether the current backend surface has a native subagent receiver."""
    if not capability.supports_native_parallel_subagents:
        return False
    if capability.name != "opencode":
        return True
    return (opencode_mode or "").strip().lower() == "plugin"


def get_backend_capability(name: str) -> BackendCapability | None:
    """Return capability metadata for a canonical backend name or alias."""
    return _BY_NAME.get(name.strip().lower())


def resolve_subagent_dispatch(
    runtime_backend: str | None,
    opencode_mode: str | None,
) -> SubagentDispatchMode:
    """Resolve the subagent dispatch mode for a runtime — the production SoT.

    Separates two registry axes that must not be conflated:

    - ``supports_native_parallel_subagents`` → a *passive* ``_subagents``
      envelope receiver exists (OpenCode plugin surface, gated on
      ``opencode_mode``). Maps to ``PLUGIN_PASSIVE``.
    - ``supports_host_driven_subagents`` → the host *model* spawns from inline
      payloads with its own primitive (e.g. Codex). No passive receiver. Maps
      to ``HOST_DRIVEN``.

    A backend may have the second without the first; routing such a backend
    into the passive-envelope path would drop the envelope (no receiver) AND
    skip the real work, so they stay on separate axes.
    """
    capability = get_backend_capability((runtime_backend or "").strip().lower())
    if capability is None:
        return SubagentDispatchMode.SEQUENTIAL
    if _supports_native_parallel_subagent_surface(capability, opencode_mode=opencode_mode):
        return SubagentDispatchMode.PLUGIN_PASSIVE
    if capability.supports_host_driven_subagents:
        return SubagentDispatchMode.HOST_DRIVEN
    return SubagentDispatchMode.SEQUENTIAL


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
    "RuntimeSubagentOrchestrationContract",
    "SkillExecutionCapability",
    "SubagentDispatchMode",
    "SubagentSpawnTriggerMechanism",
    "ToolDiscoveryMechanism",
    "backend_supports_tool_envelope",
    "build_runtime_subagent_orchestration_contract",
    "get_backend_capability",
    "interview_driver_backend_choices",
    "llm_backend_choices",
    "resolve_backend_alias",
    "render_backend_skill_capability_guide",
    "render_mcp_server_instructions",
    "resolve_interview_driver_backend",
    "resolve_llm_backend_name",
    "resolve_runtime_backend_name",
    "resolve_subagent_dispatch",
    "resolve_tool_discovery",
    "runtime_backend_choices",
    "soft_tool_enforcement_backends",
]
