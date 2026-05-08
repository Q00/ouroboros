"""Tests for the implicit `ooo <plugin> <command>` dispatch fallback.

Covers the three behavior requirements the bot's review locked in:

  1. Successful plugin invocations surface the plugin's actual
     stdout/stderr to the user's terminal — `result.message` alone is
     not enough because successful runs typically have an empty
     message.
  2. Blocked invocations (trust failure, disabled plugin, digest
     drift) MUST exit with a non-zero status so shells/CI treat them
     as failures. The firewall returns `exit_code=None` for those, so
     the dispatcher has to map status → click exit code itself.
  3. Commands flagged `requires_confirmation: true` MUST receive a
     real interactive prompt; the firewall's default
     `lambda _msg: True` (auto-confirm) bypasses the only
     destructive-action gate the contract has.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from click.testing import CliRunner
import pytest

from ouroboros.cli.commands.plugin_dispatch import build_plugin_dispatch_command
from ouroboros.plugin.lockfile import LockEntry, Lockfile
from ouroboros.plugin.trust_store import TrustStore

REFERENCE_MANIFEST: dict = {
    "schema_version": "0.1",
    "name": "github-pr-ops",
    "version": "0.1.0",
    "description": "Reference plugin used by the dispatcher tests.",
    "source": {"type": "local_path", "path": "plugins/github-pr-ops"},
    "commands": [
        {
            "namespace": "github-pr",
            "name": "review",
            "summary": "Review a pull request and summarize readiness.",
            "usage": "ooo github-pr review <pull-request-url>",
            "risk": "read_only",
            "requires_confirmation": False,
        },
        {
            "namespace": "github-pr",
            "name": "merge",
            "summary": "Merge a PR.",
            "usage": "ooo github-pr merge <url>",
            "risk": "destructive",
            "requires_confirmation": True,
        },
    ],
    "capabilities": [{"name": "ledger", "access": "write"}],
    "permissions": [
        {"scope": "github:read", "risk": "read_only", "required": True},
    ],
    "entrypoint": {"type": "command", "command": "python -m fake_plugin"},
}


def _stage_installed_plugin(
    *,
    home_root: Path,
    lockfile_path: Path,
    trust_root: Path,
    digest: str = "sha256:" + "a" * 64,
) -> Path:
    """Build an on-disk install of the reference plugin, write a
    lockfile entry pointing at it, and return the plugin home path."""
    plugin_home = home_root / REFERENCE_MANIFEST["name"]
    plugin_home.mkdir(parents=True, exist_ok=True)
    (plugin_home / "ouroboros.plugin.json").write_text(json.dumps(REFERENCE_MANIFEST))
    Lockfile(lockfile_path).add(
        LockEntry(
            name=REFERENCE_MANIFEST["name"],
            version=REFERENCE_MANIFEST["version"],
            source_kind="local",
            repository=None,
            git_sha=None,
            manifest_checksum="sha256:0" * 8,
            installed_at="2026-05-08T00:00:00Z",
            plugin_home=str(plugin_home),
            source_type="local_path",
            source_identity=str(plugin_home),
            artifact_digest=digest,
        )
    )
    TrustStore(root=trust_root).grant(
        plugin=REFERENCE_MANIFEST["name"],
        version=REFERENCE_MANIFEST["version"],
        scope="github:read",
        granted_by="user:test",
        source_type="local_path",
        source_identity=str(plugin_home),
        artifact_digest=digest,
    )
    return plugin_home


@pytest.fixture
def stub_default_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Point the dispatcher's DEFAULT_LOCKFILE_PATH / DEFAULT_TRUST_ROOT
    at tmp paths so the test never touches the real ~/.ouroboros."""
    fake_lockfile = tmp_path / "plugins.lock"
    fake_trust = tmp_path / "trust"
    fake_homes = tmp_path / "homes"
    fake_homes.mkdir()
    monkeypatch.setattr(
        "ouroboros.cli.commands.plugin_dispatch.DEFAULT_LOCKFILE_PATH", fake_lockfile
    )
    monkeypatch.setattr("ouroboros.cli.commands.plugin_dispatch.DEFAULT_TRUST_ROOT", fake_trust)
    return {"lockfile": fake_lockfile, "trust": fake_trust, "homes": fake_homes}


def test_dispatch_returns_none_for_unknown_plugin_name(
    stub_default_paths: dict[str, Path],
) -> None:
    """When no installed plugin claims the name, the dispatcher MUST
    return None so typer's normal "no such command" error fires."""
    cmd = build_plugin_dispatch_command("does-not-exist")
    assert cmd is None


