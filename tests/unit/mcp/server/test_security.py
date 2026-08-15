"""Tests for MCP server security layer."""

import hashlib
import hmac
import time

from ouroboros.mcp.errors import MCPAuthError
from ouroboros.mcp.server.security import (
    AuthConfig,
    AuthContext,
    Authenticator,
    AuthMethod,
    Authorizer,
    InputValidator,
    Permission,
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
        authenticator = Authenticator(AuthConfig(method=AuthMethod.API_KEY, required=True))
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

    def test_validate_allows_goal_punctuation_as_freetext(self) -> None:
        """Natural-language tool goals can contain punctuation like semicolons."""
        validator = InputValidator()
        result = validator.validate(
            "ouroboros_auto",
            {"goal": "Create a Seed; do not edit real projects", "skip_run": True},
        )

        assert result.is_ok

    def test_validate_still_rejects_shell_metacharacters_in_non_freetext_fields(self) -> None:
        """Shell metacharacter checks still protect non-freetext arguments."""
        validator = InputValidator()
        result = validator.validate("tool", {"name": "safe; rm -rf /tmp/nope"})

        assert result.is_err
        assert "Shell metacharacter" in str(result.error)

    def test_validate_allows_user_preferences_punctuation_as_freetext(self) -> None:
        """Operator-supplied user_preferences carry freetext, never shell input."""
        validator = InputValidator()
        result = validator.validate(
            "ouroboros_auto",
            {
                "goal": "g",
                "user_preferences": {
                    "constraints": "no shell here; just a semicolon",
                    "non_goals": "item one; item two",
                },
            },
        )

        assert result.is_ok


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


class TestFreetextInsideContainers:
    """A freetext field is freetext wherever it sits in the argument tree."""

    def test_fanout_result_content_may_quote_source(self) -> None:
        """Lane findings quote code, and code contains semicolons.

        Judged by the root key alone this arrived as ``results[0].content`` and
        was rejected under the container's name, so the only way through was to
        reword the finding — which makes the record say something the lane never
        said.
        """
        validator = InputValidator()
        result = validator.validate(
            "ouroboros_submit_fanout_results",
            {
                "session_id": "s1",
                "fanout_id": "fanout_abc",
                "correlation_key": "context.lane_id",
                "results": [
                    {
                        "key": "code_context",
                        "content": {
                            "answer_text": "int limit = 999; return limit;",
                        },
                    }
                ],
            },
        )

        assert result.is_ok, result

    def test_a_pm_finding_may_quote_source(self) -> None:
        """Lane findings quote source, and source contains ``;``.

        They arrive in ``answer``, which is where every answer arrives. A
        separate field was tried for one round and the exemption did not follow
        it, so the move itself became a rejection — identical payload, new field
        name, and the only way through was to reword the finding. With one field
        there is nothing for the exemption to fall out of step with.
        """
        validator = InputValidator()
        result = validator.validate(
            "ouroboros_pm_interview",
            {
                "session_id": "s1",
                "answer": "[from-code] src/checkout.ts: `revokeAccess(user);`",
            },
        )

        assert result.is_ok, result

    def test_a_finding_may_describe_code_in_the_words_code_is_described_in(self) -> None:
        """The exemption governs every lexical check, not only the last one.

        A faithful description of code says ``subprocess``, ``open(`` and
        ``eval(`` — those are the names of the things being described. They were
        matched by ``dangerous_patterns``, which ran before the freetext
        exemption and never consulted it, so the exemption was half true and a
        large share of ``[from-code]`` findings could not reach the server. The
        way through was to reword the finding, which leaves the record saying
        something the lane never said.
        """
        validator = InputValidator()
        for finding in (
            "[from-code] api.py: uses subprocess.run for git calls, no shell=True.",
            "[from-code] io.py: reads with open(path) and closes it in a finally.",
            "[from-code] parser.py: still calls eval() on user-supplied config.",
            "[from-code] loader.py: exec( and compile( appear only in the sandbox.",
            "[from-code] pkg/mod.py: imports from ../shared/config.py",
        ):
            result = validator.validate(
                "ouroboros_pm_interview",
                {"session_id": "s1", "answer": finding},
            )
            assert result.is_ok, f"{finding} -> {result.error}"

    def test_a_lane_finding_inside_its_container_may_do_the_same(self) -> None:
        """``results[].content`` is the same text arriving by the other door."""
        validator = InputValidator()
        result = validator.validate(
            "ouroboros_submit_fanout_results",
            {
                "results": [
                    {
                        "key": "code_context",
                        "content": (
                            "billing-api src/lapse.py: calls subprocess.run(); "
                            "storefront reads with open(path)."
                        ),
                    }
                ]
            },
        )

        assert result.is_ok, result

    def test_a_field_that_can_reach_execution_is_still_scanned(self) -> None:
        """The boundary moved; it did not disappear.

        Scoping the scan to fields that can reach execution or control is only
        safe while those fields are still scanned, so this pins the other side.
        Without it, "stop rejecting evidence" reads the same as "stop checking".
        """
        validator = InputValidator()
        for key, value in (
            ("cwd", "/tmp; rm -rf /"),
            ("cwd", "../../etc/shadow"),
            ("session_id", "__import__('os')"),
            ("session_id", "os.system('curl evil')"),
        ):
            result = validator.validate("ouroboros_pm_interview", {key: value})
            assert result.is_err, f"{key}={value} was accepted"

    def test_a_pm_question_about_code_survives_validation(self) -> None:
        """``last_question`` is the question, not a thing to run.

        It is echoed back so the answer is filed under the question it belongs
        to, and the handler stores it as the round's text. A PM question quotes
        the code it asks about, so it carries the same terms an answer does --
        and this list previously read those terms as execution and refused the
        call, losing the answer and any confirmed finding with it.

        It sat in the rejection case above until this test replaced it. That
        assertion encoded the classification rather than checking it, which is
        how a field stays misfiled: the scan and the test agreed, and both were
        wrong about what the field carries.
        """
        validator = InputValidator()
        for question in (
            "How does subprocess.run() get used for retries?",
            "What does open(config) read at startup?",
            "A subscription lapses mid-period; what happens then?",
        ):
            result = validator.validate(
                "ouroboros_pm_interview",
                {"session_id": "pm-1", "answer": "grace period", "last_question": question},
            )
            assert result.is_ok, f"{question!r} was rejected: {result}"

    def test_a_non_freetext_field_inside_a_container_is_still_checked(self) -> None:
        """Containment does not launder a field that was never freetext."""
        validator = InputValidator()
        result = validator.validate("tool", {"results": [{"name": "safe; rm -rf /tmp"}]})

        assert result.is_err
        assert "Shell metacharacter" in str(result.error)
