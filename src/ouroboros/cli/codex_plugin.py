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
    r"v?(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:(?P<pre_kind>a|b|rc)(?P<pre_number>[0-9]+))?"
    r"(?:\.post(?P<post>[0-9]+))?(?:\.dev(?P<dev>[0-9]+))?"
    r"(?:\+(?P<local>[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*))?"
)


@dataclass(frozen=True)
class CodexPluginVersion:
    """A read-only inventory result; unknown must not imply uninstalled."""

    status: Literal["installed", "not_installed", "unknown"]
    version: str | None = None
    reason: str | None = None


def _version_identity(version: str) -> tuple[object, ...] | None:
    """Normalize equality only for the release forms accepted by this inventory."""
    if len(version) > 128 or (match := _RELEASE_VERSION.fullmatch(version)) is None:
        return None
    release = [int(part) for part in match["release"].split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    local = (
        tuple(
            int(part) if part.isdecimal() else part.lower()
            for part in re.split(r"[._-]", match["local"])
        )
        if match["local"] is not None
        else None
    )
    return (
        tuple(release),
        (match["pre_kind"], int(match["pre_number"])) if match["pre_kind"] else None,
        int(match["post"]) if match["post"] is not None else None,
        int(match["dev"]) if match["dev"] is not None else None,
        local,
    )


def plugin_versions_match(plugin_version: str, package_version: str) -> bool | None:
    """Compare installed identities independently of optional packaging imports.

    This is not an ordering key for the updater. Unsupported forms are unknown,
    rather than a match inferred from a lossy release-prefix comparison.
    """
    plugin, package = _version_identity(plugin_version), _version_identity(package_version)
    return None if plugin is None or package is None else plugin == package


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
    # Windows text-mode pipe readers decode in background threads, where decode
    # errors evade the caller's handler. Decode only the inventory here; unused
    # stderr stays bytes and cannot emit a reader-thread traceback.
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return CodexPluginVersion("unknown", reason="Codex returned invalid plugin JSON")
    return _parse_inventory(output)


__all__ = ["CodexPluginVersion", "inspect_codex_plugin", "plugin_versions_match"]
