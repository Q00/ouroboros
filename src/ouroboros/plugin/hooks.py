"""Plugin lifecycle hook contract types.

This module is the first reviewable slice of issue #939. It introduces
the typed vocabulary the harness will use to dispatch plugin lifecycle
hooks — without changing runtime behavior, manifest schema validation,
or audit emission yet. The shape mirrors the contract documented in
``docs/rfc/userlevel-plugins.md`` (the boundary doc that landed in
``f718ef89e``).

What this module owns:

* :class:`HookKind` — the v1 hook vocabulary. Only the hooks listed as
  "Included" in the RFC are enumerated; deferred hooks are exposed
  separately via :class:`DeferredHookKind` so we can keep an
  explicit, audit-friendly record of v1 vs future scope without
  silently accepting them at manifest-validation time.
* :class:`HookFailurePolicy` — the v1 failure policies (``fail_open``
  / ``fail_closed``).
* :data:`HOOK_AUDIT_EVENTS` — the audit event names the wrapper emits
  for hook execution outcomes (``plugin.hook.blocked`` /
  ``plugin.hook.failed``).
* :func:`is_v1_hook_kind` / :func:`is_v1_failure_policy` — helpers
  consumed by manifest validators in follow-up PRs. They live here so
  the contract is the single source of truth.

What this module does **not** do:

* Wire hook dispatch into ``HarnessRunner`` or any runtime path.
* Change the manifest schema or the existing :class:`HookSpec`
  dataclass (validators that consume these helpers land in a follow-up
  PR).
* Emit audit events. The event names are exported as constants only.
* Add or remove permissions. Hook-specific permission emission is a
  future extension per the RFC.

Routes to #939. The full lifecycle slate lands incrementally; this
module is intentionally narrow enough to review as a typed contract
before any wiring change.
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
    #: inspection or ``fail_closed`` policy decisions.
    BEFORE_INVOCATION = "before_invocation"

    #: Runs after ``plugin.completed`` / ``plugin.failed`` is known,
    #: before the wrapper returns to the caller. Intended for
    #: observability or summary emission. Scoped to started command
    #: entrypoint invocations only.
    AFTER_INVOCATION = "after_invocation"


class DeferredHookKind(StrEnum):
    """Hook names deferred to follow-up RFC slices.

    Listing them as a separate enum makes scope-creep auditable:
    manifest validators MUST reject ``hooks[].name`` values from this
    set in v1, and any future PR that promotes one of these must do so
    explicitly by adding it to :class:`HookKind`.
    """

    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
    BEFORE_ARTIFACT_WRITE = "before_artifact_write"
    AFTER_ARTIFACT_WRITE = "after_artifact_write"
    ON_ERROR = "on_error"
    ON_CANCEL = "on_cancel"


class ExcludedHookKind(StrEnum):
    """Candidate hook names explicitly excluded from the v1 vocabulary.

    The RFC enumerates these to prevent ``ouroboros-plugins`` authors
    from inferring that ``HookKind`` will be extended toward an
    open-ended interception bus. They MUST NOT be accepted in v1
    manifests; promotion requires substrate work tracked under other
    canonical issues (#920 runtime adapters, #946 state/replay,
    eventing surfaces).
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


#: Audit event names the wrapper emits for hook execution outcomes.
#: These are exported as constants so manifest validators and audit
#: consumers reference the same string set the wrapper will emit when
#: dispatch lands in a follow-up PR.
HOOK_BLOCKED_EVENT: Final[str] = "plugin.hook.blocked"
HOOK_FAILED_EVENT: Final[str] = "plugin.hook.failed"
HOOK_AUDIT_EVENTS: Final[frozenset[str]] = frozenset({HOOK_BLOCKED_EVENT, HOOK_FAILED_EVENT})


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


def is_excluded_hook_kind(value: str) -> bool:
    """Return True iff ``value`` names an explicitly excluded hook."""
    return value in {kind.value for kind in ExcludedHookKind}


def is_v1_failure_policy(value: str) -> bool:
    """Return True iff ``value`` names a v1 failure policy."""
    return value in {policy.value for policy in HookFailurePolicy}


__all__ = [
    "HOOK_AUDIT_EVENTS",
    "HOOK_BLOCKED_EVENT",
    "HOOK_FAILED_EVENT",
    "DeferredHookKind",
    "ExcludedHookKind",
    "HookFailurePolicy",
    "HookKind",
    "is_deferred_hook_kind",
    "is_excluded_hook_kind",
    "is_v1_failure_policy",
    "is_v1_hook_kind",
]
