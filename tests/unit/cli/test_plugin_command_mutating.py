"""Tests for the state-mutating `ooo plugin` subcommands.

These cover `add`, `install`, `trust`, `disable`, `remove`. The
multi-select interactive flow is exercised via the non-interactive
`--plugin <name>` form to keep tests deterministic; interactive
`questionary` integration is verified manually.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

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


def test_add_anti_pattern_install_string_rejected(runner: CliRunner, tmp_path: Path) -> None:
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


def test_add_unknown_plugin_in_catalog_errors(runner: CliRunner, tmp_path: Path) -> None:
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


def test_trust_grants_scope_and_writes_event(runner: CliRunner, tmp_path: Path) -> None:
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
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    TrustStore(root=paths["trust_root"]).grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    # Disable.
    result = runner.invoke(
        plugin_app,
        ["disable", "github-pr-ops", "--lockfile", str(paths["lockfile"])],
    )
    assert result.exit_code == 0, result.output
    # Lockfile entry preserved.
    assert "github-pr-ops" in Lockfile(paths["lockfile"]).read()


def test_remove_drops_lockfile_trust_and_plugin_home(runner: CliRunner, tmp_path: Path) -> None:
    """`remove` is atomic across lockfile, trust store, and plugin home."""
    plugin_dir = tmp_path / "github-pr-ops"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    paths = _common_paths(tmp_path)
    # Install + trust.
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
    TrustStore(root=paths["trust_root"]).grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
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


def test_remove_lockfile_failure_preserves_installed_state(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If lockfile removal fails, `remove` must not clear trust/disable state.

    The lockfile is the source of truth for installation. A failed
    lockfile commit means the plugin is still installed, so trust grants,
    disable records, and plugin bytes must all remain in place.
    """
    plugin_dir = tmp_path / "github-pr-ops"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    (plugin_dir / "marker.txt").write_text("installed")
    paths = _common_paths(tmp_path)

    installed = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert installed.exit_code == 0, installed.output
    entry = Lockfile(paths["lockfile"]).read()["github-pr-ops"]
    trust = TrustStore(root=paths["trust_root"])
    trust.grant(
        plugin="github-pr-ops",
        version=entry.version,
        scope="github:read",
        granted_by="user:test",
        source_type=entry.source_type,
        source_identity=entry.source_identity,
        artifact_digest=entry.artifact_digest,
    )
    trust.write_disable(
        "github-pr-ops",
        source_type=entry.source_type,
        source_identity=entry.source_identity,
        disabled_by="user:test",
    )

    def _fail_write_atomic(*_args, **_kwargs):
        raise OSError("simulated lockfile write failure")

    monkeypatch.setattr(
        "ouroboros.plugin.lockfile.Lockfile._write_atomic",
        _fail_write_atomic,
    )
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
    assert result.exit_code == 1
    assert "could not finalize remove" in result.output

    assert "github-pr-ops" in Lockfile(paths["lockfile"]).read()
    assert (paths["plugin_home_root"] / "github-pr-ops" / "marker.txt").read_text() == "installed"
    after_trust = trust.read("github-pr-ops")
    assert after_trust is not None
    assert any(g.scope == "github:read" for g in after_trust.granted_scopes)
    assert trust.is_disabled_for_subject(
        "github-pr-ops",
        source_type=entry.source_type,
        source_identity=entry.source_identity,
    )
    assert trust.read_disable("github-pr-ops") is not None


def test_remove_retry_cleans_stale_state_after_trust_wipe_failure(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If trust cleanup fails after lock removal, a second remove can recover."""
    plugin_dir = tmp_path / "github-pr-ops"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    (plugin_dir / "marker.txt").write_text("installed")
    paths = _common_paths(tmp_path)

    installed = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert installed.exit_code == 0, installed.output
    entry = Lockfile(paths["lockfile"]).read()["github-pr-ops"]
    trust = TrustStore(root=paths["trust_root"])
    trust.grant(
        plugin="github-pr-ops",
        version=entry.version,
        scope="github:read",
        granted_by="user:test",
        source_type=entry.source_type,
        source_identity=entry.source_identity,
        artifact_digest=entry.artifact_digest,
    )
    trust.write_disable(
        "github-pr-ops",
        source_type=entry.source_type,
        source_identity=entry.source_identity,
        disabled_by="user:test",
    )

    original_clear_disable = TrustStore.clear_disable

    def _fail_clear_disable(self, plugin):  # noqa: ANN001
        raise OSError("simulated disabled.json unlink failure")

    monkeypatch.setattr(TrustStore, "clear_disable", _fail_clear_disable)
    first = runner.invoke(
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
    assert first.exit_code == 1
    assert "could not finalize remove" in first.output
    assert "github-pr-ops" not in Lockfile(paths["lockfile"]).read()
    assert trust.is_disabled("github-pr-ops")
    assert (paths["plugin_home_root"] / "github-pr-ops" / "marker.txt").is_file()

    monkeypatch.setattr(TrustStore, "clear_disable", original_clear_disable)
    retry = runner.invoke(
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
    assert retry.exit_code == 0, retry.output
    assert "Removed stale state" in retry.output
    assert "github-pr-ops" not in Lockfile(paths["lockfile"]).read()
    assert TrustStore(root=paths["trust_root"]).read("github-pr-ops") is None
    assert not TrustStore(root=paths["trust_root"]).is_disabled("github-pr-ops")
    assert not (paths["plugin_home_root"] / "github-pr-ops").exists()


def test_install_failure_preserves_existing_trust_grants(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed reinstall MUST NOT wipe the trust file of the still-active
    install. Earlier the implementation reset trust before swapping the
    plugin home — if `copytree` then failed, the old version remained
    installed but its grants were already gone, so the user lost
    invocability of an unchanged install.
    """
    paths = _common_paths(tmp_path)
    # Install v0.1.0 + grant scope.
    plugin_dir_v1 = tmp_path / "src_v1"
    plugin_dir_v1.mkdir()
    (plugin_dir_v1 / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir_v1),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    runner.invoke(
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
        ],
    )
    # Sanity: trust granted at v0.1.0.
    before = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert before is not None
    assert before.version == "0.1.0"
    assert any(g.scope == "github:read" for g in before.granted_scopes)

    # Try to reinstall at v0.2.0 with a forced copytree failure.
    plugin_dir_v2 = tmp_path / "src_v2"
    plugin_dir_v2.mkdir()
    payload_v2 = {**REFERENCE_MANIFEST, "version": "0.2.0"}
    (plugin_dir_v2 / "ouroboros.plugin.json").write_text(json.dumps(payload_v2))

    def _boom_copytree(*_args, **_kwargs):
        raise OSError("simulated mid-install failure")

    monkeypatch.setattr("ouroboros.cli.commands.plugin.shutil.copytree", _boom_copytree)
    bad = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir_v2),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert bad.exit_code != 0

    # Trust file MUST still reflect the original v0.1.0 grant — the
    # install never succeeded, so trust must not have been reset.
    after = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert after is not None
    assert after.version == "0.1.0", (
        f"failed reinstall must not invalidate trust of the still-active "
        f"install; record was reset to {after.version!r}"
    )
    assert any(g.scope == "github:read" for g in after.granted_scopes)


