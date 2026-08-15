"""Bearer-token authentication for network MCP transports.

The MCP SDK owns the HTTP boundary, so credentials only ever exist inside its
``TokenVerifier`` protocol -- an Ouroboros tool handler never sees a header.
This module is the bridge across that boundary: it hands the SDK a verifier
backed by Ouroboros's own :class:`~ouroboros.mcp.server.security.Authenticator`,
so a single credential authority serves both the transport gate and the
per-tool :class:`~ouroboros.mcp.server.security.SecurityLayer` checks.

Without this bridge the two halves were disconnected: ``SecurityLayer`` could
be configured with credentials that no network client had any way to present,
which is why network transports used to refuse every auth method outright.

Loopback binds keep the historical credential-free behaviour -- the SDK's own
DNS-rebinding protection already fences them off from other machines. Binding a
network transport to a routable address without credentials is refused by
:func:`resolve_network_security`, which every serve path goes through before
the SDK starts listening.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any

import structlog

from ouroboros.mcp.server.security import (
    AuthContext,
    Authenticator,
    AuthMethod,
    Permission,
    SecurityLayer,
)

log = structlog.get_logger(__name__)

#: Transports that expose an HTTP surface, and so can both carry a credential
#: and be reached by someone other than the process that started the server.
NETWORK_TRANSPORTS = ("sse", "streamable-http")

# Bind addresses for which the SDK builds DNS-rebinding protection on its own
# (``mcp/server/lowlevel/server.py``). It matches these three spellings
# literally, so any other host -- including other loopback spellings -- is left
# bare unless settings are passed explicitly.
_SDK_AUTOPROTECTED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Hostnames that always resolve to this machine. ``""`` is deliberately absent:
# an empty bind host means "every interface" to uvicorn, not loopback.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})

#: Scope minted for every accepted credential. Ouroboros authorizes per tool
#: through ``SecurityLayer``, so the SDK layer only needs one opaque scope to
#: mark a request as carrying valid credentials.
OUROBOROS_SCOPE = "ouroboros:tools"

_PERMISSIONS_CLAIM = "ouroboros_permissions"
_ROLES_CLAIM = "ouroboros_roles"


def is_loopback_host(host: str) -> bool:
    """Return True when ``host`` is only reachable from this machine.

    Args:
        host: The bind address as written on the command line.

    Returns:
        True for loopback literals and loopback hostnames. A name that cannot
        be classified without a DNS lookup (``build-box.internal``) returns
        False: refusing to guess is the safe direction, since the caller uses
        this to decide whether credentials are mandatory.
    """
    candidate = host.strip().strip("[]").lower()
    if candidate in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def is_wildcard_host(host: str) -> bool:
    """Return True when ``host`` binds every interface rather than one address.

    Such a bind is reachable under names this process never sees, so anything
    derived from the bind address itself (an advertised URL, a ``Host``
    allowlist) has to be supplied by the operator instead of inferred.
    """
    return host.strip().strip("[]") in ("", "0.0.0.0", "::")


def as_url_authority(host: str) -> str:
    """Return ``host`` spelled the way a URL or ``Host`` header needs it.

    A bind address and its URL spelling differ for IPv6: the socket layer takes
    the bare literal (``::1``) and rejects the bracketed form, while a URL or
    ``Host`` value needs brackets or the trailing ``:port`` cannot be told apart
    from another hextet. Callers keep the bare value for binding and pass it
    through here for anything address-shaped that carries a port.
    """
    candidate = host.strip()
    if candidate.startswith("["):
        return candidate
    try:
        if ipaddress.ip_address(candidate).version == 6:
            return f"[{candidate}]"
    except ValueError:
        pass
    return candidate


def _credentials_for(method: AuthMethod, token: str) -> dict[str, str] | None:
    """Map a bearer token onto the credential dict its auth method expects."""
    if method == AuthMethod.API_KEY:
        return {"api_key": token}
    if method == AuthMethod.BEARER_TOKEN:
        return {"token": token}
    return None


@dataclass(frozen=True, slots=True)
class OuroborosTokenVerifier:
    """SDK ``TokenVerifier`` that delegates to Ouroboros's authenticator.

    The SDK calls this once per HTTP request, before any Ouroboros handler
    runs. Returning ``None`` makes ``RequireAuthMiddleware`` answer 401, so an
    unauthenticated caller never reaches tool dispatch.
    """

    authenticator: Authenticator
    method: AuthMethod

    async def verify_token(self, token: str) -> Any | None:
        """Verify a bearer token presented over a network transport.

        Args:
            token: The raw credential from the ``Authorization`` header.

        Returns:
            An SDK ``AccessToken`` when the credential is valid, else None.
        """
        access_token_cls = _load_access_token_cls()
        credentials = _credentials_for(self.method, token)
        if credentials is None:
            log.warning("mcp.auth.unsupported_method", auth_method=self.method.value)
            return None

        result = self.authenticator.authenticate(credentials)
        if result.is_err:
            log.warning(
                "mcp.auth.token_rejected",
                auth_method=self.method.value,
                reason=str(result.error),
            )
            return None

        context = result.value
        if not context.authenticated:
            log.warning("mcp.auth.token_unauthenticated", auth_method=self.method.value)
            return None

        # Carry the authorization decision across the SDK boundary so the
        # per-tool check does not have to re-derive it from a token it no
        # longer holds.
        return access_token_cls(
            token=token,
            client_id=context.client_id or "unknown",
            scopes=[OUROBOROS_SCOPE],
            claims={
                _PERMISSIONS_CLAIM: sorted(p.value for p in context.permissions),
                _ROLES_CLAIM: sorted(context.roles),
            },
        )


def auth_context_from_access_token(access_token: Any) -> AuthContext | None:
    """Rebuild an Ouroboros :class:`AuthContext` from an SDK access token.

    The SDK authenticated the request at the HTTP edge; this restores the
    identity and grants that decision produced so ``SecurityLayer`` can
    authorize the specific tool and attribute rate limiting to a client.

    Args:
        access_token: The SDK ``AccessToken`` for the in-flight request, or
            None when the request arrived over a credential-free transport.

    Returns:
        The reconstructed context, or None when there is no access token.
    """
    if access_token is None:
        return None

    claims = getattr(access_token, "claims", None) or {}
    known = {member.value for member in Permission}
    raw_permissions = claims.get(_PERMISSIONS_CLAIM) or []
    permissions = {Permission(value) for value in raw_permissions if value in known}
    roles = frozenset(claims.get(_ROLES_CLAIM) or ())

    return AuthContext(
        authenticated=True,
        client_id=getattr(access_token, "client_id", None),
        permissions=frozenset(permissions),
        roles=roles,
    )


def _load_access_token_cls() -> Any:
    """Import the SDK ``AccessToken`` lazily.

    Kept out of module scope so the core package stays importable when the
    optional ``mcp`` extra is absent.
    """
    from mcp.server.auth.provider import AccessToken

    return AccessToken


def build_auth_settings(*, host: str, port: int) -> Any:
    """Build the SDK ``AuthSettings`` for a token-protected network bind.

    Ouroboros issues its own shared credentials rather than delegating to an
    OAuth authorization server, so both URLs point back at this server. They
    are advertised through the protected-resource metadata endpoint the SDK
    mounts; clients presenting a static token never need to read them.

    Args:
        host: The bind address, used to build the advertised base URL.
        port: The bind port.

    Returns:
        An SDK ``AuthSettings`` instance.
    """
    from mcp.server.auth.settings import AuthSettings

    advertised_host = "127.0.0.1" if is_wildcard_host(host) else as_url_authority(host)
    base_url = f"http://{advertised_host}:{port}"
    return AuthSettings(issuer_url=base_url, resource_server_url=base_url)


def build_transport_security(
    *,
    host: str,
    port: int,
    allowed_hosts: tuple[str, ...],
    allowed_origins: tuple[str, ...],
) -> Any:
    """Build DNS-rebinding protection settings for a network bind.

    The SDK only auto-enables this protection for loopback binds; a routable
    bind is left unprotected unless settings are passed explicitly, which is
    what this produces.

    Args:
        host: The bind address.
        port: The bind port.
        allowed_hosts: Operator-supplied ``Host`` header allowlist. When empty,
            the bind address itself is allowed on any port -- which only means
            anything for a concrete address, hence the wildcard guard below.
        allowed_origins: Operator-supplied ``Origin`` header allowlist. Left
            empty by default so that browser-originated requests -- the only
            ones that carry ``Origin`` -- are rejected outright.

    Returns:
        An SDK ``TransportSecuritySettings`` instance.

    Raises:
        ValueError: If a wildcard bind is passed with no explicit allowlist.
            Clients reach a ``0.0.0.0`` bind under some other name, so there is
            no ``Host`` value to infer; the caller must collect one first.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = list(allowed_hosts)
    if not hosts:
        if is_wildcard_host(host):
            msg = (
                f"Cannot infer a Host allowlist for wildcard bind {host!r}. "
                "Pass --allowed-host with the hostname clients will connect to."
            )
            raise ValueError(msg)
        authority = as_url_authority(host)
        hosts = [f"{authority}:{port}", f"{authority}:*"]

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=list(allowed_origins),
    )


