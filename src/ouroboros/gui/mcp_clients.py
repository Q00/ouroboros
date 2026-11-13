"""Point MCP hosts at the shared server instead of a per-session stdio process.

Supported hosts and the entry this writes:

* Codex — ``[mcp_servers.ouroboros] url = …`` in ``~/.codex/config.toml``
* Kiro, Copilot, Cursor — ``mcpServers.ouroboros = {"url": …}`` (Copilot adds
  ``"type": "http"``)
* Gemini CLI — ``mcpServers.ouroboros = {"httpUrl": …}``

Before the first change to a host, its previous Ouroboros entry is saved in
``~/.ouroboros/gui/mcp-wiring.json``; turning sharing off puts that entry
back (or, for Codex with no saved entry, the managed stdio entry ``ouroboros
setup`` writes). Other servers in the same files are never touched. Claude Code
gets Ouroboros from its plugin, which this module leaves alone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import tomllib
from typing import Any

from ouroboros.gui.daemon import mcp_url


class ClientWiringError(Exception):
    """The host's config could not be read or changed safely."""


@dataclass(frozen=True, slots=True)
class HostSpec:
    id: str
    label: str
    path: Callable[[], Path]
    kind: str  # "codex" | "json"
    url_key: str = "url"
    extra: tuple[tuple[str, str], ...] = ()


def _home() -> Path:
    return Path.home()


def _codex_config() -> Path:
    return Path(os.environ.get("CODEX_HOME") or _home() / ".codex").expanduser() / "config.toml"


HOSTS: tuple[HostSpec, ...] = (
    HostSpec("codex", "Codex", _codex_config, "codex"),
    HostSpec("kiro", "Kiro CLI", lambda: _home() / ".kiro" / "settings" / "mcp.json", "json"),
    HostSpec(
        "copilot",
        "GitHub Copilot CLI",
        lambda: _home() / ".copilot" / "mcp-config.json",
        "json",
        extra=(("type", "http"),),
    ),
    HostSpec(
        "gemini", "Gemini CLI", lambda: _home() / ".gemini" / "settings.json", "json", "httpUrl"
    ),
    HostSpec("cursor", "Cursor", lambda: _home() / ".cursor" / "mcp.json", "json"),
)
_BY_ID = {host.id: host for host in HOSTS}


def _wiring_path() -> Path:
    from ouroboros.config.models import get_config_dir

    path = get_config_dir() / "gui" / "mcp-wiring.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_wiring() -> dict[str, Any]:
    try:
        data = json.loads(_wiring_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_wiring(data: dict[str, Any]) -> None:
    tmp = _wiring_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(_wiring_path())


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ClientWiringError(f"{path} is a symlink; edit it by hand.")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".ouroboros-bak"))
    tmp = path.with_name(f".{path.name}.ouroboros-tmp")
    tmp.write_text(text, encoding="utf-8")
    if path.exists():
        shutil.copymode(path, tmp)
    tmp.replace(path)


# ── Codex ──────────────────────────────────────────────────────────────


def _codex_entry(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ClientWiringError(f"Could not read {path}: {exc}") from exc
    servers = data.get("mcp_servers")
    entry = servers.get("ouroboros") if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _codex_set(path: Path, url: str | None, previous: str | None) -> None:
    from ouroboros.cli.commands.setup import (
        _remove_codex_mcp_section,
        _render_codex_mcp_section,
        _upsert_codex_mcp_section,
    )

    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    if url is not None:
        # No comment lines: setup's section remover keeps comments, so one would
        # be left behind when the previous entry is restored.
        section = f'[mcp_servers.ouroboros]\nurl = "{url}"\n'
        updated, _ = _upsert_codex_mcp_section(raw, section)
    elif previous:
        updated, _ = _upsert_codex_mcp_section(raw, previous)
    else:
        section = _render_codex_mcp_section()
        updated = (
            _upsert_codex_mcp_section(raw, section)[0]
            if section
            else _remove_codex_mcp_section(raw)[0]
        )
    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise ClientWiringError(f"Refusing to write invalid TOML to {path}: {exc}") from exc
    _atomic_write(path, updated)


def _codex_previous_section(path: Path) -> str | None:
    """The exact lines of the current Ouroboros entry, so it can be put back verbatim."""
    import difflib

    from ouroboros.cli.commands.setup import _remove_codex_mcp_section

    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    cleaned, existed = _remove_codex_mcp_section(raw)
    if not existed:
        return None
    removed = [
        line[2:]
        for line in difflib.ndiff(raw.splitlines(), cleaned.splitlines())
        if line.startswith("- ")
    ]
    return "\n".join(removed).strip("\n") + "\n" if removed else None


# ── JSON hosts ─────────────────────────────────────────────────────────


def _json_load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError) as exc:
        raise ClientWiringError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ClientWiringError(f"{path} is not a JSON object.")
    return data


def _json_entry(path: Path) -> dict[str, Any] | None:
    servers = _json_load(path).get("mcpServers")
    entry = servers.get("ouroboros") if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _json_set(path: Path, entry: dict[str, Any] | None) -> None:
    data = _json_load(path)
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    if not isinstance(servers, dict):
        raise ClientWiringError(f"{path}: mcpServers is not an object.")
    if entry is None:
        servers.pop("ouroboros", None)
    else:
        servers["ouroboros"] = entry
    _atomic_write(path, json.dumps(data, indent=2) + "\n")


# ── public ─────────────────────────────────────────────────────────────


def _entry_url(host: HostSpec, entry: dict[str, Any] | None) -> str | None:
    if not entry:
        return None
    value = entry.get(host.url_key) or entry.get("url") or entry.get("httpUrl")
    return value if isinstance(value, str) else None


def status(port: int) -> list[dict[str, Any]]:
    shared = mcp_url(port)
    rows = []
    for host in HOSTS:
        path = host.path()
        try:
            entry = _codex_entry(path) if host.kind == "codex" else _json_entry(path)
            error = None
        except ClientWiringError as exc:
            entry, error = None, str(exc)
        url = _entry_url(host, entry)
        state = (
            "shared" if url == shared else "other_url" if url else "stdio" if entry else "absent"
        )
        rows.append(
            {
                "id": host.id,
                "label": host.label,
                "path": str(path),
                "exists": path.exists() or path.parent.exists(),
                "state": state,
                "error": error,
            }
        )
    return rows


def set_shared(client_id: str, use_shared: bool, *, port: int) -> None:
    host = _BY_ID.get(client_id)
    if host is None:
        raise ClientWiringError(f"{client_id} cannot use the shared server from here.")
    path = host.path()
    wiring = _load_wiring()
    saved = wiring.get(client_id) or {}
    url = mcp_url(port)
    if host.kind == "codex":
        entry = _codex_entry(path)
        if use_shared:
            if _entry_url(host, entry) != url and "previous" not in saved:
                wiring[client_id] = {"previous": _codex_previous_section(path) if entry else None}
                _save_wiring(wiring)
            _codex_set(path, url, None)
        else:
            _codex_set(path, None, saved.get("previous"))
            wiring.pop(client_id, None)
            _save_wiring(wiring)
        return
    entry = _json_entry(path)
    if use_shared:
        if _entry_url(host, entry) != url and "previous" not in saved:
            wiring[client_id] = {"previous": entry}
            _save_wiring(wiring)
        _json_set(path, {**dict(host.extra), host.url_key: url})
    else:
        previous = saved.get("previous")
        _json_set(path, previous if isinstance(previous, dict) else None)
        wiring.pop(client_id, None)
        _save_wiring(wiring)


__all__ = ["HOSTS", "ClientWiringError", "set_shared", "status"]
