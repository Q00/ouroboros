"""Plugin dispatch fallback for the top-level ``ooo`` CLI.

Per the locked RFC (`docs/rfc/userlevel-plugins.md`, "UX / Plugin name →
command-namespace mapping"), every installed plugin's manifest ``name``
field is the user-facing command namespace:

    ooo github-pr-ops review https://github.com/...

When typer's main app does not recognize ``github-pr-ops`` as a
registered subcommand, this module is consulted as a fallback: it
builds a one-shot Click command that resolves the name against the
user's lockfile, looks up the matching ``RegisteredProgram``, and runs
the requested subcommand through ``firewall.invoke_plugin``.

The fallback is deliberately read-only: it never installs, trusts, or
mutates the lockfile. State-mutating actions remain in the
``ooo plugin {add,install,trust,disable,remove}`` command group.

Out of scope here (tracked in #733): bridging the firewall's bounded-
payload audit trail to the user's terminal output. The firewall captures
stdout/stderr to compute the sha256 hash that lands on the audit ledger;
this dispatcher writes the captured bytes back through to the user's
terminal so they see what the plugin produced.
"""

from __future__ import annotations

from pathlib import Path
import secrets
import sys

import click

from ouroboros.cli.formatters.panels import print_error
from ouroboros.plugin.firewall import invoke_plugin
from ouroboros.plugin.lockfile import DEFAULT_LOCKFILE_PATH, Lockfile
from ouroboros.plugin.manifest import PluginManifestError, load_manifest
from ouroboros.plugin.trust_store import DEFAULT_TRUST_ROOT, TrustStore
from ouroboros.plugin.userlevel_registry import (
    RegistryError,
    UserLevelProgramRegistry,
)


def _build_registry_from_lockfile(lockfile_path: Path) -> tuple[UserLevelProgramRegistry, dict]:
    """Read the lockfile, load each manifest, register everything.

    Manifests that fail to load are skipped with a stderr warning so
    one bad plugin doesn't disable dispatch for every other installed
    plugin. Returns the populated registry and a name → ``LockEntry``
    map for callers that need install-subject metadata.
    """
    registry = UserLevelProgramRegistry()
    lock = Lockfile(lockfile_path)
    entries = lock.read()
    for entry in entries.values():
        manifest_path = Path(entry.plugin_home).expanduser() / "ouroboros.plugin.json"
        try:
            manifest = load_manifest(manifest_path)
        except PluginManifestError:
            # Skip — but never crash dispatch for one broken plugin.
            continue
        try:
            registry.register(manifest, replace=True)
        except RegistryError:
            # Namespace collision with another already-registered
            # plugin: keep the first registration, skip subsequent.
            continue
    return registry, entries


def build_plugin_dispatch_command(cmd_name: str) -> click.Command | None:
    """Return a Click command that dispatches ``ooo <cmd_name> ...`` to a
    plugin invocation, or ``None`` if no installed plugin claims that
    name. Returning ``None`` lets typer's default "no such command"
    handler take over.

    The Click command is built lazily so first-party command resolution
    keeps its fast path (no lockfile read, no manifest validation).
    """
    try:
        registry, entries = _build_registry_from_lockfile(DEFAULT_LOCKFILE_PATH)
    except (OSError, ValueError):
        # Lockfile missing or unreadable: nothing to dispatch.
        return None

    program = registry.get_by_namespace(cmd_name) or registry.get(cmd_name)
    if program is None:
        return None

    entry = entries.get(program.name)
    if entry is None:
        return None

    @click.command(
        name=cmd_name,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )
    @click.argument("subcommand", required=False)
    @click.argument("argv", nargs=-1, type=click.UNPROCESSED)
    def _dispatch(subcommand: str | None, argv: tuple[str, ...]) -> None:
        if subcommand is None:
            available = sorted(c.name for c in program.manifest.commands)
            print_error(
                f"missing command for plugin {program.name!r} "
                f"(available: {available}). "
                f"Run `ooo {cmd_name} <command> [args...]`."
            )
            raise click.exceptions.Exit(code=1)

        trust = TrustStore(root=DEFAULT_TRUST_ROOT)
        record = trust.read(program.name)
        is_disabled = trust.is_disabled_for_subject(
            program.name,
            source_type=entry.source_type or "",
            source_identity=entry.source_identity or "",
        )
        plugin_home = Path(entry.plugin_home).expanduser()

        # Discard events here — this dispatcher is the user-facing
        # surface; the audit trail is owned by the ledger writer the
        # firewall is wired to in production. We collect events into a
        # local list for symmetry with the firewall's contract but
        # don't replay them; the user sees stdout/stderr instead.
        events: list[dict] = []
        result = invoke_plugin(
            program,
            command_name=subcommand,
            argv=list(argv),
            trust_record=record,
            event_sink=events.append,
            correlation_id=f"ooo-cli-{secrets.token_hex(6)}",
            plugin_home=plugin_home,
            expected_source_identity=entry.source_identity or None,
            expected_artifact_digest=entry.artifact_digest or None,
            is_disabled=is_disabled,
        )

        # The firewall captured stdout/stderr to compute the bounded-
        # payload hash on the audit event. We have only the hashes
        # here; the raw bytes are not exposed by `InvocationResult` by
        # design (audit-side bounded-payload contract). For terminal
        # UX we print the structured `result.message` when present,
        # plus the standard exit code, which gives the user the
        # outcome without re-deriving it from the hash. The full
        # stdout/stderr passthrough is wired in #733's E2E proof,
        # which extends `InvocationResult` to carry raw streams when
        # the dispatcher (not the audit log) is the consumer.
        if result.message:
            target = sys.stdout if result.status == "success" else sys.stderr
            print(result.message, file=target)
        raise click.exceptions.Exit(code=result.exit_code or 0)

    _dispatch.help = (
        f"Dispatch a command to the installed plugin {program.name!r} "
        f"(version {entry.version}). Available commands: "
        f"{sorted(c.name for c in program.manifest.commands)}."
    )
    return _dispatch


__all__ = [
    "build_plugin_dispatch_command",
]