def current_auth_context() -> AuthContext | None:
    """Return the identity the SDK established for the in-flight HTTP request.

    Returns None for stdio and for credential-free loopback binds, where no
    token was ever presented -- ``check_request`` then falls back to its own
    authenticator, preserving the historical local behaviour.
    """
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:  # pragma: no cover - mirrors the optional-extra guard.
        return None

    return auth_context_from_access_token(get_access_token())


@dataclass(frozen=True, slots=True)
class NetworkSecurityWiring:
    """SDK security objects resolved for one bind.

    All three are None for stdio and for a credential-free loopback bind, which
    is what leaves local use exactly as it was.
    """

    token_verifier: Any | None = None
    auth_settings: Any | None = None
    transport_security: Any | None = None


def resolve_network_security(
    *,
    transport: str,
    host: str,
    port: int,
    security: SecurityLayer,
    allowed_hosts: tuple[str, ...] = (),
    allowed_origins: tuple[str, ...] = (),
) -> NetworkSecurityWiring:
    """Validate a requested bind and build the SDK wiring that protects it.

    This is the trust boundary for serving: reaching a routable bind is enough
    to execute seeds through the local agent runtime, so such a bind has to
    demand a credential before the SDK ever starts listening.

    Args:
        transport: An already-validated transport name.
        host: The requested bind address.
        port: The requested bind port.
        security: The layer whose credentials network clients must satisfy.
        allowed_hosts: ``Host`` header allowlist. Required for wildcard binds.
        allowed_origins: ``Origin`` header allowlist. Empty rejects every
            browser-originated request, the intent for non-browser MCP clients.

    Returns:
        The verifier, auth settings, and transport security for this bind.

    Raises:
        ValueError: If the bind would expose tool execution without
            credentials, if authentication is configured for a transport that
            cannot carry it, or if a wildcard bind has no Host allowlist.
    """
    is_network = transport in NETWORK_TRANSPORTS
    auth_method = security.auth_config.method
    auth_enabled = auth_method != AuthMethod.NONE

    # stdio is a pipe owned by the client that spawned this process; there is
    # no header to carry a credential on, so an auth method configured there
    # would reject every call instead of protecting anything.
    if auth_enabled and not is_network:
        msg = (
            f"stdio transport does not support authentication "
            f"(configured method: {auth_method.value}). The client owns this "
            f"process directly. Use AuthMethod.NONE for stdio."
        )
        raise ValueError(msg)

    # Rate limiting buckets by client identity, and only a credential supplies
    # one. Without auth every caller would share a single bucket.
    if security.rate_limit_config.enabled and not auth_enabled:
        msg = (
            "Rate limiting requires client identity, which only authentication "
            "provides. Configure an auth method or disable "
            "rate_limit_config.enabled."
        )
        raise ValueError(msg)

    if is_network and not is_loopback_host(host) and not auth_enabled:
        msg = (
            f"Refusing to serve {transport} on non-loopback host {host!r} "
            f"without authentication. Anyone who can reach this port could "
            f"execute seeds through the local agent runtime. Either bind to "
            f"127.0.0.1, or configure an auth token."
        )
        raise ValueError(msg)

    if not is_network:
        return NetworkSecurityWiring()

    token_verifier = None
    auth_settings = None
    if auth_enabled:
        token_verifier = OuroborosTokenVerifier(
            authenticator=security.authenticator,
            method=auth_method,
        )
        auth_settings = build_auth_settings(host=host, port=port)

    transport_security = None
    if host not in _SDK_AUTOPROTECTED_HOSTS:
        transport_security = build_transport_security(
            host=host,
            port=port,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )

    return NetworkSecurityWiring(
        token_verifier=token_verifier,
        auth_settings=auth_settings,
        transport_security=transport_security,
    )
