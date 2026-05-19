"""Unit tests for the plugin lifecycle hook contract types.

Covers the v1 contract from issue #939 (first slice — types only):

* ``HookKind`` contains exactly the v1 "Included" hooks from the RFC.
* ``DeferredHookKind`` and ``ExcludedHookKind`` enumerate the remaining
  candidate sets without overlapping ``HookKind``.
* ``HookFailurePolicy`` exposes the two v1 policies the RFC defines.
* ``HOOK_OUTCOME_AUDIT_EVENTS`` matches the v1 hook outcome event names
  currently vendored in the schema (``plugin.hook.blocked`` /
  ``plugin.hook.failed``), while ``HOOK_AUDIT_EVENTS`` remains a
  compatibility alias for the original #984 export.
* The ``is_*`` helpers route any candidate string to exactly one of
  v1 / deferred / excluded / unknown.
"""

from __future__ import annotations

from ouroboros.plugin.hooks import (
    HOOK_AUDIT_EVENTS,
    HOOK_BLOCKED_EVENT,
    HOOK_COMPLETED_EVENT,
    HOOK_EVENT_TYPES,
    HOOK_FAILED_EVENT,
    HOOK_INVOKED_EVENT,
    HOOK_OUTCOME_AUDIT_EVENTS,
    HOOK_RUNTIME_AUDIT_EVENTS,
    TERMINAL_DEFERRED_HOOK_KINDS,
    TERMINAL_DEFERRED_HOOK_NAMES,
    TERMINAL_OBSERVABILITY_HOOK_KINDS,
    TERMINAL_OBSERVABILITY_HOOK_NAMES,
    DeferredHookKind,
    ExcludedHookKind,
    HookFailurePolicy,
    HookKind,
    is_deferred_hook_kind,
    is_excluded_hook_kind,
    is_terminal_deferred_hook_kind,
    is_terminal_observability_hook_kind,
    is_v1_failure_policy,
    is_v1_hook_kind,
)


class TestHookKindEnumeration:
    def test_v1_hook_set_is_exact(self) -> None:
        # PR #1131 promotes the terminal observability hooks into v1 so
        # ``HookKind`` now lists exactly four "Included" hooks.
        assert {kind.value for kind in HookKind} == {
            "before_invocation",
            "after_invocation",
            "on_error",
            "on_cancel",
        }

    def test_terminal_observability_hook_set_is_exact(self) -> None:
        assert {"on_error", "on_cancel"} == TERMINAL_OBSERVABILITY_HOOK_NAMES
        assert {kind.value for kind in TERMINAL_OBSERVABILITY_HOOK_KINDS} == {
            "on_error",
            "on_cancel",
        }
        assert set(HookKind) >= TERMINAL_OBSERVABILITY_HOOK_KINDS

    def test_terminal_deferred_hook_set_is_empty_after_promotion(self) -> None:
        # The deferred-bucket aliases survive for one release as empty
        # frozensets so downstream importers keep working.
        assert frozenset() == TERMINAL_DEFERRED_HOOK_KINDS
        assert frozenset() == TERMINAL_DEFERRED_HOOK_NAMES

    def test_deferred_hook_set_is_exact(self) -> None:
        # ``on_error`` / ``on_cancel`` were lifted out of the deferred
        # bucket by PR #1131; the remaining names stay deferred.
        assert {kind.value for kind in DeferredHookKind} == {
            "before_tool_call",
            "after_tool_call",
            "before_artifact_write",
            "after_artifact_write",
        }

    def test_excluded_hook_set_is_exact(self) -> None:
        assert {kind.value for kind in ExcludedHookKind} == {
            "before_runtime_start",
            "after_runtime_start",
            "before_state_commit",
            "after_state_commit",
            "on_event",
            "on_rewind",
        }

    def test_hook_sets_are_disjoint(self) -> None:
        v1 = {kind.value for kind in HookKind}
        deferred = {kind.value for kind in DeferredHookKind}
        excluded = {kind.value for kind in ExcludedHookKind}
        assert v1.isdisjoint(deferred)
        assert v1.isdisjoint(excluded)
        assert deferred.isdisjoint(excluded)