def test_dispatch_runs_subprocess_with_artifact_digest_recomputed(
    stub_default_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the dispatcher resolves the plugin, computes the
    digest match, and surfaces the captured subprocess stdout to the
    user's terminal — solving the bot's BLOCKING #3 (success cases
    appearing to do nothing).
    """
    from ouroboros.plugin.digest import canonical_tree_hash

    plugin_home = _stage_installed_plugin(
        home_root=stub_default_paths["homes"],
        lockfile_path=stub_default_paths["lockfile"],
        trust_root=stub_default_paths["trust"],
    )
    # Re-stamp the lockfile + trust with the actual digest of the
    # staged plugin home (since the helper used a placeholder).
    real_digest = canonical_tree_hash(plugin_home)
    Lockfile(stub_default_paths["lockfile"]).add(
        LockEntry(
            name="github-pr-ops",
            version="0.1.0",
            source_kind="local",
            repository=None,
            git_sha=None,
            manifest_checksum="sha256:0" * 8,
            installed_at="2026-05-08T00:00:00Z",
            plugin_home=str(plugin_home),
            source_type="local_path",
            source_identity=str(plugin_home),
            artifact_digest=real_digest,
        )
    )
    TrustStore(root=stub_default_paths["trust"]).grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
        source_type="local_path",
        source_identity=str(plugin_home),
        artifact_digest=real_digest,
    )

    captured_stdout = b"PR #1 looks good to merge\n"

    def _spy_runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=captured_stdout, stderr=b""
        )

    monkeypatch.setattr("ouroboros.plugin.firewall.subprocess.run", _spy_runner)

    cmd = build_plugin_dispatch_command("github-pr-ops")
    assert cmd is not None
    runner = CliRunner()
    result = runner.invoke(cmd, ["review", "https://example.com/pr/1"])
    assert result.exit_code == 0
    # The plugin's stdout reaches the user's terminal — this is the
    # core regression catch.
    assert "PR #1 looks good to merge" in result.stdout


def test_dispatch_blocked_invocation_exits_nonzero(
    stub_default_paths: dict[str, Path],
) -> None:
    """When the firewall blocks (here: digest drift), the dispatcher
    must exit non-zero. ``InvocationResult.exit_code`` is None on the
    blocked path; mapping it to 0 would let shells/CI treat refused
    invocations as success.

    Regression catch for the bot's BLOCKING #2.
    """
    _stage_installed_plugin(
        home_root=stub_default_paths["homes"],
        lockfile_path=stub_default_paths["lockfile"],
        trust_root=stub_default_paths["trust"],
        # Lockfile records a digest that doesn't match the on-disk
        # bytes, so the firewall returns blocked / trust_subject_changed.
        digest="sha256:" + "f" * 64,
    )
    cmd = build_plugin_dispatch_command("github-pr-ops")
    assert cmd is not None
    runner = CliRunner()
    result = runner.invoke(cmd, ["review", "https://example.com/pr/1"])
    assert result.exit_code != 0, (
        f"blocked invocation must exit non-zero; got {result.exit_code}; output={result.output!r}"
    )


def test_dispatch_requires_confirmation_command_prompts_user(
    stub_default_paths: dict[str, Path],
) -> None:
    """A command marked `requires_confirmation: true` must produce a
    real prompt. The firewall's default `confirm` callback
    auto-approves — the dispatcher MUST override that with a Click
    interactive prompt that defaults to no. Declining the prompt
    blocks the invocation without launching the subprocess.

    Regression catch for the bot's BLOCKING #1.
    """
    from ouroboros.plugin.digest import canonical_tree_hash

    plugin_home = _stage_installed_plugin(
        home_root=stub_default_paths["homes"],
        lockfile_path=stub_default_paths["lockfile"],
        trust_root=stub_default_paths["trust"],
    )
    real_digest = canonical_tree_hash(plugin_home)
    Lockfile(stub_default_paths["lockfile"]).add(
        LockEntry(
            name="github-pr-ops",
            version="0.1.0",
            source_kind="local",
            repository=None,
            git_sha=None,
            manifest_checksum="sha256:0" * 8,
            installed_at="2026-05-08T00:00:00Z",
            plugin_home=str(plugin_home),
            source_type="local_path",
            source_identity=str(plugin_home),
            artifact_digest=real_digest,
        )
    )
    TrustStore(root=stub_default_paths["trust"]).grant(
        plugin="github-pr-ops",
        version="0.1.0",
        scope="github:read",
        granted_by="user:test",
        source_type="local_path",
        source_identity=str(plugin_home),
        artifact_digest=real_digest,
    )

    cmd = build_plugin_dispatch_command("github-pr-ops")
    assert cmd is not None
    runner = CliRunner()
    # Feed "n\n" to the prompt → declined → blocked, exit non-zero,
    # subprocess never launched.
    result = runner.invoke(
        cmd,
        ["merge", "https://example.com/pr/1"],
        input="n\n",
    )
    assert result.exit_code != 0, (
        f"declined confirmation must produce a non-zero exit; "
        f"got {result.exit_code}; stdout={result.stdout!r}"
    )


def test_dispatch_friendly_error_on_corrupt_trust_state(
    stub_default_paths: dict[str, Path],
) -> None:
    """Regression for the bot's BLOCKING finding on plugin_dispatch.py:111.

    The dispatcher is now a primary user-facing invocation path, so a
    malformed ``trust.json`` MUST produce a controlled refusal — not a
    raw traceback from ``trust.read()``. The fix wraps the
    trust/disable lookup in a try/except so the user sees a one-line
    error pointing at the recovery action and the process exits
    non-zero.
    """
    plugin_home = _stage_installed_plugin(
        home_root=stub_default_paths["homes"],
        lockfile_path=stub_default_paths["lockfile"],
        trust_root=stub_default_paths["trust"],
    )
    # Corrupt the trust file post-install. ``trust.read()`` must raise.
    trust_file = stub_default_paths["trust"] / REFERENCE_MANIFEST["name"] / "trust.json"
    trust_file.write_text("{ malformed json")

    cmd = build_plugin_dispatch_command("github-pr-ops")
    assert cmd is not None
    runner = CliRunner()
    result = runner.invoke(cmd, ["review", "https://example.com/pr/1"])
    assert result.exit_code != 0, (
        f"corrupt trust state must produce a non-zero exit; "
        f"got {result.exit_code}; stdout={result.stdout!r}"
    )
    # No raw traceback in user-facing output.
    assert "Traceback" not in result.output
    assert "unreadable" in result.output, result.output
    # Sanity: the plugin home is untouched (no install state was
    # mutated by the failed dispatch).
    assert plugin_home.exists()


def test_dispatch_surfaces_corrupt_lockfile_instead_of_unknown_command(
    stub_default_paths: dict[str, Path],
) -> None:
    """Regression for the bot's BLOCKING finding on plugin_dispatch.py:81.

    When ``plugins.lock`` is present but unreadable / malformed, the
    fallback MUST surface a friendly recovery hint instead of
    returning ``None`` (which makes typer say "no such command" and
    leaves an installed plugin indistinguishable from a typo). The
    dispatcher returns a stub command that prints the lockfile error
    and exits non-zero for any name the user typed.
    """
    # Write a corrupt lockfile in place of an installed-plugin lockfile.
    stub_default_paths["lockfile"].write_text("{ truncated json")

    cmd = build_plugin_dispatch_command("github-pr-ops")
    assert cmd is not None, (
        "build_plugin_dispatch_command must NOT return None when the "
        "lockfile exists but is unreadable; that hides corruption as "
        "'no such command'"
    )
    runner = CliRunner()
    result = runner.invoke(cmd, ["review", "https://example.com/pr/1"])
    assert result.exit_code != 0
    assert "lockfile is unreadable" in result.output, result.output
    assert "Traceback" not in result.output


def test_dispatch_surfaces_corrupt_manifest_instead_of_unknown_command(
    stub_default_paths: dict[str, Path],
) -> None:
    """Regression for the bot's follow-up finding on plugin_dispatch.py:58.

    When a lockfile entry's manifest fails to load, the previous code
    silently skipped that entry. Combined with
    ``build_plugin_dispatch_command`` returning ``None`` for any
    unresolved name, the operator typing ``ooo <installed-plugin> ...``
    saw Typer's generic "no such command" — indistinguishable from a
    typo — even though the lockfile insists the plugin IS installed.

    The dispatcher now records the failed-to-load lockfile entry's name
    and returns a stub command that prints a friendly recovery hint
    naming the unreadable manifest path and the recovery action
    (``ooo plugin remove <name>`` to reset the install).
    """
    plugin_home = _stage_installed_plugin(
        home_root=stub_default_paths["homes"],
        lockfile_path=stub_default_paths["lockfile"],
        trust_root=stub_default_paths["trust"],
    )
    # Corrupt the manifest: keep it parseable JSON but break the schema
    # so ``load_manifest`` raises ``PluginManifestError`` (the path the
    # dispatcher was previously hiding behind "no such command").
    manifest_path = plugin_home / "ouroboros.plugin.json"
    manifest_path.write_text(json.dumps({"schema_version": "0.1", "name": "github-pr-ops"}))

    cmd = build_plugin_dispatch_command("github-pr-ops")
    assert cmd is not None, (
        "build_plugin_dispatch_command must NOT return None when the "
        "lockfile entry exists but its manifest is corrupt; that hides "
        "manifest corruption as 'no such command'"
    )
    runner = CliRunner()
    result = runner.invoke(cmd, ["review", "https://example.com/pr/1"])
    assert result.exit_code != 0
    # Rich wraps the panel across lines AND injects panel-border glyphs
    # mid-token, so collapse the output to its plain alphanumeric form
    # before substring matching.
    no_ansi = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    compact = re.sub(r"[\s│]+", "", no_ansi)
    assert "manifestisunreadable" in compact, result.output
    # The recovery hint names both the offending manifest path and the
    # canonical reset action so the operator can act without guessing.
    assert "ouroboros.plugin.json" in compact, result.output
    assert "ooopluginremovegithub-pr-ops" in compact, result.output
    assert "Traceback" not in result.output


def test_dispatch_honors_env_overrides_for_lockfile_and_trust_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime dispatcher MUST honor the same lockfile / trust-root
    overrides that the manager subcommands accept via ``--lockfile`` and
    ``--trust-root``. Without this, a plugin installed under a non-default
    profile path is write-only: the manager can record it, but
    ``ooo <plugin>`` can't find it because dispatch is hard-wired to
    ``DEFAULT_LOCKFILE_PATH`` / ``DEFAULT_TRUST_ROOT``. Operators expose
    the same paths to dispatch via the env vars
    ``OUROBOROS_PLUGIN_LOCKFILE`` and ``OUROBOROS_PLUGIN_TRUST_ROOT``.

    This test pins the contract: with the default paths empty (no
    plugin installed there) and the env vars pointing at an alternate
    install location, ``build_plugin_dispatch_command`` MUST resolve
    against the alternate paths.
    """
    # 1. Stub default paths to a different empty location so the
    #    dispatcher's "no lockfile" fast-path would otherwise fire.
    empty_default = tmp_path / "default"
    empty_default.mkdir()
    monkeypatch.setattr(
        "ouroboros.cli.commands.plugin_dispatch.DEFAULT_LOCKFILE_PATH",
        empty_default / "plugins.lock",
    )
    monkeypatch.setattr(
        "ouroboros.cli.commands.plugin_dispatch.DEFAULT_TRUST_ROOT",
        empty_default / "trust",
    )

    # 2. Install the reference plugin at an entirely separate location
    #    that defaults are unaware of — only the env vars know it.
    alt_root = tmp_path / "alt"
    alt_root.mkdir()
    alt_lockfile = alt_root / "plugins.lock"
    alt_trust = alt_root / "trust"
    alt_homes = alt_root / "homes"
    alt_homes.mkdir()
    plugin_home = _stage_installed_plugin(
        home_root=alt_homes,
        lockfile_path=alt_lockfile,
        trust_root=alt_trust,
    )
    monkeypatch.setenv("OUROBOROS_PLUGIN_LOCKFILE", str(alt_lockfile))
    monkeypatch.setenv("OUROBOROS_PLUGIN_TRUST_ROOT", str(alt_trust))

    # 3. Dispatch resolves the plugin via the env-supplied paths even
    #    though the default lockfile is empty.
    cmd = build_plugin_dispatch_command("github-pr-ops")
    assert cmd is not None, (
        "dispatch must consult OUROBOROS_PLUGIN_LOCKFILE / "
        "OUROBOROS_PLUGIN_TRUST_ROOT, not the global defaults"
    )

    # 4. Sanity: removing the env vars and pointing dispatch back at
    #    the empty default makes the resolution fail (returns None for
    #    typer's "no such command" hint) — confirms the override is
    #    what supplied the resolution above, not stale global state.
    monkeypatch.delenv("OUROBOROS_PLUGIN_LOCKFILE")
    monkeypatch.delenv("OUROBOROS_PLUGIN_TRUST_ROOT")
    cmd_after = build_plugin_dispatch_command("github-pr-ops")
    assert cmd_after is None
    # Touch ``plugin_home`` so the staging side-effect is acknowledged.
    assert plugin_home.exists()
