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


def test_trust_without_scope_for_plugin_with_permissions_errors(
    runner: CliRunner, tmp_path: Path
) -> None:
    """For plugins WITH declared permissions, bare `trust <name>` must
    still error so the user has to make an explicit grant decision —
    silently re-enabling without trust would be a permission-boundary
    surprise.
    """
    paths = _common_paths(tmp_path)
    src = tmp_path / "src"
    _install_reference_plugin(runner, plugin_dir=src, paths=paths)  # has github:read
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
    assert "declares permissions" in flat
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
