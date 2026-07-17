"""Tests for shared ZCode CLI launcher helpers."""

from __future__ import annotations

import json
from pathlib import Path
import plistlib
from typing import Any

import pytest

from ouroboros.zcode_cli_launcher import (
    build_zcode_command_prefix,
    resolve_zcode_electron_node_path,
)


def _fake_electron_node_bundle(tmp_path: Path) -> tuple[Path, Path]:
    contents = tmp_path / "ZCode.app" / "Contents"
    cli_path = contents / "Resources" / "glm" / "zcode.cjs"
    electron_node = contents / "MacOS" / "ZCode"
    cli_path.parent.mkdir(parents=True)
    electron_node.parent.mkdir(parents=True)
    cli_path.write_text("// zcode", encoding="utf-8")
    cli_path.with_name(".node-bundle-meta.json").write_text(
        json.dumps(
            {
                "runtime": "electron-node",
                "entry": "zcode.cjs",
                "platform": "darwin-arm64",
            }
        ),
        encoding="utf-8",
    )
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": "ZCode"}, stream)
    electron_node.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    electron_node.chmod(0o755)
    return cli_path, electron_node


def test_resolve_app_bundle_electron_node_path(tmp_path: Path) -> None:
    cli_path, electron_node = _fake_electron_node_bundle(tmp_path)

    resolved = resolve_zcode_electron_node_path(cli_path)

    assert resolved == str(electron_node)
    assert build_zcode_command_prefix(str(cli_path), resolved) == [
        str(electron_node),
        str(cli_path),
    ]


def test_standalone_script_uses_system_node_without_metadata(tmp_path: Path) -> None:
    cli_path = tmp_path / "zcode.cjs"
    cli_path.write_text("// standalone zcode", encoding="utf-8")

    assert resolve_zcode_electron_node_path(cli_path) is None
    assert build_zcode_command_prefix(cli_path, None) == ["node", str(cli_path)]


def test_path_executable_runs_directly() -> None:
    assert build_zcode_command_prefix("/usr/local/bin/zcode", None) == ["/usr/local/bin/zcode"]


@pytest.mark.parametrize(
    ("metadata_content", "error_match"),
    [
        (None, "missing or unreadable"),
        ("{", "invalid JSON"),
        (json.dumps([]), "must be a JSON object"),
        (json.dumps({"runtime": "node", "entry": "zcode.cjs"}), "unsupported runtime"),
        (
            json.dumps({"runtime": "electron-node", "entry": "other.cjs"}),
            "entry does not match",
        ),
        (json.dumps({"runtime": "electron-node"}), "entry does not match"),
    ],
)
def test_app_bundle_invalid_node_metadata_fails_closed(
    tmp_path: Path,
    metadata_content: str | None,
    error_match: str,
) -> None:
    cli_path, _ = _fake_electron_node_bundle(tmp_path)
    metadata_path = cli_path.with_name(".node-bundle-meta.json")
    if metadata_content is None:
        metadata_path.unlink()
    else:
        metadata_path.write_text(metadata_content, encoding="utf-8")

    with pytest.raises(RuntimeError, match=error_match):
        resolve_zcode_electron_node_path(cli_path)


def test_app_bundle_unreadable_node_metadata_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_path, _ = _fake_electron_node_bundle(tmp_path)
    metadata_path = cli_path.with_name(".node-bundle-meta.json")
    original_read_text = Path.read_text

    def _raise_for_metadata(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == metadata_path:
            raise PermissionError("denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_metadata)

    with pytest.raises(RuntimeError, match="missing or unreadable"):
        resolve_zcode_electron_node_path(cli_path)


def test_app_bundle_non_dictionary_plist_fails_closed(tmp_path: Path) -> None:
    cli_path, _ = _fake_electron_node_bundle(tmp_path)
    info_plist = cli_path.parents[2] / "Info.plist"
    with info_plist.open("wb") as stream:
        plistlib.dump(["not", "a", "dictionary"], stream)

    with pytest.raises(RuntimeError, match="must be a dictionary"):
        resolve_zcode_electron_node_path(cli_path)


@pytest.mark.parametrize("executable_name", ["../ZCode", "nested/ZCode", r"..\ZCode", ".."])
def test_app_bundle_rejects_executable_path_traversal(
    tmp_path: Path,
    executable_name: str,
) -> None:
    cli_path, _ = _fake_electron_node_bundle(tmp_path)
    info_plist = cli_path.parents[2] / "Info.plist"
    with info_plist.open("wb") as stream:
        plistlib.dump({"CFBundleExecutable": executable_name}, stream)

    with pytest.raises(RuntimeError, match="without path separators"):
        resolve_zcode_electron_node_path(cli_path)


def test_app_bundle_missing_electron_runtime_fails_with_actionable_error(
    tmp_path: Path,
) -> None:
    cli_path, electron_node = _fake_electron_node_bundle(tmp_path)
    electron_node.unlink()

    with pytest.raises(RuntimeError, match="electron-node CLI"):
        resolve_zcode_electron_node_path(cli_path)
