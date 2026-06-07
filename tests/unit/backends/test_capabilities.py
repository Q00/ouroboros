"""Tests for the shared backend capability registry."""

from jsonschema import Draft202012Validator
import pytest

from ouroboros.backends import (
    backend_supports_tool_envelope,
    build_runtime_subagent_orchestration_contract,
    get_backend_capability,
    interview_driver_backend_choices,
    llm_backend_choices,
    render_backend_skill_capability_guide,
    resolve_backend_alias,
    resolve_llm_backend_name,
    resolve_runtime_backend_name,
    runtime_backend_choices,
    soft_tool_enforcement_backends,
)
from ouroboros.mcp.tools.definitions import get_ouroboros_tools
from ouroboros.orchestrator.capabilities import (
    build_capability_graph,
    ouroboros_tool_capability_metadata,
)
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

REQUIRED_SKILL_CAPABILITY_NAMES = {
    "ask_user",
    "inspect_code",
    "call_mcp",
    "run_lateral_review",
    "web_research",
    "run_shell",
    "refine_answer",
    "maintain_ledger",
    "run_closure_gate",
    "restate_goal",
}


def test_resolves_aliases_to_canonical_names() -> None:
    assert resolve_backend_alias("codex_cli") == "codex"
    assert resolve_backend_alias("claude_code") == "claude"
    assert resolve_backend_alias("openrouter") == "litellm"


def test_runtime_choices_include_runtime_only_backends() -> None:
    choices = runtime_backend_choices()
    assert "hermes" in choices
    assert "pi" in choices
    assert "litellm" not in choices


def test_llm_choices_include_hermes_adapter() -> None:
    choices = llm_backend_choices()
    assert "codex" in choices
    assert "hermes" in choices
    assert "pi" in choices


def test_capability_specific_resolution_rejects_wrong_surface() -> None:
    with pytest.raises(ValueError):
        resolve_runtime_backend_name("litellm")
    assert resolve_llm_backend_name("hermes_cli") == "hermes"


def test_interview_driver_choices_follow_llm_capability() -> None:
    assert "codex" in interview_driver_backend_choices()
    assert "hermes" in interview_driver_backend_choices()


def test_soft_tool_enforcement_is_registry_owned() -> None:
    assert soft_tool_enforcement_backends() == frozenset({"gemini", "goose", "opencode"})


def test_tool_envelope_support_is_registry_owned() -> None:
    assert backend_supports_tool_envelope("codex")
    assert backend_supports_tool_envelope("gemini_cli")
    assert not backend_supports_tool_envelope("hermes")
    assert not backend_supports_tool_envelope("pi")


def test_switchable_runtime_metadata_is_registry_owned() -> None:
    capability = get_backend_capability("gemini_cli")
    assert capability is not None
    assert capability.name == "gemini"
    assert capability.switchable_runtime is True
    assert capability.cli_config_key == "gemini_cli_path"


def test_codex_skill_execution_guidance_is_registry_owned() -> None:
    capability = get_backend_capability("codex_cli")

    assert capability is not None
    names = {item.name for item in capability.skill_execution_capabilities}
    assert names == REQUIRED_SKILL_CAPABILITY_NAMES


def test_generic_skill_execution_guidance_covers_interview_requirements() -> None:
    capability = get_backend_capability("claude")

    assert capability is not None
    names = {item.name for item in capability.skill_execution_capabilities}
    assert names == REQUIRED_SKILL_CAPABILITY_NAMES


def test_native_parallel_subagent_runtime_exposes_orchestrate_subagents() -> None:
    capability = get_backend_capability("opencode_cli")

    assert capability is not None
    assert capability.supports_native_parallel_subagents is True
    names = {item.name for item in capability.skill_execution_capabilities}
    assert names == REQUIRED_SKILL_CAPABILITY_NAMES | {"orchestrate_subagents"}

    guide = render_backend_skill_capability_guide("opencode")
    assert "### When a skill requires `orchestrate_subagents`" in guide
    assert "native task/subagent primitive" in guide
    assert "`_subagents` MCP directive payloads" in guide
    assert "sequential fallback" in guide


