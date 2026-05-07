"""Tests for the read-only `ooo plugin` CLI subcommands.

State-mutating subcommands (add, install, trust, disable, remove) live
in the follow-up PR.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.plugin import app as plugin_app
from ouroboros.plugin.lockfile import LockEntry, Lockfile
from ouroboros.plugin.trust_store import TrustStore

REFERENCE_MANIFEST: dict = {
    "schema_version": "0.1",
    "name": "github-pr-ops",
    "version": "0.1.0",
    "description": "Reference plugin for PR operational workflows.",
    "source": {"type": "local_path", "path": "plugins/github-pr-ops"},
    "commands": [
        {
            "namespace": "github-pr",
            "name": "review",
            "summary": "Review a pull request and summarize readiness.",
            "usage": "ooo github-pr review <pull-request-url>",
            "risk": "read_only",
            "requires_confirmation": False,
        }
    ],
    "capabilities": [
        {"name": "ledger", "access": "write"},
    ],
    "permissions": [
        {"scope": "github:read", "risk": "read_only", "required": True},
    ],
    "entrypoint": {"type": "command", "command": "python -m github_pr_ops"},
}


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False) if "mix_stderr" in CliRunner.__init__.__code__.co_varnames else CliRunner()


def _write_manifest(dir_: Path, payload: dict) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    target = dir_ / "ouroboros.plugin.json"
    target.write_text(json.dumps(payload))
    return target


def test_discover_valid_manifest(runner: CliRunner, tmp_path: Path) -> None:
    """`ooo plugin discover <dir>` accepts a directory argument and prints
    the manifest summary on success."""
    plugin_dir = tmp_path / "github-pr-ops"
    _write_manifest(plugin_dir, REFERENCE_MANIFEST)
    result = runner.invoke(plugin_app, ["discover", str(plugin_dir)])
    assert result.exit_code == 0, result.output
    assert "github-pr-ops" in result.output
    assert "0.1.0" in result.output
    assert "github:read" in result.output  # required scope listed


def test_discover_invalid_manifest_exits_nonzero(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A schema-violating manifest produces a friendly error and exit 1."""
    bad = {**REFERENCE_MANIFEST, "name": "Bad Name"}  # whitespace breaks pattern
    plugin_dir = tmp_path / "bad"
    _write_manifest(plugin_dir, bad)
    result = runner.invoke(plugin_app, ["discover", str(plugin_dir)])
    assert result.exit_code == 1
    assert "manifest invalid" in result.output
    assert "/name" in result.output  # JSON Pointer surfaced


def test_inspect_uninstalled_plugin_errors(runner: CliRunner, tmp_path: Path) -> None:
    """`inspect <name>` errors when the plugin is not in the lockfile."""
    lock_path = tmp_path / "plugins.lock"
    trust_root = tmp_path / "trust"
    result = runner.invoke(
        plugin_app,
        [
            "inspect",
            "github-pr-ops",
            "--lockfile",
            str(lock_path),
            "--trust-root",
            str(trust_root),
        ],
    )
    assert result.exit_code == 1
    assert "is not installed" in result.output


def test_inspect_installed_untrusted(
    runner: CliRunner, tmp_path: Path
) -> None:
    """An installed-but-untrusted plugin reports trust_state=installed and
    flags the missing required scope."""
    plugin_home = tmp_path / "plugin_home"
    _write_manifest(plugin_home, REFERENCE_MANIFEST)
    lock_path = tmp_path / "plugins.lock"
    Lockfile(lock_path).add(
        LockEntry(
            name="github-pr-ops",
            version="0.1.0",
            source_kind="local",
            repository=None,
            git_sha=None,
            manifest_checksum="sha256:0",
            installed_at="2026-05-08T00:00:00Z",
            plugin_home=str(plugin_home),
        )
    )
    result = runner.invoke(
        plugin_app,
        [
            "inspect",
            "github-pr-ops",
            "--lockfile",
            str(lock_path),
            "--trust-root",
            str(tmp_path / "trust"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "github-pr-ops 0.1.0" in result.output
    assert "trust_state" in result.output
    assert "installed" in result.output
    assert "missing scopes" in result.output
    assert "github:read" in result.output


def test_inspect_installed_trusted(runner: CliRunner, tmp_path: Path) -> None:
    """A plugin with all required scopes granted reports trust_state=trusted
    and no missing scopes."""
    plugin_home = tmp_path / "plugin_home"
    _write_manifest(plugin_home, REFERENCE_MANIFEST)
    lock_path = tmp_path / "plugins.lock"
    trust_root = tmp_path / "trust"
    Lockfile(lock_path).add(
        LockEntry(
            name="github-pr-ops",
            version="0.1.0",
            source_kind="local",
            repository=None,
            git_sha=None,
            manifest_checksum="sha256:0",
            installed_at="2026-05-08T00:00:00Z",
            plugin_home=str(plugin_home),
        )
    )
    TrustStore(root=trust_root).grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    result = runner.invoke(
        plugin_app,
        [
            "inspect",
            "github-pr-ops",
            "--lockfile",
            str(lock_path),
            "--trust-root",
            str(trust_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "trusted" in result.output
    assert "missing scopes" not in result.output


def test_list_empty(runner: CliRunner, tmp_path: Path) -> None:
    """`list` on an empty lockfile prints the no-plugins notice."""
    lock_path = tmp_path / "plugins.lock"
    result = runner.invoke(
        plugin_app,
        [
            "list",
            "--lockfile",
            str(lock_path),
            "--trust-root",
            str(tmp_path / "trust"),
        ],
    )
    assert result.exit_code == 0
    assert "no plugins installed" in result.output


def test_list_json_output(runner: CliRunner, tmp_path: Path) -> None:
    """`list --json` emits a parseable JSON array."""
    plugin_home = tmp_path / "ph"
    _write_manifest(plugin_home, REFERENCE_MANIFEST)
    lock_path = tmp_path / "plugins.lock"
    trust_root = tmp_path / "trust"
    Lockfile(lock_path).add(
        LockEntry(
            name="github-pr-ops",
            version="0.1.0",
            source_kind="local",
            repository=None,
            git_sha=None,
            manifest_checksum="sha256:0",
            installed_at="2026-05-08T00:00:00Z",
            plugin_home=str(plugin_home),
        )
    )
    TrustStore(root=trust_root).grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
    )
    result = runner.invoke(
        plugin_app,
        [
            "list",
            "--json",
            "--lockfile",
            str(lock_path),
            "--trust-root",
            str(trust_root),
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.strip())
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["name"] == "github-pr-ops"
    assert data[0]["trust_state"] == "trusted"
    assert data[0]["granted_scopes"] == ["github:read"]


def test_no_args_shows_help(runner: CliRunner) -> None:
    """`ooo plugin` with no subcommand prints help (Typer no_args_is_help)."""
    result = runner.invoke(plugin_app, [])
    # With no_args_is_help=True, Typer emits help and exit code 0 or 2.
    assert "discover" in result.output
    assert "inspect" in result.output
    assert "list" in result.output
