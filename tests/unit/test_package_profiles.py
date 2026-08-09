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
        ({"mcp": "2.0.0", "claude-agent-sdk": "0.2.123"}, True),
        ({"mcp": "1.28.1", "claude-agent-sdk": "0.2.123"}, False),
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
