"""Tests for MCP server security layer."""

import hashlib
import hmac
import time

import pytest

from ouroboros.mcp.errors import MCPAuthError
from ouroboros.mcp.server.security import (
    AuthConfig,
    AuthContext,
    AuthMethod,
    Authenticator,
    Authorizer,
    InputValidator,
    Permission,
    RateLimitConfig,
    RateLimiter,
    SecurityLayer,
    ToolPermission,
)


class TestAuthConfig:
    """Test AuthConfig dataclass."""

    def test_default_config(self) -> None:
        """AuthConfig has correct defaults."""
        config = AuthConfig()
        assert config.method == AuthMethod.NONE
        assert config.required is False
        assert len(config.api_keys) == 0

    def test_api_key_config(self) -> None:
        """AuthConfig with API keys."""
        config = AuthConfig(
            method=AuthMethod.API_KEY,
            api_keys=frozenset({"key1", "key2"}),
            required=True,
        )
        assert config.method == AuthMethod.API_KEY
        assert "key1" in config.api_keys


class TestAuthenticator:
    """Test Authenticator class."""

    def test_no_auth_method_allows_all(self) -> None:
        """NONE auth method allows all requests."""
        authenticator = Authenticator(AuthConfig())
        result = authenticator.authenticate(None)

        assert result.is_ok
        assert result.value.authenticated is True

    def test_required_auth_without_credentials(self) -> None:
        """Required auth fails without credentials."""
        authenticator = Authenticator(
            AuthConfig(method=AuthMethod.API_KEY, required=True)
        )
        result = authenticator.authenticate(None)

        assert result.is_err
        assert isinstance(result.error, MCPAuthError)

    def test_api_key_authentication_success(self) -> None:
        """Valid API key authenticates successfully."""
        config = AuthConfig(
            method=AuthMethod.API_KEY,
            api_keys=frozenset({"valid-key"}),
            required=True,
        )
        authenticator = Authenticator(config)
        result = authenticator.authenticate({"api_key": "valid-key"})

        assert result.is_ok
        assert result.value.authenticated is True

    def test_api_key_authentication_failure(self) -> None:
        """Invalid API key fails authentication."""
        config = AuthConfig(
            method=AuthMethod.API_KEY,
            api_keys=frozenset({"valid-key"}),
            required=True,
        )
        authenticator = Authenticator(config)
        result = authenticator.authenticate({"api_key": "invalid-key"})

        assert result.is_err
        assert "Invalid API key" in str(result.error)

    def test_bearer_token_authentication_success(self) -> None:
        """Valid bearer token authenticates successfully."""
        secret = "test-secret"
        client_id = "test-client"
        timestamp = str(int(time.time()))
        signature = hmac.new(
            secret.encode(),
            f"{client_id}:{timestamp}".encode(),
            hashlib.sha256,
        ).hexdigest()
        token = f"{client_id}:{timestamp}:{signature}"

        config = AuthConfig(
            method=AuthMethod.BEARER_TOKEN,
            token_secret=secret,
            required=True,
        )
        authenticator = Authenticator(config)
        result = authenticator.authenticate({"token": token})

        assert result.is_ok
        assert result.value.client_id == client_id

    def test_bearer_token_expired(self) -> None:
        """Expired bearer token fails authentication."""
        secret = "test-secret"
        client_id = "test-client"
        timestamp = str(int(time.time()) - 7200)  # 2 hours ago
        signature = hmac.new(
            secret.encode(),
            f"{client_id}:{timestamp}".encode(),
            hashlib.sha256,
        ).hexdigest()
        token = f"{client_id}:{timestamp}:{signature}"

        config = AuthConfig(
            method=AuthMethod.BEARER_TOKEN,
            token_secret=secret,
            required=True,
        )
        authenticator = Authenticator(config)
        result = authenticator.authenticate({"token": token})

        assert result.is_err
        assert "expired" in str(result.error).lower()


