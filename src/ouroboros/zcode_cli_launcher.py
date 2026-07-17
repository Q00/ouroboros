"""Shared ZCode CLI launch helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib

ZCODE_SCRIPT_SUFFIXES = (".cjs", ".js", ".mjs")
ZCODE_NODE_BUNDLE_METADATA = ".node-bundle-meta.json"
ZCODE_ELECTRON_NODE_RUNTIME = "electron-node"


def resolve_zcode_electron_node_path(cli_path: str | Path | None) -> str | None:
    """Return the bundled Electron executable for an official ZCode app script."""
    if not cli_path:
        return None

    path = Path(str(cli_path))
    if path.suffix.lower() not in ZCODE_SCRIPT_SUFFIXES:
        return None

    contents_dir = next(
        (
            parent
            for parent in path.parents
            if parent.name == "Contents" and parent.parent.suffix.lower() == ".app"
        ),
        None,
    )
    if contents_dir is None:
        return None

    metadata_path = path.with_name(ZCODE_NODE_BUNDLE_METADATA)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except OSError as exc:
        msg = f"ZCode app-bundle CLI metadata is missing or unreadable: {metadata_path}: {exc}"
        raise RuntimeError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"ZCode app-bundle CLI metadata is invalid JSON: {metadata_path}: {exc}"
        raise RuntimeError(msg) from exc

    if not isinstance(metadata, dict):
        msg = f"ZCode app-bundle CLI metadata must be a JSON object: {metadata_path}"
        raise RuntimeError(msg)

    runtime = metadata.get("runtime")
    if runtime != ZCODE_ELECTRON_NODE_RUNTIME:
        msg = (
            "ZCode app-bundle CLI metadata declares unsupported runtime "
            f"{runtime!r} in {metadata_path}; expected {ZCODE_ELECTRON_NODE_RUNTIME!r}"
        )
        raise RuntimeError(msg)

    entry = metadata.get("entry")
    if entry != path.name:
        msg = (
            "ZCode app-bundle CLI metadata entry does not match the configured "
            f"script in {metadata_path}: expected {path.name!r}, got {entry!r}"
        )
        raise RuntimeError(msg)

    info_plist = contents_dir / "Info.plist"
    try:
        with info_plist.open("rb") as stream:
            bundle_info = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException) as exc:
        msg = f"ZCode app bundle metadata is present but {info_plist} is unreadable: {exc}"
        raise RuntimeError(msg) from exc

    if not isinstance(bundle_info, dict):
        msg = f"ZCode app bundle metadata must be a dictionary: {info_plist}"
        raise RuntimeError(msg)

    executable_name = bundle_info.get("CFBundleExecutable")
    if not isinstance(executable_name, str) or not executable_name:
        msg = f"ZCode app bundle is missing CFBundleExecutable in {info_plist}"
        raise RuntimeError(msg)
    if executable_name in {".", ".."} or "/" in executable_name or "\\" in executable_name:
        msg = (
            "ZCode app bundle CFBundleExecutable must be a file name without "
            f"path separators: {executable_name!r} in {info_plist}"
        )
        raise RuntimeError(msg)

    electron_node = contents_dir / "MacOS" / executable_name
    if not electron_node.is_file() or not os.access(electron_node, os.X_OK):
        msg = (
            "ZCode app bundle declares an electron-node CLI but its bundled "
            f"runtime is not executable: {electron_node}"
        )
        raise RuntimeError(msg)
    return str(electron_node)


def build_zcode_command_prefix(cli_path: str | Path, electron_node_path: str | None) -> list[str]:
    """Return the correct command prefix for a ZCode CLI path."""
    cli_path_str = str(cli_path)
    if electron_node_path is not None:
        return [electron_node_path, cli_path_str]
    if cli_path_str.lower().endswith(ZCODE_SCRIPT_SUFFIXES):
        return ["node", cli_path_str]
    return [cli_path_str]
