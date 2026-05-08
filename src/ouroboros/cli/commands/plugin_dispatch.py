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


def _build_registry_from_lockfile(
    lockfile_path: Path,
) -> tuple[UserLevelProgramRegistry, dict, dict[str, str]]:
    """Read the lockfile, load each manifest, register everything.

    Manifests that fail to load are skipped (one bad plugin must not
    disable dispatch for every other installed plugin) but their
    ``(plugin_name → reason)`` is recorded in the third return value.
    Callers can use that map to distinguish "plugin is installed but
    its manifest is corrupt" from "no such command" so the operator
    receives a recovery hint instead of a generic typo error.

    Returns the populated registry, a name → ``LockEntry`` map, and a
    name → corruption-reason map.
    """
    registry = UserLevelProgramRegistry()
    lock = Lockfile(lockfile_path)
    entries = lock.read()
    corrupt: dict[str, str] = {}
    for entry in entries.values():
        manifest_path = Path(entry.plugin_home).expanduser() / "ouroboros.plugin.json"
        try:
            manifest = load_manifest(manifest_path)
        except PluginManifestError as exc:
            # Skip — but remember the failure so dispatch can surface
            # a recovery hint when this name is actually requested.
            corrupt[entry.name] = str(exc)
            continue
        try:
            registry.register(manifest, replace=True)
        except RegistryError:
            # Namespace collision with another already-registered
            # plugin: keep the first registration, skip subsequent.
            continue
    return registry, entries, corrupt


