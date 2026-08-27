"""Order GJC runtime activation without emulating a host transaction.

GJC-specific probing and MCP registration live in :mod:`ouroboros.gjc`.
Filesystem publication is owned by :mod:`ouroboros.core.fs_ownership`. This
module only sequences those independent operations and commits the Ouroboros
runtime selection after activation succeeds.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os

import yaml

from ouroboros.cli.formatters.panels import (
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ouroboros.core.fs_ownership import UnownedArtifactError, publish_owned_file
from ouroboros.gjc import (
    MCP_BRIDGE_CONFIG_CONTENT,
    gjc_agent_dir,
    gjc_bridge_path,
    gjc_mcp_bridge_config_path,
    gjc_mcp_registration_lock,
    gjc_native_mcp_autoload_support,
    gjc_ooo_bridge_source_text,
    install_gjc_skills,
    is_setup_managed_gjc_bridge,
    is_setup_managed_gjc_mcp_bridge_config,
    is_setup_managed_gjc_mcp_entry,
    persisted_gjc_mcp_entry,
    register_gjc_mcp_server,
)


@dataclass(frozen=True, slots=True)
class GjcSetupHost:
    """Setup-owned collaborators used while ordering GJC activation."""

    atomic_write_text: Callable[..., object]
    snapshot_path: Callable[..., object]
    detect_mcp_entry: Callable[..., dict[str, object] | None]
    bridge_dispatch_entry: Callable[[], tuple[str, list[str]]]


def install_gjc_skills_step() -> bool:
    """Project packaged Ouroboros skills into GJC's native user registry."""
    try:
        result = install_gjc_skills(agent_dir=gjc_agent_dir(), prune=True)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print_warning(f"Could not install GJC Ouroboros skills: {exc}")
        return False
    print_success(f"Installed {len(result.skill_paths)} Ouroboros skills → {result.target_root}")
    return True


def install_gjc_mcp_bridge_config() -> bool:
    """Install the isolated config only when setup owns the target path."""
    path = gjc_mcp_bridge_config_path()
    try:
        publish_owned_file(
            path,
            MCP_BRIDGE_CONFIG_CONTENT,
            is_owned=is_setup_managed_gjc_mcp_bridge_config,
            mode=0o600,
            trusted_ancestor=gjc_agent_dir(),
        )
    except UnownedArtifactError:
        print_error(
            f"Preserved user-managed GJC MCP bridge config at {path}; "
            "native activation requires an isolated setup-owned configuration."
        )
        return False
    except OSError as exc:
        print_warning(f"Could not install GJC MCP bridge config: {exc}")
        return False
    return True


def install_gjc_instruction_step() -> bool:
    """Install the always-applied routing guide without replacing operator files."""
    from ouroboros.runtime_instruction_artifacts import install_gjc_instruction_artifact

    try:
        artifact = install_gjc_instruction_artifact()
    except OSError as exc:
        print_warning(f"Could not install gjc instruction artifact: {exc}")
        return False
    print_success(f"Installed Gjc instruction guide → {artifact.path}")
    return True


def install_gjc_compatibility_bridge(content: str) -> bool:
    """Install the owned bridge when the host cannot autoload native MCP entries."""
    bridge = gjc_bridge_path()
    try:
        publish_owned_file(
            bridge,
            content,
            is_owned=is_setup_managed_gjc_bridge,
            trusted_ancestor=gjc_agent_dir(),
        )
    except UnownedArtifactError:
        print_error(f"Preserved custom GJC extension at {bridge}; compatibility activation failed.")
        return False
    except OSError as exc:
        print_warning(f"Could not install GJC compatibility bridge: {exc}")
        return False
    print_info("Installed GJC compatibility bridge; this host does not expose native MCP autoload.")
    return True


