from pathlib import Path
from unittest.mock import MagicMock, patch

from ouroboros.cli.windows_codex_mcp import (
    apply_windows_codex_mcp_mode,
    resolve_windows_codex_mcp_mode,
)


def test_http_mode_rejects_launcher_that_cannot_start_mcp(tmp_path: Path) -> None:
    with patch("ouroboros.cli.windows_codex_mcp._launcher_is_usable", return_value=False):
        decision = resolve_windows_codex_mcp_mode(
            "http",
            codex_config=tmp_path / "config.toml",
            launcher=("uvx", ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"]),
        )

    assert decision.success is False
    assert decision.rendered_section is None


def test_http_mode_renders_spaced_executable_and_preserves_mcp_extra(tmp_path: Path) -> None:
    info = MagicMock()
    with patch("ouroboros.cli.windows_codex_mcp._launcher_is_usable", return_value=True):
        handled, success, section = apply_windows_codex_mcp_mode(
            "http",
            codex_config=tmp_path / "config.toml",
            launcher=(
                r"C:\Program Files\uv\uvx.exe",
                ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
            ),
            print_error=MagicMock(),
            print_info=info,
        )

    assert handled is True
    assert success is True
    assert section is not None
    rendered = info.call_args.args[0]
    assert rendered.startswith(
        'Start the MCP server before opening Codex Desktop: "C:\\Program Files'
    )
    assert "ouroboros-ai\\[mcp]" in rendered
