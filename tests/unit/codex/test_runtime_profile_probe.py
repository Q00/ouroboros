"""Regression coverage for the Codex profile-format capability probe."""

from __future__ import annotations

from collections.abc import Callable
import os
import subprocess
import sys
from typing import Any
from unittest.mock import Mock

import pytest

from ouroboros.codex.runtime_profile import codex_uses_profile_v2

UNIFIED_PROFILE_HELP = """
Usage: codex [OPTIONS] [PROMPT]

Options:
  -p, --profile <CONFIG_PROFILE_V2>
          Layer $CODEX_HOME/<name>.config.toml on top of the base user config
  -h, --help
          Print help
"""

LEGACY_PROFILE_HELP = """
Usage: codex [OPTIONS] [PROMPT]

Options:
  -p, --profile <CONFIG_PROFILE>
          Configuration profile from config.toml to specify default options
      --profile-v2 <CONFIG_PROFILE_V2>
          Layer $CODEX_HOME/<name>.config.toml on top of the base user config
  -h, --help
          Print help
"""


def _help_process(
    stdout: bytes = b"", *, stderr: bytes = b"", returncode: int = 0
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    """Replace Codex with a real child emitting controlled bytes on both pipes."""
    script = (
        "import sys; "
        f"sys.stdout.buffer.write(bytes.fromhex({stdout.hex()!r})); "
        f"sys.stderr.buffer.write(bytes.fromhex({stderr.hex()!r})); "
        f"sys.exit({returncode})"
    )

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        assert command == ["test-codex", "--help"]
        # Keep the fixture hidden even when testing the pre-fix implementation.
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return subprocess.run([sys.executable, "-c", script], **kwargs)

    return run


@pytest.mark.parametrize(
    ("help_text", "expected"),
    [(UNIFIED_PROFILE_HELP, True), (LEGACY_PROFILE_HELP, False)],
    ids=["unified-selector", "legacy-split-selector"],
)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_recognizes_supported_profile_formats_on_either_stream(
    help_text: str, expected: bool, stream: str
) -> None:
    runner = _help_process(**{stream: help_text.encode("utf-8")})

    assert codex_uses_profile_v2("test-codex", run_command=runner) is expected


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_utf8_help_is_recognized_under_cp949_locale(
    stream: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise real pipe decoding, including the Windows reader-thread path."""
    help_bytes = ("Codex CLI \N{HORIZONTAL ELLIPSIS}\n" + UNIFIED_PROFILE_HELP).encode("utf-8")
    with pytest.raises(UnicodeDecodeError):
        help_bytes.decode("cp949")

    # Simulate the non-UTF-8 Windows default without changing the user's locale
    # or Python mode. A probe relying on text=True's default decoding fails here.
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp949")
    runner = _help_process(**{stream: help_bytes})

    assert codex_uses_profile_v2("test-codex", run_command=runner) is True


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_invalid_utf8_is_unknown_even_with_recognizable_help(stream: str) -> None:
    help_bytes = UNIFIED_PROFILE_HELP.encode("utf-8") + b"\xff"
    runner = _help_process(**{stream: help_bytes})

    assert codex_uses_profile_v2("test-codex", run_command=runner) is None


@pytest.mark.parametrize("help_text", [UNIFIED_PROFILE_HELP, LEGACY_PROFILE_HELP])
def test_failed_help_command_is_unknown_even_with_recognizable_output(help_text: str) -> None:
    runner = _help_process(help_text.encode("utf-8"), returncode=1)

    assert codex_uses_profile_v2("test-codex", run_command=runner) is None


@pytest.mark.parametrize(
    "help_text",
    [
        "",
        "Codex is starting, please retry later.",
        "  -p, --profile <NAME>\n          An unrecognized future profile contract\n",
        "      --profile-v2 <CONFIG_PROFILE_V2>\n"
        "          Layer $CODEX_HOME/<name>.config.toml on top of the base user config\n",
    ],
    ids=["empty", "unrecognized-help", "unrecognized-selector", "v2-only"],
)
def test_unrecognized_help_is_unknown(help_text: str) -> None:
    runner = _help_process(help_text.encode("utf-8"))

    assert codex_uses_profile_v2("test-codex", run_command=runner) is None


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("Codex executable disappeared"),
        OSError("Cannot execute Codex"),
        subprocess.TimeoutExpired(["test-codex", "--help"], timeout=5),
    ],
    ids=["missing-executable", "os-error", "timeout"],
)
def test_help_launch_failure_is_unknown(failure: Exception) -> None:
    runner = Mock(side_effect=failure)

    assert codex_uses_profile_v2("test-codex", run_command=runner) is None


def test_missing_cli_path_is_unknown_without_spawning() -> None:
    runner = Mock()

    assert codex_uses_profile_v2(None, run_command=runner) is None
    runner.assert_not_called()
