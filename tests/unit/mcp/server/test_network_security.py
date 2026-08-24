"""Tests for the network MCP trust boundary.

Reaching a network MCP bind is enough to run ``ouroboros_execute_seed``, which
hands attacker-chosen YAML to a real agent runtime in an attacker-chosen
directory. These tests pin the three controls that stand between a routable
port and that capability: mandatory credentials, DNS-rebinding protection, and
workspace confinement.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.server.auth import (
    OuroborosTokenVerifier,
    _has_scope_id,
    as_url_authority,
    auth_context_from_access_token,
    build_auth_settings,
    build_transport_security,
    is_loopback_host,
    is_wildcard_host,
    resolve_network_security,
)
from ouroboros.mcp.server.security import (
    AuthConfig,
    Authenticator,
    AuthMethod,
    Permission,
    RateLimitConfig,
    SecurityLayer,
)
from ouroboros.mcp.server.workspace import (
    WorkspacePolicy,
    get_workspace_policy,
    reset_workspace_policy,
    set_workspace_policy,
)

TOKEN = "test-shared-secret"


def _auth_config() -> AuthConfig:
    """Return an API-key config carrying the shared test token."""
    return AuthConfig(
        method=AuthMethod.API_KEY,
        api_keys=frozenset({TOKEN}),
        required=True,
    )


def _mock_sdk_server() -> tuple[MagicMock, MagicMock]:
    """Return a patched SDK server class and the instance it constructs."""
    cls = MagicMock()
    instance = MagicMock()
    instance.tool = MagicMock(return_value=lambda f: f)
    instance.resource = MagicMock(return_value=lambda f: f)
    instance.run_sse_async = AsyncMock()
    instance.run_streamable_http_async = AsyncMock()
    instance.run_stdio_async = AsyncMock()
    cls.return_value = instance
    return cls, instance


class TestHostClassification:
    """Host strings decide whether credentials are mandatory."""

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "localhost",
            "LOCALHOST",
            "localhost.",
            "LOCALHOST.",
            "::1",
            "[::1]",
            "127.0.0.2",
            " 127.0.0.1 ",
        ],
    )
    def test_loopback_hosts_recognized(self, host: str) -> None:
        """Every spelling that only this machine can reach counts as loopback."""
        assert is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "0.0.0.0",
            "::",
            "",
            "192.168.1.10",
            "example.com",
            "build-box.internal",
            "localhost.localdomain",
            "LOCALHOST.LOCALDOMAIN.",
            "127.0.0.1.",
            "127.0.0.2.",
        ],
    )
    def test_routable_hosts_are_not_loopback(self, host: str) -> None:
        """Anything reachable from elsewhere -- including unresolvable names."""
        assert is_loopback_host(host) is False

    @pytest.mark.parametrize(
        "host",
        [
            "local\u115fhost",
            "xn--localhost-fj9a",
            "localhost\u3002",
            "localhost\uff0e",
            "\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54",
            "localhost..",
            "[localhost]",
            "[127.0.0.1]",
        ],
    )
    def test_idna_and_compatibility_lookalikes_are_not_loopback(self, host: str) -> None:
        """Wire normalization must never broaden the credential-free trust set."""
        assert is_loopback_host(host) is False

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", ""])
    def test_wildcard_hosts_recognized(self, host: str) -> None:
        assert is_wildcard_host(host) is True

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "10.0.0.4"])
    def test_concrete_hosts_are_not_wildcard(self, host: str) -> None:
        assert is_wildcard_host(host) is False


class TestServeRefusesUnauthenticatedExposure:
    """The gate that closes the reported trust-boundary hole."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", ["sse", "streamable-http"])
    @pytest.mark.parametrize(
        "host", ["0.0.0.0", "192.168.1.10", "example.com", "localhost.localdomain"]
    )
    async def test_non_loopback_without_auth_is_refused(self, transport: str, host: str) -> None:
        """A routable bind with no credentials never reaches the SDK."""
        adapter = MCPServerAdapter()

        cls, _ = _mock_sdk_server()
        with (
            patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls),
            pytest.raises(ValueError, match="Refusing to serve"),
        ):
            await adapter.serve(transport=transport, host=host, port=9000)

        cls.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", ["local\u115fhost", "xn--localhost-fj9a"])
    async def test_resolver_lookalike_without_auth_is_refused(self, host: str) -> None:
        """A UTS-46 or resolver alias cannot bypass the adapter auth gate."""
        adapter = MCPServerAdapter()

        cls, _ = _mock_sdk_server()
        with (
            patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls),
            pytest.raises(ValueError, match="Refusing to serve"),
        ):
            await adapter.serve(transport="streamable-http", host=host, port=9000)

        cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_authenticated_compatibility_hostname_uses_exact_wire_host(self) -> None:
        """Authentication stays required while Host policy follows HTTPX UTS-46."""
        adapter = MCPServerAdapter(auth_config=_auth_config())

        cls, instance = _mock_sdk_server()
        with patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls):
            await adapter.serve(
                transport="streamable-http",
                host="local\u115fhost",
                port=9000,
            )

        assert isinstance(cls.call_args.kwargs["token_verifier"], OuroborosTokenVerifier)
        assert cls.call_args.kwargs["auth"] is not None
        settings = instance.run_streamable_http_async.await_args.kwargs["transport_security"]
        assert settings.allowed_hosts == ["localhost:9000"]
        assert settings.allowed_origins == []

    @pytest.mark.asyncio
    async def test_refusal_names_the_capability_at_risk(self) -> None:
        """The error explains why, so an operator does not just add --host back."""
        adapter = MCPServerAdapter()

        with pytest.raises(ValueError, match="execute seeds through the local agent runtime"):
            await adapter.serve(transport="streamable-http", host="0.0.0.0", port=9000)

    @pytest.mark.asyncio
    async def test_loopback_without_auth_still_serves(self) -> None:
        """Local stdio-equivalent use is unchanged: no credential required."""
        adapter = MCPServerAdapter()

        cls, instance = _mock_sdk_server()
        with patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls):
            await adapter.serve(transport="streamable-http", host="127.0.0.1", port=9000)

        assert cls.call_args.kwargs["token_verifier"] is None
        assert cls.call_args.kwargs["auth"] is None
        instance.run_streamable_http_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_loopback_with_auth_serves_with_token_verifier(self) -> None:
        """With credentials configured the remote bind is allowed and protected."""
        adapter = MCPServerAdapter(auth_config=_auth_config())

        cls, instance = _mock_sdk_server()
        with patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls):
            await adapter.serve(
                transport="streamable-http",
                host="0.0.0.0",
                port=9000,
                allowed_hosts=("ouroboros.internal:9000",),
            )

        assert isinstance(cls.call_args.kwargs["token_verifier"], OuroborosTokenVerifier)
        assert cls.call_args.kwargs["auth"] is not None

        settings = instance.run_streamable_http_async.await_args.kwargs["transport_security"]
        assert settings.enable_dns_rebinding_protection is True
        assert settings.allowed_hosts == ["ouroboros.internal:9000"]
        # No Origin is allowlisted, so browser-originated requests are rejected.
        assert settings.allowed_origins == []

    @pytest.mark.asyncio
    async def test_wildcard_bind_requires_an_explicit_host_allowlist(self) -> None:
        """A 0.0.0.0 bind has no inferable Host value to allow."""
        adapter = MCPServerAdapter(auth_config=_auth_config())

        with pytest.raises(ValueError, match="Cannot infer a Host allowlist"):
            await adapter.serve(transport="streamable-http", host="0.0.0.0", port=9000)

    @pytest.mark.asyncio
    async def test_loopback_spelling_the_sdk_ignores_still_gets_protection(self) -> None:
        """The SDK auto-protects three literals; other loopback names need ours."""
        adapter = MCPServerAdapter()

        cls, instance = _mock_sdk_server()
        with patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls):
            await adapter.serve(transport="sse", host="127.0.0.2", port=9000)

        settings = instance.run_sse_async.await_args.kwargs["transport_security"]
        assert settings is not None
        assert settings.enable_dns_rebinding_protection is True
        assert settings.allowed_hosts == ["127.0.0.2:9000"]

    @pytest.mark.asyncio
    async def test_stdio_ignores_network_hardening(self) -> None:
        """stdio has no HTTP edge, so none of these settings apply to it."""
        adapter = MCPServerAdapter()

        cls, instance = _mock_sdk_server()
        with patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls):
            await adapter.serve(transport="stdio")

        instance.run_stdio_async.assert_awaited_once()
        assert cls.call_args.kwargs["token_verifier"] is None

    @pytest.mark.asyncio
    async def test_rate_limiting_is_allowed_once_auth_supplies_identity(self) -> None:
        """Rate limiting was blanket-refused before; auth makes it workable."""
        adapter = MCPServerAdapter(
            auth_config=_auth_config(),
            rate_limit_config=RateLimitConfig(enabled=True, requests_per_minute=10),
        )

        cls, _ = _mock_sdk_server()
        with patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls):
            await adapter.serve(
                transport="streamable-http",
                host="10.0.0.5",
                port=9000,
            )

        cls.assert_called_once()

    @pytest.mark.asyncio
    async def test_scoped_loopback_with_explicit_token_adapter_rejects(self) -> None:
        """Adapter.serve() rejects scoped loopback when auth_config is explicit.

        Regression test: previously this reached build_auth_settings and crashed
        inside Pydantic URL validation with an opaque error.
        """
        adapter = MCPServerAdapter(auth_config=_auth_config())

        cls, _ = _mock_sdk_server()
        with (
            patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls),
            pytest.raises(
                ValueError,
                match="Cannot serve on scoped IPv6 address.*with authentication enabled",
            ),
        ):
            await adapter.serve(
                transport="streamable-http",
                host="::1%lo",
                port=8080,
            )

        cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_scoped_loopback_with_inherited_token_adapter_rejects(self) -> None:
        """Adapter.serve() rejects scoped loopback with an inherited bearer token.

        Regression test: tokens can be inherited from env/config rather than
        passed explicitly via --auth-token. The adapter must still reject.
        """
        inherited_auth = AuthConfig(
            method=AuthMethod.BEARER_TOKEN,
            api_keys=frozenset({"env-inherited-secret"}),
            required=True,
        )
        adapter = MCPServerAdapter(auth_config=inherited_auth)

        cls, _ = _mock_sdk_server()
        with (
            patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls),
            pytest.raises(
                ValueError,
                match="Cannot serve on scoped IPv6 address.*with authentication enabled",
            ),
        ):
            await adapter.serve(
                transport="sse",
                host="[::1%lo0]",
                port=9000,
            )

        cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_scoped_loopback_without_auth_adapter_serves(self) -> None:
        """Adapter.serve() still allows scoped loopback when credential-free.

        Unauthenticated scoped loopback should work fine because no
        AuthSettings URL construction is needed.
        """
        adapter = MCPServerAdapter()

        cls, instance = _mock_sdk_server()
        with patch("ouroboros.mcp.server.adapter._OuroborosSDKServer", cls):
            await adapter.serve(
                transport="streamable-http",
                host="::1%lo",
                port=8080,
            )

        cls.assert_called_once()
        assert cls.call_args.kwargs["token_verifier"] is None
        assert cls.call_args.kwargs["auth"] is None


