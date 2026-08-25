"""Ownership judgment and durable-state primitives for GJC MCP registrations.

Every function here is side-effect free except
:func:`remove_persisted_gjc_mcp_server`, which mutates only a registration
that exactly matches setup's own generation. Progress narration and GJC CLI
invocations live in :mod:`ouroboros.cli.gjc_setup`.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
import json
from pathlib import Path
import stat

from ouroboros.core.file_lock import file_lock
from ouroboros.gjc.paths import gjc_mcp_bridge_config_path, gjc_mcp_config_path

MCP_BRIDGE_CONFIG_CONTENT = "# Managed by ouroboros setup --runtime gjc\nmcp_servers: []\n"
MCP_SHARING = "per-session"
MCP_TIMEOUT = 30000
MCP_RUNTIME_STATUS = "autoload"
_MCP_CONFIG_FIELDS = {"type", "command", "args", "env", "sharing", "timeout"}


def is_setup_managed_gjc_mcp_bridge_config(path: Path) -> bool:
    """Return whether *path* is the exact setup-owned empty bridge config."""
    try:
        return (
            not path.is_symlink() and path.read_text(encoding="utf-8") == MCP_BRIDGE_CONFIG_CONTENT
        )
    except (OSError, UnicodeDecodeError):
        return False


def _is_exact_launcher_args(command: str, args: Sequence[object]) -> bool:
    """Match only launcher argv generations emitted by Ouroboros GJC setup."""
    runtime_suffix = ["--runtime", "gjc"]
    package_spec = "ouroboros-ai[mcp]"
    if command == "uvx":
        expected = [
            "--isolated",
            "--python",
            ">=3.12",
            "--from",
            package_spec,
            "ouroboros",
            "mcp",
            "serve",
            *runtime_suffix,
        ]
    elif command == "pipx":
        expected = [
            "run",
            "--spec",
            package_spec,
            "ouroboros",
            "mcp",
            "serve",
            *runtime_suffix,
        ]
    else:
        return False
    return list(args) == expected


def gjc_mcp_entry_config(entry: object) -> dict[str, object] | None:
    """Return the execution config from either a CLI row or persistent entry."""
    if not isinstance(entry, dict):
        return None
    nested = entry.get("config")
    if isinstance(nested, dict):
        return nested
    return entry


def is_setup_managed_gjc_mcp_entry(entry: object, *, allow_redacted_env: bool = False) -> bool:
    """Return whether *entry* exactly matches setup's execution contract."""
    config = gjc_mcp_entry_config(entry)
    if config is None:
        return False
    command = config.get("command")
    args = config.get("args")
    env = config.get("env")
    expected_env_values = {str(gjc_mcp_bridge_config_path())}
    if allow_redacted_env:
        expected_env_values.add("<redacted>")
    return (
        set(config) == _MCP_CONFIG_FIELDS
        and config.get("type") == "stdio"
        and isinstance(command, str)
        and isinstance(args, list)
        and _is_exact_launcher_args(command, args)
        and isinstance(env, dict)
        and set(env) == {"OUROBOROS_MCP_CONFIG"}
        and env.get("OUROBOROS_MCP_CONFIG") in expected_env_values
        and config.get("sharing") == MCP_SHARING
        and config.get("timeout") == MCP_TIMEOUT
    )


def is_active_gjc_mcp_entry(entry: object) -> bool:
    """Return whether GJC reports the registration as session-autoloaded."""
    return isinstance(entry, dict) and entry.get("runtimeStatus") == MCP_RUNTIME_STATUS


def persisted_gjc_mcp_entry(path: Path | None = None) -> dict[str, object] | None:
    """Read an active durable registration without requiring the GJC launcher."""
    config_path = path or gjc_mcp_config_path()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    disabled = payload.get("disabledServers", [])
    if not isinstance(disabled, list) or "ouroboros" in disabled:
        return None
    servers = payload.get("mcpServers")
    entry = servers.get("ouroboros") if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _generation_matches(claimed: Path, expected_raw: str) -> bool:
    """Return whether the claimed config is exactly the generation that was read."""
    try:
        return claimed.read_text(encoding="utf-8") == expected_raw
    except (OSError, UnicodeDecodeError):
        return False


def _atomic_replace_json(path: Path, payload: dict[str, object], expected_raw: str) -> None:
    """Publish one JSON generation bound to the exact generation that was read.

    The rewrite runs as a claim-and-verify compare-and-swap: the live config
    is claimed, re-validated against *expected_raw*, and the new document is
    published with a no-replace rename — a write by GJC itself or an operator
    between the read and the publication is preserved and this mutation fails
    with "changed concurrently" instead of overwriting it.
    """
    from ouroboros.core.fs_ownership import UnownedArtifactError, publish_owned_file

    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except OSError as exc:
        raise OSError("GJC MCP config changed concurrently") from exc
    try:
        publish_owned_file(
            path,
            json.dumps(payload, indent=2) + "\n",
            is_owned=lambda claimed: _generation_matches(claimed, expected_raw),
            mode=mode,
            trusted_ancestor=path.parent,
            require_existing=True,
        )
    except UnownedArtifactError as exc:
        raise OSError("GJC MCP config changed concurrently") from exc


def gjc_mcp_registration_lock() -> AbstractContextManager[None]:
    """Cross-process lock serializing Ouroboros mutations of the durable registration.

    Every Ouroboros code path that creates or removes the ``ouroboros`` entry
    in GJC's durable MCP config must run under this lock, so one invocation's
    failure cleanup can never be attributed to a registration another
    invocation created concurrently.
    """
    return file_lock(gjc_mcp_config_path())


def remove_persisted_gjc_mcp_server(path: Path | None = None) -> bool:
    """Atomically remove only the setup-owned generation, preserving concurrent state."""
    config_path = path or gjc_mcp_config_path()
    if config_path.is_symlink():
        return False
    try:
        with file_lock(config_path):
            return remove_persisted_gjc_mcp_server_locked(config_path)
    except OSError:
        return False


def remove_persisted_gjc_mcp_server_locked(path: Path | None = None) -> bool:
    """Remove the setup-owned generation; the caller must hold the registration lock."""
    config_path = path or gjc_mcp_config_path()
    try:
        if config_path.is_symlink():
            return False
        raw = config_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        servers = payload.get("mcpServers") if isinstance(payload, dict) else None
        entry = servers.get("ouroboros") if isinstance(servers, dict) else None
        disabled = payload.get("disabledServers", []) if isinstance(payload, dict) else []
        if not isinstance(disabled, list) or "ouroboros" in disabled:
            return False
        if not is_setup_managed_gjc_mcp_entry(entry):
            return False
        del servers["ouroboros"]
        _atomic_replace_json(config_path, payload, raw)
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True
