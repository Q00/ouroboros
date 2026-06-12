"""Shared Kiro CLI launch policy helpers for runtime and provider callers.

Mirrors :mod:`ouroboros.codex.cli_policy` and :mod:`ouroboros.copilot.cli_policy`:
the kiro provider adapter (LLMAdapter) and the kiro orchestrator adapter
(AgentRuntime) previously hand-copied ~120 lines of identical kiro-specific
policy — the model-name map, native trust-category mapping, stripped env keys,
ANSI stripping, retryable exit codes, CLI-path resolution, and child-env
construction.  Both adapters now import the single implementation from here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
import shutil

from ouroboros.runtime.child_env import DEFAULT_MAX_OUROBOROS_DEPTH, build_child_env

DEFAULT_KIRO_CLI_NAME = "kiro-cli"

# Kiro CLI in ``--no-interactive`` mode emits terminal prompt markers and color
# escapes on stdout (e.g. ``\x1b[38;5;141m> \x1b[0m`` before the actual
# content).  Downstream parsers and log collectors want clean text, so we strip
# SGR/CSI escapes from every stdout line.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Environment keys stripped from child processes to prevent recursive MCP
# startup and nested session detection conflicts.  Kiro strips CLAUDECODE (like
# codex/copilot) — preserve that.
_STRIPPED_ENV_KEYS = (
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_LLM_BACKEND",
    "OUROBOROS_RUNTIME",
    "CLAUDECODE",
)

_RETRYABLE_EXIT_CODES = (1, 137)

_MODEL_NAME_MAP: dict[str, str] = {
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-haiku-4-5": "claude-haiku-4.5",
}

_KIRO_TOOL_NAME_MAP: dict[str, str] = {
    "bash": "shell",
    "edit": "write",
    "glob": "read",
    "grep": "grep",
    "ls": "read",
    "multiedit": "write",
    "read": "read",
    "shell": "shell",
    "write": "write",
}
_KIRO_TRUST_CATEGORIES = frozenset(_KIRO_TOOL_NAME_MAP.values())


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI/SGR escape sequences from a string."""
    return _ANSI_ESCAPE_RE.sub("", text)


def normalize_tool_name(tool: str) -> str:
    """Normalise a tool identifier for trust-category lookup."""
    return tool.strip().lower().replace("_", "-")


def map_kiro_model_name(model: str) -> str:
    """Map an Ouroboros model id onto Kiro's dotted model naming."""
    return _MODEL_NAME_MAP.get(model, model)


def kiro_native_trust_category(tool: str) -> str | None:
    """Return a Kiro-native trust category for known local tools only.

    Ouroboros policy allow-lists can include MCP tool names (for example
    ``mcp__server__tool``). Kiro's ``--trust-tools`` flag accepts only native
    trust categories such as ``read`` or ``shell``, so unknown/MCP names must
    not be forwarded to the CLI flag. They remain in the prompt-level allow-list
    but are filtered out of native trust-category argv.
    """
    normalized = normalize_tool_name(tool)
    mapped = _KIRO_TOOL_NAME_MAP.get(normalized.replace("-", ""), normalized)
    if mapped in _KIRO_TRUST_CATEGORIES:
        return mapped
    return None


def resolve_kiro_cli_path(cli_path: str | Path | None) -> str:
    """Resolve the Kiro CLI binary path.

    Checks, in order: the explicit *cli_path*,
    :func:`ouroboros.config.get_kiro_cli_path`, ``PATH``, and finally the bare
    binary name as a last resort.
    """
    if cli_path:
        return str(cli_path)
    from ouroboros.config import get_kiro_cli_path

    configured = get_kiro_cli_path()
    if configured:
        return configured
    return shutil.which(DEFAULT_KIRO_CLI_NAME) or DEFAULT_KIRO_CLI_NAME


def _mark_kiro_subagent(env: dict[str, str]) -> None:
    """Tag the child env as an Ouroboros sub-agent (kiro-specific marker)."""
    env["OUROBOROS_SUBAGENT"] = "1"


def build_kiro_child_env(
    *,
    max_depth: int = DEFAULT_MAX_OUROBOROS_DEPTH,
    depth_error_factory: Callable[[int, int], Exception],
) -> dict[str, str]:
    """Build an isolated environment for a child kiro-cli process.

    Strips the recursion/nesting markers (including CLAUDECODE), enforces the
    depth ceiling, and tags the child as an Ouroboros sub-agent.
    """
    return build_child_env(
        strip_keys=_STRIPPED_ENV_KEYS,
        max_depth=max_depth,
        depth_error_factory=depth_error_factory,
        post_build=_mark_kiro_subagent,
    )


__all__ = [
    "DEFAULT_KIRO_CLI_NAME",
    "build_kiro_child_env",
    "kiro_native_trust_category",
    "map_kiro_model_name",
    "normalize_tool_name",
    "resolve_kiro_cli_path",
    "strip_ansi",
]
