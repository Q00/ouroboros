"""MCP Server security layer.

This module provides security features for the MCP server including:
- Authentication (API key, token validation)
- Authorization (tool-level permissions)
- Input validation
- Rate limiting
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import hmac
import threading
import time
from typing import Any, TypeVar

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPAuthError, MCPServerError

log = structlog.get_logger(__name__)

T = TypeVar("T")


class AuthMethod(StrEnum):
    """Authentication method type."""

    NONE = "none"
    API_KEY = "api_key"
    BEARER_TOKEN = "bearer_token"


class Permission(StrEnum):
    """Permission levels for tool access."""

    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Authentication configuration.

    Attributes:
        method: Authentication method to use.
        api_keys: Valid API keys (for API_KEY method).
        token_secret: Secret for token validation (for BEARER_TOKEN method).
        required: Whether authentication is required.
    """

    method: AuthMethod = AuthMethod.NONE
    api_keys: frozenset[str] = field(default_factory=frozenset)
    token_secret: str | None = None
    required: bool = False


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Rate limiting configuration.

    Attributes:
        enabled: Whether rate limiting is enabled.
        requests_per_minute: Maximum requests per minute per client.
        burst_size: Maximum burst size.
    """

    enabled: bool = False
    requests_per_minute: int = 60
    burst_size: int = 10


@dataclass(frozen=True, slots=True)
class ToolPermission:
    """Permission configuration for a tool.

    Attributes:
        tool_name: Name of the tool.
        required_permissions: Permissions required to call this tool.
        allowed_roles: Roles that can access this tool.
    """

    tool_name: str
    required_permissions: frozenset[Permission] = field(
        default_factory=lambda: frozenset({Permission.EXECUTE})
    )
    allowed_roles: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Context for an authenticated request.

    Attributes:
        authenticated: Whether the request is authenticated.
        client_id: Identifier for the client.
        permissions: Granted permissions.
        roles: Assigned roles.
        metadata: Additional auth metadata.
    """

    authenticated: bool = False
    client_id: str | None = None
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    metadata: dict[str, Any] = field(default_factory=dict)


class RateLimiter:
    """Token bucket rate limiter.

    Implements a token bucket algorithm for rate limiting requests
    per client.
    """

    def __init__(
        self,
        requests_per_minute: int,
        burst_size: int,
    ) -> None:
        """Initialize rate limiter.

        Args:
            requests_per_minute: Maximum requests per minute.
            burst_size: Maximum burst size (bucket capacity).
        """
        self._rate = requests_per_minute / 60.0  # Requests per second
        self._burst_size = burst_size
        self._buckets: dict[str, tuple[float, float]] = {}  # client_id -> (tokens, last_update)
        self._lock = asyncio.Lock()
        self._sync_lock = threading.Lock()

    async def check(self, client_id: str) -> bool:
        """Check if a request is allowed.

        Args:
            client_id: Identifier for the client.

        Returns:
            True if the request is allowed, False if rate limited.
        """
        async with self._lock:
            now = time.monotonic()
            tokens, last_update = self._buckets.get(client_id, (self._burst_size, now))

            # Add tokens based on time elapsed
            elapsed = now - last_update
            tokens = min(self._burst_size, tokens + elapsed * self._rate)

            if tokens >= 1:
                self._buckets[client_id] = (tokens - 1, now)
                return True
            else:
                self._buckets[client_id] = (tokens, now)
                return False

    def reset(self, client_id: str) -> None:
        """Reset rate limit for a client.

        Args:
            client_id: Identifier for the client.
        """
        with self._sync_lock:
            if client_id in self._buckets:
                del self._buckets[client_id]


