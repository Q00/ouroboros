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
from pathlib import Path
import re
import shlex
import subprocess
from typing import Literal

from ouroboros.plugin.digest import canonical_tree_hash
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


# --- argv redaction ---------------------------------------------------------
#
# Per the locked RFC ("Audit-event compatibility / Bounded payloads — argv
# handling"), the firewall MUST apply a built-in argv redaction policy
# before ledger write. This is the safety net for the case where a plugin
# accidentally accepts secrets via argv despite documentation telling
# users not to.
#
# The minimum policy is enumerated in the RFC and implemented here:
#   1. Values of well-known secret flags (`--token`, `--password`, etc.),
#      whether `--flag=value` or `--flag value`. The flag NAME survives;
#      only the VALUE is replaced with the literal `[redacted]`.
#   2. Tokens with high-confidence formats: `Bearer …`, GitHub
#      `gh[oprsu]_…`, OpenAI `sk-…`, AWS `AKIA…` access keys, and
#      JWT-shaped strings (three dot-separated base64url segments).
#
# The hash of the original argv (sha256 over the un-redacted form) MAY be
# recorded alongside the redacted argv for forensic reconciliation; we
# attach it to the event provenance under `argv_sha256` when redaction
# actually fired so the original value can be re-confirmed against an
# out-of-band store but never read straight off the ledger.

_REDACTED = "[redacted]"

_SECRET_FLAG_NAMES: frozenset[str] = frozenset(
    {
        "--token",
        "-t",
        "--password",
        "--passwd",
        "--api-key",
        "--apikey",
        "--secret",
        "--auth",
        "--authorization",
        "--client-secret",
        "--access-token",
        "--refresh-token",
        "--bearer",
        "--credential",
        "--credentials",
    }
)

# High-confidence secret patterns. Anchored where useful; safe to err on
# the side of redacting things that look secret-shaped.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub PAT / app / OAuth tokens
    re.compile(r"^gh[oprsu]_[A-Za-z0-9]{20,}$"),
    # OpenAI keys
    re.compile(r"^sk-[A-Za-z0-9_-]{20,}$"),
    # AWS access key id
    re.compile(r"^AKIA[0-9A-Z]{16}$"),
    # JWT-shaped: three dot-separated base64url segments
    re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$"),
)


def _is_secret_value(value: str) -> bool:
    """Return True for argv values that match one of the locked
    high-confidence secret formats. Conservative: bare strings that don't
    match are passed through untouched so non-secret argv stays readable
    in the ledger."""
    if value.startswith("Bearer ") and len(value) > len("Bearer "):
        return True
    return any(p.fullmatch(value) for p in _SECRET_VALUE_PATTERNS)


def _redact_argv(argv: list[str]) -> tuple[list[str], bool]:
    """Apply the RFC's built-in argv redaction policy.

    Returns a tuple ``(redacted_argv, redaction_fired)``. ``redaction_fired``
    is True iff at least one element was rewritten — the caller uses that
    to decide whether to record the sha256 of the original argv on the
    event for forensic reconciliation.
    """
    redacted: list[str] = []
    fired = False
    pending_value_redact = False
    for token in argv:
        if pending_value_redact:
            redacted.append(_REDACTED)
            fired = True
            pending_value_redact = False
            continue
        # `--flag=value` form: split on first `=`.
        if token.startswith("-") and "=" in token:
            flag, _, _ = token.partition("=")
            if flag in _SECRET_FLAG_NAMES:
                redacted.append(f"{flag}={_REDACTED}")
                fired = True
                continue
        # Bare flag form: value is the next argv element.
        if token in _SECRET_FLAG_NAMES:
            redacted.append(token)
            pending_value_redact = True
            continue
        # High-confidence value-shaped match (Bearer …, gh*_…, sk-…,
        # AKIA…, JWT-shaped).
        if _is_secret_value(token):
            redacted.append(_REDACTED)
            fired = True
            continue
        redacted.append(token)
    if pending_value_redact:
        # Trailing `--token` with no value: nothing to redact, but the
        # plugin will reject it at parse-time anyway. Emit it as-is.
        pass
    return redacted, fired


