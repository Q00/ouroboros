"""Read the installed Ouroboros plugin version through the selected Codex CLI."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
import sys
from typing import Literal

_PLUGIN_ID = "ouroboros@ouroboros"
_QUERY_TIMEOUT_SECONDS = 5
_RELEASE_VERSION = re.compile(
    r"v?[0-9]+(?:\.[0-9]+)*"
    r"(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
    r"(?:\+[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*)?"
)


@dataclass(frozen=True)
class CodexPluginVersion:
    """A read-only inventory result; unknown must not imply uninstalled."""

    status: Literal["installed", "not_installed", "unknown"]
    version: str | None = None
    reason: str | None = None


def _parse_inventory(output: str) -> CodexPluginVersion:
    try:
        inventory = json.loads(output)
    except (ValueError, TypeError):
        return CodexPluginVersion("unknown", reason="Codex returned invalid plugin JSON")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("installed"), list):
        return CodexPluginVersion(
            "unknown", reason="Codex returned an unsupported plugin inventory"
        )

    matches = []
    for entry in inventory["installed"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("pluginId"), str):
            return CodexPluginVersion(
                "unknown", reason="Codex returned a malformed installed entry"
            )
        if entry["pluginId"] == _PLUGIN_ID:
            matches.append(entry)

    if not matches:
        return CodexPluginVersion("not_installed")
    if len(matches) != 1:
        return CodexPluginVersion("unknown", reason="Codex returned duplicate Ouroboros entries")
    entry = matches[0]
    if entry.get("installed") is False:
        return CodexPluginVersion("not_installed")
    if entry.get("installed") is not True:
        return CodexPluginVersion("unknown", reason="Codex did not confirm the plugin installation")
    version = entry.get("version")
    if (
        not isinstance(version, str)
        or len(version) > 128
        or _RELEASE_VERSION.fullmatch(version) is None
    ):
        return CodexPluginVersion("unknown", reason="Codex did not report a release plugin version")
    return CodexPluginVersion("installed", version=version)


def inspect_codex_plugin(codex_executable: str | None) -> CodexPluginVersion:
    """Query installed metadata without changing marketplace refs or configuration.

    The inherited environment preserves the caller's CODEX_HOME. Errors remain
    advisory and contain no command output, which may include local paths or
    arbitrary plugin metadata. Available marketplace versions are not evidence
    of the installed plugin version.
    """
    if not codex_executable:
        return CodexPluginVersion("unknown", reason="Codex executable is unavailable")
    try:
        result = subprocess.run(
            [codex_executable, "plugin", "list", "--marketplace", "ouroboros", "--json"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=_QUERY_TIMEOUT_SECONDS,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except subprocess.TimeoutExpired:
        return CodexPluginVersion("unknown", reason="Codex plugin query timed out")
    except (OSError, ValueError, subprocess.SubprocessError):
        return CodexPluginVersion("unknown", reason="Codex plugin query could not be completed")
    if result.returncode != 0:
        return CodexPluginVersion("unknown", reason="Codex plugin query exited unsuccessfully")
    return _parse_inventory(result.stdout)


__all__ = ["CodexPluginVersion", "inspect_codex_plugin"]
