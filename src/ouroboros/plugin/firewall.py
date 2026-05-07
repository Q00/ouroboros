"""Plugin invocation firewall.

Every UserLevel plugin command must pass through `invoke_plugin`. The
firewall is the single chokepoint that:

  1. Pre-invocation trust check (locked Q1 of Q00/ouroboros-plugins#9):
     refuse + clean error if a `required: true` permission is not trusted;
     emit only `plugin.failed (status=blocked)`. NO `plugin.invoked` is
     emitted in this case.
  2. Single confirmation gate (locked Q2): if the command sets
     `requires_confirmation: true`, prompt the user once. No second
     prompt for permission risk.
  3. Emit `plugin.invoked` before launching the entrypoint subprocess.
  4. Emit `plugin.permission_used` for each `required: true` permission
     declared by the manifest. v0 uses Option (a): coarse declared-set
     emission, not per-call granular tracking.
  5. Run the entrypoint out-of-process via subprocess.
  6. Emit `plugin.completed` (status=success) or `plugin.failed`
     (status=failed) on terminal.

Audit events conform to schemas/0.1/audit-event.schema.json. Bounded
payloads: argv stored as-is, raw stdout/stderr replaced with a sha256
hash. Tokens, channel IDs, free-form user messages are forbidden by
contract.

The firewall does NOT own the audit log. Callers pass an `event_sink`
(any callable taking a dict) which is typically wired to the core
ledger writer (#737). Tests pass a list-appender for inspection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import shlex
import subprocess
from typing import Literal

from ouroboros.plugin.manifest import PluginManifest
from ouroboros.plugin.trust_store import TrustRecord
from ouroboros.plugin.userlevel_registry import RegisteredProgram

SCHEMA_VERSION = "0.1"

EventSink = Callable[[dict], None]
ConfirmFn = Callable[[str], bool]


@dataclass(frozen=True)
class InvocationResult:
    status: Literal["success", "blocked", "failed"]
    exit_code: int | None = None
    message: str = ""
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    events: tuple[dict, ...] = field(default_factory=tuple)


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source_type_for_event(manifest: PluginManifest) -> str:
    return manifest.source.type


def _event_envelope(
    *,
    event_type: str,
    manifest: PluginManifest,
    namespace: str,
    command_name: str,
    argv: list[str] | None,
    trust_state: str,
    capabilities_used: Iterable[str] = (),
    permissions_used: Iterable[str] = (),
    result: dict | None = None,
    provenance: dict[str, str] | None = None,
) -> dict:
    """Build an event matching schemas/0.1/audit-event.schema.json."""
    cmd: dict = {"namespace": namespace, "name": command_name}
    if argv is not None:
        cmd["argv"] = list(argv)
    event: dict = {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type,
        "occurred_at": _utc_now_iso(),
        "plugin": {
            "name": manifest.name,
            "version": manifest.version,
            "source_type": _source_type_for_event(manifest),
        },
        "command": cmd,
        "trust_state": trust_state,
        "capabilities_used": list(capabilities_used),
        "permissions_used": list(permissions_used),
        "result": result or {"status": "success"},
    }
    if provenance is not None:
        event["provenance"] = dict(provenance)
    return event


def _required_permissions(manifest: PluginManifest) -> list[str]:
    return [p.scope for p in manifest.permissions if p.required]


def _trust_state_label(
    manifest: PluginManifest,
    trust_record: TrustRecord | None,
) -> str:
    if manifest.source.type == "first_party":
        return "first_party"
    if trust_record is not None and trust_record.granted_scopes:
        return "trusted"
    return "installed"


def _missing_required(
    manifest: PluginManifest,
    trust_record: TrustRecord | None,
) -> list[str]:
    required = _required_permissions(manifest)
    if not required:
        return []
    if trust_record is None:
        return list(required)
    return trust_record.missing(required)


def _format_blocked_message(plugin_name: str, missing: list[str], risks: dict[str, str]) -> str:
    """Per locked Q1: name the missing scope and the exact trust command."""
    first = missing[0]
    risk = risks.get(first, "?")
    return (
        f"plugin requires `{first}` ({risk}), which is not yet trusted. "
        f"Run: ooo plugin trust {plugin_name} --scope {first}"
    )


def _scope_risk_index(manifest: PluginManifest) -> dict[str, str]:
    return {p.scope: p.risk for p in manifest.permissions}


def invoke_plugin(
    program: RegisteredProgram,
    *,
    command_name: str,
    argv: list[str],
    trust_record: TrustRecord | None,
    event_sink: EventSink,
    correlation_id: str,
    confirm: ConfirmFn = lambda _msg: True,
    subprocess_runner: Callable[..., subprocess.CompletedProcess] | None = None,
) -> InvocationResult:
    """Invoke a UserLevel plugin command through the firewall.

    Args:
        program: Registered UserLevel program (from `userlevel_registry`).
        command_name: The name of the command within the plugin's namespace.
        argv: User-provided argument vector for the command.
        trust_record: The plugin's TrustRecord (None if not yet trusted).
            For first-party programs, may be None — the firewall does not
            consult it for them.
        event_sink: Callable that receives audit events. Wire to the core
            ledger writer (#737) in production; pass `events.append` in
            tests.
        correlation_id: Cross-event correlation id for the ledger.
        confirm: Optional callable for confirmation prompts. Default is
            "auto-confirm" (returns True). CLI passes a function that
            actually prompts.
        subprocess_runner: Optional override (for tests) of subprocess.run.

    Returns:
        `InvocationResult` with status, exit code, sha256 hashes of
        stdout/stderr, and the events emitted (also pushed to event_sink).
    """
    manifest = program.manifest
    namespace = program.namespace
    command = program.find_command(command_name)
    if command is None:
        # Treat unknown command as a failure that emits no events — the
        # caller (CLI) is responsible for surfacing this. Returning a
        # failed result keeps the contract simple.
        return InvocationResult(
            status="failed",
            exit_code=2,
            message=f"unknown command {command_name!r} in namespace {namespace!r}",
        )

    trust_state = _trust_state_label(manifest, trust_record)
    risks = _scope_risk_index(manifest)
    emitted: list[dict] = []

    def _emit(event: dict) -> None:
        event_sink(event)
        emitted.append(event)

    # 1. Pre-invocation trust check (locked Q1).
    # First-party programs skip the trust check (per Q00/ouroboros-plugins#8).
    if manifest.source.type != "first_party":
        missing = _missing_required(manifest, trust_record)
        if missing:
            message = _format_blocked_message(manifest.name, missing, risks)
            _emit(
                _event_envelope(
                    event_type="plugin.failed",
                    manifest=manifest,
                    namespace=namespace,
                    command_name=command_name,
                    argv=argv,
                    trust_state=trust_state,
                    result={"status": "blocked", "message": message},
                    provenance={"correlation_id": correlation_id},
                )
            )
            return InvocationResult(
                status="blocked",
                exit_code=None,
                message=message,
                events=tuple(emitted),
            )

    # 2. Confirmation gate (locked Q2 — ONE prompt, command-level).
    if command.requires_confirmation:
        prompt = (
            f"This command is destructive and requires confirmation.\n"
            f"Plugin: {manifest.name} {manifest.version}\n"
            f"Action: {command_name} {' '.join(argv)}\n"
            f"Continue?"
        )
        if not confirm(prompt):
            message = "user declined confirmation"
            _emit(
                _event_envelope(
                    event_type="plugin.failed",
                    manifest=manifest,
                    namespace=namespace,
                    command_name=command_name,
                    argv=argv,
                    trust_state=trust_state,
                    result={"status": "blocked", "message": message},
                    provenance={"correlation_id": correlation_id},
                )
            )
            return InvocationResult(
                status="blocked",
                exit_code=None,
                message=message,
                events=tuple(emitted),
            )

    # 3. Emit `plugin.invoked` before launch.
    _emit(
        _event_envelope(
            event_type="plugin.invoked",
            manifest=manifest,
            namespace=namespace,
            command_name=command_name,
            argv=argv,
            trust_state=trust_state,
            provenance={"correlation_id": correlation_id},
        )
    )

    # 4. Emit one `plugin.permission_used` per required permission.
    for scope in _required_permissions(manifest):
        _emit(
            _event_envelope(
                event_type="plugin.permission_used",
                manifest=manifest,
                namespace=namespace,
                command_name=command_name,
                argv=argv,
                trust_state=trust_state,
                permissions_used=[scope],
                provenance={"correlation_id": correlation_id, "scope": scope},
            )
        )

    # 5. Run entrypoint out-of-process.
    cmd_template = manifest.entrypoint.command
    cmd_argv = shlex.split(cmd_template) + [command_name] + list(argv)
    runner = subprocess_runner or subprocess.run
    try:
        completed = runner(
            cmd_argv,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        # Cover the full launch-failure surface (FileNotFoundError,
        # PermissionError, IsADirectoryError, ENOEXEC, etc.). The firewall is
        # the documented single chokepoint, so every launch failure must emit
        # a terminal `plugin.failed` event rather than escaping as an
        # uncaught exception. Exit code uses the conventional shell mapping
        # (127 = not found, 126 = found-but-not-executable, otherwise 1).
        if isinstance(exc, FileNotFoundError):
            message = f"entrypoint not found: {cmd_argv[0]!r} ({exc})"
            launch_exit_code = 127
        elif isinstance(exc, PermissionError):
            message = f"entrypoint not executable: {cmd_argv[0]!r} ({exc})"
            launch_exit_code = 126
        else:
            message = f"entrypoint launch failed: {cmd_argv[0]!r} ({exc})"
            launch_exit_code = 1
        _emit(
            _event_envelope(
                event_type="plugin.failed",
                manifest=manifest,
                namespace=namespace,
                command_name=command_name,
                argv=argv,
                trust_state=trust_state,
                result={"status": "failed", "message": message},
                provenance={"correlation_id": correlation_id},
            )
        )
        return InvocationResult(
            status="failed",
            exit_code=launch_exit_code,
            message=message,
            events=tuple(emitted),
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    stdout_hash = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
    stderr_hash = hashlib.sha256(stderr.encode("utf-8")).hexdigest()

    # 6. Terminal event: completed or failed.
    if completed.returncode == 0:
        _emit(
            _event_envelope(
                event_type="plugin.completed",
                manifest=manifest,
                namespace=namespace,
                command_name=command_name,
                argv=argv,
                trust_state=trust_state,
                result={"status": "success"},
                provenance={
                    "correlation_id": correlation_id,
                    "stdout_sha256": stdout_hash,
                    "stderr_sha256": stderr_hash,
                },
            )
        )
        return InvocationResult(
            status="success",
            exit_code=0,
            stdout_sha256=stdout_hash,
            stderr_sha256=stderr_hash,
            events=tuple(emitted),
        )
    else:
        message = f"entrypoint exited with code {completed.returncode}"
        _emit(
            _event_envelope(
                event_type="plugin.failed",
                manifest=manifest,
                namespace=namespace,
                command_name=command_name,
                argv=argv,
                trust_state=trust_state,
                result={"status": "failed", "message": message},
                provenance={
                    "correlation_id": correlation_id,
                    "stdout_sha256": stdout_hash,
                    "stderr_sha256": stderr_hash,
                },
            )
        )
        return InvocationResult(
            status="failed",
            exit_code=completed.returncode,
            message=message,
            stdout_sha256=stdout_hash,
            stderr_sha256=stderr_hash,
            events=tuple(emitted),
        )


__all__ = [
    "ConfirmFn",
    "EventSink",
    "InvocationResult",
    "SCHEMA_VERSION",
    "invoke_plugin",
]
