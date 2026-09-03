"""Codex MCP env helpers for setup-owned telemetry opt-out."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from ouroboros.config.telemetry_opt_out import telemetry_opt_out_environ

MCP_SECTION_TEMPLATE = """# Ouroboros MCP hookup for Codex CLI.
# Keep Ouroboros runtime settings and per-role model overrides in
# ~/.ouroboros/config.yaml (for example: clarification.default_model,
# llm.qa_model, evaluation.semantic_model, consensus.*).
# This file is only for the Codex MCP/env registration block.

[mcp_servers.ouroboros]
{command_lines}

[mcp_servers.ouroboros.env]
{env_lines}
"""

TELEMETRY_OPT_OUT_ENV_KEYS = frozenset({"DO_NOT_TRACK", "OUROBOROS_TELEMETRY"})
TELEMETRY_OPT_OUT_TRUTHY = frozenset({"1", "true", "on", "yes"})
TELEMETRY_OPT_OUT_FALSY = frozenset({"0", "false", "off", "no"})


def base_env(env: Mapping[str, object]) -> dict[str, object]:
    """Return Codex MCP env without telemetry opt-out keys."""
    return {key: value for key, value in env.items() if key not in TELEMETRY_OPT_OUT_ENV_KEYS}


def env_is_setup_owned(
    env: object,
    *,
    managed_env: Mapping[str, str],
    host_env: Mapping[str, str],
) -> bool:
    """Return whether Codex MCP env is still setup-owned, including opt-out keys."""
    if env is None:
        return True
    if not isinstance(env, dict):
        return False
    extra_keys = set(env) - set(managed_env) - set(host_env)
    if extra_keys - TELEMETRY_OPT_OUT_ENV_KEYS:
        return False
    base = base_env(env)
    if base != dict(managed_env) and base != dict(host_env):
        return False
    do_not_track = env.get("DO_NOT_TRACK")
    if do_not_track is not None and str(do_not_track).strip().lower() not in (
        TELEMETRY_OPT_OUT_TRUTHY
    ):
        return False
    telemetry_flag = env.get("OUROBOROS_TELEMETRY")
    return telemetry_flag is None or str(telemetry_flag).strip().lower() in (
        TELEMETRY_OPT_OUT_FALSY
    )


def registration_env(base: Mapping[str, str]) -> dict[str, str]:
    """Merge setup-owned Codex MCP env with the current process opt-out."""
    return {**base, **telemetry_opt_out_environ()}


def render_env_lines(base: Mapping[str, str], toml_string: Callable[[str], str]) -> str:
    """Render the Codex MCP env table body for a setup-owned registration."""
    return "\n".join(
        f"{key} = {toml_string(value)}" for key, value in registration_env(base).items()
    )