def _argv_sha256(argv: list[str]) -> str:
    """Hex sha256 over the un-redacted argv joined by a record separator
    that cannot appear in argv tokens (so the digest is collision-resistant
    across argv boundary). Forensic-only — never persisted alongside the
    raw value."""
    h = hashlib.sha256()
    for token in argv:
        h.update(token.encode("utf-8", errors="surrogateescape"))
        h.update(b"\x1f")  # ASCII unit separator
    return h.hexdigest()


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
    """Build an event matching schemas/0.1/audit-event.schema.json.

    Per the RFC's bounded-payload argv contract, ``argv`` is run through
    ``_redact_argv`` before reaching the ledger; when redaction fires, a
    ``argv_sha256`` field is added to ``provenance`` for forensic
    reconciliation against the un-redacted argv stored out-of-band.
    """
    cmd: dict = {"namespace": namespace, "name": command_name}
    redaction_fired = False
    argv_hash: str | None = None
    if argv is not None:
        redacted, redaction_fired = _redact_argv(list(argv))
        cmd["argv"] = redacted
        if redaction_fired:
            argv_hash = _argv_sha256(list(argv))
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
    final_provenance: dict[str, str] = dict(provenance) if provenance is not None else {}
    if argv_hash is not None:
        final_provenance.setdefault("argv_sha256", argv_hash)
    if final_provenance:
        event["provenance"] = final_provenance
    return event


def _required_permissions(manifest: PluginManifest) -> list[str]:
    return [p.scope for p in manifest.permissions if p.required]


def _record_matches_subject(
    manifest: PluginManifest,
    trust_record: TrustRecord | None,
    *,
    expected_source_identity: str | None,
    expected_artifact_digest: str | None,
) -> bool:
    """A trust record is authoritative iff it matches the install subject.

    Per the locked RFC ("Trust identity"), the subject is
    ``(version, source.type, source_identity, artifact_digest)``. ANY
    field changing voids the grant. When `expected_*` are None, fall
    back to the legacy version-only check (so unit tests of the firewall
    that don't plumb a plugin_home stay green).
    """
    if trust_record is None or trust_record.version != manifest.version:
        return False
    # source.type is always known from the manifest; require record's
    # source_type (when set) to match. An empty record source_type is a
    # legacy record — accept under the version-only contract.
    if trust_record.source_type and trust_record.source_type != manifest.source.type:
        return False
    if expected_source_identity is not None:
        if (
            trust_record.source_identity
            and trust_record.source_identity != expected_source_identity
        ):
            return False
    if expected_artifact_digest is not None:
        if (
            trust_record.artifact_digest
            and trust_record.artifact_digest != expected_artifact_digest
        ):
            return False
    return True


def _trust_state_label(
    manifest: PluginManifest,
    trust_record: TrustRecord | None,
    *,
    expected_source_identity: str | None = None,
    expected_artifact_digest: str | None = None,
) -> str:
    """Return the trust state label for `manifest`.

    `"trusted"` is reserved for the state in which an invocation will NOT
    be blocked on the trust check: the record matches the installed
    subject, has at least one granted scope, and covers every
    `required: true` permission. A partial grant set still leaves the
    plugin gated by `_missing_required`, so reporting it as `"trusted"`
    would mis-label a permission boundary in audit events and in the
    `inspect`/`list` UX.
    """
    if manifest.source.type == "first_party":
        return "first_party"
    if not _record_matches_subject(
        manifest,
        trust_record,
        expected_source_identity=expected_source_identity,
        expected_artifact_digest=expected_artifact_digest,
    ):
        return "installed"
    assert trust_record is not None  # narrowed by _record_matches_subject
    if not trust_record.granted_scopes:
        return "installed"
    required = _required_permissions(manifest)
    if required and trust_record.missing(required):
        return "installed"
    return "trusted"