def remove_legacy_gjc_bridge() -> bool:
    """Remove the obsolete setup-owned input bridge without touching custom files."""
    from ouroboros.core.fs_ownership import claim_and_remove_owned

    bridge = gjc_bridge_path()
    if not os.path.lexists(bridge):
        return True
    try:
        removed = claim_and_remove_owned(
            bridge,
            is_owned=is_setup_managed_gjc_bridge,
            trusted_ancestor=gjc_agent_dir(),
        )
    except OSError as exc:
        print_warning(f"Could not remove legacy GJC bridge: {exc}")
        return False
    if not removed:
        print_info(f"Preserved custom GJC extension at {bridge}")
        return True
    try:
        bridge.parent.rmdir()
    except OSError:
        pass
    print_info("Removed obsolete GJC input bridge; native skills now own ooo routing.")
    return True


def install_gjc_runtime_artifacts(gjc_path: str, *, host: GjcSetupHost) -> bool:
    """Sequence independent GJC publications; never emulate a host transaction."""
    native_support = gjc_native_mcp_autoload_support(gjc_path)
    if native_support is None:
        return False
    if not native_support:
        command, args = host.bridge_dispatch_entry()
        return install_gjc_compatibility_bridge(gjc_ooo_bridge_source_text(command, args))

    if not install_gjc_mcp_bridge_config():
        return False
    if not install_gjc_skills_step():
        return False
    if not install_gjc_instruction_step():
        return False

    # Registration is one adapter-owned host operation. Setup only orders the
    # legacy-route retirement after the validated durable entry exists. The
    # lock covers this narrow cutover, not the other independent artifacts.
    try:
        with gjc_mcp_registration_lock():
            if not register_gjc_mcp_server(
                gjc_path,
                detect_mcp_entry=host.detect_mcp_entry,
                assume_registration_lock=True,
            ):
                return False
            if not is_setup_managed_gjc_mcp_entry(persisted_gjc_mcp_entry()):
                print_warning(
                    "The GJC MCP registration disappeared before the legacy route "
                    "was retired; kept the compatibility bridge."
                )
                return False
            return remove_legacy_gjc_bridge()
    except OSError as exc:
        print_warning(f"Could not serialize GJC route cutover: {exc}")
        return False


def setup_gjc_runtime(gjc_path: str, *, host: GjcSetupHost) -> bool:
    """Activate GJC artifacts, then commit the runtime selection once."""
    from ouroboros.config.loader import ensure_config_dir
    from ouroboros.config.models import get_default_config, get_default_credentials

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    credentials_path = config_dir / "credentials.yaml"
    config_generation = host.snapshot_path(config_path, follow_links=False)
    credentials_generation = host.snapshot_path(credentials_path, follow_links=False)
    fresh_config = not config_path.exists()

    try:
        if fresh_config:
            config = get_default_config().model_dump(mode="json")
        else:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(config, dict):
            print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting GJC setup.")
            return False

        orchestrator = config.get("orchestrator")
        if not isinstance(orchestrator, dict):
            orchestrator = {}
            config["orchestrator"] = orchestrator
        orchestrator.update(runtime_backend="gjc", gjc_cli_path=gjc_path)
        llm = config.get("llm")
        if not isinstance(llm, dict):
            llm = {}
            config["llm"] = llm
        llm["backend"] = "gjc"

        if not install_gjc_runtime_artifacts(gjc_path, host=host):
            raise OSError("runtime artifact activation failed")

        if fresh_config and not credentials_path.exists():
            credentials = get_default_credentials().model_dump(mode="json")
            host.atomic_write_text(
                credentials_path,
                yaml.dump(credentials, default_flow_style=False, sort_keys=False),
                mode=0o600,
                expected_current=credentials_generation,
            )
        host.atomic_write_text(
            config_path,
            yaml.dump(config, default_flow_style=False, sort_keys=False),
            expected_current=config_generation,
        )
    except (OSError, yaml.YAMLError) as exc:
        print_error(
            "GJC setup failed; completed setup-owned generations were preserved "
            f"for a later refresh: {exc}"
        )
        return False

    print_success(f"Configured GJC runtime (CLI: {gjc_path})")
    print_info(f"Config saved to: {config_path}")
    return True
