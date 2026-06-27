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

_LATERAL_PANEL_DIRECTIVE_METADATA = {
    "sequential_fallback": {
        "supported": True,
        "mode": "sequential_persona_payload_dispatch",
        "trigger": "runtime_has_no_native_parallel_subagent_primitive",
    }
}

_CANCEL_JOB_CAPABILITY = {
    "tool_name": "ouroboros_cancel_job",
    "source_kind": "attached_mcp",
    "source_name": "ouroboros",
    "fallback_used": False,
    "execution_mode": "cancel",
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["job_id"],
    },
    "required_context_keys": ["job_id"],
    "cancel": {
        "supported": True,
        "mode": "background_job_control",
        "companions": [
            "ouroboros_job_status",
            "ouroboros_job_wait",
            "ouroboros_job_result",
        ],
        "target_context_keys": ["job_id"],
    },
}

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
    assert resolve_backend_alias("gajae-code") == "gjc"
    assert resolve_backend_alias("gajae_code") == "gjc"


def test_runtime_choices_include_runtime_only_backends() -> None:
    choices = runtime_backend_choices()
    assert "hermes" in choices
    assert "pi" in choices
    assert "gjc" in choices
    assert resolve_runtime_backend_name("gajae_code") == "gjc"
    assert "litellm" not in choices


def test_llm_choices_include_hermes_adapter() -> None:
    choices = llm_backend_choices()
    assert "codex" in choices
    assert "hermes" in choices
    assert "pi" in choices
    assert "gjc" in choices


def test_capability_specific_resolution_rejects_wrong_surface() -> None:
    with pytest.raises(ValueError):
        resolve_runtime_backend_name("litellm")
    assert resolve_llm_backend_name("hermes_cli") == "hermes"
    assert resolve_llm_backend_name("gajae-code") == "gjc"


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
    assert not backend_supports_tool_envelope("gjc")


def test_switchable_runtime_metadata_is_registry_owned() -> None:
    capability = get_backend_capability("gemini_cli")
    assert capability is not None
    assert capability.name == "gemini"
    assert capability.switchable_runtime is True
    assert capability.cli_config_key == "gemini_cli_path"
    gjc_capability = get_backend_capability("gajae-code")
    assert gjc_capability is not None
    assert gjc_capability.name == "gjc"
    assert gjc_capability.cli_name == "gjc"
    assert gjc_capability.cli_config_key == "gjc_cli_path"
    assert gjc_capability.switchable_runtime is True
    assert gjc_capability.supports_runtime is True
    assert gjc_capability.supports_llm is True


def test_codex_skill_execution_guidance_is_registry_owned() -> None:
    capability = get_backend_capability("codex_cli")

    assert capability is not None
    names = {item.name for item in capability.skill_execution_capabilities}
    # Codex additionally exposes host-driven subagent orchestration guidance so
    # the host model fans out the inline ``host_driven`` dispatch itself.
    assert names == REQUIRED_SKILL_CAPABILITY_NAMES | {"orchestrate_subagents"}


def test_codex_host_driven_subagent_orchestration_is_registry_owned() -> None:
    capability = get_backend_capability("codex")

    assert capability is not None
    assert capability.supports_host_driven_subagents is True
    assert capability.supports_native_parallel_subagents is False

    guide = render_backend_skill_capability_guide("codex")
    assert "### When a skill requires `orchestrate_subagents`" in guide
    assert "dispatch_mode=host_driven" in guide
    assert "host_action=spawn_subagents" in guide
    assert "multi_agent_v1.spawn_agent" in guide
    assert "context.persona" in guide


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
    # ``gemini`` has neither a passive bridge nor a host-driven primitive flag,
    # so it stays on the sequential-fallback branch.
    contract = build_runtime_subagent_orchestration_contract(
        "gemini_cli",
        directive_metadata=_LATERAL_PANEL_DIRECTIVE_METADATA,
    )

    assert contract.backend_name == "gemini"
    assert contract.supports_native_parallel_subagents is False
    assert contract.dispatch_mode == "sequential"
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
    assert contract.to_dict()["sequential_fallback"] == dict(
        _LATERAL_PANEL_DIRECTIVE_METADATA["sequential_fallback"]
    )