class Authenticator:
    """Handles authentication for MCP requests."""

    def __init__(self, config: AuthConfig) -> None:
        """Initialize authenticator.

        Args:
            config: Authentication configuration.
        """
        self._config = config
        # Hash API keys for secure comparison
        self._hashed_keys: frozenset[str] = frozenset(
            self._hash_key(key) for key in config.api_keys
        )

    @staticmethod
    def _hash_key(key: str) -> str:
        """Hash an API key for secure storage and comparison."""
        return hashlib.sha256(key.encode()).hexdigest()

    def authenticate(
        self,
        credentials: dict[str, str] | None,
    ) -> Result[AuthContext, MCPAuthError]:
        """Authenticate a request.

        Args:
            credentials: Credentials provided by the client.

        Returns:
            Result containing auth context or auth error.
        """
        if self._config.method == AuthMethod.NONE:
            return Result.ok(
                AuthContext(
                    authenticated=not self._config.required,
                    permissions=frozenset(Permission),
                )
            )

        if not credentials:
            if self._config.required:
                return Result.err(
                    MCPAuthError(
                        "Authentication required",
                        auth_method=self._config.method.value,
                    )
                )
            return Result.ok(AuthContext(authenticated=False))

        if self._config.method == AuthMethod.API_KEY:
            return self._authenticate_api_key(credentials)
        elif self._config.method == AuthMethod.BEARER_TOKEN:
            return self._authenticate_token(credentials)

        return Result.err(
            MCPAuthError(
                f"Unknown auth method: {self._config.method}",
                auth_method=self._config.method.value,
            )
        )

    def _authenticate_api_key(
        self,
        credentials: dict[str, str],
    ) -> Result[AuthContext, MCPAuthError]:
        """Authenticate using API key.

        Args:
            credentials: Must contain 'api_key'.

        Returns:
            Result containing auth context or auth error.
        """
        api_key = credentials.get("api_key")
        if not api_key:
            return Result.err(
                MCPAuthError(
                    "API key required",
                    auth_method=AuthMethod.API_KEY.value,
                )
            )

        hashed = self._hash_key(api_key)
        if hashed in self._hashed_keys:
            log.info("mcp.auth.api_key_valid")
            return Result.ok(
                AuthContext(
                    authenticated=True,
                    client_id=hashed[:16],  # Use prefix as client ID
                    permissions=frozenset(Permission),
                )
            )

        log.warning("mcp.auth.invalid_api_key")
        return Result.err(
            MCPAuthError(
                "Invalid API key",
                auth_method=AuthMethod.API_KEY.value,
            )
        )

    def _authenticate_token(
        self,
        credentials: dict[str, str],
    ) -> Result[AuthContext, MCPAuthError]:
        """Authenticate using bearer token.

        Args:
            credentials: Must contain 'token'.

        Returns:
            Result containing auth context or auth error.
        """
        token = credentials.get("token")
        if not token:
            return Result.err(
                MCPAuthError(
                    "Bearer token required",
                    auth_method=AuthMethod.BEARER_TOKEN.value,
                )
            )

        if not self._config.token_secret:
            return Result.err(
                MCPAuthError(
                    "Token validation not configured",
                    auth_method=AuthMethod.BEARER_TOKEN.value,
                )
            )

        # Simple token validation (in production, use JWT or similar)
        # Format: client_id:timestamp:signature
        parts = token.split(":")
        if len(parts) != 3:
            return Result.err(
                MCPAuthError(
                    "Invalid token format",
                    auth_method=AuthMethod.BEARER_TOKEN.value,
                )
            )

        client_id, timestamp_str, signature = parts

        # Verify signature
        expected = hmac.new(
            self._config.token_secret.encode(),
            f"{client_id}:{timestamp_str}".encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected):
            log.warning("mcp.auth.invalid_token_signature")
            return Result.err(
                MCPAuthError(
                    "Invalid token signature",
                    auth_method=AuthMethod.BEARER_TOKEN.value,
                )
            )

        # Check timestamp (tokens valid for 1 hour, 60s clock skew tolerance)
        try:
            timestamp = int(timestamp_str)
            now = time.time()
            if timestamp > now + 60:
                return Result.err(
                    MCPAuthError(
                        "Token timestamp is in the future",
                        auth_method=AuthMethod.BEARER_TOKEN.value,
                    )
                )
            if now - timestamp > 3600:
                return Result.err(
                    MCPAuthError(
                        "Token expired",
                        auth_method=AuthMethod.BEARER_TOKEN.value,
                    )
                )
        except ValueError:
            return Result.err(
                MCPAuthError(
                    "Invalid token timestamp",
                    auth_method=AuthMethod.BEARER_TOKEN.value,
                )
            )

        log.info("mcp.auth.token_valid", client_id=client_id)
        return Result.ok(
            AuthContext(
                authenticated=True,
                client_id=client_id,
                permissions=frozenset(Permission),
            )
        )