def _missing_required(
    manifest: PluginManifest,
    trust_record: TrustRecord | None,
    *,
    expected_source_identity: str | None,
    expected_artifact_digest: str | None,
) -> list[str]:
    required = _required_permissions(manifest)
    if not required:
        return []
    if not _record_matches_subject(
        manifest,
        trust_record,
        expected_source_identity=expected_source_identity,
        expected_artifact_digest=expected_artifact_digest,
    ):
        # Treat a subject-mismatched record as if no trust were granted:
        # the user must re-grant scopes against the new subject.
        return list(required)
    assert trust_record is not None  # narrowed by _record_matches_subject
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
    plugin_home: Path | None = None,
    expected_source_identity: str | None = None,
    expected_artifact_digest: str | None = None,
    is_disabled: bool = False,
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
        plugin_home: Path to the installed plugin directory. When
            provided, the entrypoint is launched with ``cwd=plugin_home``
            so that manifest-declared interpreters like
            ``python -m github_pr_ops`` resolve the plugin's modules from
            its installed root rather than from the user's terminal cwd.
            The firewall ALSO recomputes the canonical tree hash of this
            directory before invocation and refuses to launch on drift
            (RFC: ``result.status="trust_subject_changed"``).
        expected_source_identity: The lockfile's recorded
            ``source_identity`` for this plugin. When set, the trust
            record's ``source_identity`` must match.
        expected_artifact_digest: The lockfile's recorded
            ``artifact_digest``. When set, the firewall recomputes the
            canonical tree hash of ``plugin_home`` and refuses to launch
            if it does not match (closes the code-substitution path).
        is_disabled: When True, the firewall refuses invocation
            unconditionally. RFC: "the firewall MUST consult the disable
            record before any invocation, independently of whether trust
            records exist".

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

    trust_state = _trust_state_label(
        manifest,
        trust_record,
        expected_source_identity=expected_source_identity,
        expected_artifact_digest=expected_artifact_digest,
    )
    risks = _scope_risk_index(manifest)
    emitted: list[dict] = []

    def _emit(event: dict) -> None:
        event_sink(event)
        emitted.append(event)

    # 0. Disable check — fires before everything, including before the
    # trust check, so a plugin with no `required: true` permissions
    # cannot bypass `disable` by having an empty trust subject.
    if is_disabled:
        message = (
            f"plugin {manifest.name!r} is disabled; run "
            f"`ooo plugin trust {manifest.name} --scope <scope>` to re-enable."
        )
        _emit(
            _event_envelope(
                event_type="plugin.failed",
                manifest=manifest,
                namespace=namespace,
                command_name=command_name,
                argv=argv,
                trust_state="disabled",
                result={"status": "blocked", "message": message},
                provenance={"correlation_id": correlation_id, "reason": "disabled"},
            )
        )
        return InvocationResult(
            status="blocked",
            exit_code=None,
            message=message,
            events=tuple(emitted),
        )

    # 0b. Code-substitution check (per the RFC's per-invocation
    # re-verification rule). Only enforced when the caller plumbs the
    # expected digest + plugin_home; tests of the firewall's other
    # contracts remain green without this plumbing.
    if expected_artifact_digest is not None and plugin_home is not None:
        try:
            current_digest = canonical_tree_hash(plugin_home)
        except FileNotFoundError as exc:
            message = f"plugin home missing: {plugin_home} ({exc})"
            _emit(
                _event_envelope(
                    event_type="plugin.failed",
                    manifest=manifest,
                    namespace=namespace,
                    command_name=command_name,
                    argv=argv,
                    trust_state="installed",
                    result={"status": "trust_subject_changed", "message": message},
                    provenance={
                        "correlation_id": correlation_id,
                        "reason": "plugin_home_missing",
                    },
                )
            )
            return InvocationResult(
                status="blocked",
                exit_code=None,
                message=message,
                events=tuple(emitted),
            )
        if current_digest != expected_artifact_digest:
            message = (
                f"plugin {manifest.name!r} bytes have changed since "
                f"installation; refusing to invoke. Run "
                f"`ooo plugin add ...` (or `ooo plugin install ...`) to "
                f"re-record the trust subject and re-grant scopes."
            )
            _emit(
                _event_envelope(
                    event_type="plugin.failed",
                    manifest=manifest,
                    namespace=namespace,
                    command_name=command_name,
                    argv=argv,
                    trust_state="installed",
                    result={
                        "status": "trust_subject_changed",
                        "message": message,
                    },
                    provenance={
                        "correlation_id": correlation_id,
                        "expected_artifact_digest": expected_artifact_digest,
                        "current_artifact_digest": current_digest,
                    },
                )
            )
            return InvocationResult(
                status="blocked",
                exit_code=None,
                message=message,
                events=tuple(emitted),
            )

    # 1. Pre-invocation trust check (locked Q1).
    # First-party programs skip the trust check (per Q00/ouroboros-plugins#8).
    if manifest.source.type != "first_party":
        missing = _missing_required(
            manifest,
            trust_record,
            expected_source_identity=expected_source_identity,
            expected_artifact_digest=expected_artifact_digest,
        )
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

    # 5. Run entrypoint out-of-process. The launch cwd is set to
    # ``plugin_home`` when the caller plumbs it (the CLI does this), so
    # manifest entrypoints like ``python -m github_pr_ops`` resolve from
    # the installed plugin root rather than from the user's terminal cwd.
    # When plugin_home is None (firewall unit tests / first-party
    # programs that ship their own absolute entrypoint), we fall back to
    # the caller's cwd to preserve the previous test contract.
    cmd_template = manifest.entrypoint.command
    cmd_argv = shlex.split(cmd_template) + [command_name] + list(argv)
    runner = subprocess_runner or subprocess.run
    run_kwargs: dict = {
        "capture_output": True,
        "text": True,
        "check": False,
    }
    if plugin_home is not None:
        run_kwargs["cwd"] = str(plugin_home)
    try:
        completed = runner(cmd_argv, **run_kwargs)
    except FileNotFoundError as exc:
        message = f"entrypoint not found: {cmd_argv[0]!r} ({exc})"
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
            exit_code=127,
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