class TestResolveNetworkSecurity:
    """The policy entry point, exercised without standing a server up."""

    def test_stdio_resolves_to_no_wiring(self) -> None:
        wiring = resolve_network_security(
            transport="stdio",
            host="localhost",
            port=8080,
            security=SecurityLayer(),
        )

        assert wiring.token_verifier is None
        assert wiring.auth_settings is None
        assert wiring.transport_security is None

    def test_routable_bind_without_credentials_raises(self) -> None:
        with pytest.raises(ValueError, match="Refusing to serve"):
            resolve_network_security(
                transport="sse",
                host="10.0.0.5",
                port=8080,
                security=SecurityLayer(),
            )

    def test_routable_bind_with_credentials_returns_full_wiring(self) -> None:
        wiring = resolve_network_security(
            transport="streamable-http",
            host="10.0.0.5",
            port=8080,
            security=SecurityLayer(auth_config=_auth_config()),
        )

        assert isinstance(wiring.token_verifier, OuroborosTokenVerifier)
        assert wiring.auth_settings is not None
        assert wiring.transport_security.enable_dns_rebinding_protection is True

    def test_loopback_bind_needs_no_credentials(self) -> None:
        wiring = resolve_network_security(
            transport="streamable-http",
            host="127.0.0.1",
            port=8080,
            security=SecurityLayer(),
        )

        assert wiring.token_verifier is None
        assert wiring.transport_security is not None
        assert wiring.transport_security.allowed_hosts == [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]
        assert wiring.transport_security.allowed_origins == []

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
    def test_explicit_host_override_replaces_sdk_loopback_defaults(self, host: str) -> None:
        """Documented Host overrides must reach even SDK-autoprotected binds."""
        wiring = resolve_network_security(
            transport="streamable-http",
            host=host,
            port=8080,
            security=SecurityLayer(),
            allowed_hosts=("proxy.internal:8443",),
        )

        assert wiring.transport_security is not None
        assert wiring.transport_security.allowed_hosts == ["proxy.internal:8443"]
        assert wiring.transport_security.allowed_origins == []

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
    def test_explicit_origin_override_replaces_sdk_loopback_defaults(self, host: str) -> None:
        """A browser Origin override must not be discarded on loopback."""
        wiring = resolve_network_security(
            transport="sse",
            host=host,
            port=8080,
            security=SecurityLayer(),
            allowed_origins=("https://app.example",),
        )

        assert wiring.transport_security is not None
        assert wiring.transport_security.allowed_origins == ["https://app.example"]

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("LOCALHOST", ["127.0.0.1:*", "localhost:*", "[::1]:*"]),
            ("LOCALHOST.", ["localhost.:8080"]),
        ],
    )
    def test_nonliteral_loopback_spelling_uses_normalized_explicit_settings(
        self, host: str, expected: list[str]
    ) -> None:
        """Safe loopback aliases and their wire Host spelling must agree."""
        wiring = resolve_network_security(
            transport="streamable-http",
            host=host,
            port=8080,
            security=SecurityLayer(),
        )

        assert wiring.transport_security is not None
        assert wiring.transport_security.allowed_hosts == expected


