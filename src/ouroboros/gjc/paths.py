"""Canonical filesystem locations for every GJC integration artifact."""

from __future__ import annotations

import os
from pathlib import Path

_GUIDE_FILENAME = "ouroboros-skill-capability-guide.md"


def gjc_agent_dir(home: str | Path | None = None, environ: dict[str, str] | None = None) -> Path:
    """Return GJC's agent directory for rules/extensions discovery.

    Mirrors gjc's own resolution (``@gajae-code/utils`` ``dirs.ts``):
    ``GJC_CODING_AGENT_DIR`` overrides the agent directory as a path, while
    ``GJC_CONFIG_DIR`` (with ``PI_CONFIG_DIR`` as fallback) is a directory
    *name* joined under the home directory — not an absolute path.
    """
    env = os.environ if environ is None else environ
    explicit_agent_dir = env.get("GJC_CODING_AGENT_DIR", "").strip()
    if explicit_agent_dir:
        return Path(explicit_agent_dir).expanduser()

    root = Path(home).expanduser() if home is not None else Path.home()
    config_dir_name = (
        env.get("GJC_CONFIG_DIR", "").strip() or env.get("PI_CONFIG_DIR", "").strip() or ".gjc"
    )
    normalized_parts = config_dir_name.replace("\\", "/").split("/")
    if ".." in normalized_parts:
        config_dir_name = ".gjc"
    return root / config_dir_name.lstrip("/\\") / "agent"


def gjc_instruction_path(
    home: str | Path | None = None, environ: dict[str, str] | None = None
) -> Path:
    """Return GJC's global rules artifact path."""
    return gjc_agent_dir(home=home, environ=environ) / "rules" / _GUIDE_FILENAME


def gjc_mcp_config_path() -> Path:
    """Return GJC's durable user MCP registration file."""
    return gjc_agent_dir() / "mcp.json"


def gjc_mcp_bridge_config_path() -> Path:
    """Return the setup-owned empty upstream bridge config for GJC sessions."""
    return gjc_agent_dir() / "ouroboros" / "mcp-bridge.yaml"


def gjc_bridge_path() -> Path:
    """Return the compatibility input bridge path for the active GJC profile."""
    return gjc_agent_dir() / "extensions" / "ouroboros-ooo-bridge" / "index.ts"


def has_orphaned_gjc_claims() -> bool:
    """Return whether any GJC artifact directory holds an interrupted claim.

    A crashed transaction leaves the managed generation under a hidden claim
    sibling; discovery must treat that as installed state so refresh and
    uninstall reconcile it instead of skipping the artifact.
    """
    from ouroboros.core.fs_ownership import find_orphaned_claims

    agent_dir = gjc_agent_dir()
    parents = (
        agent_dir,
        agent_dir / "skills",
        gjc_instruction_path().parent,
        gjc_mcp_bridge_config_path().parent,
        gjc_bridge_path().parent,
    )
    return any(find_orphaned_claims(parent) for parent in parents)