def build_plugin_dispatch_command(cmd_name: str) -> click.Command | None:
    """Return a Click command that dispatches ``ooo <cmd_name> ...`` to a
    plugin invocation, or ``None`` if no installed plugin claims that
    name. Returning ``None`` lets typer's default "no such command"
    handler take over.

    The Click command is built lazily so first-party command resolution
    keeps its fast path (no lockfile read, no manifest validation).

    There are three terminal states for the resolution attempt:

    - lockfile missing → genuinely "no plugins installed"; return
      ``None`` so typer's "no such command" handler runs (the user
      really did mistype something).
    - lockfile present but unreadable/malformed → DO NOT pretend the
      plugin is absent; that hides corruption behind "no such
      command" and leaves the operator without a recovery hint.
      Return a stub command that prints a friendly error and exits
      non-zero, regardless of which plugin name was typed.
    - lockfile readable but no entry matches ``cmd_name`` → return
      ``None`` (real "unknown command").
    """
    if not DEFAULT_LOCKFILE_PATH.exists():
        # No plugins installed at all. Let typer surface the standard
        # "unknown command" hint.
        return None
    try:
        registry, entries, corrupt = _build_registry_from_lockfile(DEFAULT_LOCKFILE_PATH)
    except (OSError, ValueError) as exc:
        # Lockfile present but corrupt — surface the corruption
        # directly. Hiding this as "no such command" makes a real
        # installed plugin indistinguishable from a typo whenever the
        # operator's plugins.lock breaks.
        captured = str(exc)

        @click.command(
            name=cmd_name,
            context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
        )
        @click.argument("argv", nargs=-1, type=click.UNPROCESSED)
        def _broken_lockfile(argv: tuple[str, ...]) -> None:  # noqa: ARG001 — argv ignored
            print_error(
                f"plugin lockfile is unreadable ({DEFAULT_LOCKFILE_PATH}): "
                f"{captured}. "
                f"Inspect or replace the file (`ooo plugin list --lockfile "
                f"<path>` accepts an override), then retry."
            )
            raise click.exceptions.Exit(code=1)

        return _broken_lockfile

    program = registry.get_by_namespace(cmd_name) or registry.get(cmd_name)
    if program is None:
        # Distinguish "plugin is installed but its manifest is corrupt"
        # from a real typo: if ``cmd_name`` matches a lockfile entry
        # whose manifest failed to load, surface a friendly recovery
        # hint instead of letting Typer say "no such command".
        if cmd_name in corrupt:
            captured_reason = corrupt[cmd_name]
            corrupt_entry = entries.get(cmd_name)
            corrupt_home = (
                str(Path(corrupt_entry.plugin_home).expanduser())
                if corrupt_entry is not None
                else "<unknown plugin home>"
            )

            @click.command(
                name=cmd_name,
                context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
            )
            @click.argument("argv", nargs=-1, type=click.UNPROCESSED)
            def _broken_manifest(argv: tuple[str, ...]) -> None:  # noqa: ARG001 — argv ignored
                print_error(
                    f"plugin {cmd_name!r} is installed but its manifest is "
                    f"unreadable: {captured_reason}. "
                    f"Inspect or repair the manifest at {corrupt_home}/"
                    f"ouroboros.plugin.json, or run `ooo plugin remove "
                    f"{cmd_name}` to reset its install."
                )
                raise click.exceptions.Exit(code=1)

            return _broken_manifest
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
        # The dispatcher is now a primary user-facing invocation path,
        # so a malformed `trust.json` / `disabled.json` MUST produce a
        # controlled refusal here instead of a traceback. Surface a
        # one-line message that names the recovery action and exit
        # non-zero — the same shape every other CLI failure takes.
        try:
            record = trust.read(program.name)
            # Use the canonical lock-entry subject so the disable
            # lookup keys against exactly the (source_type,
            # source_identity) tuple `ooo plugin disable` writes —
            # legacy lockfile rows (no source_type/source_identity
            # columns) would otherwise look up under empty strings
            # while `disable` wrote a non-empty fallback, leaving the
            # plugin invocable after a successful `disable`.
            subject_type, subject_identity = entry.subject_for_disable()
            is_disabled = trust.is_disabled_for_subject(
                program.name,
                source_type=subject_type,
                source_identity=subject_identity,
            )
        except (ValueError, OSError) as exc:
            print_error(
                f"trust state for {program.name!r} is unreadable: {exc}. "
                f"Run `ooo plugin inspect {program.name}` for details, or "
                f"remove the offending file under {DEFAULT_TRUST_ROOT}."
            )
            raise click.exceptions.Exit(code=1) from exc
        plugin_home = Path(entry.plugin_home).expanduser()

        # Per the locked RFC ("Invocation Contract / Confirmation gate"),
        # commands marked `requires_confirmation: true` MUST receive a
        # real prompt — not the firewall's auto-confirm default. Wire a
        # Click confirmation that defaults to "no" so a bare Enter
        # rejects the destructive action.
        def _interactive_confirm(prompt: str) -> bool:
            return click.confirm(prompt, default=False)

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
            confirm=_interactive_confirm,
        )

        # Surface the plugin's actual stdout/stderr to the user's
        # terminal. The firewall captured them as bytes for the audit
        # hash; the dispatcher writes them back through to the user
        # without re-decoding so binary output (color codes, mixed
        # encodings) round-trips faithfully. The audit ledger never
        # sees these raw bytes — only the sha256 hash — so the
        # bounded-payload contract is preserved.
        if result.stdout_bytes:
            sys.stdout.buffer.write(result.stdout_bytes)
            sys.stdout.flush()
        if result.stderr_bytes:
            sys.stderr.buffer.write(result.stderr_bytes)
            sys.stderr.flush()
        # Print the structured failure/blocked message after the raw
        # streams so it's the last thing the user sees and is clearly
        # attributable to the firewall, not the plugin itself.
        if result.status != "success" and result.message:
            print(result.message, file=sys.stderr)

        # Exit code mapping. The firewall returns ``exit_code=None``
        # for the blocked path (trust failure, disabled plugin, digest
        # drift, declined confirmation): those are NOT user successes
        # and shells/CI must see a non-zero status. Map blocked/failed
        # without a captured exit code to 1; preserve real subprocess
        # exit codes when present.
        if result.exit_code is not None:
            click_exit_code = result.exit_code
        elif result.status == "success":
            click_exit_code = 0
        else:
            # Blocked or failed without a launched subprocess — use 1
            # so shells / CI treat the refused invocation as failure.
            click_exit_code = 1
        raise click.exceptions.Exit(code=click_exit_code)

    _dispatch.help = (
        f"Dispatch a command to the installed plugin {program.name!r} "
        f"(version {entry.version}). Available commands: "
        f"{sorted(c.name for c in program.manifest.commands)}."
    )
    return _dispatch


__all__ = [
    "build_plugin_dispatch_command",
]
