"""Package-profile identity and fail-closed boundary tests."""

from importlib import metadata as importlib_metadata
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.package_profiles import (
    UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE,
    has_unsupported_claude_sdk_mcp_mix,
    public_runtime_backend,
)


@pytest.mark.parametrize(
    ("profile", "backend"),
    [
        (None, None),
        ("claude", "claude"),
        ("claude-cli", "claude_mcp"),
        ("claude-sdk", "claude"),
        ("claude_sdk", "claude"),
        ("codex", "codex"),
    ],
)
def test_public_runtime_backend_preserves_profile_contract(
    profile: str | None, backend: str | None
) -> None:
    assert public_runtime_backend(profile) == backend


@pytest.mark.parametrize(
    ("versions", "unsupported"),
    [
        ({"mcp": "2.0.0", "claude-agent-sdk": "0.2.139"}, True),
        ({"mcp": "1.28.1", "claude-agent-sdk": "0.2.139"}, False),
        ({"mcp": "2.0.0"}, False),
    ],
)
def test_forced_mixed_environment_detection(versions: dict[str, str], unsupported: bool) -> None:
    def fake_version(distribution: str) -> str:
        try:
            return versions[distribution]
        except KeyError as exc:
            raise importlib_metadata.PackageNotFoundError(distribution) from exc

    with patch("ouroboros.package_profiles.importlib_metadata.version", side_effect=fake_version):
        assert has_unsupported_claude_sdk_mcp_mix() is unsupported


def test_platform_matrix_uses_canonical_unsupported_message() -> None:
    root = Path(__file__).parent.parent.parent
    content = (root / "docs" / "platform-support.md").read_text(encoding="utf-8")

    assert UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE in content


def test_sdk_runtime_message_names_the_fix_not_a_reinstall() -> None:
    """The default-runtime failure must not read as a packaging problem.

    Regression for the report in #2038: `ouroboros mcp serve` with no
    `--runtime` inherits the `claude` default and printed the package-profile
    message, so a user with a perfectly good install was told to change extras.
    """
    from ouroboros.package_profiles import SDK_RUNTIME_IN_MCP_SERVER_MESSAGE

    assert "--runtime" in SDK_RUNTIME_IN_MCP_SERVER_MESSAGE
    assert "claude-cli" in SDK_RUNTIME_IN_MCP_SERVER_MESSAGE
    # It must not send the reader back to the package extras.
    assert "[mcp]" not in SDK_RUNTIME_IN_MCP_SERVER_MESSAGE
    # Dependency validation happens later, so this early runtime diagnostic
    # must not make an unconditional claim about installation health.
    assert "install" not in SDK_RUNTIME_IN_MCP_SERVER_MESSAGE.lower()
    assert SDK_RUNTIME_IN_MCP_SERVER_MESSAGE != UNSUPPORTED_CLAUDE_SDK_MCP_MESSAGE