class TestIPv6Binds:
    """A bind address and its URL spelling differ for IPv6.

    Sockets take the bare literal and reject brackets; URLs and ``Host`` values
    require brackets or the trailing ``:port`` merges into the address. Getting
    this backwards made authenticated IPv6 serving impossible to start.
    """

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("::1", "[::1]"),
            ("2001:db8::1", "[2001:db8::1]"),
            ("[::1]", "[::1]"),
            ("[2001:0DB8:0:0:0:0:0:1]", "[2001:db8::1]"),
            ("127.0.0.1", "127.0.0.1"),
            ("example.com", "example.com"),
            ("EXAMPLE.COM.", "example.com."),
            ("BÜCHER.Example.", "xn--bcher-kva.example."),
            ("faß.de", "xn--fa-hia.de"),
        ],
    )
    def test_url_authority_normalizes_wire_spelling(self, host: str, expected: str) -> None:
        assert as_url_authority(host) == expected

    @pytest.mark.parametrize("host", ["::1", "2001:db8::1"])
    def test_auth_settings_build_for_ipv6(self, host: str) -> None:
        """Building these raised a Pydantic ValidationError before bracketing."""
        settings = build_auth_settings(host=host, port=8080)

        assert str(settings.issuer_url).startswith(f"http://[{host.strip('[]')}]:8080")

    @pytest.mark.parametrize(
        "host",
        ["Example.COM.", "BÜCHER.Example.", "faß.de"],
    )
    def test_auth_settings_match_httpx2_advertised_authority(self, host: str) -> None:
        """Advertised URLs use the same authority MCP 2 clients serialize."""
        import httpx2

        wire_request = httpx2.Request("GET", f"http://{host}:8080/mcp")
        settings = build_auth_settings(host=host, port=8080)

        assert str(settings.issuer_url).rstrip("/") == f"http://{wire_request.headers['host']}"
        assert str(settings.resource_server_url).rstrip("/") == (
            f"http://{wire_request.headers['host']}"
        )

    def test_inferred_host_allowlist_is_unambiguous_for_ipv6(self) -> None:
        settings = build_transport_security(
            host="2001:db8::1",
            port=8080,
            allowed_hosts=(),
            allowed_origins=(),
        )

        assert settings.allowed_hosts == ["[2001:db8::1]:8080"]

    def test_authenticated_ipv6_loopback_bind_resolves(self) -> None:
        """An inherited token on a valid `--host ::1` must not break startup."""
        wiring = resolve_network_security(
            transport="streamable-http",
            host="::1",
            port=8080,
            security=SecurityLayer(auth_config=_auth_config()),
        )

        assert isinstance(wiring.token_verifier, OuroborosTokenVerifier)
        assert wiring.transport_security.allowed_hosts == [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]
        assert wiring.transport_security.allowed_origins == []

    def test_authenticated_ipv6_routable_bind_resolves(self) -> None:
        wiring = resolve_network_security(
            transport="streamable-http",
            host="2001:db8::1",
            port=8080,
            security=SecurityLayer(auth_config=_auth_config()),
        )

        assert isinstance(wiring.token_verifier, OuroborosTokenVerifier)
        assert wiring.transport_security.allowed_hosts == ["[2001:db8::1]:8080"]

    def test_ipv6_loopback_without_auth_is_still_credential_free(self) -> None:
        wiring = resolve_network_security(
            transport="sse",
            host="::1",
            port=8080,
            security=SecurityLayer(),
        )

        assert wiring.token_verifier is None

    def test_routable_ipv6_without_auth_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Refusing to serve"):
            resolve_network_security(
                transport="sse",
                host="2001:db8::1",
                port=8080,
                security=SecurityLayer(),
            )

    # --- Scoped IPv6 (zone ID) regression tests ---

    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("fe80::1%lo", True),
            ("fe80::1%eth0", True),
            ("[fe80::1%lo]", True),
            ("::1%lo", True),
            ("fe80::1", False),
            ("::1", False),
            ("2001:db8::1", False),
            ("127.0.0.1", False),
            ("localhost", False),
        ],
    )
    def test_has_scope_id_detection(self, host: str, expected: bool) -> None:
        """Scoped IPv6 addresses are correctly identified by zone ID presence."""
        assert _has_scope_id(host) == expected

    def test_scoped_ipv6_link_local_authenticated_bind_is_rejected(self) -> None:
        """A link-local scoped IPv6 bind requiring auth fails early.

        The MCP SDK's Pydantic URL parser rejects zone identifiers in both raw
        and percent-encoded forms. Rather than crashing inside AuthSettings
        construction, ``resolve_network_security`` rejects scoped IPv6 network
        binds without advertising an allowlist override that cannot bypass the
        unsupported bind-address gate.
        """
        with pytest.raises(ValueError) as exc_info:
            resolve_network_security(
                transport="streamable-http",
                host="fe80::1%lo",
                port=8080,
                security=SecurityLayer(auth_config=_auth_config()),
                allowed_hosts=("node.example:8080",),
            )

        message = str(exc_info.value)
        assert "Cannot serve on scoped IPv6 address" in message
        assert "Use the address without a zone ID" in message
        assert "bind to a non-link-local address" in message
        assert "--allowed-host" not in message

    def test_scoped_ipv6_bracketed_link_local_bind_is_rejected(self) -> None:
        """Bracketed scoped IPv6 bind also fails early."""
        with pytest.raises(ValueError, match="Cannot serve on scoped IPv6 address"):
            resolve_network_security(
                transport="sse",
                host="[fe80::1%eth0]",
                port=8080,
                security=SecurityLayer(auth_config=_auth_config()),
            )

    def test_scoped_ipv6_loopback_bind_remains_credential_free(self) -> None:
        """A scoped loopback like ``::1%lo`` is still loopback — no auth needed.

        Scoped loopback does not hit the rejection path because
        ``is_loopback_host`` returns True and authentication is not required.
        """
        wiring = resolve_network_security(
            transport="streamable-http",
            host="::1%lo",
            port=8080,
            security=SecurityLayer(),
        )
        # Credential-free loopback still gets transport security
        assert wiring.token_verifier is None
        assert wiring.auth_settings is None
        assert wiring.transport_security is not None

    def test_scoped_ipv6_url_authority_includes_zone_id(self) -> None:
        """as_url_authority preserves the zone ID — it's callers that must gate."""
        # The zone ID passes through as_url_authority verbatim, which is correct
        # for the text serialization. The SDK URL parser is what rejects it.
        authority = as_url_authority("fe80::1%lo")
        assert authority == "[fe80::1%lo]"

    def test_build_auth_settings_rejects_scoped_ipv6(self) -> None:
        """Confirm the SDK AuthSettings actually rejects a scoped IPv6 URL.

        This documents the SDK limitation that motivates the early rejection
        in ``resolve_network_security``.
        """
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="invalid IPv6 address"):
            build_auth_settings(host="fe80::1%lo", port=8080)

    def test_scoped_loopback_with_explicit_auth_token_is_rejected(self) -> None:
        """A scoped loopback (::1%lo) with an explicit --auth-token must fail.

        Even though ::1%lo is loopback (safe from a network exposure
        standpoint), the zone ID cannot appear in AuthSettings issuer URLs.
        The rejection must be early and actionable, not a Pydantic crash.
        """
        with pytest.raises(
            ValueError,
            match="Cannot serve on scoped IPv6 address.*with authentication enabled",
        ):
            resolve_network_security(
                transport="streamable-http",
                host="::1%lo",
                port=8080,
                security=SecurityLayer(auth_config=_auth_config()),
            )

    def test_scoped_loopback_with_inherited_bearer_token_is_rejected(self) -> None:
        """A scoped loopback with an inherited bearer token must also fail.

        Tokens can be inherited from environment config (e.g. OUROBOROS_AUTH_TOKEN)
        rather than passed explicitly. The rejection must apply regardless of
        how the auth config reached the security layer.
        """
        inherited_auth = AuthConfig(
            method=AuthMethod.BEARER_TOKEN,
            api_keys=frozenset({"inherited-secret"}),
            required=True,
        )
        with pytest.raises(
            ValueError,
            match="Cannot serve on scoped IPv6 address.*with authentication enabled",
        ):
            resolve_network_security(
                transport="sse",
                host="[::1%lo0]",
                port=9000,
                security=SecurityLayer(auth_config=inherited_auth),
            )

    def test_scoped_loopback_without_auth_still_works(self) -> None:
        """Unauthenticated scoped loopback must remain usable (no AuthSettings).

        This confirms the fix does not regress the credential-free case.
        """
        wiring = resolve_network_security(
            transport="streamable-http",
            host="::1%lo",
            port=8080,
            security=SecurityLayer(),
        )
        assert wiring.token_verifier is None
        assert wiring.auth_settings is None
        assert wiring.transport_security is not None

    @pytest.mark.parametrize("host", ["::1%lo", "::1%lo0", "[::1%eth0]"])
    def test_scoped_loopback_variants_all_rejected_with_auth(self, host: str) -> None:
        """All scoped loopback spellings are rejected when auth is enabled."""
        with pytest.raises(
            ValueError,
            match="Cannot serve on scoped IPv6 address.*with authentication enabled",
        ):
            resolve_network_security(
                transport="streamable-http",
                host=host,
                port=8080,
                security=SecurityLayer(auth_config=_auth_config()),
            )