class Authorizer:
    """Handles authorization for MCP tool calls."""

    def __init__(self) -> None:
        """Initialize authorizer."""
        self._tool_permissions: dict[str, ToolPermission] = {}

    def register_tool_permission(self, permission: ToolPermission) -> None:
        """Register permission requirements for a tool.

        Args:
            permission: Permission configuration for the tool.
        """
        self._tool_permissions[permission.tool_name] = permission

    def authorize(
        self,
        tool_name: str,
        auth_context: AuthContext,
    ) -> Result[None, MCPAuthError]:
        """Check if a request is authorized to call a tool.

        Args:
            tool_name: Name of the tool being called.
            auth_context: Authentication context.

        Returns:
            Result.ok(None) if authorized, Result.err otherwise.
        """
        permission = self._tool_permissions.get(tool_name)

        # If no specific permission is registered, allow authenticated users
        if permission is None:
            if auth_context.authenticated:
                return Result.ok(None)
            return Result.err(
                MCPAuthError(
                    f"Authentication required for tool: {tool_name}",
                    required_permission=Permission.EXECUTE.value,
                )
            )

        # Check if user has required permissions
        if not permission.required_permissions.issubset(auth_context.permissions):
            missing = permission.required_permissions - auth_context.permissions
            return Result.err(
                MCPAuthError(
                    f"Missing permissions for tool {tool_name}: {missing}",
                    required_permission=", ".join(p.value for p in missing),
                )
            )

        # Check if user has an allowed role (if roles are specified)
        if permission.allowed_roles and not permission.allowed_roles.intersection(
            auth_context.roles
        ):
            return Result.err(
                MCPAuthError(
                    f"Role not authorized for tool: {tool_name}",
                    required_permission=f"roles: {permission.allowed_roles}",
                )
            )

        return Result.ok(None)


class InputValidator:
    """Validates tool input arguments."""

    def __init__(self) -> None:
        """Initialize validator."""
        self._validators: dict[str, Callable[[dict[str, Any]], Result[None, str]]] = {}

    def register_validator(
        self,
        tool_name: str,
        validator: Callable[[dict[str, Any]], Result[None, str]],
    ) -> None:
        """Register a custom validator for a tool.

        Args:
            tool_name: Name of the tool.
            validator: Validation function.
        """
        self._validators[tool_name] = validator

    def validate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        _schema: dict[str, Any] | None = None,
    ) -> Result[None, MCPServerError]:
        """Validate tool arguments.

        Args:
            tool_name: Name of the tool.
            arguments: Arguments to validate.
            _schema: Optional JSON schema for validation (reserved for future use).

        Returns:
            Result.ok(None) if valid, Result.err otherwise.
        """
        # Check for dangerous patterns in string arguments
        dangerous_patterns = [
            "__import__",
            "subprocess",
            "os.popen",
            "os.system",
            "eval(",
            "exec(",
            "compile(",
            "open(",
        ]
        path_traversal_patterns = ["../", "..\\"]
        shell_metacharacters = [";", "|", "&&", "||"]

        for key, value in arguments.items():
            if isinstance(value, str):
                for pattern in dangerous_patterns:
                    if pattern in value:
                        return Result.err(
                            MCPServerError(
                                f"Potentially dangerous input in {key}",
                                details={"pattern": pattern},
                            )
                        )
                for pattern in path_traversal_patterns:
                    if pattern in value:
                        return Result.err(
                            MCPServerError(
                                f"Path traversal detected in {key}",
                                details={"pattern": pattern},
                            )
                        )
                for char in shell_metacharacters:
                    if char in value:
                        return Result.err(
                            MCPServerError(
                                f"Shell metacharacter detected in {key}",
                                details={"pattern": char},
                            )
                        )

        # Run custom validator if registered
        if tool_name in self._validators:
            result = self._validators[tool_name](arguments)
            if result.is_err:
                return Result.err(
                    MCPServerError(
                        f"Validation failed for {tool_name}: {result.error}",
                    )
                )

        return Result.ok(None)