def test_unsupported_parallel_subagent_runtime_gets_sequential_fallback_contract() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    graph = build_capability_graph(assemble_session_tool_catalog(attached_tools=owned_tools))
    descriptors = {descriptor.name: descriptor for descriptor in graph.capabilities}
    lateral = descriptors["ouroboros_lateral_think"]
    assert lateral.metadata is not None
    panel_metadata = lateral.metadata.orchestration["lateral_panel"]

    contract = build_runtime_subagent_orchestration_contract(
        "codex_cli",
        directive_metadata=panel_metadata,
    )

    assert contract.backend_name == "codex"
    assert contract.supports_native_parallel_subagents is False
    assert contract.dispatch_mode == "sequential_fallback"
    assert contract.mcp_directive_keys == ("_subagent", "_subagents")
    assert contract.sequential_fallback == {
        "supported": True,
        "mode": "sequential_persona_payload_dispatch",
        "trigger": "runtime_has_no_native_parallel_subagent_primitive",
    }
    assert "no native parallel subagent primitive" in contract.runtime_instruction_handling
    assert "process each structured subagent payload sequentially" in (
        contract.runtime_instruction_handling
    )
    assert contract.to_dict()["sequential_fallback"] == dict(panel_metadata["sequential_fallback"])


def test_subagent_orchestration_cancel_job_capability_stays_callable_in_same_envelope() -> None:
    owned_tools = tuple(handler.definition for handler in get_ouroboros_tools(include_auto=False))
    definitions = {definition.name: definition for definition in owned_tools}
    graph = build_capability_graph(assemble_session_tool_catalog(attached_tools=owned_tools))
    descriptors = graph.by_name()
    lateral = descriptors["ouroboros_lateral_think"]
    cancel = descriptors["ouroboros_cancel_job"]
    assert lateral.metadata is not None
    assert cancel.metadata is not None

    cancel_capability = ouroboros_tool_capability_metadata("ouroboros_cancel_job")
    contract = build_runtime_subagent_orchestration_contract(
        "opencode_cli",
        directive_metadata=lateral.metadata.orchestration["lateral_panel"],
        callable_mcp_tool_capabilities=(cancel_capability,),
    )
    envelope = contract.to_dict()

    assert envelope["dispatch_mode"] == "native_parallel_subagents"
    assert envelope["mcp_directive_keys"] == ["_subagent", "_subagents"]
    assert envelope["callable_mcp_tool_capabilities"] == [cancel_capability]

    callable_cancel = envelope["callable_mcp_tool_capabilities"][0]
    assert callable_cancel["tool_name"] == "ouroboros_cancel_job"
    assert callable_cancel["source_kind"] == "attached_mcp"
    assert callable_cancel["source_name"] == "ouroboros"
    assert callable_cancel["fallback_used"] is False
    assert callable_cancel["execution_mode"] == "cancel"
    assert callable_cancel["input_schema"] == definitions[
        "ouroboros_cancel_job"
    ].to_input_schema()
    assert callable_cancel["required_context_keys"] == ["job_id"]
    assert callable_cancel["cancel"] == {
        "supported": True,
        "mode": "background_job_control",
        "companions": [
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
        ],
        "target_context_keys": ["job_id"],
    }
    Draft202012Validator(callable_cancel["input_schema"]).validate(
        {"job_id": "job-subagent-123", "reason": "cancel delegated subagent job"}
    )

    assert cancel.metadata.fallback_used is False
    assert cancel.metadata.execution_mode == "cancel"
    assert cancel.metadata.cancel["mode"] == "background_job_control"
    assert cancel.name in {
        capability["tool_name"]
        for capability in envelope["callable_mcp_tool_capabilities"]
    }


def test_renders_codex_skill_capability_guide_as_stable_markdown() -> None:
    guide = render_backend_skill_capability_guide("codex")

    assert guide.startswith("## Ouroboros Skill Capability Guide: Codex\n")
    assert "### When a skill requires `ask_user`" in guide
    assert "request_user_input" in guide
    assert "### When a skill requires `inspect_code`" in guide
    assert "`rg`" in guide
    assert "### When a skill requires `call_mcp`" in guide
    assert "Do not rely on Claude-specific `ToolSearch` names." in guide
    assert "### When a skill requires `run_lateral_review`" in guide
    assert "lateral_review_required=true" in guide
    assert "### When a skill requires `run_closure_gate`" in guide
    assert "MCP `seed-ready`" in guide
    assert "### When a skill requires `restate_goal`" in guide
    assert "require explicit user approval" in guide


def test_renders_generic_skill_capability_guides_for_runtime_backends() -> None:
    for backend_name in ("hermes", "claude", "opencode", "gemini", "kiro", "copilot", "pi"):
        guide = render_backend_skill_capability_guide(backend_name)

        assert guide.startswith(f"## Ouroboros Skill Capability Guide: {backend_name.title()}\n")
        for capability_name in REQUIRED_SKILL_CAPABILITY_NAMES:
            assert f"### When a skill requires `{capability_name}`" in guide