class TestTransportSecurityBuilder:
    """Direct coverage of the DNS-rebinding settings builder."""

    def test_wildcard_without_allowlist_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot infer a Host allowlist"):
            build_transport_security(
                host="0.0.0.0",
                port=8080,
                allowed_hosts=(),
                allowed_origins=(),
            )

    def test_operator_allowlists_are_passed_through(self) -> None:
        settings = build_transport_security(
            host="0.0.0.0",
            port=8080,
            allowed_hosts=("a.example:8080", "b.example:*"),
            allowed_origins=("https://a.example",),
        )

        assert settings.allowed_hosts == ["a.example:8080", "b.example:*"]
        assert settings.allowed_origins == ["https://a.example"]

    @pytest.mark.parametrize("host", ["127.0.0.2", "LOCALHOST.", "0:0:0:0:0:0:0:1"])
    def test_nondefault_ephemeral_port_requires_an_explicit_allowlist(self, host: str) -> None:
        """A concrete inferred policy cannot know port zero's eventual wire value."""
        with pytest.raises(ValueError, match="exact Host allowlist for ephemeral port"):
            build_transport_security(
                host=host,
                port=0,
                allowed_hosts=(),
                allowed_origins=(),
            )

    def test_explicit_allowlist_preserves_operator_ephemeral_port_policy(self) -> None:
        """Operators may still opt into the SDK wildcard for an ephemeral bind."""
        settings = build_transport_security(
            host="127.0.0.2",
            port=0,
            allowed_hosts=("127.0.0.2:*",),
            allowed_origins=(),
        )

        assert settings.allowed_hosts == ["127.0.0.2:*"]

    @pytest.mark.parametrize(
        ("host", "url"),
        [
            ("LOCALHOST", "http://LOCALHOST:8080/mcp"),
            ("LOCALHOST.", "http://LOCALHOST.:8080/mcp"),
            ("Example.COM.", "http://Example.COM.:8080/mcp"),
            ("BÜCHER.Example.", "http://BÜCHER.Example.:8080/mcp"),
            ("faß.de", "http://faß.de:8080/mcp"),
            ("127.0.0.1", "http://127.0.0.1:8080/mcp"),
            (
                "2001:0DB8:0:0:0:0:0:1",
                "http://[2001:0DB8:0:0:0:0:0:1]:8080/mcp",
            ),
            (
                "0:0:0:0:0:0:0:1",
                "http://[0:0:0:0:0:0:0:1]:8080/mcp",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_inferred_allowlist_accepts_real_httpx2_wire_host(
        self, host: str, url: str
    ) -> None:
        """Inference accepts the Host bytes emitted by MCP 2's HTTP runtime."""
        import httpx2
        from mcp.server.transport_security import TransportSecurityMiddleware
        from starlette.requests import Request

        settings = build_transport_security(
            host=host,
            port=8080,
            allowed_hosts=(),
            allowed_origins=(),
        )
        middleware = TransportSecurityMiddleware(settings)
        wire_request = httpx2.Request("GET", url)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "headers": [(name.lower(), value) for name, value in wire_request.headers.raw],
            }
        )

        assert middleware._validate_host(wire_request.headers["host"]) is True
        assert await middleware.validate_request(request) is None

    @pytest.mark.asyncio
    async def test_compatibility_mapped_host_uses_exact_wire_port(self) -> None:
        """A deceptive bind gets UTS-46 wire policy, never loopback wildcards."""
        import httpx2
        from mcp.server.transport_security import TransportSecurityMiddleware
        from starlette.requests import Request

        host = "local\u115fhost"
        settings = build_transport_security(
            host=host,
            port=8080,
            allowed_hosts=(),
            allowed_origins=(),
        )
        middleware = TransportSecurityMiddleware(settings)

        assert as_url_authority(host) == "localhost"
        assert settings.allowed_hosts == ["localhost:8080"]

        wire_request = httpx2.Request("GET", "http://localhost:8080/mcp")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "headers": [(name.lower(), value) for name, value in wire_request.headers.raw],
            }
        )

        assert await middleware.validate_request(request) is None
        assert middleware._validate_host("localhost:8081") is False

    @pytest.mark.parametrize(
        "authority",
        ["[2001:db8::1]", "[2001:0DB8:0:0:0:0:0:1]"],
    )
    @pytest.mark.parametrize(
        ("port_suffix", "expected_status"),
        [
            (":8080", 204),
            (":8081", 421),
            (":", 421),
            (":evil", 421),
            (":8080.evil", 421),
            (":8080:evil", 421),
        ],
    )
    @pytest.mark.asyncio
    async def test_inferred_ipv6_allowlist_enforces_exact_well_formed_port(
        self,
        authority: str,
        port_suffix: str,
        expected_status: int,
    ) -> None:
        """Canonical and input spellings admit only the configured wire authority."""
        from mcp.server.transport_security import TransportSecurityMiddleware
        from starlette.requests import Request

        settings = build_transport_security(
            host="2001:0DB8:0:0:0:0:0:1",
            port=8080,
            allowed_hosts=(),
            allowed_origins=(),
        )
        middleware = TransportSecurityMiddleware(settings)
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "headers": [(b"host", f"{authority}{port_suffix}".encode())],
            }
        )

        response = await middleware.validate_request(request)
        status = 204 if response is None else response.status_code
        assert status == expected_status

    def test_inferred_ipv6_allowlist_rejects_textual_lookalikes(self) -> None:
        """Adding an operator spelling must not turn it into a prefix allowlist."""
        from mcp.server.transport_security import TransportSecurityMiddleware

        settings = build_transport_security(
            host="2001:0DB8:0:0:0:0:0:1",
            port=8080,
            allowed_hosts=(),
            allowed_origins=(),
        )
        middleware = TransportSecurityMiddleware(settings)

        assert settings.allowed_hosts == [
            "[2001:db8::1]:8080",
            "[2001:0DB8:0:0:0:0:0:1]:8080",
        ]
        assert middleware._validate_host("[2001:db8::2]:8080") is False
        assert middleware._validate_host("[2001:0DB8:0:0:0:0:0:1].evil:8080") is False

    @pytest.mark.asyncio
    async def test_explicit_loopback_origin_is_honored_at_wire_boundary(self) -> None:
        """Explicit loopback Origin policy replaces the SDK's fixed defaults."""
        from mcp.server.transport_security import TransportSecurityMiddleware
        from starlette.requests import Request

        wiring = resolve_network_security(
            transport="streamable-http",
            host="localhost",
            port=8080,
            security=SecurityLayer(),
            allowed_origins=("https://app.example",),
        )
        middleware = TransportSecurityMiddleware(wiring.transport_security)

        allowed = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "headers": [
                    (b"host", b"localhost:8080"),
                    (b"origin", b"https://app.example"),
                ],
            }
        )
        refused = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "headers": [
                    (b"host", b"localhost:8080"),
                    (b"origin", b"http://localhost:3000"),
                ],
            }
        )

        assert await middleware.validate_request(allowed) is None
        response = await middleware.validate_request(refused)
        assert response is not None and response.status_code == 403

    @pytest.mark.parametrize(
        "origin",
        ["http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"],
    )
    @pytest.mark.asyncio
    async def test_empty_loopback_origins_reject_every_browser_origin(self, origin: str) -> None:
        """The documented empty-origin default must override SDK browser defaults."""
        from mcp.server.transport_security import TransportSecurityMiddleware
        from starlette.requests import Request

        wiring = resolve_network_security(
            transport="streamable-http",
            host="localhost",
            port=8080,
            security=SecurityLayer(),
        )
        middleware = TransportSecurityMiddleware(wiring.transport_security)

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/mcp",
                "headers": [
                    (b"host", b"localhost:8080"),
                    (b"origin", origin.encode()),
                ],
            }
        )

        response = await middleware.validate_request(request)
        assert response is not None and response.status_code == 403


