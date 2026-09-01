"""CLI-level telemetry regression: closed-vocabulary `command` values.

``_PluginAwareGroup`` (src/ouroboros/cli/main.py) resolves any top-level
name not in the static command table as a dynamically-installed plugin
command via ``build_plugin_dispatch_command``, so ``ctx.invoked_subcommand``
-- and therefore ``telemetry.capture_cli_command``'s ``subcommand``
argument -- is exactly as caller-controlled as an MCP tool name or a job
type. This exercises the REAL Typer app through a genuine dynamic plugin
dispatch (not a direct ``capture_cli_command()`` call) to prove the
integration wiring folds a dynamically-resolved plugin name to the fixed
``extension_command`` literal end to end, mirroring the fixture pattern
``tests/unit/cli/test_plugin_dispatch.py`` already uses to stage an
installed plugin (``stub_default_paths`` + ``_stage_installed_plugin``).
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest
from typer.testing import CliRunner

from ouroboros import telemetry
from ouroboros.cli.main import app as ouroboros_app
from ouroboros.plugin.digest import canonical_tree_hash
from ouroboros.plugin.lockfile import LockEntry, Lockfile
from ouroboros.plugin.trust_store import TrustStore

_PLUGIN_NAME = "acme-private-project"
_MANIFEST: dict[str, Any] = {
    "schema_version": "0.1",
    "name": _PLUGIN_NAME,
    "version": "0.1.0",
    "description": "Minimal reference plugin for the CLI telemetry regression.",
    "source": {"type": "local_path", "path": f"plugins/{_PLUGIN_NAME}"},
    "commands": [
        {
            "namespace": _PLUGIN_NAME,
            "name": "run",
            "summary": "No-op reference command.",
            "usage": f"ooo {_PLUGIN_NAME} run",
            "risk": "read_only",
            "requires_confirmation": False,
        },
    ],
    "capabilities": [],
    "permissions": [],
    "entrypoint": {"type": "command", "command": "python -m fake_plugin"},
}


@pytest.fixture
def installed_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Stage a minimal installed plugin so _PluginAwareGroup resolves its
    name to a real click.Command -- same lockfile/trust-store staging
    tests/unit/cli/test_plugin_dispatch.py's stub_default_paths +
    _stage_installed_plugin use, inlined here to keep this file
    self-contained rather than importing another test module's private
    helpers."""
    lockfile_path = tmp_path / "plugins.lock"
    trust_root = tmp_path / "trust"
    plugin_home = tmp_path / "homes" / _PLUGIN_NAME
    plugin_home.mkdir(parents=True)
    (plugin_home / "ouroboros.plugin.json").write_text(json.dumps(_MANIFEST))

    monkeypatch.setattr(
        "ouroboros.cli.commands.plugin_dispatch.DEFAULT_LOCKFILE_PATH", lockfile_path
    )
    monkeypatch.setattr("ouroboros.cli.commands.plugin_dispatch.DEFAULT_TRUST_ROOT", trust_root)

    digest = canonical_tree_hash(plugin_home)
    Lockfile(lockfile_path).add(
        LockEntry(
            name=_PLUGIN_NAME,
            version="0.1.0",
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
        plugin=_PLUGIN_NAME,
        version="0.1.0",
        scope="",
        granted_by="user:test",
        source_type="local_path",
        source_identity=str(plugin_home),
        artifact_digest=digest,
    )
    return _PLUGIN_NAME


def test_dynamic_plugin_dispatch_captures_extension_command_not_plugin_name(
    installed_plugin: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL Click dynamic dispatch (not a direct capture_cli_command()
    call): the plugin name is resolved by _PluginAwareGroup exactly the
    way an actually-installed third-party plugin would be, and the
    telemetry captured from that dispatch must carry `extension_command`,
    never the plugin's identifying name -- checked against the full
    captured event, not just the `command` field."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda event, properties=None: captured.append({"event": event, "properties": properties}),
    )

    monkeypatch.setattr(
        "ouroboros.plugin.firewall.subprocess.run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"ok\n", stderr=b""
        ),
    )

    runner = CliRunner()
    result = runner.invoke(ouroboros_app, [installed_plugin, "run"])

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert captured[0]["event"] == "command_run"
    assert captured[0]["properties"]["command"] == "extension_command"
    assert installed_plugin not in repr(captured[0])


def test_canonical_builtin_command_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control case: a genuine built-in top-level command (no plugin
    dispatch involved) must still capture its own real command value."""
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        telemetry,
        "capture",
        lambda event, properties=None: captured.append({"event": event, "properties": properties}),
    )

    runner = CliRunner()
    runner.invoke(ouroboros_app, ["status", "--help"])

    assert len(captured) == 1
    assert captured[0]["properties"]["command"] == "status"


def test_statically_registered_commands_are_subset_of_canonical_set() -> None:
    """Sync guard: every command Click actually resolves without falling
    through to plugin dispatch must be in the audited set -- a new
    built-in command that forgets to update _CANONICAL_CLI_COMMANDS fails
    here instead of silently starting to look like an extension command."""
    import typer

    click_command = typer.main.get_command(ouroboros_app)
    registered = set(click_command.commands.keys())
    assert registered <= telemetry._CANONICAL_CLI_COMMANDS