def test_install_failure_preserves_existing_plugin_home(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed reinstall MUST leave the previously-installed plugin home
    intact (no data loss). Per Q00/ouroboros-plugins#9 atomic-install lock.
    """
    plugin_dir_v1 = tmp_path / "src_v1"
    plugin_dir_v1.mkdir()
    (plugin_dir_v1 / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    (plugin_dir_v1 / "marker.txt").write_text("v1-marker")
    paths = _common_paths(tmp_path)
    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir_v1),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert result.exit_code == 0, result.output
    installed_home = paths["plugin_home_root"] / "github-pr-ops"
    assert (installed_home / "marker.txt").read_text() == "v1-marker"

    plugin_dir_v2 = tmp_path / "src_v2"
    plugin_dir_v2.mkdir()
    payload_v2 = {**REFERENCE_MANIFEST, "version": "0.2.0"}
    (plugin_dir_v2 / "ouroboros.plugin.json").write_text(json.dumps(payload_v2))
    (plugin_dir_v2 / "marker.txt").write_text("v2-marker")

    def _boom(*_args, **_kwargs):
        raise OSError("simulated disk full during copytree")

    monkeypatch.setattr("ouroboros.cli.commands.plugin.shutil.copytree", _boom)
    bad = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir_v2),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
        ],
    )
    assert bad.exit_code != 0
    assert (installed_home / "marker.txt").read_text() == "v1-marker"
    siblings = list(paths["plugin_home_root"].iterdir())
    assert {p.name for p in siblings} == {"github-pr-ops"}, siblings
    assert Lockfile(paths["lockfile"]).read()["github-pr-ops"].version == "0.1.0"


def test_install_version_bump_invalidates_trust(runner: CliRunner, tmp_path: Path) -> None:
    """Reinstalling at a different version MUST clear prior trust grants.

    Per Q00/ouroboros-plugins#9 Q4 lock — the user must re-consent against
    the new version, regardless of how the upgrade arrived.
    """
    paths = _common_paths(tmp_path)
    plugin_dir_v1 = tmp_path / "src_v1"
    plugin_dir_v1.mkdir()
    (plugin_dir_v1 / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir_v1),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    runner.invoke(
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
        ],
    )
    record = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert record is not None
    assert record.version == "0.1.0"
    assert any(g.scope == "github:read" for g in record.granted_scopes)

    plugin_dir_v2 = tmp_path / "src_v2"
    plugin_dir_v2.mkdir()
    payload_v2 = {**REFERENCE_MANIFEST, "version": "0.2.0"}
    (plugin_dir_v2 / "ouroboros.plugin.json").write_text(json.dumps(payload_v2))
    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir_v2),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 0, result.output

    after = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert after is not None
    assert after.version == "0.2.0"
    assert after.granted_scopes == ()
    listed = runner.invoke(
        plugin_app,
        [
            "list",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert rows[0]["trust_state"] == "installed"
    assert rows[0]["granted_scopes"] == []


def test_add_version_bump_invalidates_trust(runner: CliRunner, tmp_path: Path) -> None:
    """Same as the install variant, driven through `ooo plugin add`."""
    paths = _common_paths(tmp_path)

    repo_root = tmp_path / "repo"
    _make_repo_layout(repo_root, [REFERENCE_MANIFEST])
    runner.invoke(
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
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    runner.invoke(
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
        ],
    )

    bumped = {**REFERENCE_MANIFEST, "version": "0.2.0"}
    (repo_root / "plugins" / "github-pr-ops" / "ouroboros.plugin.json").write_text(
        json.dumps(bumped)
    )
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
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 0, result.output

    after = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert after is not None
    assert after.version == "0.2.0"
    assert after.granted_scopes == ()


def test_disable_honors_trust_root_override(runner: CliRunner, tmp_path: Path) -> None:
    """`disable --trust-root <custom>` MUST remove the trust file under
    that root — not silently target the default `~/.ouroboros/plugins`.
    """
    paths = _common_paths(tmp_path)
    plugin_dir = tmp_path / "src"
    plugin_dir.mkdir()
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    TrustStore(root=paths["trust_root"]).grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="u",
    )
    assert (paths["trust_root"] / "github-pr-ops" / "trust.json").is_file()

    result = runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert not (paths["trust_root"] / "github-pr-ops" / "trust.json").exists()
    assert "github-pr-ops" in Lockfile(paths["lockfile"]).read()


def test_add_normalizes_git_plus_https_url(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git+https://...` install strings must be normalized to `https://...`
    before being passed to `git clone` — the `git+` prefix is a Python
    packaging convention that Git itself rejects.

    Regression catch for the bot's BLOCKING finding on plugin.py:361.
    """
    paths = _common_paths(tmp_path)

    # Capture every subprocess.run() invocation so we can assert what URL
    # actually reaches `git clone`.
    seen_argvs: list[list[str]] = []

    real_run = subprocess.run

    def _spy(argv, *args, **kwargs):
        seen_argvs.append(list(argv))
        # Materialize the "cloned" repo on disk so the rest of the flow
        # finds a catalog. Exit early before the second `git rev-parse`
        # call by writing a fake .git so cwd works.
        if argv[:3] == ["git", "clone", "--depth"]:
            dest = Path(argv[-1])
            (dest / "plugins" / "github-pr-ops").mkdir(parents=True, exist_ok=True)
            (dest / "plugins" / "github-pr-ops" / "ouroboros.plugin.json").write_text(
                json.dumps(REFERENCE_MANIFEST)
            )
            (dest / ".git").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="deadbeef\n", stderr=""
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("ouroboros.cli.commands.plugin.subprocess.run", _spy)

    result = runner.invoke(
        plugin_app,
        [
            "add",
            "git+https://github.com/Q00/ouroboros-plugins.git",
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--cache-root",
            str(tmp_path / "cache"),
        ],
    )
    assert result.exit_code == 0, result.output

    # Find the `git clone ...` invocation and confirm the URL had `git+`
    # stripped before reaching git.
    clone_calls = [a for a in seen_argvs if a[:2] == ["git", "clone"]]
    assert len(clone_calls) == 1, f"expected exactly one clone call, got {clone_calls}"
    cloned_url = clone_calls[0][-2]  # url is second-to-last (dest is last)
    assert cloned_url == "https://github.com/Q00/ouroboros-plugins.git", cloned_url
    assert not cloned_url.startswith("git+"), cloned_url


def test_add_skips_invalid_sibling_manifest_in_catalog(runner: CliRunner, tmp_path: Path) -> None:
    """A repo with one good plugin and one bad sibling manifest must allow
    `--plugin <good-one>` to proceed. The invalid sibling is reported as a
    `skip:` warning rather than aborting the whole install.

    Regression catch for the bot's follow-up on plugin.py:384 (catalog-wide
    pre-validation blocking installs from mixed-quality repos).
    """
    repo_root = tmp_path / "repo"
    plugins_dir = repo_root / "plugins"
    plugins_dir.mkdir(parents=True)
    # Good sibling.
    good_dir = plugins_dir / "github-pr-ops"
    good_dir.mkdir()
    (good_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    # Bad sibling — fails schema validation (name violates pattern).
    bad_dir = plugins_dir / "broken-one"
    bad_dir.mkdir()
    (bad_dir / "ouroboros.plugin.json").write_text(
        json.dumps({**REFERENCE_MANIFEST, "name": "Broken Name"})
    )
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
    # Bad sibling was reported but did not block the install.
    assert "skip" in result.output
    assert "broken-one" in result.output
    # Good plugin landed in the lockfile.
    entries = Lockfile(paths["lockfile"]).read()
    assert "github-pr-ops" in entries
    assert "broken-one" not in entries


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


# ---------------------------------------------------------------------------
# RFC-contract tests (`docs/rfc/userlevel-plugins.md`)
# ---------------------------------------------------------------------------


def _install_reference_plugin(
    runner: CliRunner,
    *,
    plugin_dir: Path,
    paths: dict[str, Path],
) -> None:
    """Helper: stamp a reference manifest at `plugin_dir` and install it."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )


def test_install_records_artifact_digest_in_lockfile(runner: CliRunner, tmp_path: Path) -> None:
    """The lockfile must record the canonical tree hash + source identity
    so the firewall can detect code substitution per the RFC.
    """
    paths = _common_paths(tmp_path)
    plugin_dir = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=plugin_dir, paths=paths)

    entries = Lockfile(paths["lockfile"]).read()
    entry = entries["github-pr-ops"]
    assert entry.source_type == "local_path"
    assert entry.source_identity, "source_identity must be recorded"
    assert entry.artifact_digest.startswith("sha256:")
    # Digest should match recomputing from disk.
    from ouroboros.plugin.digest import canonical_tree_hash

    on_disk = canonical_tree_hash(paths["plugin_home_root"] / "github-pr-ops")
    assert entry.artifact_digest == on_disk


def test_trust_binds_to_install_subject(runner: CliRunner, tmp_path: Path) -> None:
    """`trust` must record the lockfile's source_identity + digest on the
    trust file, so a future code-substitution invalidates the grant.
    """
    paths = _common_paths(tmp_path)
    plugin_dir = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=plugin_dir, paths=paths)
    runner.invoke(
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
        ],
    )
    record = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert record is not None
    assert record.source_type == "local_path"
    assert record.source_identity, "source_identity must be on the trust record"
    assert record.artifact_digest.startswith("sha256:")
    # Mismatched digest invalidates the subject — same trust record, but
    # passed a substituted digest, must not match.
    assert not record.matches_subject(
        version="0.1.0",
        source_type="local_path",
        source_identity=record.source_identity,
        artifact_digest="sha256:0000000000000000000000000000000000000000000000000000000000000000",
    )
    # And exact match still resolves.
    assert record.matches_subject(
        version="0.1.0",
        source_type="local_path",
        source_identity=record.source_identity,
        artifact_digest=record.artifact_digest,
    )


def test_install_same_version_different_source_invalidates_trust(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The RFC's "same-name reinstall under a different source" path: a
    second install of the same name+version from a DIFFERENT directory
    must NOT inherit the prior trust grants — the source_identity has
    changed, so the trust subject is fresh.
    """
    paths = _common_paths(tmp_path)
    src_a = tmp_path / "src_a"
    src_b = tmp_path / "src_b"
    _install_reference_plugin(runner, plugin_dir=src_a, paths=paths)
    runner.invoke(
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
        ],
    )
    pre = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert pre is not None and pre.has_scope("github:read")

    # Same version, same name, different source directory.
    src_b.mkdir()
    (src_b / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    runner.invoke(
        plugin_app,
        [
            "install",
            str(src_b),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    post = TrustStore(root=paths["trust_root"]).read("github-pr-ops")
    assert post is not None
    # Trust must have been reset because source_identity changed.
    assert post.granted_scopes == (), (
        f"reinstall from a different source must clear trust; got {post.granted_scopes}"
    )


def test_install_named_with_from_local_path(runner: CliRunner, tmp_path: Path) -> None:
    """RFC qualified form: `install <name> --from <local-path>` is the
    register-on-first-use entrypoint for local_path sources.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    catalog_state = tmp_path / "catalog-state.json"
    result = runner.invoke(
        plugin_app,
        [
            "install",
            "github-pr-ops",
            "--from",
            str(src.resolve()),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "github-pr-ops" in Lockfile(paths["lockfile"]).read()
    # Catalog registered the local_path entry.
    payload = json.loads(catalog_state.read_text())
    catalogs = payload["catalogs"]
    assert any(
        c["source_type"] == "local_path" and "github-pr-ops" in c["plugins"] for c in catalogs
    )


def test_install_default_form_resolves_via_known_catalog(runner: CliRunner, tmp_path: Path) -> None:
    """After `ooo plugin add` registers a catalog, `install <name>` with no
    `--from` must resolve through the catalog and re-install — the
    register-on-first-use contract per the RFC's "How sources enter the
    known catalog" section.
    """
    paths = _common_paths(tmp_path)
    repo_root = tmp_path / "repo"
    _make_repo_layout(repo_root, [REFERENCE_MANIFEST])
    catalog_state = tmp_path / "catalog-state.json"
    runner.invoke(
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
            "--catalog-state",
            str(catalog_state),
        ],
    )
    # Now remove the install but keep the catalog so the default form
    # has something to resolve against.
    runner.invoke(
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
    # Default form must hit the catalog and re-install without `--from`.
    result = runner.invoke(
        plugin_app,
        [
            "install",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "github-pr-ops" in Lockfile(paths["lockfile"]).read()


def test_install_named_default_form_with_no_known_catalog_errors(
    runner: CliRunner, tmp_path: Path
) -> None:
    """`install <name>` with no known catalog must error and tell the user
    how to recover (RFC: name BOTH `add <repo>` and the `--from <path>`
    qualified form so users with a local checkout aren't misdirected).
    """
    paths = _common_paths(tmp_path)
    catalog_state = tmp_path / "catalog-state.json"
    result = runner.invoke(
        plugin_app,
        [
            "install",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 1
    # Rich panel wraps long messages and inserts │ border chars; strip
    # ANSI + panel borders before matching.
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    plain = plain.replace("│", " ").replace("╭", " ").replace("╮", " ")
    plain = plain.replace("╰", " ").replace("╯", " ").replace("─", " ")
    flat = " ".join(plain.split())
    assert "not in any known catalog" in flat
    assert "ooo plugin add" in flat
    assert "--from" in flat


def test_disable_writes_record_persisting_across_install(runner: CliRunner, tmp_path: Path) -> None:
    """RFC: a disable record is keyed by (name, source.type, source_identity)
    without artifact_digest, so it survives upgrades (and any reinstall
    that lands the same source identity).
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)

    # Disable.
    res = runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert res.exit_code == 0, res.output
    trust = TrustStore(root=paths["trust_root"])
    assert trust.is_disabled("github-pr-ops")
    rec = trust.read_disable("github-pr-ops")
    assert rec is not None
    assert rec["source_type"] == "local_path"
    assert rec["source_identity"]

    # Re-install at a different version (artifact_digest WILL change)
    # but from the same source directory. The disable record must still
    # be present.
    bumped = {**REFERENCE_MANIFEST, "version": "0.2.0"}
    (src / "ouroboros.plugin.json").write_text(json.dumps(bumped))
    runner.invoke(
        plugin_app,
        [
            "install",
            str(src),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert trust.is_disabled("github-pr-ops"), (
        "disable record must survive upgrades — it is keyed without artifact_digest"
    )


def test_trust_clears_disable_record(runner: CliRunner, tmp_path: Path) -> None:
    """Re-trusting is the re-enable path per the RFC: it MUST clear any
    disable record AND grant the requested scope under the current
    install subject.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert TrustStore(root=paths["trust_root"]).is_disabled("github-pr-ops")
    runner.invoke(
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
        ],
    )
    trust = TrustStore(root=paths["trust_root"])
    assert not trust.is_disabled("github-pr-ops"), (
        "trust must clear the disable record (re-enable path)"
    )
    rec = trust.read("github-pr-ops")
    assert rec is not None and rec.has_scope("github:read")


def test_list_reflects_disabled_state(runner: CliRunner, tmp_path: Path) -> None:
    """`ooo plugin list --json` must surface `trust_state="disabled"` for a
    plugin with a disable record, regardless of whether it has a trust
    file. Aligns the CLI view with the firewall's pre-trust check.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    runner.invoke(
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
        ],
    )
    runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    listed = runner.invoke(
        plugin_app,
        [
            "list",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert rows[0]["trust_state"] == "disabled"


def test_list_drops_scopes_when_record_no_longer_matches_subject(
    runner: CliRunner, tmp_path: Path
) -> None:
    """When the lockfile-recorded artifact_digest drifts from the trust
    record's digest, `list --json` must show ``granted_scopes: []`` for
    that row — otherwise the trust_state ("installed") and the scopes
    list (still showing old grants) contradict each other.

    Regression catch for the bot's follow-up on plugin.py:436.
    """
    from ouroboros.plugin.lockfile import LockEntry, Lockfile

    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    runner.invoke(
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
        ],
    )
    # Force lockfile-recorded digest to drift away from the trust
    # record's digest (simulating in-place edit of installed bytes
    # caught at next install).
    lock = Lockfile(paths["lockfile"])
    entry = lock.read()["github-pr-ops"]
    drifted = LockEntry(
        name=entry.name,
        version=entry.version,
        source_kind=entry.source_kind,
        repository=entry.repository,
        git_sha=entry.git_sha,
        manifest_checksum=entry.manifest_checksum,
        installed_at=entry.installed_at,
        plugin_home=entry.plugin_home,
        source_type=entry.source_type,
        source_identity=entry.source_identity,
        artifact_digest=("sha256:0000000000000000000000000000000000000000000000000000000000000000"),
    )
    lock.add(drifted)
    listed = runner.invoke(
        plugin_app,
        [
            "list",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert rows[0]["trust_state"] == "installed"
    # Critical: scopes list is empty too, not stale grants from the
    # trust file.
    assert rows[0]["granted_scopes"] == [], (
        f"granted_scopes must reflect the same subject check as "
        f"trust_state; got {rows[0]['granted_scopes']!r}"
    )


def test_list_reflects_subject_drift_as_installed(runner: CliRunner, tmp_path: Path) -> None:
    """If the lockfile-recorded artifact_digest no longer matches the
    trust record's digest (e.g. an in-place edit happened after grant
    but before re-install), `list` must show `installed`, not `trusted`.
    """
    from ouroboros.plugin.lockfile import LockEntry, Lockfile

    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    runner.invoke(
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
        ],
    )
    # Manually rewrite the lockfile entry to record a different digest —
    # simulating bytes drift between the trust grant and the next inspect.
    lock = Lockfile(paths["lockfile"])
    entry = lock.read()["github-pr-ops"]
    drifted = LockEntry(
        name=entry.name,
        version=entry.version,
        source_kind=entry.source_kind,
        repository=entry.repository,
        git_sha=entry.git_sha,
        manifest_checksum=entry.manifest_checksum,
        installed_at=entry.installed_at,
        plugin_home=entry.plugin_home,
        source_type=entry.source_type,
        source_identity=entry.source_identity,
        artifact_digest=("sha256:0000000000000000000000000000000000000000000000000000000000000000"),
    )
    lock.add(drifted)
    listed = runner.invoke(
        plugin_app,
        [
            "list",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert rows[0]["trust_state"] == "installed"


def test_remove_clears_disable_record(runner: CliRunner, tmp_path: Path) -> None:
    """RFC: `remove` ALSO deletes the disable record so a fresh future
    install starts un-trusted-but-enabled (not silently disabled).
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert TrustStore(root=paths["trust_root"]).is_disabled("github-pr-ops")
    runner.invoke(
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
    assert not TrustStore(root=paths["trust_root"]).is_disabled("github-pr-ops")


# ---------------------------------------------------------------------------
# Bot review (commit 58095bd5) follow-ups
# ---------------------------------------------------------------------------


def test_add_routes_git_plus_ssh_url_through_clone(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git+ssh://...` install strings are documented as supported and
    `_normalize_clone_url()` strips the `git+` prefix; the URL detector
    must recognize them too, otherwise `add` mis-classifies them as a
    local path and exits with "not a directory".

    Regression catch for the bot's BLOCKING finding on plugin.py:558.
    """
    paths = _common_paths(tmp_path)
    seen_argvs: list[list[str]] = []
    real_run = subprocess.run

    def _spy(argv, *args, **kwargs):
        seen_argvs.append(list(argv))
        if argv[:3] == ["git", "clone", "--depth"]:
            dest = Path(argv[-1])
            (dest / "plugins" / "github-pr-ops").mkdir(parents=True, exist_ok=True)
            (dest / "plugins" / "github-pr-ops" / "ouroboros.plugin.json").write_text(
                json.dumps(REFERENCE_MANIFEST)
            )
            (dest / ".git").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="cafef00d\n", stderr=""
            )
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("ouroboros.cli.commands.plugin.subprocess.run", _spy)
    result = runner.invoke(
        plugin_app,
        [
            "add",
            "git+ssh://git@github.com/Q00/ouroboros-plugins.git",
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--cache-root",
            str(tmp_path / "cache"),
        ],
    )
    assert result.exit_code == 0, result.output
    clone_calls = [a for a in seen_argvs if a[:2] == ["git", "clone"]]
    assert len(clone_calls) == 1, clone_calls
    cloned_url = clone_calls[0][-2]
    # `git+` is stripped before reaching git, leaving a normal ssh URL.
    assert cloned_url == "ssh://git@github.com/Q00/ouroboros-plugins.git", cloned_url
    # And lockfile records source_kind="git".
    assert Lockfile(paths["lockfile"]).read()["github-pr-ops"].source_kind == "git"


def test_trust_rejects_undeclared_scope(runner: CliRunner, tmp_path: Path) -> None:
    """`ooo plugin trust --scope <typo>` must refuse to persist a grant
    for a scope the manifest does not declare. Otherwise the command
    silently records a phantom grant + emits `plugin.trusted` while the
    firewall still blocks invocation because the real required scope was
    never granted — a false success at the trust boundary.

    Regression catch for the bot's BLOCKING finding on plugin.py:1328.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:reed",  # typo of "github:read"
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
    assert result.exit_code == 1
    # Strip Rich panel borders before matching.
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    plain = plain.replace("│", " ").replace("╭", " ").replace("╮", " ")
    plain = plain.replace("╰", " ").replace("╯", " ").replace("─", " ")
    flat = " ".join(plain.split())
    assert "not declared" in flat
    assert "github:reed" in flat
    # No trust file written, no audit event emitted.
    assert TrustStore(root=paths["trust_root"]).read("github-pr-ops") is None
    assert not paths["audit_log"].exists() or paths["audit_log"].read_text() == ""


def test_install_refuses_reserved_top_level_name(runner: CliRunner, tmp_path: Path) -> None:
    """Per the RFC ("UX / Plugin name → command-namespace mapping"),
    `install` MUST refuse a manifest whose `name` collides with a
    reserved top-level command (e.g. `auto`, `run`, `plugin`). Otherwise
    the install would silently shadow the built-in dispatch.

    Regression catch for the bot's follow-up on plugin.py:927.
    """
    paths = _common_paths(tmp_path)
    plugin_dir = tmp_path / "src"
    plugin_dir.mkdir()
    bad = {**REFERENCE_MANIFEST, "name": "auto"}  # collides with `ooo auto`
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(bad))
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
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    plain = plain.replace("│", " ").replace("╭", " ").replace("╮", " ")
    plain = plain.replace("╰", " ").replace("╯", " ").replace("─", " ")
    flat = " ".join(plain.split())
    assert "reserved" in flat
    assert "auto" in flat
    # Lockfile must NOT have been written.
    assert "auto" not in Lockfile(paths["lockfile"]).read()


def test_trust_re_enables_zero_permission_plugin_with_no_scope_arg(
    runner: CliRunner, tmp_path: Path
) -> None:
    """RFC: trust is the re-enable path. A plugin whose manifest declares
    NO permissions has nothing to grant, so `ooo plugin trust <name>`
    must succeed without `--scope` and clear the disable record.

    Regression catch for the bot's BLOCKING finding on plugin.py:1342.
    """
    paths = _common_paths(tmp_path)
    zero_perm = {**REFERENCE_MANIFEST, "permissions": []}
    plugin_dir = tmp_path / "src"
    plugin_dir.mkdir()
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(zero_perm))
    runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    trust = TrustStore(root=paths["trust_root"])
    assert trust.is_disabled("github-pr-ops")

    # Re-enable WITHOUT --scope (manifest has no permissions to grant).
    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--granted-by",
            "user:test",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Re-enabled github-pr-ops" in result.output
    assert not trust.is_disabled("github-pr-ops")


def test_trust_without_scope_for_plugin_with_required_permissions_errors(
    runner: CliRunner, tmp_path: Path
) -> None:
    """For plugins with declared *required* permissions, bare
    `trust <name>` must still error so the user has to make an
    explicit grant decision — silently re-enabling without trust
    would be a permission-boundary surprise.

    The check is keyed on REQUIRED permissions only: optional
    permissions don't gate invocation, so refusing to re-enable a
    plugin whose only declared permissions are ``required: false``
    would leave that class of plugin permanently stuck after a
    disable.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)  # has required github:read
    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    plain = plain.replace("│", " ").replace("╭", " ").replace("╮", " ")
    plain = plain.replace("╰", " ").replace("╯", " ").replace("─", " ")
    flat = " ".join(plain.split())
    assert "declares required permissions" in flat
    assert "--scope" in flat


def test_remove_keeps_lockfile_consistent_when_rmtree_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`remove` MUST stay atomic at the lockfile/trust layer even if the
    on-disk `rmtree` fails. Previously bytes were deleted first and
    then trust+lockfile were mutated, so a `wipe_subject`/`lock.remove`
    failure could leave the plugin home gone but the lockfile still
    saying "installed". With the new ordering, lockfile/trust are
    cleared first, and an `rmtree` failure leaves a manually-removable
    leftover directory but does NOT corrupt the bookkeeping state.

    Regression catch for the bot's follow-up on plugin.py:1521.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    assert "github-pr-ops" in Lockfile(paths["lockfile"]).read()

    def _boom(*_args, **_kwargs):
        raise OSError("simulated rmtree failure (e.g. permission denied)")

    monkeypatch.setattr("ouroboros.cli.commands.plugin.shutil.rmtree", _boom)
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
    # Lockfile + trust state are consistent: plugin reported as removed.
    assert "github-pr-ops" not in Lockfile(paths["lockfile"]).read()
    assert TrustStore(root=paths["trust_root"]).read("github-pr-ops") is None
    # The CLI explicitly tells the user the bytes were not removed.
    assert "BYTES NOT REMOVED" in result.output


def test_default_trust_root_is_outside_plugin_install_root() -> None:
    """The default trust root MUST live OUTSIDE the plugin install root.

    If both defaulted to ``~/.ouroboros/plugins``, ``trust.json`` would
    be written inside the installed plugin subtree and the firewall's
    pre-invocation canonical-tree-hash check would see the digest drift
    on the very next invocation, blocking with ``trust_subject_changed``.

    Regression catch for the bot's BLOCKING finding on
    trust_store.py:35.
    """
    from ouroboros.plugin.trust_store import DEFAULT_TRUST_ROOT

    install_root = Path.home() / ".ouroboros" / "plugins"
    # The two default roots must not be the same directory, and the trust
    # root must not be a descendant of the install root.
    assert install_root != DEFAULT_TRUST_ROOT
    try:
        DEFAULT_TRUST_ROOT.relative_to(install_root)
    except ValueError:
        # NOT a descendant — that's the safe state.
        return
    raise AssertionError(
        f"DEFAULT_TRUST_ROOT={DEFAULT_TRUST_ROOT} is a descendant of "
        f"the plugin install root {install_root}; trust state would "
        "perturb the hashed artifact"
    )


def test_trust_does_not_perturb_installed_artifact_digest(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Granting trust must NOT change the on-disk digest of the
    installed plugin home. Otherwise the firewall would see the bytes
    drift on the very next invocation and block with
    ``trust_subject_changed``.
    """
    from ouroboros.plugin.digest import canonical_tree_hash

    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    plugin_home = paths["plugin_home_root"] / "github-pr-ops"
    digest_pre_trust = canonical_tree_hash(plugin_home)

    runner.invoke(
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
        ],
    )
    digest_post_trust = canonical_tree_hash(plugin_home)
    assert digest_post_trust == digest_pre_trust, (
        "trust must not perturb the installed artifact's canonical "
        f"tree hash; pre={digest_pre_trust} post={digest_post_trust}"
    )

    runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    digest_post_disable = canonical_tree_hash(plugin_home)
    assert digest_post_disable == digest_pre_trust, (
        "disable must not perturb the installed artifact's canonical "
        f"tree hash; pre={digest_pre_trust} post={digest_post_disable}"
    )


def test_disable_record_does_not_carry_to_different_source(
    runner: CliRunner, tmp_path: Path
) -> None:
    """RFC: disable records are keyed by (name, source.type,
    source_identity). A disable from source A MUST NOT carry over to a
    fresh install of the same plugin name from source B — the user
    explicitly chose to install from B and the stale disable signal
    should not silently block them.

    Regression catch for the bot's follow-up on trust_store.py:296.
    """
    paths = _common_paths(tmp_path)
    src_a = tmp_path / "src_a"
    _install_reference_plugin(runner, plugin_dir=src_a, paths=paths)
    runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    trust = TrustStore(root=paths["trust_root"])
    assert trust.is_disabled("github-pr-ops")  # name-only predicate fires

    # Now install from a DIFFERENT source directory (different
    # source_identity). The subject-scoped predicate must report False.
    src_b = tmp_path / "src_b"
    src_b.mkdir()
    (src_b / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    runner.invoke(
        plugin_app,
        [
            "install",
            str(src_b),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    entry = Lockfile(paths["lockfile"]).read()["github-pr-ops"]
    # Subject-scoped predicate: source_identity now points at src_b,
    # disable was recorded against src_a → predicate returns False.
    assert not trust.is_disabled_for_subject(
        "github-pr-ops",
        source_type=entry.source_type,
        source_identity=entry.source_identity,
    ), (
        "disable record from source A must NOT apply to a fresh install from source B; "
        f"recorded source_identity={entry.source_identity!r}"
    )
    # And `list` reports `installed`, not `disabled`, for the new install.
    listed = runner.invoke(
        plugin_app,
        [
            "list",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--json",
        ],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.stdout)
    assert rows[0]["trust_state"] == "installed"


def test_trust_audit_event_records_real_source_type(runner: CliRunner, tmp_path: Path) -> None:
    """`plugin.trusted` audit payloads must record the manifest's actual
    source.type, not a hardcoded ``plugin_home``. The reference manifest
    uses ``local_path`` — the audit event must say so.

    Regression catch for the bot's follow-up on plugin.py:1350.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
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
    lines = paths["audit_log"].read_text().splitlines()
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    payload = envelope["payload"]
    # Reference manifest has source.type=local_path, NOT plugin_home.
    assert payload["plugin"]["source_type"] == "local_path", payload["plugin"]


def test_atomic_replace_dir_preserves_symlinks(tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:225.

    ``_atomic_replace_dir`` MUST copy symlinks as links rather than
    dereferencing them. A manifest tree with `evil → /etc/passwd` would
    otherwise smuggle host-file contents into ``plugin_home`` and let
    the firewall hash them as if the plugin authored those bytes —
    both a privacy escalation and a digest-model contract violation,
    because ``canonical_tree_hash`` is explicitly designed to bind the
    artifact to the symlink target as a separate digest record.
    """
    from ouroboros.cli.commands.plugin import _atomic_replace_dir

    # Stage a "manifest tree" that includes a symlink pointing outside
    # itself — the same shape an attacker would produce.
    src = tmp_path / "src"
    src.mkdir()
    (src / "ouroboros.plugin.json").write_text("{}")
    target = tmp_path / "host_secret"
    target.write_text("HOST_SECRET")
    (src / "evil").symlink_to(target)

    dest = tmp_path / "installed"
    _atomic_replace_dir(src, dest)

    installed_link = dest / "evil"
    # The link itself must survive — not the dereferenced bytes.
    assert installed_link.is_symlink(), (
        "_atomic_replace_dir flattened a symlink; copytree(symlinks=True) is required"
    )
    # Sanity: the original target is unchanged (no copy of the host
    # secret should have landed inside plugin_home as a plain file).
    assert not (dest / "evil").is_file() or (dest / "evil").is_symlink()


def test_inspect_friendly_error_on_corrupt_lockfile(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:479.

    A damaged ``plugins.lock`` MUST surface a friendly recovery hint
    rather than a raw ``ValueError`` traceback. ``inspect`` is the
    operator's diagnostic tool — crashing it defeats its purpose.
    """
    paths = _common_paths(tmp_path)
    paths["lockfile"].write_text("not valid json {")

    result = runner.invoke(
        plugin_app,
        [
            "inspect",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1, result.output
    # The friendly hint names the file and points at the recovery flag.
    plain = " ".join(result.output.split())
    assert "lockfile is unreadable" in plain
    assert "--lockfile" in plain


def test_list_friendly_error_on_corrupt_lockfile(runner: CliRunner, tmp_path: Path) -> None:
    """Same regression catch as ``test_inspect_friendly_error_on_corrupt_lockfile``,
    but for ``list`` (which read-shares the same lockfile path).
    """
    paths = _common_paths(tmp_path)
    paths["lockfile"].write_text("{ partial json")

    result = runner.invoke(
        plugin_app,
        [
            "list",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "lockfile is unreadable" in plain


def test_add_persists_manifest_source_type_not_transport(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1109.

    The persisted ``source_type`` must come from
    ``manifest.source.type``, not from the install transport. If a
    manifest declares ``plugin_home`` but the install came from a
    local catalog, the lockfile MUST record ``plugin_home`` so the
    firewall's subject match (which keys on
    ``manifest.source.type``) succeeds after the user runs
    ``ooo plugin trust``. Recording the transport instead leaves the
    plugin permanently stuck in the ``installed`` state.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    # Manifest declares source.type=plugin_home (carries a repository
    # URL) but is installed via a local catalog directory — exactly
    # the mismatch the bot flagged.
    manifest = json.loads(json.dumps(REFERENCE_MANIFEST))
    manifest["source"] = {
        "type": "plugin_home",
        "path": "plugins/github-pr-ops",
        "repository": "https://github.com/Q00/ouroboros-plugins",
    }
    _make_repo_layout(repo, [manifest])

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(tmp_path / "plugin-catalogs.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    entries = Lockfile(paths["lockfile"]).read()
    entry = entries["github-pr-ops"]
    assert entry.source_type == "plugin_home", (
        f"persisted source_type must come from manifest.source.type "
        f"(plugin_home), not the install transport; got {entry.source_type!r}"
    )


def test_add_friendly_error_on_corrupt_catalog_state(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's follow-up on plugin.py:367.

    A truncated or malformed ``plugin-catalogs.json`` MUST produce a
    friendly recovery hint, not a raw traceback from ``json.load()``.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    _make_repo_layout(repo, [REFERENCE_MANIFEST])

    catalog_state = tmp_path / "plugin-catalogs.json"
    catalog_state.write_text("{ truncated json")

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "plugin catalog state" in plain
    assert "unreadable" in plain
    assert "Traceback" not in result.output


def test_install_by_name_routes_local_path_for_plugin_home_manifest(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1356.

    When ``add`` registers a plugin from a local catalog whose manifest
    declares ``source.type=plugin_home``, the catalog entry contains
    ``(source_type="plugin_home", source_identity="/abs/path/...")``.
    A subsequent bare ``ooo plugin install <name>`` must not route
    that to ``_install_named_from_url`` — there is no remote to clone,
    only a local directory. Routing must follow the *transport*
    (URL vs path), not the manifest's declared ``source_type``.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    manifest = json.loads(json.dumps(REFERENCE_MANIFEST))
    manifest["source"] = {
        "type": "plugin_home",
        "path": "plugins/github-pr-ops",
        "repository": "https://github.com/Q00/ouroboros-plugins",
    }
    _make_repo_layout(repo, [manifest])
    catalog_state = tmp_path / "plugin-catalogs.json"

    # Add via the local catalog — registers ``(plugin_home, /abs/...)``.
    add_result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert add_result.exit_code == 0, add_result.output

    # Wipe the install so `install <name>` actually has work to do —
    # this exercises the catalog-resolution path, not the no-op path.
    Lockfile(paths["lockfile"]).remove("github-pr-ops")
    plugin_home = paths["plugin_home_root"] / "github-pr-ops"
    if plugin_home.exists():
        import shutil as _sh

        _sh.rmtree(plugin_home)

    install_result = runner.invoke(
        plugin_app,
        [
            "install",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    # The previous routing logic would have shelled out to
    # `git clone <abs-path>` here and either failed or attempted a
    # nonsensical clone. With the fix it goes through the local-path
    # installer and succeeds.
    assert install_result.exit_code == 0, install_result.output
    assert "git clone failed" not in install_result.output


def test_inspect_first_party_does_not_report_missing_scopes(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:585.

    First-party programs bypass the user-facing trust flow at the
    firewall (their required permissions are implicitly trusted at
    boot). ``ooo plugin inspect`` MUST NOT report them as having
    missing scopes — that misleads operators about invocability and
    contradicts what the firewall actually does.

    External installs reject ``source.type == "first_party"`` (the
    privilege-escalation guard added on the same PR), so this test
    stages the lockfile + plugin home directly instead of using the
    ``install`` CLI — first-party plugins are bundled with ouroboros
    and are not user-installable by design.
    """
    from ouroboros.plugin.lockfile import LockEntry

    paths = _common_paths(tmp_path)
    plugin_home = paths["plugin_home_root"] / "github-pr-ops"
    plugin_home.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(json.dumps(REFERENCE_MANIFEST))
    manifest["source"] = {"type": "first_party"}
    (plugin_home / "ouroboros.plugin.json").write_text(json.dumps(manifest))

    Lockfile(paths["lockfile"]).add(
        LockEntry(
            name="github-pr-ops",
            version="0.1.0",
            source_kind="local",
            repository=None,
            git_sha=None,
            manifest_checksum="sha256:0" * 8,
            installed_at="2026-05-08T00:00:00Z",
            plugin_home=str(plugin_home),
            source_type="first_party",
            source_identity="bundled:github-pr-ops",
            artifact_digest="sha256:" + "a" * 64,
        )
    )

    inspect_result = runner.invoke(
        plugin_app,
        [
            "inspect",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert inspect_result.exit_code == 0, inspect_result.output
    plain = " ".join(inspect_result.output.split())
    assert "missing scopes" not in plain, (
        f"first_party inspect must not list missing scopes; got: {plain!r}"
    )
    assert "first_party" in plain  # trust_state line names the implicit grant


def test_add_friendly_error_on_structurally_corrupt_catalog_state(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:367.

    A parseable JSON file whose inner shape is wrong (e.g.
    ``{"catalogs": 1}``) must surface the same friendly recovery
    hint as outright malformed JSON — not crash with
    ``TypeError``/``AttributeError`` from the iterator path.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    _make_repo_layout(repo, [REFERENCE_MANIFEST])

    catalog_state = tmp_path / "plugin-catalogs.json"
    catalog_state.write_text(json.dumps({"catalogs": 1}))

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "non-list" in plain or "non-dict" in plain or "catalogs" in plain
    assert "Traceback" not in result.output


def test_add_friendly_error_on_corrupt_inner_plugins_field(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:431.

    Inner-shape validation must reach the ``plugins`` field too:
    ``register()`` does ``set(entry.get("plugins", []))`` and
    ``find_sources_for()`` does ``plugin_name in entry.get("plugins", [])``.
    A parseable file whose ``plugins`` value is a non-iterable (e.g.
    ``"plugins": 1``) or a non-list must surface the friendly recovery
    hint, not crash with ``TypeError``.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    _make_repo_layout(repo, [REFERENCE_MANIFEST])

    catalog_state = tmp_path / "plugin-catalogs.json"
    catalog_state.write_text(
        json.dumps(
            {
                "catalogs": [
                    {
                        "source_type": "local_path",
                        "source_identity": str(repo),
                        # Wrong type — should be list[str].
                        "plugins": 1,
                    }
                ]
            }
        )
    )

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    # Friendly recovery error names the field that drifted.
    assert "plugins" in plain, plain
    assert "Traceback" not in result.output


def test_add_friendly_error_on_non_string_plugin_name_in_catalog(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Sibling regression: a list-typed but non-string-element ``plugins``
    field (e.g. ``[1, 2]``) also breaks the membership/set paths
    silently. The validator must reject it before the iterator path
    runs.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    _make_repo_layout(repo, [REFERENCE_MANIFEST])

    catalog_state = tmp_path / "plugin-catalogs.json"
    catalog_state.write_text(
        json.dumps(
            {
                "catalogs": [
                    {
                        "source_type": "local_path",
                        "source_identity": str(repo),
                        "plugins": [1, 2],
                    }
                ]
            }
        )
    )

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "plugins" in plain, plain
    assert "Traceback" not in result.output


def test_add_friendly_error_on_missing_source_type_in_catalog(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:397.

    Inner-shape validation must also reach the ``source_type`` and
    ``source_identity`` keys: a parseable file like
    ``{"catalogs":[{"plugins":["github-pr-ops"]}]}`` would otherwise
    pass the new preflight and crash inside ``install_command`` with
    raw ``KeyError`` when it does ``s["source_type"]``.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    _make_repo_layout(repo, [REFERENCE_MANIFEST])

    catalog_state = tmp_path / "plugin-catalogs.json"
    catalog_state.write_text(
        json.dumps(
            {
                "catalogs": [
                    {
                        # Missing both "source_type" and "source_identity".
                        "plugins": ["github-pr-ops"],
                    }
                ]
            }
        )
    )

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "source_type" in plain or "missing required field" in plain, plain
    assert "Traceback" not in result.output


def test_add_rejects_external_first_party_manifest(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1171.

    The firewall skips the trust gate whenever
    ``manifest.source.type == "first_party"``. That semantic is
    reserved for plugins bundled with ouroboros itself; allowing
    ``ooo plugin add`` to persist a ``first_party`` source type from
    an arbitrary external repo would let any plugin author bypass the
    user-facing trust flow entirely (privilege escalation). The CLI
    install paths must reject such manifests before any filesystem
    mutation.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    malicious = {**REFERENCE_MANIFEST, "source": {"type": "first_party"}}
    _make_repo_layout(repo, [malicious])

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    # The malicious manifest is filtered out of the discoverable
    # catalog (yellow `skip:` warning). With no other plugins to
    # install, ``add`` exits with a "no valid manifests" error rather
    # than producing a lockfile entry — privilege escalation blocked.
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "first_party" in plain, plain
    # Crucially, no install state was persisted.
    assert not paths["lockfile"].exists() or Lockfile(paths["lockfile"]).read() == {}, plain


def test_install_dir_rejects_external_first_party_manifest(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Sibling regression for the BLOCKING finding on plugin.py:1171.

    The single-plugin ``install <plugin-dir>`` form must also reject
    external ``first_party`` manifests. Since this path skips the
    catalog-level filter, the rejection happens at the manifest-load
    site (and ``_install_one`` re-checks as defense-in-depth).
    """
    paths = _common_paths(tmp_path)
    plugin_dir = tmp_path / "single-plugin"
    plugin_dir.mkdir()
    malicious = {**REFERENCE_MANIFEST, "source": {"type": "first_party"}}
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(malicious))

    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "first_party" in plain, plain
    # No install state was persisted.
    assert not paths["lockfile"].exists() or Lockfile(paths["lockfile"]).read() == {}


def test_install_aborts_before_mutating_when_digest_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1257/1540/1698.

    ``canonical_tree_hash`` must run on the SOURCE bytes BEFORE
    ``_atomic_replace_dir``. If the hasher refuses (escaping symlink,
    unsupported entry, etc.), the previous order had already renamed
    the prior install away — leaving the user with the old install
    gone, the new bytes on disk, and no lockfile entry to reflect
    either. With the fix, a hash-time failure aborts before the prior
    install is touched.
    """
    paths = _common_paths(tmp_path)

    # Stage a prior install that the failed upgrade must NOT clobber.
    prior_src = tmp_path / "prior"
    prior_src.mkdir()
    (prior_src / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    install_prior = runner.invoke(
        plugin_app,
        [
            "install",
            str(prior_src),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert install_prior.exit_code == 0, install_prior.output
    plugin_home = paths["plugin_home_root"] / REFERENCE_MANIFEST["name"]
    prior_manifest_text = (plugin_home / "ouroboros.plugin.json").read_text()
    assert prior_manifest_text, "prior install bytes missing"

    # Stage a fresh src for the failing upgrade.
    new_src = tmp_path / "new"
    new_src.mkdir()
    new_manifest = {**REFERENCE_MANIFEST, "version": "0.2.0"}
    (new_src / "ouroboros.plugin.json").write_text(json.dumps(new_manifest))

    import ouroboros.cli.commands.plugin as plugin_module

    real_hash = plugin_module.canonical_tree_hash
    call_count = {"n": 0}

    def _failing_hash(path):  # noqa: ANN001
        # First call is on the source path; raise to simulate an
        # unsupported entry. Later calls (e.g. firewall recompute on
        # the prior install) keep working so we don't break unrelated
        # paths.
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ValueError("synthetic: unsupported entry in source tree")
        return real_hash(path)

    monkeypatch.setattr(plugin_module, "canonical_tree_hash", _failing_hash)

    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(new_src),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    # Install fails…
    assert result.exit_code != 0, result.output
    # …and crucially, the prior install bytes are intact.
    assert (plugin_home / "ouroboros.plugin.json").read_text() == prior_manifest_text, (
        "the prior install was clobbered by a failed upgrade"
    )
    # Lockfile still reports the prior version, not the failed upgrade.
    entries = Lockfile(paths["lockfile"]).read()
    assert REFERENCE_MANIFEST["name"] in entries
    assert entries[REFERENCE_MANIFEST["name"]].version == REFERENCE_MANIFEST["version"], (
        "lockfile version drifted to the failed-upgrade version"
    )


def test_add_uses_actual_plugin_dir_when_folder_name_disagrees_with_manifest(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:950.

    When a repo contains ``plugins/foo/ouroboros.plugin.json`` whose
    declared name is ``bar``, the previous code validated ``foo``'s
    manifest but copied bytes from ``plugins/bar`` — silently
    installing unvalidated code from a different subtree (or failing
    to find it altogether). The fix tracks the directory each manifest
    was loaded from and uses that for the byte copy AND the persisted
    ``source_identity``, so the manifest-to-artifact binding is
    preserved.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "catalog"
    plugins_dir = repo / "plugins"
    plugins_dir.mkdir(parents=True)

    # Folder name "vendored-name" differs from manifest name "github-pr-ops".
    odd_dir = plugins_dir / "vendored-name"
    odd_dir.mkdir()
    sentinel = odd_dir / "BYTES_FROM_VENDORED_NAME.txt"
    sentinel.write_text("source-of-truth-bytes")
    (odd_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 0, result.output

    plugin_home = paths["plugin_home_root"] / "github-pr-ops"
    # Sentinel must be present — proves bytes came from the validated
    # subtree, not from ``plugins/<manifest.name>``.
    assert (plugin_home / "BYTES_FROM_VENDORED_NAME.txt").read_text() == "source-of-truth-bytes"
    # Source identity in the lockfile points at the actual directory.
    entries = Lockfile(paths["lockfile"]).read()
    recorded = entries["github-pr-ops"].source_identity
    assert recorded == str(odd_dir.resolve()), (
        f"lockfile source_identity must point at the actual plugin dir; got {recorded!r}"
    )


def test_trust_friendly_error_on_corrupt_lockfile(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1843.

    ``trust`` is one of the commands operators run to repair plugin
    state, so a malformed ``plugins.lock`` MUST surface a one-line
    recovery hint — not a raw traceback from ``lock.read()``.
    """
    paths = _common_paths(tmp_path)
    paths["lockfile"].parent.mkdir(parents=True, exist_ok=True)
    paths["lockfile"].write_text("not = valid = toml = at = all = +++")

    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "lockfile is unreadable" in plain, plain
    assert "Traceback" not in result.output


def test_disable_friendly_error_on_corrupt_lockfile(runner: CliRunner, tmp_path: Path) -> None:
    """Sibling regression: ``disable`` MUST surface a friendly
    recovery hint when ``plugins.lock`` is corrupt, not a traceback.
    """
    paths = _common_paths(tmp_path)
    paths["lockfile"].parent.mkdir(parents=True, exist_ok=True)
    paths["lockfile"].write_text("not = valid = toml = at = all = +++")

    result = runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "lockfile is unreadable" in plain, plain
    assert "Traceback" not in result.output


def test_remove_friendly_error_on_corrupt_lockfile(runner: CliRunner, tmp_path: Path) -> None:
    """Sibling regression: ``remove`` MUST surface a friendly
    recovery hint when ``plugins.lock`` is corrupt, not a traceback.
    """
    paths = _common_paths(tmp_path)
    paths["lockfile"].parent.mkdir(parents=True, exist_ok=True)
    paths["lockfile"].write_text("not = valid = toml = at = all = +++")

    result = runner.invoke(
        plugin_app,
        [
            "remove",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "lockfile is unreadable" in plain, plain
    assert "Traceback" not in result.output


def test_trust_friendly_error_on_corrupt_trust_file(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1843.

    ``trust.is_disabled`` / ``trust.grant`` read the trust file from
    disk; a malformed ``trust.json`` must surface a recovery hint
    pointing at the offending file, not a raw traceback.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    # Corrupt the trust file post-install.
    trust_file = paths["trust_root"] / "github-pr-ops" / "trust.json"
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    trust_file.write_text("{ malformed json")

    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    # Recovery hint names trust state and points at the trust root.
    assert "trust state" in plain or "trust grant" in plain, plain
    assert "Traceback" not in result.output


def test_install_warns_when_trust_file_is_corrupt_but_completes(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:292.

    The post-install ``_maybe_invalidate_trust_for_subject_change``
    call previously did ``trust.read()`` with no try/except. If
    ``trust.json`` was malformed, the install aborted AFTER the new
    plugin home + lockfile were written — leaving a half-applied
    state with no recovery message.

    With the fix, a corrupt trust file produces a yellow warning that
    names the recovery action; the lockfile + plugin home stay
    consistent, and the firewall blocks invocation until trust is
    re-granted (matching the auto-reset's end state).
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)

    # Corrupt the trust file, then re-install at a NEW version so the
    # post-install path tries to read trust to decide on invalidation.
    trust_file = paths["trust_root"] / "github-pr-ops" / "trust.json"
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    trust_file.write_text("{ malformed json")

    new_src = tmp_path / "v2"
    new_src.mkdir()
    new_manifest = {**REFERENCE_MANIFEST, "version": "0.2.0"}
    (new_src / "ouroboros.plugin.json").write_text(json.dumps(new_manifest))

    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(new_src),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    # Install completes even though trust is unreadable.
    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    # Yellow warning surfaced (not a traceback).
    assert "trust file" in plain and "unreadable" in plain, plain
    assert "Traceback" not in result.output
    # Lockfile reflects the new install (the post-install warning
    # didn't roll back the just-written state).
    entries = Lockfile(paths["lockfile"]).read()
    assert entries["github-pr-ops"].version == "0.2.0"


def test_install_rolls_back_plugin_home_on_lockfile_write_failure(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1754.

    The install pipeline used to swap ``plugin_home`` into place
    BEFORE attempting the lockfile write. If the lockfile write
    failed, the prior install was irrecoverable while the lockfile
    still pointed at the old subject. With
    ``_atomic_install_with_rollback``, a failed lockfile commit
    restores the prior bytes over ``plugin_home``.
    """
    paths = _common_paths(tmp_path)

    prior_src = tmp_path / "prior"
    prior_src.mkdir()
    (prior_src / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    sentinel_marker = "PRIOR_INSTALL_BYTES"
    (prior_src / "marker.txt").write_text(sentinel_marker)
    install_prior = runner.invoke(
        plugin_app,
        [
            "install",
            str(prior_src),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert install_prior.exit_code == 0, install_prior.output
    plugin_home = paths["plugin_home_root"] / REFERENCE_MANIFEST["name"]
    assert (plugin_home / "marker.txt").read_text() == sentinel_marker

    new_src = tmp_path / "new"
    new_src.mkdir()
    new_manifest = {**REFERENCE_MANIFEST, "version": "0.2.0"}
    (new_src / "ouroboros.plugin.json").write_text(json.dumps(new_manifest))
    (new_src / "marker.txt").write_text("NEW_INSTALL_BYTES")

    from ouroboros.plugin.lockfile import Lockfile as _Lockfile

    real_add = _Lockfile.add

    def _failing_add(self, entry):  # noqa: ANN001
        raise OSError("synthetic: lockfile write blocked (EROFS)")

    monkeypatch.setattr(_Lockfile, "add", _failing_add)

    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(new_src),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    monkeypatch.setattr(_Lockfile, "add", real_add)

    assert result.exit_code != 0, result.output
    plain = " ".join(result.output.split())
    assert "could not commit install" in plain or "lockfile" in plain.lower(), plain
    assert "Traceback" not in result.output
    # Rollback restored the prior install bytes.
    assert (plugin_home / "marker.txt").read_text() == sentinel_marker, (
        "the rollback failed to restore the prior plugin home"
    )


def test_trust_friendly_error_on_clear_disable_failure(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:2069.

    ``trust.clear_disable`` is the LAST step of ``trust``. The
    previous code ran it outside any error handling, so an unlink
    failure crashed AFTER the user had already seen ``Granted: ...``
    — leaving a partial-commit at the trust boundary where the trust
    file looked updated while the plugin remained disabled.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)

    runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )

    def _failing_clear_disable(self, name):  # noqa: ANN001
        raise OSError("synthetic: cannot unlink disabled.json (EACCES)")

    monkeypatch.setattr(TrustStore, "clear_disable", _failing_clear_disable)

    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code != 0, result.output
    plain = " ".join(result.output.split())
    assert "clearing the disable record" in plain, plain
    assert "Traceback" not in result.output


def test_trust_friendly_error_on_structurally_corrupt_lockfile(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:279.

    ``Lockfile.read()`` previously did unchecked ``raw["name"]``,
    ``raw["version"]``, etc. A parseable-but-structurally-corrupt
    ``plugins.lock`` (TOML table missing required fields) raised
    ``KeyError`` rather than ``ValueError`` — escaping the wrappers
    in ``trust``/``disable``/``remove``/``inspect``/``list`` and
    producing a raw traceback. Loader now validates per-entry shape.
    """
    paths = _common_paths(tmp_path)
    paths["lockfile"].parent.mkdir(parents=True, exist_ok=True)
    # Valid TOML, valid schema_version, but the [[plugin]] entry is
    # missing every required field.
    paths["lockfile"].write_text(
        'schema_version = "0.1"\n\n[[plugin]]\nsomething_else = "ignored"\n'
    )

    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "lockfile is unreadable" in plain, plain
    assert "Traceback" not in result.output


def test_trust_friendly_error_on_structurally_corrupt_trust_file(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on trust_store.py:165.

    ``TrustStore.read()`` previously did unchecked ``data["plugin"]``
    / ``data["version"]`` / ``g["scope"]`` lookups. A parseable JSON
    file missing those keys would raise ``KeyError`` instead of
    ``ValueError`` — escaping the wrappers in
    ``trust``/``inspect``/``list``/dispatch and producing a raw
    traceback.

    The fix validates per-record shape and raises ``ValueError`` for
    any structural drift so the wrappers catch it.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    # Write a parseable JSON file missing every required field.
    trust_file = paths["trust_root"] / "github-pr-ops" / "trust.json"
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    trust_file.write_text(json.dumps({"schema_version": "0.1"}))

    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code != 0, result.output
    plain = " ".join(result.output.split())
    # Friendly recovery hint surfaced; no traceback.
    assert "trust" in plain.lower(), plain
    assert "Traceback" not in result.output


def test_install_friendly_error_on_escaping_symlink(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1461.

    ``canonical_tree_hash`` raises ``EscapingSymlinkError``
    (subclass of ``ValueError``) when the source tree contains a
    symlink targeting outside its root. The previous code computed
    the digest BEFORE the install try/except, so this surfaced as a
    raw traceback. The digest is now inside the try block so it
    surfaces as a controlled install failure.
    """
    paths = _common_paths(tmp_path)
    plugin_dir = tmp_path / "evil-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    # Plant an escaping symlink that points outside the plugin tree.
    outside = tmp_path / "outside.txt"
    outside.write_text("forbidden")
    (plugin_dir / "evil-link").symlink_to(outside)

    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(plugin_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output, result.output
    # Lockfile was never written.
    assert not paths["lockfile"].exists() or Lockfile(paths["lockfile"]).read() == {}


def test_install_warns_when_catalog_register_fails(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1498.

    Catalog registration runs AFTER the plugin home + lockfile are
    committed. The previous code left ``catalog_state.register()``
    unguarded, so an OSError on the catalog write crashed AFTER the
    install was already on disk — turning a successful install into
    a partial-commit error path. The fix surfaces the failure as a
    yellow warning so the install is still reported as successful
    (it actually IS — only the resolution-cache convenience is
    missing).
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))

    import ouroboros.cli.commands.plugin as plugin_module

    real_register = plugin_module.CatalogRegistry.register

    def _failing_register(self, **kwargs):  # noqa: ANN001
        raise OSError("synthetic: cannot write plugin-catalogs.json (EACCES)")

    monkeypatch.setattr(plugin_module.CatalogRegistry, "register", _failing_register)

    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(src),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    monkeypatch.setattr(plugin_module.CatalogRegistry, "register", real_register)

    # Install succeeds (lockfile + plugin home are committed)…
    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    # …and catalog-registration failure surfaces as a warning.
    assert "catalog registration failed" in plain, plain
    assert "Traceback" not in result.output
    # Lockfile was written.
    entries = Lockfile(paths["lockfile"]).read()
    assert "github-pr-ops" in entries


def test_trust_failure_after_disable_check_keeps_disable_record(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1723.

    The previous order cleared the disable record up-front, before
    audit-log open and grant writes. A failure after that point left
    the plugin re-enabled with a partial / missing grant set — a real
    state-corruption path at the trust boundary. With the fix, every
    fallible step runs first and ``clear_disable`` runs LAST, so a
    failure leaves the plugin still disabled and the user can re-run.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)
    # Disable, sanity-check the disable record exists.
    runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    trust = TrustStore(root=paths["trust_root"])
    assert trust.is_disabled("github-pr-ops")

    # Make the grant write fail. With the new ordering, the failure
    # must NOT clear the disable record.
    original_grant = TrustStore.grant

    def _failing_grant(self, **kwargs):  # noqa: ANN001 — pytest patch shape
        raise OSError("disk full simulating mid-grant failure")

    monkeypatch.setattr(TrustStore, "grant", _failing_grant)

    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--granted-by",
            "user:tester",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code != 0, result.output
    # Re-read trust state with the original grant restored.
    monkeypatch.setattr(TrustStore, "grant", original_grant)
    assert trust.is_disabled("github-pr-ops"), (
        "trust command failed mid-grant; the disable record MUST remain "
        "in place so the plugin stays gated until the user re-runs"
    )


def test_trust_reenables_disabled_plugin_with_only_optional_permissions(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1675.

    The firewall only blocks invocation on missing *required* scopes.
    A plugin whose entire ``permissions`` list is ``required: false``
    is firewall-equivalent to a zero-permission plugin: bare
    ``ooo plugin trust <name>`` should clear the disable record and
    re-enable it without forcing the user to grant an unnecessary
    optional scope.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    manifest = json.loads(json.dumps(REFERENCE_MANIFEST))
    manifest["permissions"] = [
        {"scope": "github:read", "risk": "read_only", "required": False},
    ]
    (src / "ouroboros.plugin.json").write_text(json.dumps(manifest))

    install_result = runner.invoke(
        plugin_app,
        [
            "install",
            str(src),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert install_result.exit_code == 0, install_result.output

    # Disable, then re-enable with bare `trust <name>` (no --scope).
    disable_result = runner.invoke(
        plugin_app,
        [
            "disable",
            "github-pr-ops",
            "--trust-root",
            str(paths["trust_root"]),
            "--lockfile",
            str(paths["lockfile"]),
        ],
    )
    assert disable_result.exit_code == 0, disable_result.output

    reenable_result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert reenable_result.exit_code == 0, reenable_result.output
    trust = TrustStore(root=paths["trust_root"])
    assert not trust.is_disabled("github-pr-ops")


def test_add_registers_full_repo_catalog_regardless_of_selection(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1523.

    The locked RFC ("How sources enter the known catalog") states that
    ``ooo plugin add <repo>`` makes the repo a known catalog at that
    moment, regardless of which plugins the user selects. Sibling
    plugins that were not chosen during the original ``add`` MUST
    still be addressable by ``ooo plugin install <name>`` later, so
    every discovered manifest in the repo's ``plugins/`` directory
    must end up in ``plugin-catalogs.json``.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "multi-plugin-repo"
    sibling = json.loads(json.dumps(REFERENCE_MANIFEST))
    sibling["name"] = "github-issue-ops"
    sibling["source"] = {
        "type": "local_path",
        "path": "plugins/github-issue-ops",
    }
    _make_repo_layout(repo, [REFERENCE_MANIFEST, sibling])

    catalog_state = tmp_path / "plugin-catalogs.json"
    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--plugin",
            "github-pr-ops",  # only one of two siblings selected
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(catalog_state.read_text())
    plugins_recorded: set[str] = set()
    for entry in payload["catalogs"]:
        plugins_recorded.update(entry.get("plugins", []))
    assert "github-pr-ops" in plugins_recorded
    assert "github-issue-ops" in plugins_recorded, (
        "the unselected sibling MUST still be addressable via "
        "`ooo plugin install <name>` after `add`; missing it strands "
        "the rest of the repo from name-only resolution"
    )


def test_trust_friendly_error_on_unwritable_audit_log(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's follow-up on plugin.py:2172.

    ``trust_command()`` opens ``--audit-log`` for append. If the parent
    directory does not exist (or the file is not writable), the open
    raises ``OSError`` and would dump a raw traceback BEFORE any grant
    was attempted. The fix surfaces the same controlled-exit shape as
    every other state-file failure in this command.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)

    # Point ``--audit-log`` at a path whose parent does not exist.
    bad_audit_log = tmp_path / "missing-parent" / "audit.jsonl"
    assert not bad_audit_log.parent.exists()

    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--granted-by",
            "user:tester",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--audit-log",
            str(bad_audit_log),
        ],
    )
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "could not open audit log" in plain
    assert "Traceback" not in result.output


def test_add_registers_catalog_when_interactive_selection_empty(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1431.

    ``_select_plugins`` exits with code 0 when the user picks nothing
    in the interactive multi-select. The locked RFC ("How sources
    enter the known catalog") still requires ``ooo plugin add <repo>``
    to publish the repo as a known catalog in that case — otherwise a
    later ``ooo plugin install <name>`` cannot resolve the repo by
    name. The fix moves catalog registration BEFORE the selection
    gate so an empty/cancelled selection still records the repo.
    """
    paths = _common_paths(tmp_path)
    repo = tmp_path / "repo-skipped-selection"
    sibling = json.loads(json.dumps(REFERENCE_MANIFEST))
    sibling["name"] = "github-issue-ops"
    sibling["source"] = {
        "type": "local_path",
        "path": "plugins/github-issue-ops",
    }
    _make_repo_layout(repo, [REFERENCE_MANIFEST, sibling])

    catalog_state = tmp_path / "plugin-catalogs.json"

    # Force the interactive path: do NOT pass --plugin. Stub questionary
    # so ``_select_plugins`` returns an empty selection, which calls
    # ``typer.Exit(code=0)``. Without the fix, the catalog file is
    # never written; with the fix, every discovered manifest is
    # already registered before the selection gate.
    import sys
    import types

    fake_q = types.SimpleNamespace()

    class _FakeChoice:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN001
            self.args = args
            self.kwargs = kwargs

    class _FakeAsker:
        def ask(self) -> list[str]:
            return []

    fake_q.Choice = _FakeChoice
    fake_q.checkbox = lambda *a, **kw: _FakeAsker()  # noqa: ARG005
    monkeypatch.setitem(sys.modules, "questionary", fake_q)

    result = runner.invoke(
        plugin_app,
        [
            "add",
            str(repo),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--catalog-state",
            str(catalog_state),
        ],
    )
    # Empty selection is a clean abort, exit 0.
    assert result.exit_code == 0, result.output
    # And the catalog file MUST list every discovered plugin so a later
    # `install <name>` can resolve them.
    assert catalog_state.exists(), (
        "catalog state must be written even when the selection prompt "
        "is empty/cancelled — RFC: `add` makes the repo a known catalog "
        "regardless of selection"
    )
    payload = json.loads(catalog_state.read_text())
    plugins_recorded: set[str] = set()
    for entry in payload["catalogs"]:
        plugins_recorded.update(entry.get("plugins", []))
    assert plugins_recorded == {"github-pr-ops", "github-issue-ops"}, plugins_recorded


def test_atomic_install_rollback_preserves_backup_on_restore_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:327.

    The previous rollback path called ``shutil.rmtree(backup)``
    unconditionally in ``finally``. If ``os.rename(backup, dest)``
    failed (EXDEV across filesystems, ``dest`` re-appearance, etc.)
    AFTER ``dest`` had already been removed, both the new tree and
    the saved backup were destroyed — the exact data-loss path the
    rollback was supposed to prevent.

    The fix removes the unconditional cleanup: a successful
    ``os.rename`` consumes the source naturally, and a failed
    rename leaves the backup on disk for manual recovery.
    """
    import ouroboros.cli.commands.plugin as plugin_module

    src = tmp_path / "src"
    src.mkdir()
    (src / "plugin.py").write_text("new bytes")

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "plugin.py").write_text("PRIOR INSTALL")

    real_rename = plugin_module.os.rename

    def _selective_rename(a, b):  # noqa: ANN001
        # The restore rename is the one that targets ``dest`` from a
        # ``backup`` source path containing ``.bak-``. Reject only that
        # one so we exercise the data-loss-prevention case.
        if ".bak-" in str(a) and str(b) == str(dest):
            raise OSError("synthetic: restore rename failed (EXDEV)")
        return real_rename(a, b)

    monkeypatch.setattr(plugin_module.os, "rename", _selective_rename)

    with pytest.raises(RuntimeError, match="caller follow-up failed"):
        with plugin_module._atomic_install_with_rollback(src, dest):
            raise RuntimeError("caller follow-up failed (lockfile write)")

    # After the rollback's restore failure, the backup MUST still
    # exist on disk so the operator can recover the prior install.
    backup_dirs = list(dest.parent.glob(f"{dest.name}.bak-*"))
    assert len(backup_dirs) == 1, (
        f"backup must be preserved on restore failure; found {backup_dirs}"
    )
    # And the backup must contain the prior install bytes.
    assert (backup_dirs[0] / "plugin.py").read_text() == "PRIOR INSTALL"


def test_inspect_friendly_error_on_corrupt_disabled_json(runner: CliRunner, tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on trust_store.py:461.

    ``inspect`` / ``list`` / dispatch read the disable record via
    ``read_disable``; a malformed ``disabled.json`` previously escaped
    as a raw ``JSONDecodeError`` traceback in the very commands
    operators use to repair plugin state. With the fix, ``read_disable``
    raises a typed ``ValueError`` that the existing CLI wrappers
    (``(ValueError, OSError)``) convert into a friendly recovery hint.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)

    # Corrupt disabled.json by writing invalid JSON at the documented path.
    disabled_path = paths["trust_root"] / "github-pr-ops" / "disabled.json"
    disabled_path.parent.mkdir(parents=True, exist_ok=True)
    disabled_path.write_text("{ truncated json")

    result = runner.invoke(
        plugin_app,
        [
            "inspect",
            "github-pr-ops",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    # The command MUST surface a controlled error, NOT a raw traceback.
    assert "Traceback" not in result.output, result.output


def test_install_refuses_namespace_collision_with_already_installed_plugin(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:1323.

    ``plugin_dispatch._build_registry_from_lockfile`` silently ``continue``s
    past ``UserLevelProgramRegistry.register()`` failures (per the locked
    "first registration wins" rule). Without an install-time guard, a
    plugin whose namespace/command/name collides with an already-installed
    one persists to the lockfile but is unreachable at runtime — install
    reports success and ``ooo <name>`` reports "no such command". The
    fix surfaces the collision BEFORE the lockfile is written.
    """
    paths = _common_paths(tmp_path)

    # Install plugin A (namespace `github-pr`, command `review`).
    a_dir = tmp_path / "plugin-a"
    _install_reference_plugin(runner, plugin_dir=a_dir, paths=paths)

    # Build plugin B with a *different* plugin name but the SAME namespace.
    # The runtime registry would refuse this with RegistryError; the
    # dispatcher would silently skip it. The install MUST refuse it
    # BEFORE the lockfile is written.
    b_manifest = json.loads(json.dumps(REFERENCE_MANIFEST))
    b_manifest["name"] = "github-pr-ops-rival"
    b_manifest["source"] = {"type": "local_path", "path": "plugin-b"}
    # Same namespace as A (`github-pr`) but a different command name to
    # isolate the collision to the namespace axis.
    b_manifest["commands"] = [
        {
            "namespace": "github-pr",
            "name": "summarize",
            "summary": "Conflicting plugin: same namespace as A.",
            "usage": "ooo github-pr summarize",
            "risk": "read_only",
            "requires_confirmation": False,
        }
    ]
    b_dir = tmp_path / "plugin-b"
    b_dir.mkdir()
    (b_dir / "ouroboros.plugin.json").write_text(json.dumps(b_manifest))

    # Snapshot lockfile state so we can prove it was NOT mutated by the
    # rejected install.
    lock_before = Lockfile(paths["lockfile"]).read()
    assert "github-pr-ops" in lock_before
    assert "github-pr-ops-rival" not in lock_before

    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(b_dir),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    # Install MUST be rejected with a controlled exit, NOT a traceback.
    assert result.exit_code == 1, result.output
    plain = " ".join(result.output.split())
    assert "refusing to install" in plain
    assert "Traceback" not in result.output

    # Lockfile MUST be unchanged — the colliding plugin was never persisted.
    lock_after = Lockfile(paths["lockfile"]).read()
    assert "github-pr-ops-rival" not in lock_after, (
        "lockfile must not record a plugin whose namespace/command/name "
        "collides with an already-installed one — dispatch would skip it"
    )
    assert lock_before == lock_after


def test_install_allows_same_name_reinstall_at_new_version(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Same-name reinstall (e.g. version bump) MUST NOT be blocked by the
    new collision guard — the runtime registry releases the prior
    plugin's namespace/command slots before the replacement registers,
    so a same-name install is exempt from cross-plugin collision.
    """
    paths = _common_paths(tmp_path)
    a_dir = tmp_path / "plugin-a"
    _install_reference_plugin(runner, plugin_dir=a_dir, paths=paths)

    bumped = json.loads(json.dumps(REFERENCE_MANIFEST))
    bumped["version"] = "0.2.0"
    a_dir_v2 = tmp_path / "plugin-a-v2"
    a_dir_v2.mkdir()
    (a_dir_v2 / "ouroboros.plugin.json").write_text(json.dumps(bumped))

    result = runner.invoke(
        plugin_app,
        [
            "install",
            str(a_dir_v2),
            "--lockfile",
            str(paths["lockfile"]),
            "--plugin-home-root",
            str(paths["plugin_home_root"]),
            "--trust-root",
            str(paths["trust_root"]),
        ],
    )
    assert result.exit_code == 0, result.output
    entries = Lockfile(paths["lockfile"]).read()
    assert entries["github-pr-ops"].version == "0.2.0"


def test_trust_friendly_error_on_audit_log_write_failure(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the bot's follow-up on plugin.py:2286.

    ``trust_command`` previously guarded ``--audit-log`` open failures
    but not write failures. An ``OSError`` mid-write (disk full,
    broken pipe, NFS error) escaped as a raw traceback AFTER the trust
    grant was already persisted — exactly the partial-commit shape the
    rest of the command is hardened against. The fix wraps the write
    in a typer.Exit(1) with a recovery hint that names the audit log
    path.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)

    # Stub Path.open for the audit-log path so write() raises.
    audit_log = paths["audit_log"]
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    audit_log.touch()  # parent and file exist so the OPEN succeeds

    real_path_open = Path.open

    class _FailingHandle:
        def __init__(self, real):  # noqa: ANN001
            self._real = real

        def write(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("synthetic: ENOSPC writing audit log")

        def flush(self):
            return self._real.flush()

        def close(self):
            return self._real.close()

        def __enter__(self):
            return self

        def __exit__(self, *a):  # noqa: ANN002
            self.close()

    def _fake_open(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if str(self) == str(audit_log):
            return _FailingHandle(real_path_open(self, *args, **kwargs))
        return real_path_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _fake_open)

    result = runner.invoke(
        plugin_app,
        [
            "trust",
            "github-pr-ops",
            "--scope",
            "github:read",
            "--granted-by",
            "user:tester",
            "--lockfile",
            str(paths["lockfile"]),
            "--trust-root",
            str(paths["trust_root"]),
            "--audit-log",
            str(audit_log),
        ],
    )
    assert result.exit_code == 1, result.output
    # Strip ANSI codes + Rich panel borders before matching (the message
    # wraps inside a panel and the assertion would otherwise depend on
    # terminal width).
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for ch in ("│", "╭", "╮", "╰", "╯", "─"):
        plain = plain.replace(ch, " ")
    plain = " ".join(plain.split())
    assert "audit-log write" in plain
    assert "failed" in plain
    assert "Traceback" not in result.output


def test_catalog_register_does_not_lose_concurrent_updates(tmp_path: Path) -> None:
    """Regression for the bot's BLOCKING finding on plugin.py:654.

    ``CatalogRegistry.register()`` is read-modify-write. Without an
    inter-process lock, two concurrent ``ooo plugin add`` /
    ``ooo plugin install <name> --from ...`` calls would each load the
    same prior payload, merge in their own plugin, and the last
    ``os.replace()`` would silently drop the other's entry after both
    commands had already reported success. The fix wraps the read-
    modify-write in the same POSIX flock pattern ``Lockfile`` /
    ``TrustStore`` already use.

    Use threads as a proxy for the cross-process race; fcntl flocks
    serialize POSIX-locked critical sections regardless of whether the
    callers are threads or processes, and the fix removes the lost-
    update window in either case.
    """
    import threading

    from ouroboros.cli.commands.plugin import CatalogRegistry

    state_path = tmp_path / "plugin-catalogs.json"
    registry = CatalogRegistry(state_path=state_path)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def _register(idx: int) -> None:
        try:
            barrier.wait()
            registry.register(
                source_type="local_path",
                source_identity=f"/tmp/plugin-{idx}",
                plugin_name=f"plugin-{idx}",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_register, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent register raised: {errors}"

    payload = json.loads(state_path.read_text())
    plugins_recorded: set[str] = set()
    for entry in payload["catalogs"]:
        plugins_recorded.update(entry.get("plugins", []))
    expected = {f"plugin-{i}" for i in range(8)}
    assert plugins_recorded == expected, (
        f"concurrent register lost updates: missing "
        f"{expected - plugins_recorded}, got {plugins_recorded}"
    )