class TestTokenVerifier:
    """The SDK's credential check delegates to Ouroboros's authenticator."""

    @pytest.mark.asyncio
    async def test_valid_token_is_accepted(self) -> None:
        verifier = OuroborosTokenVerifier(
            authenticator=Authenticator(_auth_config()),
            method=AuthMethod.API_KEY,
        )

        access_token = await verifier.verify_token(TOKEN)

        assert access_token is not None
        assert access_token.client_id
        # The raw secret is never used as the identity.
        assert access_token.client_id != TOKEN

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["", "wrong", TOKEN + "x", TOKEN.upper()])
    async def test_invalid_token_is_rejected(self, bad: str) -> None:
        verifier = OuroborosTokenVerifier(
            authenticator=Authenticator(_auth_config()),
            method=AuthMethod.API_KEY,
        )

        assert await verifier.verify_token(bad) is None

    @pytest.mark.asyncio
    async def test_same_token_yields_a_stable_client_id(self) -> None:
        """Rate limiting buckets by this value, so it must not drift per call."""
        verifier = OuroborosTokenVerifier(
            authenticator=Authenticator(_auth_config()),
            method=AuthMethod.API_KEY,
        )

        first = await verifier.verify_token(TOKEN)
        second = await verifier.verify_token(TOKEN)

        assert first.client_id == second.client_id


