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
