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
    return (
        CliRunner(mix_stderr=False)
        if "mix_stderr" in CliRunner.__init__.__code__.co_varnames
        else CliRunner()
    )


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


def test_discover_invalid_manifest_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
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


def test_inspect_installed_untrusted(runner: CliRunner, tmp_path: Path) -> None:
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


# Manifest with TWO required scopes — used to exercise partial-trust
# regression cases that the single-required-scope fixture cannot reach.
TWO_REQUIRED_MANIFEST: dict = {
    **REFERENCE_MANIFEST,
    "permissions": [
        {"scope": "github:read", "risk": "read_only", "required": True},
        {
            "scope": "github:pull_request:write",
            "risk": "destructive",
            "required": True,
        },
    ],
}


def test_inspect_partial_trust_reports_installed_not_trusted(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the trust_state misreport: when at least one of
    the manifest's required scopes is missing, `inspect` must NOT call
    the plugin "trusted" — the firewall would still block invocation.
    """
    plugin_home = tmp_path / "plugin_home"
    _write_manifest(plugin_home, TWO_REQUIRED_MANIFEST)
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
    # Grant only ONE of the two required scopes.
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
    # The display must say "installed" on the trust_state line. We assert
    # the row text instead of substring matches to avoid the prior false
    # positive where "trusted" leaked in via a different field.
    assert "trust_state:    installed" in result.output
    # The granted scope is still listed truthfully.
    assert "github:read" in result.output
    # And the missing required scope is surfaced.
    assert "missing scopes" in result.output
    assert "github:pull_request:write" in result.output


def test_inspect_stale_version_reports_installed(runner: CliRunner, tmp_path: Path) -> None:
    """Regression: a trust file recorded for an older plugin version
    must NOT make `inspect` say "trusted" — the firewall treats it as
    invalidated, and the CLI must agree.
    """
    plugin_home = tmp_path / "plugin_home"
    _write_manifest(plugin_home, REFERENCE_MANIFEST)  # version 0.1.0
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
    # Trust granted against an older version of the same plugin.
    TrustStore(root=trust_root).grant(
        plugin="github-pr-ops",
        version="0.0.9",
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
    assert "trust_state:    installed" in result.output
    # User must see WHY trust flipped back to installed, otherwise the
    # report is just contradictory. Rich may soft-wrap the line, so we
    # assert the words separately.
    assert "trust_version" in result.output
    assert "version bump" in result.output
    assert "invalidated trust" in result.output
    assert "missing scopes" in result.output
    assert "github:read" in result.output


def test_inspect_first_party_ignores_corrupt_trust_file(runner: CliRunner, tmp_path: Path) -> None:
    """Regression: a leftover/corrupt `trust.json` for a first-party
    plugin must NOT make `inspect` fail. The firewall ignores the
    trust store for `source.type == 'first_party'` by design, so the
    CLI's read-only commands need to do the same instead of treating
    that file as authoritative."""
    plugin_home = tmp_path / "plugin_home"
    fp_manifest = {
        **REFERENCE_MANIFEST,
        "name": "ooo-builtin",
        "source": {"type": "first_party"},
        "permissions": [],
    }
    _write_manifest(plugin_home, fp_manifest)
    lock_path = tmp_path / "plugins.lock"
    trust_root = tmp_path / "trust"
    Lockfile(lock_path).add(
        LockEntry(
            name="ooo-builtin",
            version="0.1.0",
            source_kind="local",
            repository=None,
            git_sha=None,
            manifest_checksum="sha256:0",
            installed_at="2026-05-08T00:00:00Z",
            plugin_home=str(plugin_home),
        )
    )
    # Plant a corrupt trust file for the same plugin name. A pre-fix
    # build would crash on `_read_trust_or_exit`; the new path skips
    # the read entirely for first_party.
    trust_dir = trust_root / "ooo-builtin"
    trust_dir.mkdir(parents=True)
    (trust_dir / "trust.json").write_text("{garbage")

    result = runner.invoke(
        plugin_app,
        [
            "inspect",
            "ooo-builtin",
            "--lockfile",
            str(lock_path),
            "--trust-root",
            str(trust_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "first_party" in result.output


def test_list_json_zeroes_stale_grants_and_flags_version_drift(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression for the `list --json` reporting suggestion: a trust
    file recorded against an older plugin version must NOT echo its
    grants in the JSON view, because those grants are no longer
    effective. The row also exposes `trust_version_stale: true` so
    consumers can branch deterministically."""
    plugin_home = tmp_path / "ph"
    _write_manifest(plugin_home, REFERENCE_MANIFEST)  # version 0.1.0
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
    # Trust granted against an older version of the same plugin.
    TrustStore(root=trust_root).grant(
        plugin="github-pr-ops",
        version="0.0.9",
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
    assert isinstance(data, list) and len(data) == 1
    row = data[0]
    assert row["trust_state"] == "installed"
    assert row["trust_version_stale"] is True
    # No effective grants remain — don't lie to JSON consumers about
    # what the firewall would actually accept.
    assert row["granted_scopes"] == []
    # Required scope still surfaces as missing.
    assert row["missing_required_scopes"] == ["github:read"]


def test_discover_unreadable_manifest_reports_friendly_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression: `discover` is also a diagnostic command — an
    unreadable manifest (chmod 000, broken symlink, transient I/O)
    must surface as a friendly error rather than a raw OSError
    traceback. Mirrors the same guarantee `inspect`/`list` make."""
    import os
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("POSIX permission semantics required")
    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX file permissions")

    plugin_dir = tmp_path / "github-pr-ops"
    manifest_path = _write_manifest(plugin_dir, REFERENCE_MANIFEST)
    original_mode = manifest_path.stat().st_mode
    manifest_path.chmod(0o000)
    try:
        result = runner.invoke(plugin_app, ["discover", str(plugin_dir)])
    finally:
        manifest_path.chmod(original_mode)

    assert result.exit_code == 1
    assert "manifest is unreadable" in result.output
    assert "Traceback" not in result.output


def test_inspect_structurally_corrupt_lockfile_reports_friendly_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression: a parseable-but-structurally-corrupt plugins.lock
    (TOML parses fine, but the [[plugin]] block is missing a required
    field like `name`) used to raise a raw KeyError straight through
    `inspect`/`list`. The friendly-error guard now covers KeyError /
    TypeError too, since `Lockfile.read()` builds dataclasses via
    unchecked `raw[...]` lookups."""
    lock_path = tmp_path / "plugins.lock"
    # Valid TOML, valid schema_version, but plugin block has no `name`.
    lock_path.write_text(
        'schema_version = "0.1"\n\n'
        "[[plugin]]\n"
        'version = "0.1.0"\n'
        'source_kind = "local"\n'
        'manifest_checksum = "sha256:0"\n'
        'installed_at = "2026-05-08T00:00:00Z"\n'
        'plugin_home = "/tmp/x"\n'
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
    assert result.exit_code == 1
    assert "lockfile is unreadable" in result.output
    assert "Traceback" not in result.output
    # KeyError prints as the missing key — the operator needs to see it.
    assert "name" in result.output


def test_inspect_unreadable_manifest_reports_friendly_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression: an unreadable on-disk manifest (e.g. wrong perms)
    used to escape `load_manifest` as a raw `OSError`. `inspect` is
    a diagnostic command and must surface a clean message instead of
    a raw traceback. We exercise the path with chmod 000.
    """
    import os
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("POSIX permission semantics required")
    if os.geteuid() == 0:
        pytest.skip("root bypasses POSIX file permissions")

    plugin_home = tmp_path / "plugin_home"
    manifest_path = _write_manifest(plugin_home, REFERENCE_MANIFEST)
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
    # Strip read perms so `path.open()` raises PermissionError without
    # tripping the `is_file()` early-exit check inside `load_manifest`.
    original_mode = manifest_path.stat().st_mode
    manifest_path.chmod(0o000)
    try:
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
    finally:
        manifest_path.chmod(original_mode)

    assert result.exit_code == 1
    assert "manifest is unreadable" in result.output
    assert "Traceback" not in result.output


def test_inspect_malformed_lockfile_reports_friendly_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression: a malformed plugins.lock must produce a friendly
    error and exit code 1, NOT a raw TOMLDecodeError traceback."""
    lock_path = tmp_path / "plugins.lock"
    lock_path.write_text("this = is = not valid TOML\n")  # parser bait
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
    assert result.exit_code == 1
    assert "lockfile is unreadable" in result.output
    assert "Traceback" not in result.output


def test_list_malformed_lockfile_reports_friendly_error(runner: CliRunner, tmp_path: Path) -> None:
    """Same as above for `list`, which also reads the lockfile."""
    lock_path = tmp_path / "plugins.lock"
    # Wrong schema_version triggers Lockfile.read's ValueError path —
    # a separate failure mode from raw TOML decode errors.
    lock_path.write_text('schema_version = "9.9"\n')
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
    assert result.exit_code == 1
    assert "lockfile is unreadable" in result.output
    assert "Traceback" not in result.output


def test_inspect_malformed_trust_file_reports_friendly_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression: a corrupt trust.json must surface a friendly error,
    not a JSONDecodeError traceback. `inspect` is meant to diagnose
    plugin state, so the diagnostic itself can't crash on bad state."""
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
    # Write a malformed trust.json under the trust root.
    trust_dir = trust_root / "github-pr-ops"
    trust_dir.mkdir(parents=True)
    (trust_dir / "trust.json").write_text("{not valid json")

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
    assert "trust file" in result.output
    assert "unreadable" in result.output
    assert "Traceback" not in result.output


def test_list_json_partial_trust_reports_installed_not_trusted(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Regression: `list --json` must mirror the firewall's gate. With
    at least one required scope missing, the row's trust_state cannot
    say "trusted".
    """
    plugin_home = tmp_path / "ph"
    _write_manifest(plugin_home, TWO_REQUIRED_MANIFEST)
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
    assert isinstance(data, list) and len(data) == 1
    row = data[0]
    assert row["name"] == "github-pr-ops"
    assert row["trust_state"] == "installed", row
    assert row["granted_scopes"] == ["github:read"]
    # And the row exposes the firewall-blocking scopes as structured
    # output so consumers can pipe to jq for an automated re-trust step.
    assert row["missing_required_scopes"] == ["github:pull_request:write"]
