"""v28 bridge-plugin install hardening tests.

Covers atomic write, content-hash skip, and plugin-entry dedupe logic in
:mod:`ouroboros.cli.commands.setup`. Complements
:mod:`tests.unit.cli.test_bridge_plugin_lifecycle` which covers base install
and uninstall behaviour.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from ouroboros.cli.commands.setup import (
    _atomic_write_text,
    _ensure_opencode_plugin_entry,
    _install_opencode_bridge_plugin,
    _is_bridge_plugin_entry,
)

# Patch targets — both namespaces so internal calls through
# find_opencode_config() also resolve to the test directory.
_OCD_SETUP = "ouroboros.cli.commands.setup.opencode_config_dir"
_OCD_CONFIG = "ouroboros.cli.opencode_config.opencode_config_dir"

# ── helpers ──────────────────────────────────────────────────────


def _patch_source(content: str):
    """Return a context manager that makes the importlib source return *content*."""
    src = MagicMock()
    src.read_text.return_value = content
    pkg = MagicMock()
    pkg.joinpath.return_value = src
    return patch("importlib.resources.files", return_value=pkg)


# ── content-hash skip ────────────────────────────────────────────


class TestContentHashSkip:
    """v28: identical content → no rewrite, no mtime bump."""

    def test_identical_content_skips_write(self, tmp_path: Path) -> None:
        content = "// v28 bridge\n"
        oc_dir = tmp_path / "opencode"
        dest = oc_dir / "plugins" / "ouroboros-bridge" / "ouroboros-bridge.ts"
        dest.parent.mkdir(parents=True)
        dest.write_text(content)
        original_mtime = dest.stat().st_mtime_ns
        original_inode = dest.stat().st_ino

        # Sleep-equivalent: force a distinct mtime tick if a write DID happen
        os.utime(dest, ns=(original_mtime - 1_000_000_000, original_mtime - 1_000_000_000))
        baseline_mtime = dest.stat().st_mtime_ns

        with (
            patch(_OCD_SETUP, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            _patch_source(content),
        ):
            _install_opencode_bridge_plugin()

        # mtime untouched → no write happened
        assert dest.stat().st_mtime_ns == baseline_mtime
        # inode untouched → no atomic replace happened
        assert dest.stat().st_ino == original_inode
        assert dest.read_text() == content

    def test_different_content_triggers_write(self, tmp_path: Path) -> None:
        oc_dir = tmp_path / "opencode"
        dest = oc_dir / "plugins" / "ouroboros-bridge" / "ouroboros-bridge.ts"
        dest.parent.mkdir(parents=True)
        dest.write_text("// old v27\n")

        with (
            patch(_OCD_SETUP, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            _patch_source("// new v28\n"),
        ):
            _install_opencode_bridge_plugin()

        assert dest.read_text() == "// new v28\n"


# ── atomic write ─────────────────────────────────────────────────


class TestAtomicWrite:
    """Crash mid-write must never leave a corrupted .ts file."""

    def test_atomic_write_replaces_atomically(self, tmp_path: Path) -> None:
        target = tmp_path / "nest" / "file.ts"
        _atomic_write_text(target, "hello\n")
        assert target.read_text() == "hello\n"
        # No leftover temp files
        leftovers = list(target.parent.glob(".file.ts.*"))
        assert leftovers == []

    def test_mid_write_failure_preserves_original(self, tmp_path: Path) -> None:
        target = tmp_path / "file.ts"
        target.write_text("original\n")

        # Force os.replace to fail → tempfile must be cleaned, original intact
        with patch("os.replace", side_effect=OSError("boom")):
            import pytest

            with pytest.raises(OSError):
                _atomic_write_text(target, "new content\n")

        assert target.read_text() == "original\n"
        leftovers = list(target.parent.glob(".file.ts.*"))
        assert leftovers == []

    def test_install_uses_atomic_write(self, tmp_path: Path) -> None:
        """Install path never leaves partial .ts when os.replace fails."""
        oc_dir = tmp_path / "opencode"
        dest_parent = oc_dir / "plugins" / "ouroboros-bridge"

        with (
            patch(_OCD_SETUP, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            _patch_source("// content\n"),
            patch("os.replace", side_effect=OSError("disk full")),
            patch("ouroboros.cli.commands.setup.print_warning") as warn,
        ):
            _install_opencode_bridge_plugin()

        # Warning surfaced, no partial .ts left
        warn.assert_called_once()
        assert not (dest_parent / "ouroboros-bridge.ts").exists()
        leftovers = list(dest_parent.glob(".ouroboros-bridge.ts.*"))
        assert leftovers == []


# ── _is_bridge_plugin_entry ──────────────────────────────────────


class TestIsBridgePluginEntry:
    """Matcher must accept all known bridge paths and reject everything else."""

    def test_canonical_linux_path(self) -> None:
        assert _is_bridge_plugin_entry(
            "/home/alice/.config/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts"
        )

    def test_canonical_macos_path(self) -> None:
        assert _is_bridge_plugin_entry(
            "/Users/bob/Library/Application Support/OpenCode/plugins/ouroboros-bridge/ouroboros-bridge.ts"
        )

    def test_windows_backslash_path(self) -> None:
        assert _is_bridge_plugin_entry(
            r"C:\Users\carol\AppData\Roaming\OpenCode\plugins\ouroboros-bridge\ouroboros-bridge.ts"
        )

    def test_xdg_shifted_path(self) -> None:
        assert _is_bridge_plugin_entry(
            "/tmp/xdg/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts"
        )

    def test_sudo_root_path(self) -> None:
        assert _is_bridge_plugin_entry(
            "/root/.config/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts"
        )

    def test_rejects_non_bridge_plugin(self) -> None:
        assert not _is_bridge_plugin_entry(
            "/home/alice/.config/opencode/plugins/other-plugin/other.ts"
        )

    def test_rejects_wrong_basename(self) -> None:
        assert not _is_bridge_plugin_entry(
            "/home/alice/.config/opencode/plugins/ouroboros-bridge/index.ts"
        )

    def test_rejects_wrong_subdir(self) -> None:
        assert not _is_bridge_plugin_entry(
            "/home/alice/.config/opencode/plugins/bridge/ouroboros-bridge.ts"
        )

    def test_rejects_empty_and_non_string(self) -> None:
        assert not _is_bridge_plugin_entry("")
        assert not _is_bridge_plugin_entry(None)
        assert not _is_bridge_plugin_entry(42)
        assert not _is_bridge_plugin_entry({"path": "foo"})

    def test_rejects_short_path(self) -> None:
        assert not _is_bridge_plugin_entry("ouroboros-bridge.ts")
        assert not _is_bridge_plugin_entry("ouroboros-bridge/ouroboros-bridge.ts")


# ── _ensure_opencode_plugin_entry ────────────────────────────────


class TestEnsurePluginEntry:
    """Plugin-array management: append, dedupe, idempotent, atomic."""

    def _setup_cfg(self, tmp_path: Path, data: dict | None) -> Path:
        cfg_dir = tmp_path / "opencode"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = cfg_dir / "opencode.json"
        if data is not None:
            cfg.write_text(json.dumps(data))
        return cfg

    def _canonical(self, tmp_path: Path) -> str:
        return str(tmp_path / "opencode" / "plugins" / "ouroboros-bridge" / "ouroboros-bridge.ts")

    def test_appends_when_missing(self, tmp_path: Path) -> None:
        cfg = self._setup_cfg(tmp_path, {"provider": "x"})
        oc_dir = tmp_path / "opencode"

        with patch(_OCD_SETUP, return_value=oc_dir), patch(_OCD_CONFIG, return_value=oc_dir):
            _ensure_opencode_plugin_entry()

        data = json.loads(cfg.read_text())
        assert data["plugin"] == [self._canonical(tmp_path)]
        assert data["provider"] == "x"  # preserved

    def test_creates_config_when_missing(self, tmp_path: Path) -> None:
        oc_dir = tmp_path / "opencode"
        cfg = oc_dir / "opencode.json"

        with patch(_OCD_SETUP, return_value=oc_dir), patch(_OCD_CONFIG, return_value=oc_dir):
            _ensure_opencode_plugin_entry()

        data = json.loads(cfg.read_text())
        assert data["plugin"] == [self._canonical(tmp_path)]

    def test_idempotent_when_already_registered(self, tmp_path: Path) -> None:
        canonical = self._canonical(tmp_path)
        cfg = self._setup_cfg(tmp_path, {"plugin": [canonical]})
        mtime_before = cfg.stat().st_mtime_ns
        os.utime(cfg, ns=(mtime_before - 1_000_000_000, mtime_before - 1_000_000_000))
        baseline = cfg.stat().st_mtime_ns
        oc_dir = tmp_path / "opencode"

        with patch(_OCD_SETUP, return_value=oc_dir), patch(_OCD_CONFIG, return_value=oc_dir):
            _ensure_opencode_plugin_entry()

        # No rewrite → mtime unchanged
        assert cfg.stat().st_mtime_ns == baseline

    def test_dedupes_stale_bridge_entries(self, tmp_path: Path) -> None:
        stale1 = "/old/home/.config/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts"
        stale2 = "/root/.config/opencode/plugins/ouroboros-bridge/ouroboros-bridge.ts"
        other = "/home/alice/.config/opencode/plugins/other/other.ts"
        cfg = self._setup_cfg(tmp_path, {"plugin": [stale1, other, stale2]})
        oc_dir = tmp_path / "opencode"

        with patch(_OCD_SETUP, return_value=oc_dir), patch(_OCD_CONFIG, return_value=oc_dir):
            _ensure_opencode_plugin_entry()

        data = json.loads(cfg.read_text())
        # Non-bridge plugin kept, stales removed, canonical appended last
        assert data["plugin"] == [other, self._canonical(tmp_path)]

    def test_repoints_single_stale_to_canonical(self, tmp_path: Path) -> None:
        stale = "/wrong/path/plugins/ouroboros-bridge/ouroboros-bridge.ts"
        cfg = self._setup_cfg(tmp_path, {"plugin": [stale]})
        oc_dir = tmp_path / "opencode"

        with patch(_OCD_SETUP, return_value=oc_dir), patch(_OCD_CONFIG, return_value=oc_dir):
            _ensure_opencode_plugin_entry()

        data = json.loads(cfg.read_text())
        assert data["plugin"] == [self._canonical(tmp_path)]

    def test_rewrites_non_dict_root(self, tmp_path: Path) -> None:
        """A bare array at root is replaced with a proper dict."""
        oc_dir = tmp_path / "opencode"
        oc_dir.mkdir(parents=True, exist_ok=True)
        cfg = oc_dir / "opencode.json"
        cfg.write_text(json.dumps(["broken"]))

        with patch(_OCD_SETUP, return_value=oc_dir), patch(_OCD_CONFIG, return_value=oc_dir):
            _ensure_opencode_plugin_entry()

        data = json.loads(cfg.read_text())
        assert isinstance(data, dict)
        assert data["plugin"] == [self._canonical(tmp_path)]

    def test_skips_on_parse_error(self, tmp_path: Path) -> None:
        oc_dir = tmp_path / "opencode"
        oc_dir.mkdir(parents=True, exist_ok=True)
        cfg = oc_dir / "opencode.json"
        cfg.write_text("{ this is not json")

        with (
            patch(_OCD_SETUP, return_value=oc_dir),
            patch(_OCD_CONFIG, return_value=oc_dir),
            patch("ouroboros.cli.commands.setup.print_warning") as warn,
        ):
            _ensure_opencode_plugin_entry()

        warn.assert_called_once()
        # File left untouched
        assert cfg.read_text() == "{ this is not json"

    def test_atomic_json_write(self, tmp_path: Path) -> None:
        """JSON config written atomically — no leftover temp files."""
        cfg = self._setup_cfg(tmp_path, {})
        oc_dir = tmp_path / "opencode"

        with patch(_OCD_SETUP, return_value=oc_dir), patch(_OCD_CONFIG, return_value=oc_dir):
            _ensure_opencode_plugin_entry()

        leftovers = list(cfg.parent.glob(".opencode.json.*"))
        assert leftovers == []

    def test_multiple_runs_converge(self, tmp_path: Path) -> None:
        """Running N times = running once."""
        self._setup_cfg(
            tmp_path,
            {"plugin": ["/stale1/plugins/ouroboros-bridge/ouroboros-bridge.ts"]},
        )
        oc_dir = tmp_path / "opencode"
        with patch(_OCD_SETUP, return_value=oc_dir), patch(_OCD_CONFIG, return_value=oc_dir):
            _ensure_opencode_plugin_entry()
            _ensure_opencode_plugin_entry()
            _ensure_opencode_plugin_entry()

        cfg = oc_dir / "opencode.json"
        data = json.loads(cfg.read_text())
        assert data["plugin"] == [self._canonical(tmp_path)]