def test_host_driven_runtime_gets_host_driven_contract() -> None:
    """Codex has a native primitive but no passive bridge → host_driven contract.

    Guards against the contract regressing to ``sequential_fallback`` (which
    would emit a wrong instruction once a consumer reads the contract) while
    the resolver says ``host_driven``.
    """
    contract = build_runtime_subagent_orchestration_contract(
        "codex_cli",
        directive_metadata=_LATERAL_PANEL_DIRECTIVE_METADATA,
        opencode_mode="plugin",
    )

    assert contract.backend_name == "codex"
    # The boolean is the *passive bridge* axis only — host-driven runtimes are False here.
    assert contract.supports_native_parallel_subagents is False
    assert contract.dispatch_mode == "host_driven"
    assert "host_action=spawn_subagents" in contract.runtime_instruction_handling
    assert "no passive" in contract.runtime_instruction_handling


@pytest.mark.parametrize("directive_metadata", [{}, {"sequential_fallback": "invalid"}])
def test_subagent_orchestration_contract_handles_absent_or_malformed_fallback(
    directive_metadata: dict[str, object],
) -> None:
    contract = build_runtime_subagent_orchestration_contract(
        "gemini_cli",
        directive_metadata=directive_metadata,
    )

    assert contract.dispatch_mode == "sequential"
    assert contract.sequential_fallback == {}
    assert "MCP `sequential_fallback` contract" in contract.runtime_instruction_handling


def test_subagent_orchestration_cancel_job_capability_stays_callable_in_same_envelope() -> None:
    contract = build_runtime_subagent_orchestration_contract(
        "opencode_cli",
        directive_metadata=_LATERAL_PANEL_DIRECTIVE_METADATA,
        opencode_mode="plugin",
        callable_mcp_tool_capabilities=(_CANCEL_JOB_CAPABILITY,),
    )
    envelope = contract.to_dict()

    assert envelope["dispatch_mode"] == "plugin_passive"
    assert envelope["mcp_directive_keys"] == ["_subagent", "_subagents"]
    assert envelope["callable_mcp_tool_capabilities"] == [_CANCEL_JOB_CAPABILITY]

    callable_cancel = envelope["callable_mcp_tool_capabilities"][0]
    assert callable_cancel["tool_name"] == "ouroboros_cancel_job"
    assert callable_cancel["source_kind"] == "attached_mcp"
    assert callable_cancel["source_name"] == "ouroboros"
    assert callable_cancel["fallback_used"] is False
    assert callable_cancel["execution_mode"] == "cancel"
    assert callable_cancel["input_schema"] == _CANCEL_JOB_CAPABILITY["input_schema"]
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


@pytest.mark.parametrize("opencode_mode", [None, "subprocess"])
def test_opencode_without_plugin_surface_uses_sequential_fallback(
    opencode_mode: str | None,
) -> None:
    contract = build_runtime_subagent_orchestration_contract(
        "opencode",
        directive_metadata=_LATERAL_PANEL_DIRECTIVE_METADATA,
        opencode_mode=opencode_mode,
    )

    assert contract.backend_name == "opencode"
    assert contract.supports_native_parallel_subagents is False
    assert contract.dispatch_mode == "sequential"
    assert "no native parallel subagent primitive" in contract.runtime_instruction_handling


def test_opencode_plugin_surface_uses_native_parallel_subagents() -> None:
    contract = build_runtime_subagent_orchestration_contract(
        "opencode",
        directive_metadata=_LATERAL_PANEL_DIRECTIVE_METADATA,
        opencode_mode="plugin",
    )

    assert contract.backend_name == "opencode"
    assert contract.supports_native_parallel_subagents is True
    assert contract.dispatch_mode == "plugin_passive"
    assert "opencode_mode=plugin" in contract.runtime_instruction_handling


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
    for backend_name in ("hermes", "claude", "opencode", "gemini", "kiro", "copilot", "pi", "gjc"):
        guide = render_backend_skill_capability_guide(backend_name)

        assert guide.startswith(f"## Ouroboros Skill Capability Guide: {backend_name.title()}\n")
        for capability_name in REQUIRED_SKILL_CAPABILITY_NAMES:
            assert f"### When a skill requires `{capability_name}`" in guide