@dataclass
class SecurityLayer:
    """Combined security layer for MCP server.

    Provides authentication, authorization, rate limiting, and input validation
    in a single interface.
    """

    auth_config: AuthConfig = field(default_factory=AuthConfig)
    rate_limit_config: RateLimitConfig = field(default_factory=RateLimitConfig)

    def __post_init__(self) -> None:
        """Initialize security components."""
        self._authenticator = Authenticator(self.auth_config)
        self._authorizer = Authorizer()
        self._validator = InputValidator()
        self._rate_limiter: RateLimiter | None = None

        if self.rate_limit_config.enabled:
            self._rate_limiter = RateLimiter(
                self.rate_limit_config.requests_per_minute,
                self.rate_limit_config.burst_size,
            )

    def register_tool_permission(self, permission: ToolPermission) -> None:
        """Register permission requirements for a tool."""
        self._authorizer.register_tool_permission(permission)

    def register_validator(
        self,
        tool_name: str,
        validator: Callable[[dict[str, Any]], Result[None, str]],
    ) -> None:
        """Register a custom validator for a tool."""
        self._validator.register_validator(tool_name, validator)

    async def check_request(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        credentials: dict[str, str] | None = None,
    ) -> Result[AuthContext, MCPServerError]:
        """Check if a request passes all security checks.

        Args:
            tool_name: Name of the tool being called.
            arguments: Arguments for the tool.
            credentials: Client credentials.

        Returns:
            Result containing auth context or security error.
        """
        # 1. Authenticate
        auth_result = self._authenticator.authenticate(credentials)
        if auth_result.is_err:
            return Result.err(auth_result.error)

        auth_context = auth_result.value

        # 2. Rate limit (if enabled)
        if (
            self._rate_limiter
            and auth_context.client_id
            and not await self._rate_limiter.check(auth_context.client_id)
        ):
            return Result.err(
                MCPServerError(
                    "Rate limit exceeded",
                    is_retriable=True,
                    details={"retry_after": 60},
                )
            )

        # 3. Authorize
        authz_result = self._authorizer.authorize(tool_name, auth_context)
        if authz_result.is_err:
            return Result.err(authz_result.error)

        # 4. Validate input
        valid_result = self._validator.validate(tool_name, arguments)
        if valid_result.is_err:
            return Result.err(valid_result.error)

        return Result.ok(auth_context)


def create_security_middleware(
    security_layer: SecurityLayer,
) -> Callable[
    [
        str,
        dict[str, Any],
        dict[str, str] | None,
        Callable[..., Awaitable[Result[T, MCPServerError]]],
    ],
    Awaitable[Result[T, MCPServerError]],
]:
    """Create a security middleware function.

    Args:
        security_layer: The security layer to use.

    Returns:
        A middleware function that wraps tool handlers.
    """

    async def middleware(
        tool_name: str,
        arguments: dict[str, Any],
        credentials: dict[str, str] | None,
        handler: Callable[..., Awaitable[Result[T, MCPServerError]]],
    ) -> Result[T, MCPServerError]:
        """Security middleware that checks requests before calling handlers."""
        check_result = await security_layer.check_request(tool_name, arguments, credentials)
        if check_result.is_err:
            return Result.err(check_result.error)

        return await handler(arguments)

    return middleware