class TestAuthorizer:
    """Test Authorizer class."""

    def test_authorize_without_registration(self) -> None:
        """Authorization allows authenticated users by default."""
        authorizer = Authorizer()
        context = AuthContext(authenticated=True)

        result = authorizer.authorize("any_tool", context)

        assert result.is_ok

    def test_authorize_requires_authentication(self) -> None:
        """Authorization fails for unauthenticated users."""
        authorizer = Authorizer()
        context = AuthContext(authenticated=False)

        result = authorizer.authorize("any_tool", context)

        assert result.is_err
        assert "Authentication required" in str(result.error)

    def test_authorize_with_permissions(self) -> None:
        """Authorization checks required permissions."""
        authorizer = Authorizer()
        authorizer.register_tool_permission(
            ToolPermission(
                tool_name="admin_tool",
                required_permissions=frozenset({Permission.ADMIN}),
            )
        )

        # User without ADMIN permission
        context = AuthContext(
            authenticated=True,
            permissions=frozenset({Permission.EXECUTE}),
        )
        result = authorizer.authorize("admin_tool", context)

        assert result.is_err
        assert "Missing permissions" in str(result.error)

    def test_authorize_with_roles(self) -> None:
        """Authorization checks allowed roles."""
        authorizer = Authorizer()
        authorizer.register_tool_permission(
            ToolPermission(
                tool_name="special_tool",
                allowed_roles=frozenset({"admin", "superuser"}),
            )
        )

        # User with wrong role
        context = AuthContext(
            authenticated=True,
            permissions=frozenset(Permission),
            roles=frozenset({"user"}),
        )
        result = authorizer.authorize("special_tool", context)

        assert result.is_err
        assert "Role not authorized" in str(result.error)


class TestInputValidator:
    """Test InputValidator class."""

    def test_validate_safe_input(self) -> None:
        """Safe input passes validation."""
        validator = InputValidator()
        result = validator.validate("tool", {"name": "test", "value": 123})

        assert result.is_ok

    def test_validate_dangerous_patterns(self) -> None:
        """Dangerous patterns are rejected."""
        validator = InputValidator()
        result = validator.validate(
            "tool",
            {"code": "__import__('os').system('rm -rf /')"},
        )

        assert result.is_err
        assert "Potentially dangerous" in str(result.error)


class TestRateLimiter:
    """Test RateLimiter class."""

    async def test_rate_limiter_allows_initial_requests(self) -> None:
        """Rate limiter allows requests within burst limit."""
        limiter = RateLimiter(requests_per_minute=60, burst_size=5)

        for _ in range(5):
            assert await limiter.check("client1") is True

    async def test_rate_limiter_blocks_excess_requests(self) -> None:
        """Rate limiter blocks requests exceeding burst."""
        limiter = RateLimiter(requests_per_minute=60, burst_size=3)

        # Use up burst
        for _ in range(3):
            await limiter.check("client1")

        # Should be blocked
        assert await limiter.check("client1") is False

    async def test_rate_limiter_separate_clients(self) -> None:
        """Rate limiter tracks clients separately."""
        limiter = RateLimiter(requests_per_minute=60, burst_size=2)

        # Use up client1's burst
        await limiter.check("client1")
        await limiter.check("client1")
        assert await limiter.check("client1") is False

        # Client2 should still be allowed
        assert await limiter.check("client2") is True


class TestSecurityLayer:
    """Test SecurityLayer class."""

    async def test_security_layer_no_auth(self) -> None:
        """Security layer passes with no auth required."""
        layer = SecurityLayer()
        result = await layer.check_request("tool", {"arg": "value"})

        assert result.is_ok

    async def test_security_layer_validates_input(self) -> None:
        """Security layer validates input."""
        layer = SecurityLayer()
        result = await layer.check_request(
            "tool",
            {"code": "__import__('subprocess')"},
        )

        assert result.is_err
        assert "dangerous" in str(result.error).lower()
