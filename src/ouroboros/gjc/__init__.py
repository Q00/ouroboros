"""GJC runtime integration: host adapter, paths, artifacts, and ownership.

This package owns GJC-specific probing, MCP registration, artifact rendering,
canonical paths, and generation ownership. CLI setup only orders these
operations and narrates their results.
"""

from ouroboros.gjc.adapter import (
    gjc_native_mcp_autoload_support,
    register_gjc_mcp_server,
    verify_gjc_mcp_endpoint,
)
from ouroboros.gjc.artifacts import (
    GJC_SKILL_NAMESPACE,
    GjcSkillInstallResult,
    gjc_skills_root,
    has_orphaned_gjc_claims,
    has_setup_owned_gjc_skills,
    install_gjc_skills,
    recover_gjc_skill_claims,
    remove_gjc_skills,
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
    "gjc_native_mcp_autoload_support",
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
    "register_gjc_mcp_server",
    "recover_gjc_skill_claims",
    "remove_gjc_skills",
    "remove_persisted_gjc_mcp_server",
    "remove_persisted_gjc_mcp_server_locked",
    "verify_gjc_mcp_endpoint",
    "render_gjc_guide",
]
