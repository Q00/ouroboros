"""GJC runtime integration: paths, artifact projection, and ownership judgment.

This package is the single home for GJC domain logic. It renders every
setup-owned artifact (skill projections, the compatibility bridge, the
routing guide, the MCP bridge config), resolves their canonical paths, and
judges whether an on-disk file or MCP registration is still the exact
generation setup produced. Nothing here prints or spawns the GJC CLI —
orchestration and narration live in :mod:`ouroboros.cli.gjc_setup`.
"""

from ouroboros.gjc.artifacts import (
    GJC_SKILL_NAMESPACE,
    GjcSkillInstallResult,
    gjc_skills_root,
    has_orphaned_gjc_claims,
    has_setup_owned_gjc_skills,
    install_gjc_skills,
    recover_gjc_skill_claims,
    remove_gjc_skills,
    setup_owned_gjc_skill_paths,
)
from ouroboros.gjc.bridge import (
    gjc_ooo_bridge_source_text,
    is_gjc_ooo_bridge_source_text,
    is_setup_managed_gjc_bridge,
)
from ouroboros.gjc.guide import (
    is_setup_managed_gjc_instruction,
    render_gjc_guide,
)
from ouroboros.gjc.mcp import (
    MCP_BRIDGE_CONFIG_CONTENT,
    MCP_SHARING,
    MCP_TIMEOUT,
    gjc_mcp_entry_config,
    gjc_mcp_entry_generation,
    gjc_mcp_registration_lock,
    is_active_gjc_mcp_entry,
    is_setup_managed_gjc_mcp_bridge_config,
    is_setup_managed_gjc_mcp_entry,
    persisted_gjc_mcp_entry,
    remove_persisted_gjc_mcp_server,
    remove_persisted_gjc_mcp_server_locked,
)
from ouroboros.gjc.paths import (
    gjc_agent_dir,
    gjc_bridge_path,
    gjc_instruction_path,
    gjc_mcp_bridge_config_path,
    gjc_mcp_config_path,
)

__all__ = [
    "GJC_SKILL_NAMESPACE",
    "MCP_BRIDGE_CONFIG_CONTENT",
    "MCP_SHARING",
    "MCP_TIMEOUT",
    "GjcSkillInstallResult",
    "gjc_agent_dir",
    "gjc_bridge_path",
    "gjc_instruction_path",
    "gjc_mcp_bridge_config_path",
    "gjc_mcp_config_path",
    "gjc_mcp_entry_config",
    "gjc_mcp_entry_generation",
    "gjc_mcp_registration_lock",
    "gjc_ooo_bridge_source_text",
    "gjc_skills_root",
    "has_orphaned_gjc_claims",
    "has_setup_owned_gjc_skills",
    "install_gjc_skills",
    "is_active_gjc_mcp_entry",
    "is_gjc_ooo_bridge_source_text",
    "is_setup_managed_gjc_bridge",
    "is_setup_managed_gjc_instruction",
    "is_setup_managed_gjc_mcp_bridge_config",
    "is_setup_managed_gjc_mcp_entry",
    "persisted_gjc_mcp_entry",
    "recover_gjc_skill_claims",
    "remove_gjc_skills",
    "remove_persisted_gjc_mcp_server",
    "remove_persisted_gjc_mcp_server_locked",
    "render_gjc_guide",
    "setup_owned_gjc_skill_paths",
]