class TestFailurePolicy:
    def test_v1_failure_policies(self) -> None:
        assert {policy.value for policy in HookFailurePolicy} == {
            "fail_open",
            "fail_closed",
        }

    def test_is_v1_failure_policy(self) -> None:
        assert is_v1_failure_policy("fail_open")
        assert is_v1_failure_policy("fail_closed")
        assert not is_v1_failure_policy("retry")
        assert not is_v1_failure_policy("")


class TestAuditEventConstants:
    def test_hook_outcome_event_set(self) -> None:
        assert frozenset({"plugin.hook.blocked", "plugin.hook.failed"}) == HOOK_OUTCOME_AUDIT_EVENTS

    def test_legacy_audit_event_alias_points_to_outcome_events(self) -> None:
        assert HOOK_AUDIT_EVENTS is HOOK_OUTCOME_AUDIT_EVENTS

    def test_blocked_event_constant(self) -> None:
        assert HOOK_BLOCKED_EVENT == "plugin.hook.blocked"
        assert HOOK_BLOCKED_EVENT in HOOK_OUTCOME_AUDIT_EVENTS

    def test_failed_event_constant(self) -> None:
        assert HOOK_FAILED_EVENT == "plugin.hook.failed"
        assert HOOK_FAILED_EVENT in HOOK_OUTCOME_AUDIT_EVENTS


class TestRoutingHelpers:
    def test_v1_hook_kind_router(self) -> None:
        assert is_v1_hook_kind("before_invocation")
        assert is_v1_hook_kind("after_invocation")
        # Terminal observability hooks were promoted into v1 by PR #1131.
        assert is_v1_hook_kind("on_error")
        assert is_v1_hook_kind("on_cancel")
        assert not is_v1_hook_kind("before_tool_call")
        assert not is_v1_hook_kind("on_event")
        assert not is_v1_hook_kind("unknown_hook")

    def test_terminal_deferred_hook_kind_router_is_empty(self) -> None:
        # The deferred-bucket router stays exported for one release but
        # returns ``False`` for every input after the promotion.
        assert not is_terminal_deferred_hook_kind("on_error")
        assert not is_terminal_deferred_hook_kind("on_cancel")
        assert not is_terminal_deferred_hook_kind("before_tool_call")
        assert not is_terminal_deferred_hook_kind("before_invocation")

    def test_terminal_observability_hook_kind_router(self) -> None:
        assert is_terminal_observability_hook_kind("on_error")
        assert is_terminal_observability_hook_kind("on_cancel")
        assert not is_terminal_observability_hook_kind("before_invocation")
        assert not is_terminal_observability_hook_kind("after_invocation")
        assert not is_terminal_observability_hook_kind("before_tool_call")
        assert not is_terminal_observability_hook_kind("unknown_hook")

    def test_deferred_hook_kind_router(self) -> None:
        assert is_deferred_hook_kind("before_tool_call")
        assert is_deferred_hook_kind("after_artifact_write")
        assert not is_deferred_hook_kind("before_invocation")
        assert not is_deferred_hook_kind("on_event")
        # on_error / on_cancel are no longer deferred.
        assert not is_deferred_hook_kind("on_error")
        assert not is_deferred_hook_kind("on_cancel")

    def test_excluded_hook_kind_router(self) -> None:
        assert is_excluded_hook_kind("before_runtime_start")
        assert is_excluded_hook_kind("on_rewind")
        assert not is_excluded_hook_kind("before_invocation")
        assert not is_excluded_hook_kind("before_tool_call")

    def test_unknown_hook_routes_to_none(self) -> None:
        unknown = "made_up_hook_name"
        assert not is_v1_hook_kind(unknown)
        assert not is_deferred_hook_kind(unknown)
        assert not is_excluded_hook_kind(unknown)


def test_hook_runtime_audit_event_contract_includes_invoked_and_completed() -> None:
    assert HOOK_INVOKED_EVENT == "plugin.hook.invoked"
    assert HOOK_COMPLETED_EVENT == "plugin.hook.completed"
    assert frozenset({"plugin.hook.invoked", "plugin.hook.completed"}) == HOOK_RUNTIME_AUDIT_EVENTS
    assert HOOK_RUNTIME_AUDIT_EVENTS <= HOOK_EVENT_TYPES
    assert HOOK_OUTCOME_AUDIT_EVENTS <= HOOK_EVENT_TYPES
