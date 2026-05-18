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

from ouroboros.plugin.digest import (
    EscapingSymlinkError,
    UnsupportedFileTypeError,
    canonical_tree_hash,
)
from ouroboros.plugin.hooks import (
    HOOK_COMPLETED_EVENT,
    HOOK_FAILED_EVENT,
    HOOK_INVOKED_EVENT,
    HookFailurePolicy,
    HookKind,
)
from ouroboros.plugin.manifest import HookSpec, PluginManifest
from ouroboros.plugin.trust_store import TrustRecord
from ouroboros.plugin.userlevel_registry import RegisteredProgram

SCHEMA_VERSION = "0.1"
HOOK_AUDIT_SCHEMA_VERSION = "0.3"
DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS = 300.0

EventSink = Callable[[dict], None]
ConfirmFn = Callable[[str], bool]


@dataclass(frozen=True)
class InvocationResult:
    """Outcome of a plugin invocation through the firewall.

    `stdout_sha256` / `stderr_sha256` are the hashes that land on the
    audit ledger (the RFC's bounded-payload contract). The raw
    `stdout_bytes` / `stderr_bytes` fields are the captured streams
    themselves — kept in memory for in-process consumers (e.g., the
    `ooo <plugin>` dispatcher needs to re-emit plugin output to the
    user's terminal). Audit-event emission never reads those bytes
    directly; only the hashes go on the wire. They default to `None`
    for blocked/failed paths where the entrypoint never produced
    output.
    """

    status: Literal["success", "blocked", "failed"]
    exit_code: int | None = None
    message: str = ""
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    stdout_bytes: bytes | None = None
    stderr_bytes: bytes | None = None
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
    pending_bearer_tail_redact = False
    for token in argv:
        if pending_bearer_tail_redact:
            redacted.append(_REDACTED)
            fired = True
            pending_bearer_tail_redact = False
            continue
        if pending_value_redact:
            redacted.append(_REDACTED)
            fired = True
            pending_value_redact = False
            if token == "Bearer":
                pending_bearer_tail_redact = True
            continue
        # `--flag=value` form: split on first `=`.
        if token.startswith("-") and "=" in token:
            flag, _, value = token.partition("=")
            if flag in _SECRET_FLAG_NAMES:
                redacted.append(f"{flag}={_REDACTED}")
                fired = True
                if value == "Bearer":
                    pending_bearer_tail_redact = True
                continue
        # Bare flag form: value is the next argv element.
        if token in _SECRET_FLAG_NAMES:
            redacted.append(token)
            pending_value_redact = True
            continue
        # High-confidence value-shaped match (Bearer …, gh*_…, sk-…,
        # AKIA…, JWT-shaped).  Split ``Bearer`` auth values are only
        # treated as secret context when introduced by a known secret flag
        # above; a standalone literal ``Bearer`` may be ordinary user input
        # and must not hide subsequent real flags from prompts/audit logs.
        if _is_secret_value(token):
            redacted.append(_REDACTED)
            fired = True
            continue
        redacted.append(token)
    if pending_value_redact:
        # Trailing `--token` with no value: nothing to redact, but the
        # plugin will reject it at parse-time anyway. Emit it as-is.
        pass
    if pending_bearer_tail_redact:
        # Trailing `Bearer` without a credential tail was already
        # redacted above. Nothing else to consume.
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