class TestAuthContextBridge:
    """The SDK's decision survives the crossing into SecurityLayer."""

    def test_none_access_token_yields_no_context(self) -> None:
        assert auth_context_from_access_token(None) is None

    @pytest.mark.asyncio
    async def test_round_trip_preserves_identity_and_permissions(self) -> None:
        verifier = OuroborosTokenVerifier(
            authenticator=Authenticator(_auth_config()),
            method=AuthMethod.API_KEY,
        )
        access_token = await verifier.verify_token(TOKEN)

        context = auth_context_from_access_token(access_token)

        assert context is not None
        assert context.authenticated is True
        assert context.client_id == access_token.client_id
        assert Permission.EXECUTE in context.permissions

    @pytest.mark.asyncio
    async def test_pre_authenticated_context_authorizes_a_tool_call(self) -> None:
        """call_tool must accept the transport's decision, not re-check a
        credential that no longer exists at this layer."""
        from ouroboros.mcp.server.security import SecurityLayer

        layer = SecurityLayer(auth_config=_auth_config())
        verifier = OuroborosTokenVerifier(
            authenticator=layer.authenticator,
            method=AuthMethod.API_KEY,
        )
        context = auth_context_from_access_token(await verifier.verify_token(TOKEN))

        result = await layer.check_request(
            "ouroboros_execute_seed",
            {"seed_content": "goal: x"},
            credentials=None,
            pre_authenticated=context,
        )

        assert result.is_ok

    @pytest.mark.asyncio
    async def test_missing_context_with_auth_configured_is_rejected(self) -> None:
        """Without the transport's decision and without credentials, no entry."""
        from ouroboros.mcp.server.security import SecurityLayer

        layer = SecurityLayer(auth_config=_auth_config())

        result = await layer.check_request(
            "ouroboros_execute_seed",
            {"seed_content": "goal: x"},
            credentials=None,
        )

        assert result.is_err


