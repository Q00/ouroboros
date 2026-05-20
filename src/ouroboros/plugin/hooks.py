"""Plugin lifecycle hook contract types.

This module defines the typed vocabulary used to validate and dispatch
v1 plugin lifecycle hooks. The shape mirrors the contract documented in
``docs/rfc/userlevel-plugins.md``.

What this module owns:

* :class:`HookKind` — the v1 hook vocabulary. Only the hooks listed as
  "Included" in the RFC are enumerated; deferred hooks are exposed
  separately via :class:`DeferredHookKind` so we can keep an
  explicit, audit-friendly record of v1 vs future scope without
  silently accepting them at manifest-validation time.
* :class:`HookFailurePolicy` — the v1 failure policies (``fail_open``
  / ``fail_closed``).
* :data:`HOOK_EVENT_TYPES` — the v1 hook event names vendored in the
  manifest/audit schemas and emitted or reserved by the firewall
  lifecycle wrapper (``plugin.hook.invoked``,
  ``plugin.hook.completed``, ``plugin.hook.blocked``, and
  ``plugin.hook.failed``).
* :func:`is_v1_hook_kind` / :func:`is_v1_failure_policy` — helpers
  consumed by manifest validators. They live here so the contract is
  the single source of truth.

This module deliberately stays contract-only: runtime execution belongs
to ``ouroboros.plugin.firewall`` and manifest shape belongs to the
versioned schemas plus ``ouroboros.plugin.manifest``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class HookKind(StrEnum):
    """V1 plugin lifecycle hook vocabulary.

    Values match the keys ``ouroboros-plugins`` manifests will use in
    their ``hooks[].name`` field. Only hooks listed as "Included" in
    ``docs/rfc/userlevel-plugins.md`` are enumerated here — deferred
    candidates are kept separate in :class:`DeferredHookKind` so a
    manifest cannot quietly opt into a hook before its runtime
    semantics are nailed down.
    """

    #: Runs after trust check and confirmation gate, before
    #: ``plugin.invoked`` is emitted. Intended for read-only
    #: inspection or policy decisions declared with the stronger
    #: lifecycle policy scope.
    BEFORE_INVOCATION = "before_invocation"

    #: Runs after ``plugin.completed`` / ``plugin.failed`` is known,
    #: before the wrapper returns to the caller. Intended for
    #: observability or summary emission. Scoped to started command
    #: entrypoint invocations only.
    AFTER_INVOCATION = "after_invocation"

    #: Observability-only hook that runs after the firewall has emitted
    #: a terminal ``plugin.failed`` event. The hook receives a bounded,
    #: redacted payload; its exit code, output, or failure can never
    #: mask the original error cause, which has already reached the
    #: caller through the terminal event and the returned
    #: ``InvocationResult``. Must declare ``fail_open`` and is gated by
    #: the read-only ``plugin:lifecycle:read`` permission.
    ON_ERROR = "on_error"

    #: Observability-only hook that runs after a cancellation signal
    #: forced the firewall to emit a terminal ``plugin.failed`` event
    #: with reason ``cancelled``. Same fail-open / observation-only /
    #: bounded-payload contract as :data:`ON_ERROR`; cleanup side
    #: effects are explicitly out of scope and must wait for a
    #: separate ``plugin:lifecycle:cleanup`` permission to be defined.
    ON_CANCEL = "on_cancel"


class DeferredHookKind(StrEnum):
    """Hook names deferred to follow-up RFC slices.

    Listing these as a separate enum makes scope-creep auditable.
    This module exposes the routing helper :func:`is_deferred_hook_kind`
    so manifest validators and downstream consumers can detect the
    intent; this PR (the types-only slice) **does not** itself reject
    these names at manifest load — the live rejection wiring lands in
    the follow-up manifest-validator slice and the JSON-schema enum
    tightening slice. Until those land, the existing v0.2 JSON Schema
    still accepts deferred names as plain strings.

    Any future PR that promotes one of these names into v1 must do so
    by moving the value into :class:`HookKind`, which is a visible
    diff in review.
    """

    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    BEFORE_ARTIFACT_WRITE = "before_artifact_write"
    AFTER_ARTIFACT_WRITE = "after_artifact_write"


#: Frozen subset of :class:`HookKind` that observes terminal plugin-wrapper
#: outcomes only. ``on_error`` runs after the firewall emits a terminal
#: ``plugin.failed`` event; ``on_cancel`` runs after an explicit cancellation
#: signal forces the wrapper to fail with cause ``cancelled``. Both are
#: observation-only: their failure cannot mask the original error/cancel cause,
#: and they are constrained to ``fail_open`` at the manifest layer.
TERMINAL_OBSERVABILITY_HOOK_KINDS: Final[frozenset[HookKind]] = frozenset(
    {HookKind.ON_ERROR, HookKind.ON_CANCEL}
)

TERMINAL_OBSERVABILITY_HOOK_NAMES: Final[frozenset[str]] = frozenset(
    hook.value for hook in TERMINAL_OBSERVABILITY_HOOK_KINDS
)
"""String names for v1 terminal observability hooks."""

#: Backward-compatible aliases for the original #1129 export. ``on_error`` and
#: ``on_cancel`` are no longer deferred; the empty frozensets keep downstream
#: importers from breaking while the new
#: :data:`TERMINAL_OBSERVABILITY_HOOK_KINDS` /
#: :data:`TERMINAL_OBSERVABILITY_HOOK_NAMES` exports describe the promoted v1
#: vocabulary.
TERMINAL_DEFERRED_HOOK_KINDS: Final[frozenset[DeferredHookKind]] = frozenset()
TERMINAL_DEFERRED_HOOK_NAMES: Final[frozenset[str]] = frozenset()


class ExcludedHookKind(StrEnum):
    """Candidate hook names explicitly excluded from the v1 vocabulary.

    The RFC enumerates these to prevent ``ouroboros-plugins`` authors
    from inferring that :class:`HookKind` will be extended toward an
    open-ended interception bus. Like :class:`DeferredHookKind`, this
    PR exposes them as a routing surface only; the live manifest /
    schema-level rejection of these names lands in the follow-up
    validator and schema-enum slices. Promoting any of these
    requires substrate work tracked under other canonical issues
    (#920 runtime adapters, #946 state/replay, eventing surfaces).
    """

    BEFORE_RUNTIME_START = "before_runtime_start"
    AFTER_RUNTIME_START = "after_runtime_start"
    BEFORE_STATE_COMMIT = "before_state_commit"
    AFTER_STATE_COMMIT = "after_state_commit"
    ON_EVENT = "on_event"
    ON_REWIND = "on_rewind"


class HookFailurePolicy(StrEnum):
    """Failure handling stance for a hook declaration."""

    #: Record the failure and continue the original invocation.
    #: Permitted only for observability-only hooks whose output cannot
    #: authorize or mutate work.
    FAIL_OPEN = "fail_open"

    #: Stop the original invocation and emit a failed/blocked audit
    #: result. Required for policy, security, mutating, or authority-
    #: bearing hooks.
    FAIL_CLOSED = "fail_closed"


#: V1 hook event names vendored in the manifest/audit schemas.
#: These are exported as constants so manifest validators, audit
#: consumers, and the firewall lifecycle wrapper reference the same
#: string set for hook start, completion, blocked, and failed outcomes.
HOOK_INVOKED_EVENT: Final[str] = "plugin.hook.invoked"
HOOK_COMPLETED_EVENT: Final[str] = "plugin.hook.completed"
HOOK_BLOCKED_EVENT: Final[str] = "plugin.hook.blocked"
HOOK_FAILED_EVENT: Final[str] = "plugin.hook.failed"
HOOK_RUNTIME_AUDIT_EVENTS: Final[frozenset[str]] = frozenset(
    {HOOK_INVOKED_EVENT, HOOK_COMPLETED_EVENT}
)
HOOK_OUTCOME_AUDIT_EVENTS: Final[frozenset[str]] = frozenset(
    {HOOK_BLOCKED_EVENT, HOOK_FAILED_EVENT}
)
HOOK_EVENT_TYPES: Final[frozenset[str]] = HOOK_RUNTIME_AUDIT_EVENTS | HOOK_OUTCOME_AUDIT_EVENTS

#: Backward-compatible alias for the original #984 export. Prefer
#: :data:`HOOK_OUTCOME_AUDIT_EVENTS` in new call sites so the name does
#: not imply that only blocked/failed hook outcomes exist.
HOOK_AUDIT_EVENTS: Final[frozenset[str]] = HOOK_OUTCOME_AUDIT_EVENTS

#: Hook permission scope reserved for read-only lifecycle observation
#: (the v1 baseline used by ``before_invocation`` / ``after_invocation``
#: observability hooks per ``docs/rfc/userlevel-plugins.md``). Manifest
#: authors declare it under top-level ``permissions[].scope`` so the
#: existing ``plugin.permission_used`` emission rule covers it without
#: a separate event family.
HOOK_LIFECYCLE_READ_SCOPE: Final[str] = "plugin:lifecycle:read"

#: Hook permission scope required for lifecycle hooks that can veto an
#: invocation through ``fail_closed``. Read-only lifecycle observation
#: remains available through :data:`HOOK_LIFECYCLE_READ_SCOPE`; this
#: ``plugin:lifecycle:policy`` is the explicit authority boundary for
#: policy decisions.
HOOK_LIFECYCLE_POLICY_SCOPE: Final[str] = "plugin:lifecycle:policy"

#: Frozen set of v1 hook permission scopes. Validators and manifest
#: authors reference this set rather than the bare string so the
#: contract intent is observable at every call site.
HOOK_LIFECYCLE_SCOPES: Final[frozenset[str]] = frozenset(
    {HOOK_LIFECYCLE_READ_SCOPE, HOOK_LIFECYCLE_POLICY_SCOPE}
)


def is_v1_hook_kind(value: str) -> bool:
    """Return True iff ``value`` names a hook included in v1.

    Use this in manifest validators rather than touching
    :class:`HookKind` membership directly so the contract intent
    (deferred vs excluded vs accepted) is preserved at the call site.
    """
    return value in {kind.value for kind in HookKind}


def is_deferred_hook_kind(value: str) -> bool:
    """Return True iff ``value`` names a deferred candidate hook."""
    return value in {kind.value for kind in DeferredHookKind}


def is_terminal_deferred_hook_kind(value: str) -> bool:
    """Return True iff ``value`` names a deferred terminal outcome hook.

    Retained as a backward-compatible no-op routing helper after PR #1131 / #939
    Wave 1-E promoted ``on_error`` and ``on_cancel`` into the v1 manifest
    vocabulary. Manifests should consume :func:`is_v1_hook_kind` or the new
    :func:`is_terminal_observability_hook_kind` helper instead — this function
    now returns ``False`` for every input.
    """
    return value in TERMINAL_DEFERRED_HOOK_NAMES


def is_terminal_observability_hook_kind(value: str) -> bool:
    """Return True iff ``value`` names a v1 terminal observability hook.

    ``on_error`` and ``on_cancel`` are observation-only lifecycle hooks: they
    receive bounded, redacted payloads after the firewall has emitted the
    terminal ``plugin.failed`` event, and their failures cannot mask the
    original error or cancel cause. Validators and runtime dispatch reference
    :data:`TERMINAL_OBSERVABILITY_HOOK_NAMES` through this helper so the
    contract intent is observable at every call site.
    """
    return value in TERMINAL_OBSERVABILITY_HOOK_NAMES


def is_excluded_hook_kind(value: str) -> bool:
    """Return True iff ``value`` names an explicitly excluded hook."""
    return value in {kind.value for kind in ExcludedHookKind}


def is_v1_failure_policy(value: str) -> bool:
    """Return True iff ``value`` names a v1 failure policy."""
    return value in {policy.value for policy in HookFailurePolicy}


def is_hook_lifecycle_scope(value: str) -> bool:
    """Return True iff ``value`` names a v1 hook lifecycle permission scope.

    Use this in manifest validators and capability resolvers so the
    set of acceptable lifecycle scopes stays in sync with
    :data:`HOOK_LIFECYCLE_SCOPES` — no new scope can sneak past the
    routing path without an explicit code change here.
    """
    return value in HOOK_LIFECYCLE_SCOPES


__all__ = [
    "HOOK_AUDIT_EVENTS",
    "HOOK_BLOCKED_EVENT",
    "HOOK_COMPLETED_EVENT",
    "HOOK_EVENT_TYPES",
    "HOOK_FAILED_EVENT",
    "HOOK_INVOKED_EVENT",
    "HOOK_LIFECYCLE_POLICY_SCOPE",
    "HOOK_LIFECYCLE_READ_SCOPE",
    "HOOK_LIFECYCLE_SCOPES",
    "HOOK_OUTCOME_AUDIT_EVENTS",
    "HOOK_RUNTIME_AUDIT_EVENTS",
    "TERMINAL_DEFERRED_HOOK_KINDS",
    "TERMINAL_DEFERRED_HOOK_NAMES",
    "TERMINAL_OBSERVABILITY_HOOK_KINDS",
    "TERMINAL_OBSERVABILITY_HOOK_NAMES",
    "DeferredHookKind",
    "ExcludedHookKind",
    "HookFailurePolicy",
    "HookKind",
    "is_deferred_hook_kind",
    "is_excluded_hook_kind",
    "is_hook_lifecycle_scope",
    "is_terminal_deferred_hook_kind",
    "is_terminal_observability_hook_kind",
    "is_v1_failure_policy",
    "is_v1_hook_kind",
]