def _argv_summary(argv: list[str]) -> dict:
    """Compute a bounded fingerprint of argv for the audit envelope.

    Returns ``{argc, byte_length, sha256}``. Hashing uses NUL as the
    element separator so two different argv lists with the same
    concatenation cannot collide
    (``["ab", "cd"]`` vs ``["abcd"]``). ``byte_length`` excludes the
    separators so the number reflects the operator-visible payload
    size. The summary is computed over the **redacted** argv (the
    same value that lands in ``cmd["argv"]``) so the byte-length and
    sha256 describe what actually appears on the audit ledger; this
    keeps the observation-only metric consistent with the redaction
    contract that secret values never reach persisted state.
    """
    parts = [s.encode("utf-8", errors="replace") for s in argv]
    digest = hashlib.sha256(b"\x00".join(parts)).hexdigest()
    return {
        "argc": len(argv),
        "byte_length": sum(len(p) for p in parts),
        "sha256": digest,
    }


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
    schema_version: str = SCHEMA_VERSION,
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
        # ``argv_summary`` is observation-only sizing of what actually
        # lands on the ledger. Compute it over the redacted argv so the
        # byte_length / sha256 describe the persisted shape, not the
        # pre-redaction one — secret-shaped values must not contribute
        # to a hash that an audit consumer might reverse-correlate.
        cmd["argv_summary"] = _argv_summary(redacted)
        if redaction_fired:
            argv_hash = _argv_sha256(list(argv))
    event: dict = {
        "schema_version": schema_version,
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


def _matching_hooks(manifest: PluginManifest, hook_kind: HookKind) -> tuple[HookSpec, ...]:
    if manifest.schema_version != "0.3":
        return ()
    return tuple(hook for hook in manifest.hooks if hook.name == hook_kind.value)


def _hook_timeout_seconds(hook: HookSpec) -> float:
    return float(hook.timeout_seconds or DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS)


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
    # When the caller plumbs the lockfile-recorded ``expected_*``
    # (production CLI dispatch path), we are operating under the new
    # subject contract: a record that is silent on a subject column
    # cannot prove it was granted for THIS install subject. Refuse such
    # legacy/partial records so a same-version reinstall from a
    # different repo / path / bytes does NOT silently inherit pre-RFC
    # grants. Firewall unit tests that don't plumb ``expected_*`` keep
    # the legacy version-only behavior for backwards compatibility.
    if expected_source_identity is not None or expected_artifact_digest is not None:
        if (
            not trust_record.source_type
            or not trust_record.source_identity
            or not trust_record.artifact_digest
        ):
            return False
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
    runner = subprocess_runner or subprocess.run

    def _emit(event: dict) -> None:
        event_sink(event)
        emitted.append(event)

    # Coerce stdout/stderr to bytes for hashing, regardless of whether
    # the runner returned ``bytes`` (real subprocess.run without
    # ``text=True``) or ``str`` (test fakes that pre-decode). This also
    # handles partial buffers attached to ``subprocess.TimeoutExpired``.
    # ``surrogateescape`` round-trips arbitrary byte sequences through
    # str without raising, matching how Python decodes filesystem paths.
    def _to_bytes(value: object) -> bytes:
        if value is None:
            return b""
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8", errors="surrogateescape")
        # Defensive: any other type is treated as empty so we never
        # crash on an unexpected runner return shape.
        return b""

    def _run_lifecycle_hooks(hook_kind: HookKind) -> tuple[bool, str]:
        for hook in _matching_hooks(manifest, hook_kind):
            hook_provenance = {
                "correlation_id": correlation_id,
                "hook_name": hook.name,
                "failure_policy": hook.failure_policy,
            }
            _emit(
                _event_envelope(
                    event_type=HOOK_INVOKED_EVENT,
                    manifest=manifest,
                    namespace=namespace,
                    command_name=command_name,
                    argv=argv,
                    trust_state=trust_state,
                    permissions_used=hook.permissions,
                    provenance=hook_provenance,
                    schema_version=HOOK_AUDIT_SCHEMA_VERSION,
                )
            )
            try:
                hook_argv = shlex.split(hook.entrypoint.command)
            except ValueError as exc:
                hook_argv = []
                hook_error = f"hook entrypoint is not parseable: {exc}"
            else:
                hook_error = ""
            if not hook_argv and not hook_error:
                hook_error = "hook entrypoint command is empty after tokenization"

            if not hook_error:
                try:
                    hook_completed = runner(
                        hook_argv,
                        capture_output=True,
                        check=False,
                        timeout=_hook_timeout_seconds(hook),
                        cwd=str(plugin_home) if plugin_home is not None else None,
                    )
                    hook_stdout = _to_bytes(hook_completed.stdout)
                    hook_stderr = _to_bytes(hook_completed.stderr)
                    hook_provenance["returncode"] = str(hook_completed.returncode)
                    hook_provenance["stdout_sha256"] = hashlib.sha256(hook_stdout).hexdigest()
                    hook_provenance["stderr_sha256"] = hashlib.sha256(hook_stderr).hexdigest()
                    if hook_completed.returncode != 0:
                        hook_error = (
                            f"hook {hook.name} exited with code {hook_completed.returncode}"
                        )
                except subprocess.TimeoutExpired as exc:
                    hook_stdout = _to_bytes(exc.stdout)
                    hook_stderr = _to_bytes(exc.stderr)
                    hook_provenance["stdout_sha256"] = hashlib.sha256(hook_stdout).hexdigest()
                    hook_provenance["stderr_sha256"] = hashlib.sha256(hook_stderr).hexdigest()
                    hook_error = (
                        f"hook {hook.name} timed out after {_hook_timeout_seconds(hook):g}s: "
                        f"{type(exc).__name__}"
                    )
                except OSError as exc:
                    hook_error = f"hook {hook.name} failed to start: {type(exc).__name__}: {exc}"

            if hook_error:
                _emit(
                    _event_envelope(
                        event_type=HOOK_FAILED_EVENT,
                        manifest=manifest,
                        namespace=namespace,
                        command_name=command_name,
                        argv=argv,
                        trust_state=trust_state,
                        permissions_used=hook.permissions,
                        result={"status": "failed", "message": hook_error},
                        provenance=hook_provenance,
                        schema_version=HOOK_AUDIT_SCHEMA_VERSION,
                    )
                )
                if hook.failure_policy == HookFailurePolicy.FAIL_CLOSED.value:
                    return False, hook_error
                continue

            _emit(
                _event_envelope(
                    event_type=HOOK_COMPLETED_EVENT,
                    manifest=manifest,
                    namespace=namespace,
                    command_name=command_name,
                    argv=argv,
                    trust_state=trust_state,
                    permissions_used=hook.permissions,
                    provenance=hook_provenance,
                    schema_version=HOOK_AUDIT_SCHEMA_VERSION,
                )
            )
        return True, ""

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
        except (FileNotFoundError, NotADirectoryError) as exc:
            # Plugin home gone or replaced with a non-directory: the
            # firewall MUST refuse rather than crash. Treat both as the
            # subject having changed since install (the trusted bytes
            # are no longer hashable at the recorded path).
            message = f"plugin home unreadable: {plugin_home} ({type(exc).__name__}: {exc})"
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
        except (EscapingSymlinkError, UnsupportedFileTypeError) as exc:
            # ``canonical_tree_hash`` rejects symlinks that escape the
            # plugin root and unsupported file types (devices, FIFOs,
            # sockets) — both indicate post-install tampering that the
            # digest contract says we must refuse to invoke. Fail closed
            # with ``trust_subject_changed`` so the audit trail records
            # the exact reason, not a stack trace.
            message = (
                f"plugin home failed digest verification: {plugin_home} "
                f"({type(exc).__name__}: {exc})"
            )
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
                        "reason": "plugin_home_tampered",
                        "exception_type": type(exc).__name__,
                    },
                )
            )
            return InvocationResult(
                status="blocked",
                exit_code=None,
                message=message,
                events=tuple(emitted),
            )
        except (OSError, RuntimeError) as exc:
            # Permission errors, broken symlink loops, generic I/O on
            # plugin_home: same fail-closed contract as above. Without
            # this branch the exception would escape the firewall and
            # skip the required terminal ``plugin.failed`` event.
            message = f"plugin home unreadable: {plugin_home} ({type(exc).__name__}: {exc})"
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
                        "reason": "plugin_home_unreadable",
                        "exception_type": type(exc).__name__,
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
        redacted_prompt_argv, _ = _redact_argv(list(argv))
        prompt = (
            f"This command is destructive and requires confirmation.\n"
            f"Plugin: {manifest.name} {manifest.version}\n"
            f"Action: {command_name} {' '.join(redacted_prompt_argv)}\n"
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

    before_ok, before_message = _run_lifecycle_hooks(HookKind.BEFORE_INVOCATION)
    if not before_ok:
        message = f"before_invocation hook blocked invocation: {before_message}"
        _emit(
            _event_envelope(
                event_type="plugin.failed",
                manifest=manifest,
                namespace=namespace,
                command_name=command_name,
                argv=argv,
                trust_state=trust_state,
                result={"status": "blocked", "message": message},
                provenance={"correlation_id": correlation_id, "reason": "hook_failed"},
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
    # The schema only enforces ``minLength: 1`` on entrypoint.command, so
    # a manifest may carry a whitespace-only string (which tokenises to
    # ``[]``) or a string with an unmatched quote (which raises
    # ``ValueError`` from ``shlex``). Both shapes are installable today
    # and will reach this point. Without explicit handling, the
    # ``ValueError`` would escape the firewall — skipping the required
    # terminal ``plugin.failed`` event — and an empty token list would
    # bubble into an opaque launcher failure. Convert both to a
    # controlled ``plugin.failed`` outcome so dispatch always closes
    # with a terminal event.
    try:
        parsed_argv = shlex.split(cmd_template)
    except ValueError as exc:
        message = f"entrypoint command is not parseable: {exc}"
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
                    "exception_type": type(exc).__name__,
                },
            )
        )
        return InvocationResult(
            status="failed",
            exit_code=126,
            message=message,
            events=tuple(emitted),
        )
    if not parsed_argv:
        message = "entrypoint command is empty after tokenization"
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
            exit_code=126,
            message=message,
            events=tuple(emitted),
        )
    cmd_argv = parsed_argv + [command_name] + list(argv)
    # Capture stdout/stderr as **bytes** rather than asking subprocess
    # to decode them. The firewall only ever stores a sha256 hash of
    # those streams (the RFC's bounded-payload contract), so we do
    # not need a Unicode str here. Asking for ``text=True`` would
    # surface ``UnicodeDecodeError`` from a plugin that writes
    # non-UTF-8 bytes — and that exception would escape the firewall,
    # skipping the required terminal ``plugin.failed`` event.
    run_kwargs: dict = {
        "capture_output": True,
        "check": False,
        "timeout": DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS,
    }
    if plugin_home is not None:
        run_kwargs["cwd"] = str(plugin_home)

    try:
        completed = runner(cmd_argv, **run_kwargs)
    except FileNotFoundError as exc:
        # Entrypoint executable not on PATH. Posix shell convention is
        # exit code 127 ("command not found").
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
    except subprocess.TimeoutExpired as exc:
        # A plugin command is an external process under the firewall's
        # control boundary. Bound its lifetime the same way the Auto and
        # Ralph runtimes bound their agent loops, and always close the
        # audit sequence with a terminal ``plugin.failed`` event rather
        # than leaving the caller hung indefinitely.
        stdout_bytes = _to_bytes(exc.stdout)
        stderr_bytes = _to_bytes(exc.stderr)
        stdout_hash = hashlib.sha256(stdout_bytes).hexdigest()
        stderr_hash = hashlib.sha256(stderr_bytes).hexdigest()
        message = (
            f"entrypoint timed out after "
            f"{DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS:g}s: {cmd_argv[0]!r}"
        )
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
                    "reason": "timeout",
                    "exception_type": type(exc).__name__,
                    "timeout_seconds": f"{DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS:g}",
                    "stdout_sha256": stdout_hash,
                    "stderr_sha256": stderr_hash,
                },
            )
        )
        return InvocationResult(
            status="failed",
            exit_code=124,
            message=message,
            stdout_sha256=stdout_hash,
            stderr_sha256=stderr_hash,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            events=tuple(emitted),
        )
    except OSError as exc:
        # Other OS-level launch failures: PermissionError (entrypoint
        # not executable), NotADirectoryError (bad cwd), generic IO
        # failures. Posix shell convention is exit code 126 ("command
        # found but not executable"). The firewall MUST always emit a
        # terminal `plugin.failed` event; without this branch the
        # exception would escape `invoke_plugin` and crash the caller
        # while skipping the audit trail.
        message = f"entrypoint failed to start: {type(exc).__name__}: {exc}"
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
                    "exception_type": type(exc).__name__,
                },
            )
        )
        return InvocationResult(
            status="failed",
            exit_code=126,
            message=message,
            events=tuple(emitted),
        )

    stdout_bytes = _to_bytes(completed.stdout)
    stderr_bytes = _to_bytes(completed.stderr)
    stdout_hash = hashlib.sha256(stdout_bytes).hexdigest()
    stderr_hash = hashlib.sha256(stderr_bytes).hexdigest()

    after_ok, after_message = _run_lifecycle_hooks(HookKind.AFTER_INVOCATION)
    terminal_provenance = {
        "correlation_id": correlation_id,
        "stdout_sha256": stdout_hash,
        "stderr_sha256": stderr_hash,
    }

    # 6. Terminal event: completed or failed. after_invocation hooks run
    # before this event so a fail_closed hook can still control the final
    # outcome instead of being hidden behind an already-emitted success.
    if not after_ok:
        message = f"after_invocation hook blocked invocation result: {after_message}"
        _emit(
            _event_envelope(
                event_type="plugin.failed",
                manifest=manifest,
                namespace=namespace,
                command_name=command_name,
                argv=argv,
                trust_state=trust_state,
                result={"status": "blocked", "message": message},
                provenance=terminal_provenance | {"reason": "hook_failed"},
            )
        )
        return InvocationResult(
            status="blocked",
            exit_code=None,
            message=message,
            stdout_sha256=stdout_hash,
            stderr_sha256=stderr_hash,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            events=tuple(emitted),
        )

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
                provenance=terminal_provenance,
            )
        )
        return InvocationResult(
            status="success",
            exit_code=0,
            stdout_sha256=stdout_hash,
            stderr_sha256=stderr_hash,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            events=tuple(emitted),
        )

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
            provenance=terminal_provenance,
        )
    )
    return InvocationResult(
        status="failed",
        exit_code=completed.returncode,
        message=message,
        stdout_sha256=stdout_hash,
        stderr_sha256=stderr_hash,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        events=tuple(emitted),
    )


__all__ = [
    "ConfirmFn",
    "EventSink",
    "InvocationResult",
    "SCHEMA_VERSION",
    "invoke_plugin",
]