class TestWorkspacePolicy:
    """Confinement for the directory an agent runtime is pointed at."""

    def teardown_method(self) -> None:
        reset_workspace_policy()

    def test_default_policy_permits_everything(self) -> None:
        assert get_workspace_policy().permits(Path("/tmp")) is True
        assert get_workspace_policy().restricted is False

    def test_paths_inside_a_root_are_permitted(self, tmp_path: Path) -> None:
        policy = WorkspacePolicy.from_paths([tmp_path])

        assert policy.permits(tmp_path) is True
        assert policy.permits(tmp_path / "nested" / "deeper") is True

    def test_paths_outside_every_root_are_refused(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        policy = WorkspacePolicy.from_paths([allowed])

        assert policy.permits(tmp_path / "elsewhere") is False
        assert policy.permits(Path("/etc")) is False

    def test_sibling_prefix_is_not_treated_as_contained(self, tmp_path: Path) -> None:
        """``/srv/work`` must not admit ``/srv/work-evil``."""
        allowed = tmp_path / "work"
        allowed.mkdir()
        sibling = tmp_path / "work-evil"
        sibling.mkdir()
        policy = WorkspacePolicy.from_paths([allowed])

        assert policy.permits(sibling) is False

    def test_symlink_out_of_the_root_is_refused(self, tmp_path: Path) -> None:
        """Resolution happens on both sides, so a link cannot smuggle a path out."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = allowed / "escape"
        link.symlink_to(outside)
        policy = WorkspacePolicy.from_paths([allowed])

        assert policy.permits(link) is False

    def test_policy_is_readable_after_being_set(self, tmp_path: Path) -> None:
        set_workspace_policy(WorkspacePolicy.from_paths([tmp_path]))

        assert get_workspace_policy().restricted is True
        assert str(tmp_path.resolve()) in get_workspace_policy().describe()


class TestExecuteSeedCwdConfinement:
    """The policy is enforced where cwd becomes an agent's working tree."""

    def teardown_method(self) -> None:
        reset_workspace_policy()

    def test_cwd_outside_the_workspace_is_rejected(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        set_workspace_policy(WorkspacePolicy.from_paths([allowed]))

        result = ExecuteSeedHandler._resolve_dispatch_cwd_result(
            str(outside),
            tool_name="ouroboros_execute_seed",
        )

        assert result.is_err
        assert "outside the permitted workspace" in str(result.error)

    def test_cwd_inside_the_workspace_is_accepted(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler

        allowed = tmp_path / "allowed"
        nested = allowed / "project"
        nested.mkdir(parents=True)
        set_workspace_policy(WorkspacePolicy.from_paths([allowed]))

        result = ExecuteSeedHandler._resolve_dispatch_cwd_result(
            str(nested),
            tool_name="ouroboros_execute_seed",
        )

        assert result.is_ok
        assert result.value == nested.resolve()

    def test_confinement_is_checked_before_existence(self, tmp_path: Path) -> None:
        """A refused path must not reveal whether it exists on this machine."""
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        set_workspace_policy(WorkspacePolicy.from_paths([allowed]))

        result = ExecuteSeedHandler._resolve_dispatch_cwd_result(
            str(tmp_path / "does-not-exist"),
            tool_name="ouroboros_execute_seed",
        )

        assert result.is_err
        assert "outside the permitted workspace" in str(result.error)
        assert "does not exist" not in str(result.error)

    def test_unrestricted_policy_keeps_local_behaviour(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler

        result = ExecuteSeedHandler._resolve_dispatch_cwd_result(
            str(tmp_path),
            tool_name="ouroboros_execute_seed",
        )

        assert result.is_ok


class TestCliExposureGate:
    """The CLI refuses the same binds, with guidance instead of a traceback."""

    def test_remote_bind_without_token_exits(self) -> None:
        import typer

        from ouroboros.cli.commands.mcp import _resolve_network_security

        with pytest.raises(typer.Exit):
            _resolve_network_security(
                transport="streamable-http",
                host="0.0.0.0",
                port=8080,
                auth_token="",
                allow_remote=True,
                allowed_hosts=("a.example:8080",),
            )

    @pytest.mark.parametrize("host", ["local\u115fhost", "xn--localhost-fj9a"])
    def test_resolver_lookalike_without_token_exits(self, host: str) -> None:
        """The CLI shares the adapter's strict, non-UTS-46 trust classifier."""
        import typer

        from ouroboros.cli.commands.mcp import _resolve_network_security

        with pytest.raises(typer.Exit):
            _resolve_network_security(
                transport="streamable-http",
                host=host,
                port=8080,
                auth_token="",
                allow_remote=True,
                allowed_hosts=(),
            )

    def test_remote_bind_without_acknowledgement_exits(self) -> None:
        import typer

        from ouroboros.cli.commands.mcp import _resolve_network_security

        with pytest.raises(typer.Exit):
            _resolve_network_security(
                transport="streamable-http",
                host="10.0.0.5",
                port=8080,
                auth_token=TOKEN,
                allow_remote=False,
                allowed_hosts=(),
            )

    def test_wildcard_bind_without_allowed_host_exits(self) -> None:
        import typer

        from ouroboros.cli.commands.mcp import _resolve_network_security

        with pytest.raises(typer.Exit):
            _resolve_network_security(
                transport="streamable-http",
                host="0.0.0.0",
                port=8080,
                auth_token=TOKEN,
                allow_remote=True,
                allowed_hosts=(),
            )

    def test_fully_specified_remote_bind_is_allowed(self) -> None:
        from ouroboros.cli.commands.mcp import _resolve_network_security

        token = _resolve_network_security(
            transport="streamable-http",
            host="0.0.0.0",
            port=8080,
            auth_token=TOKEN,
            allow_remote=True,
            allowed_hosts=("ouroboros.internal:8080",),
        )

        assert token == TOKEN

    def test_loopback_needs_nothing(self) -> None:
        from ouroboros.cli.commands.mcp import _resolve_network_security

        assert (
            _resolve_network_security(
                transport="streamable-http",
                host="localhost",
                port=8080,
                auth_token="",
                allow_remote=False,
                allowed_hosts=(),
            )
            == ""
        )

    def test_stdio_drops_an_inherited_token(self) -> None:
        """A globally exported OUROBOROS_MCP_AUTH_TOKEN must not break stdio."""
        from ouroboros.cli.commands.mcp import _resolve_network_security

        assert (
            _resolve_network_security(
                transport="stdio",
                host="localhost",
                port=8080,
                auth_token=TOKEN,
                allow_remote=False,
                allowed_hosts=(),
            )
            == ""
        )
