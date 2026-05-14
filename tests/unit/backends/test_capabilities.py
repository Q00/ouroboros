"""Tests for the shared backend capability registry."""

import pytest

from ouroboros.backends import (
    backend_supports_tool_envelope,
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


def test_resolves_aliases_to_canonical_names() -> None:
    assert resolve_backend_alias("codex_cli") == "codex"
    assert resolve_backend_alias("claude_code") == "claude"
    assert resolve_backend_alias("openrouter") == "litellm"


def test_runtime_choices_include_runtime_only_backends() -> None:
    choices = runtime_backend_choices()
    assert "hermes" in choices
    assert "litellm" not in choices


def test_llm_choices_include_hermes_adapter() -> None:
    choices = llm_backend_choices()
    assert "codex" in choices
    assert "hermes" in choices


def test_capability_specific_resolution_rejects_wrong_surface() -> None:
    with pytest.raises(ValueError):
        resolve_runtime_backend_name("litellm")
    assert resolve_llm_backend_name("hermes_cli") == "hermes"


def test_interview_driver_choices_follow_llm_capability() -> None:
    assert "codex" in interview_driver_backend_choices()
    assert "hermes" in interview_driver_backend_choices()


def test_soft_tool_enforcement_is_registry_owned() -> None:
    assert soft_tool_enforcement_backends() == frozenset({"gemini", "opencode"})


def test_tool_envelope_support_is_registry_owned() -> None:
    assert backend_supports_tool_envelope("codex")
    assert backend_supports_tool_envelope("gemini_cli")
    assert not backend_supports_tool_envelope("hermes")


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
    assert {
        "ask_user",
        "inspect_code",
        "call_mcp",
        "web_research",
        "run_shell",
        "refine_answer",
        "maintain_ledger",
        "run_closure_gate",
        "restate_goal",
    }.issubset(names)


def test_generic_skill_execution_guidance_covers_interview_requirements() -> None:
    capability = get_backend_capability("claude")

    assert capability is not None
    names = {item.name for item in capability.skill_execution_capabilities}
    assert {
        "ask_user",
        "inspect_code",
        "call_mcp",
        "web_research",
        "run_shell",
        "refine_answer",
        "maintain_ledger",
        "run_closure_gate",
        "restate_goal",
    }.issubset(names)


def test_renders_codex_skill_capability_guide_as_stable_markdown() -> None:
    guide = render_backend_skill_capability_guide("codex")

    assert guide.startswith("## Ouroboros Skill Capability Guide: Codex\n")
    assert "### When a skill requires `ask_user`" in guide
    assert "request_user_input" in guide
    assert "### When a skill requires `inspect_code`" in guide
    assert "`rg`" in guide
    assert "### When a skill requires `call_mcp`" in guide
    assert "Do not rely on Claude-specific `ToolSearch` names." in guide
    assert "### When a skill requires `run_closure_gate`" in guide
    assert "MCP `seed-ready`" in guide
    assert "### When a skill requires `restate_goal`" in guide
    assert "require explicit user approval" in guide
