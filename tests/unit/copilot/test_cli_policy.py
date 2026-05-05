"""Tests for shared GitHub Copilot CLI launch policy helpers."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from ouroboros.copilot.cli_policy import (
    build_copilot_child_env,
    resolve_copilot_cli_path,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append(("warning", event, kwargs))

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append(("info", event, kwargs))


def _write_wrapper(path: Path) -> Path:
    path.write_bytes(b"\xcf\xfa\xed\xfe")
    path.chmod(0o755)
    return path


def _write_script(path: Path) -> Path:
    path.write_text("#!/usr/bin/env node\nconsole.log('copilot')\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Wrapper-detection test relies on POSIX exec bits and Mach-O magic.",
)
class TestResolveCopilotCliPath:
    def test_falls_back_from_wrapper_to_real_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrapper = _write_wrapper(tmp_path / "copilot-wrapper")
        real_dir = tmp_path / "real-bin"
        real_dir.mkdir()
        real_cli = _write_script(real_dir / "copilot")
        logger = _FakeLogger()

        monkeypatch.setenv("PATH", str(real_dir))

        resolution = resolve_copilot_cli_path(
            explicit_cli_path=wrapper,
            configured_cli_path=None,
            logger=logger,
            log_namespace="copilot_cli_adapter",
        )

        assert resolution.cli_path == str(real_cli)
        assert resolution.wrapper_path == str(wrapper)
        assert resolution.fallback_path == str(real_cli)
        assert logger.events == [
            (
                "warning",
                "copilot_cli_adapter.cli_wrapper_detected",
                {
                    "wrapper_path": str(wrapper),
                    "hint": "Searching PATH for the real Node.js copilot CLI.",
                },
            ),
            (
                "info",
                "copilot_cli_adapter.cli_resolved_via_fallback",
                {"fallback_path": str(real_cli)},
            ),
        ]

    def test_keeps_wrapper_when_no_real_cli_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrapper = _write_wrapper(tmp_path / "copilot-wrapper")
        logger = _FakeLogger()

        monkeypatch.setenv("PATH", "")

        resolution = resolve_copilot_cli_path(
            explicit_cli_path=wrapper,
            configured_cli_path=None,
            logger=logger,
            log_namespace="copilot_cli_runtime",
        )

        assert resolution.cli_path == str(wrapper)
        assert resolution.wrapper_path == str(wrapper)
        assert resolution.fallback_path is None
        assert logger.events[-1] == (
            "warning",
            "copilot_cli_runtime.cli_no_fallback",
            {"wrapper_path": str(wrapper)},
        )


class TestBuildCopilotChildEnv:
    def test_strips_recursive_markers_and_increments_depth(self) -> None:
        env = build_copilot_child_env(
            base_env={
                "OUROBOROS_AGENT_RUNTIME": "copilot",
                "OUROBOROS_LLM_BACKEND": "copilot",
                "COPILOT_SESSION_ID": "session-123",
                "COPILOT_RESUME": "1",
                "COPILOT_ALLOW_ALL": "1",
                "CLAUDECODE": "1",
                "CODEX_THREAD_ID": "thread-xyz",
                "_OUROBOROS_DEPTH": "2",
                "GH_TOKEN": "ghp_secret",
                "GITHUB_TOKEN": "ghp_other",
                "KEEP_ME": "ok",
            },
            depth_error_factory=lambda depth, max_depth: RuntimeError(f"depth {depth}/{max_depth}"),
        )

        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env
        assert "COPILOT_SESSION_ID" not in env
        assert "COPILOT_RESUME" not in env
        assert "COPILOT_ALLOW_ALL" not in env
        assert "CLAUDECODE" not in env
        assert "CODEX_THREAD_ID" not in env
        assert env["_OUROBOROS_DEPTH"] == "3"
        assert env["KEEP_ME"] == "ok"
        # GH_TOKEN / GITHUB_TOKEN must survive — child needs them to authenticate.
        assert env["GH_TOKEN"] == "ghp_secret"
        assert env["GITHUB_TOKEN"] == "ghp_other"

    def test_uses_supplied_error_factory_for_depth_guard(self) -> None:
        class DepthExceededError(RuntimeError):
            pass

        with pytest.raises(DepthExceededError, match="depth 6/5"):
            build_copilot_child_env(
                base_env={"_OUROBOROS_DEPTH": "5"},
                depth_error_factory=lambda depth, max_depth: DepthExceededError(
                    f"depth {depth}/{max_depth}"
                ),
            )

    def test_handles_missing_depth_var(self) -> None:
        env = build_copilot_child_env(
            base_env={},
            depth_error_factory=lambda depth, max_depth: RuntimeError(f"depth {depth}/{max_depth}"),
        )
        assert env["_OUROBOROS_DEPTH"] == "1"

    def test_handles_corrupt_depth_var(self) -> None:
        env = build_copilot_child_env(
            base_env={"_OUROBOROS_DEPTH": "not-a-number"},
            depth_error_factory=lambda depth, max_depth: RuntimeError(f"depth {depth}/{max_depth}"),
        )
        assert env["_OUROBOROS_DEPTH"] == "1"
