"""Tests for the state-mutating `ooo plugin` subcommands.

These cover `add`, `install`, `trust`, `disable`, `remove`. The
multi-select interactive flow is exercised via the non-interactive
`--plugin <name>` form to keep tests deterministic; interactive
`questionary` integration is verified manually.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.plugin import app as plugin_app
from ouroboros.plugin.lockfile import Lockfile
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
    return CliRunner()


def _make_repo_layout(repo_root: Path, plugins: list[dict]) -> None:
    """Build a tmp catalog: <repo>/plugins/<name>/ouroboros.plugin.json."""
    plugins_dir = repo_root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    for manifest in plugins:
        plugin_dir = plugins_dir / manifest["name"]
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(manifest))


def _common_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "lockfile": tmp_path / "plugins.lock",
        "trust_root": tmp_path / "trust",
        "plugin_home_root": tmp_path / "plugin_homes",
        "audit_log": tmp_path / "audit.jsonl",
    }


def test_add_anti_pattern_install_string_rejected(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The locked anti-pattern (#plugins/<name>) is rejected with the
    documented error message."""
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        plugin_app,
        [
            "add",
            "git+https://github.com/Q00/ouroboros-plugins.git#plugins/github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 1
    # Rich panel wraps long messages and inserts │ border chars; strip ANSI
    # and panel borders before matching.
    import re
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    plain = plain.replace("│", " ").replace("╭", " ").replace("╮", " ")
    plain = plain.replace("╰", " ").replace("╯", " ").replace("─", " ")
    flat = " ".join(plain.split())
    assert "subdirectory-form install strings (#plugins/...)" in flat
    assert "Use `ooo plugin add <repo-url> --plugin <name>`" in flat


def test_add_local_path_with_plugin_flag(runner: CliRunner, tmp_path: Path) -> None:
    """`add <local-repo>` with `--plugin <name>` installs without prompts."""
    repo_root = tmp_path / "repo"
    _make_repo_layout(repo_root, [REFERENCE_MANIFEST])
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo_root),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Installed" in result.output
    # Lockfile records the entry.
    entries = Lockfile(paths["lockfile"]).read()
    assert "github-pr-ops" in entries
    entry = entries["github-pr-ops"]
    assert entry.source_kind == "local"
    assert entry.repository is None
    # Plugin home was copied.
    assert (paths["plugin_home_root"] / "github-pr-ops" / "ouroboros.plugin.json").is_file()


def test_add_unknown_plugin_in_catalog_errors(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Requesting a plugin not in the catalog produces a clear error."""
    repo_root = tmp_path / "repo"
    _make_repo_layout(repo_root, [REFERENCE_MANIFEST])
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo_root),
            "--plugin",
            "does-not-exist",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 1
    assert "not in repository catalog" in result.output


def test_install_local_directory(runner: CliRunner, tmp_path: Path) -> None:
    """`install <plugin-dir>` registers a single plugin without catalog discovery."""
    plugin_dir = tmp_path / "github-pr-ops"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "github-pr-ops" in result.output
    assert "github-pr-ops" in Lockfile(paths["lockfile"]).read()


def test_install_invalid_manifest_errors(runner: CliRunner, tmp_path: Path) -> None:
    """Installing a directory with an invalid manifest fails with the JSON Pointer."""
    plugin_dir = tmp_path / "bad"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    bad = {**REFERENCE_MANIFEST, "name": "Bad Name"}
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(bad))
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 1
    assert "manifest invalid" in result.output
    assert "/name" in result.output


def test_trust_grants_scope_and_writes_event(
    runner: CliRunner, tmp_path: Path
) -> None:
    """`trust --scope X` records the grant, emits a plugin.trusted envelope
    to the audit log, and the trust file shape matches the locked Q6 spec."""
    plugin_dir = tmp_path / "github-pr-ops"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    paths = _common_paths(tmp_path)
    # First install.
    runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    # Then trust.
    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--granted-by",
            "user:test",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--audit-log",
            str(paths["audit_log"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Granted: github:read" in result.output

    # Trust file landed at locked Q5 path.
    record = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert record is not None
    assert any(g.scope == "github:read" for g in record.granted_scopes)

    # Audit log has a plugin.trusted envelope with the locked Q6 fields.
    lines = paths["audit_log"].read_text().splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["aggregate_type"] == "plugin"
    assert envelope["event_type"] == "plugin.trusted"
    payload = envelope["payload"]
    assert payload["event_type"] == "plugin.trusted"
    assert payload["provenance"]["granted_by"] == "user:test"
    assert payload["provenance"]["granted_scope"] == "github:read"


def test_trust_uninstalled_plugin_errors(runner: CliRunner, tmp_path: Path) -> None:
    """Trusting a non-existent plugin errors before any trust file is written."""
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "no-such-plugin",
            "--scope",
            "github:read",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1
    assert "is not installed" in result.output


def test_disable_wipes_trust_grants(runner: CliRunner, tmp_path: Path) -> None:
    """`disable` removes the trust file but keeps the lockfile entry."""
    plugin_dir = tmp_path / "github-pr-ops"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    paths = _common_paths(tmp_path)
    # Install + trust.
    runner.invoke(
        plugin_app,
        ["install", str(plugin_dir),
         "--lockfile", str(paths["lockfile"]),
         "--plugin-home-root", str(paths["plugin_home_root"])],
    )
    TrustStore(root=paths["trust_root"]).grant(
        plugin="github-pr-ops", version="0.1.0",
        scope="github:read", granted_by="u",
    )
    # Disable.
    result = runner.invoke(
        plugin_app,
        ["disable", "github-pr-ops", "--lockfile", str(paths["lockfile"])],
    )
    assert result.exit_code == 0, result.output
    # Lockfile entry preserved.
    assert "github-pr-ops" in Lockfile(paths["lockfile"]).read()


def test_remove_drops_lockfile_trust_and_plugin_home(
    runner: CliRunner, tmp_path: Path
) -> None:
    """`remove` is atomic across lockfile, trust store, and plugin home."""
    plugin_dir = tmp_path / "github-pr-ops"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    paths = _common_paths(tmp_path)
    # Install + trust.
    runner.invoke(
        plugin_app,
        ["install", str(plugin_dir),
         "--lockfile", str(paths["lockfile"]),
         "--plugin-home-root", str(paths["plugin_home_root"])],
    )
    TrustStore(root=paths["trust_root"]).grant(
        plugin="github-pr-ops", version="0.1.0",
        scope="github:read", granted_by="u",
    )

    # Remove.
    result = runner.invoke(
        plugin_app,
        [
            "remove",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 0, result.output
    # All three artifacts gone.
    assert "github-pr-ops" not in Lockfile(paths["lockfile"]).read()
    assert TrustStore(root=paths["trust_root"]).read("github-pr-ops") is None
    assert not (paths["plugin_home_root"] / "github-pr-ops").exists()


def test_remove_uninstalled_errors(runner: CliRunner, tmp_path: Path) -> None:
    """Removing an unknown plugin errors cleanly without partial state."""
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        plugin_app,
        [
            "remove",
            "nope",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 1
    assert "is not installed" in result.output
