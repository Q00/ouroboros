"""Unit tests for hook lifecycle permission scope constants.

Fourth slice of #939. v1 baseline scope for observability hooks is
``plugin:lifecycle:read``; ``HOOK_LIFECYCLE_SCOPES`` carries the
frozen membership set and ``is_hook_lifecycle_scope`` is the routing
helper consumed by manifest validators / capability resolvers in a
later slice.

The manifest validator consumes the constants so v0.3 hook declarations
must opt into the lifecycle read boundary before runtime dispatch lands.
"""

from __future__ import annotations

from ouroboros.plugin.hooks import (
    HOOK_LIFECYCLE_READ_SCOPE,
    HOOK_LIFECYCLE_SCOPES,
    is_hook_lifecycle_scope,
)


class TestHookLifecycleScopeConstants:
    def test_read_scope_value(self) -> None:
        assert HOOK_LIFECYCLE_READ_SCOPE == "plugin:lifecycle:read"

    def test_scope_set_is_frozen(self) -> None:
        assert isinstance(HOOK_LIFECYCLE_SCOPES, frozenset)

    def test_scope_set_membership(self) -> None:
        assert frozenset({"plugin:lifecycle:read"}) == HOOK_LIFECYCLE_SCOPES

    def test_read_scope_is_in_set(self) -> None:
        assert HOOK_LIFECYCLE_READ_SCOPE in HOOK_LIFECYCLE_SCOPES


class TestRoutingHelper:
    def test_accepts_canonical_scope(self) -> None:
        assert is_hook_lifecycle_scope("plugin:lifecycle:read")

    def test_rejects_blank(self) -> None:
        assert not is_hook_lifecycle_scope("")

    def test_rejects_unrelated_scope(self) -> None:
        assert not is_hook_lifecycle_scope("github:read")
        assert not is_hook_lifecycle_scope("plugin.tool.intercept")
        assert not is_hook_lifecycle_scope("plugin.lifecycle.read")

    def test_rejects_case_variant(self) -> None:
        # The scope set is exact-match by design; capability resolvers
        # cannot quietly accept stylistic variants.
        assert not is_hook_lifecycle_scope("PLUGIN:LIFECYCLE:READ")
        assert not is_hook_lifecycle_scope("Plugin:Lifecycle:Read")

    def test_rejects_unknown_subscope(self) -> None:
        # A future lifecycle scope must be added explicitly to
        # HOOK_LIFECYCLE_SCOPES; the helper must not silently accept
        # forward-looking values.
        assert not is_hook_lifecycle_scope("plugin:lifecycle:decide")
        assert not is_hook_lifecycle_scope("plugin:lifecycle:write")
