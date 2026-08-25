"""Ownership judgment and durable-state primitives for GJC MCP registrations.

Every function here is side-effect free except
:func:`remove_persisted_gjc_mcp_server`, which mutates only a registration
that exactly matches setup's own generation. Progress narration and GJC CLI
invocations live in :mod:`ouroboros.cli.gjc_setup`.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import stat
import tempfile

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


def _atomic_replace_json(path: Path, payload: dict[str, object], expected_raw: str) -> None:
    """Publish one JSON generation without following a config symlink."""
    mode = stat.S_IMODE(path.lstat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.is_symlink() or path.read_text(encoding="utf-8") != expected_raw:
            raise OSError("GJC MCP config changed concurrently")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(temp_name).unlink()
        except OSError:
            pass
        raise


def remove_persisted_gjc_mcp_server(path: Path | None = None) -> bool:
    """Atomically remove only the setup-owned generation, preserving concurrent state."""
    config_path = path or gjc_mcp_config_path()
    if config_path.is_symlink():
        return False
    try:
        with file_lock(config_path):
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
