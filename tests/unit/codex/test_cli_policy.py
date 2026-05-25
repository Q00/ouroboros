"""Tests for shared Codex CLI launch policy helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.codex.cli_policy import (
    build_codex_child_env,
    is_wrapper_binary,
    resolve_codex_cli_path,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def warning(self, event: str, **kwargs: object) -> None:
        self.events.append(("warning", event, kwargs))

    def info(self, event: str, **kwargs: object) -> None:
        self.events.append(("info", event, kwargs))


MACHO_64_MAGIC = b"\xcf\xfa\xed\xfe"
ELF_MAGIC = b"\x7fELF"
OFFICIAL_RUST_CODEX_MARKER = b"OpenAI Codex codex-rs 0.132.0"


def _write_official_rust_codex(path: Path, *, magic: bytes) -> Path:
    path.write_bytes(magic + b"\0" * 64 + OFFICIAL_RUST_CODEX_MARKER)
    path.chmod(0o755)
    return path


def _write_wrapper(path: Path, *, magic: bytes = MACHO_64_MAGIC) -> Path:
    path.write_bytes(magic + b"\0" * 32 + b"zeude codex-wrapper")
    path.chmod(0o755)
    return path


def _write_script(path: Path) -> Path:
    path.write_text("#!/usr/bin/env node\nconsole.log('codex')\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class TestIsWrapperBinary:
    def test_official_rust_macho_codex_is_not_wrapper(self, tmp_path: Path) -> None:
        """Native macOS OpenAI Codex Rust binaries must not be rejected as wrappers."""
        codex = _write_official_rust_codex(tmp_path / "codex", magic=MACHO_64_MAGIC)

        assert is_wrapper_binary(str(codex)) is False

    def test_official_rust_elf_codex_is_not_wrapper(self, tmp_path: Path) -> None:
        """Native Linux OpenAI Codex Rust binaries must not be rejected as wrappers."""
        codex = _write_official_rust_codex(tmp_path / "codex", magic=ELF_MAGIC)

        assert is_wrapper_binary(str(codex)) is False

    def test_known_compiled_macho_codex_wrapper_is_wrapper(self, tmp_path: Path) -> None:
        """Compiled candidates still need a wrapper-specific marker to be rejected."""
        wrapper = _write_wrapper(tmp_path / "codex-wrapper", magic=MACHO_64_MAGIC)

        assert is_wrapper_binary(str(wrapper)) is True

    def test_known_compiled_elf_codex_wrapper_is_wrapper(self, tmp_path: Path) -> None:
        """Linux compiled candidates also need a wrapper marker to be rejected."""
        wrapper = _write_wrapper(tmp_path / "codex-wrapper", magic=ELF_MAGIC)

        assert is_wrapper_binary(str(wrapper)) is True


class TestResolveCodexCliPath:
    def test_keeps_official_rust_macho_codex_without_wrapper_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS Codex Rust binaries should resolve as real CLI targets."""
        codex = _write_official_rust_codex(tmp_path / "codex", magic=MACHO_64_MAGIC)
        logger = _FakeLogger()

        monkeypatch.setenv("PATH", str(tmp_path))

        resolution = resolve_codex_cli_path(
            explicit_cli_path=None,
            configured_cli_path=None,
            logger=logger,
            log_namespace="codex_cli_runtime",
        )

        assert resolution.cli_path == str(codex)
        assert resolution.wrapper_path is None
        assert logger.events == []

    def test_keeps_official_rust_elf_codex_without_wrapper_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Linux Codex Rust binaries should resolve as real CLI targets."""
        codex = _write_official_rust_codex(tmp_path / "codex", magic=ELF_MAGIC)
        logger = _FakeLogger()

        monkeypatch.setenv("PATH", str(tmp_path))

        resolution = resolve_codex_cli_path(
            explicit_cli_path=None,
            configured_cli_path=None,
            logger=logger,
            log_namespace="codex_cli_runtime",
        )

        assert resolution.cli_path == str(codex)
        assert resolution.wrapper_path is None
        assert logger.events == []

    def test_falls_back_from_wrapper_to_real_cli(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wrapper candidates should resolve to the first real CLI on PATH."""
        wrapper = _write_wrapper(tmp_path / "codex-wrapper")
        real_dir = tmp_path / "real-bin"
        real_dir.mkdir()
        real_cli = _write_script(real_dir / "codex")
        logger = _FakeLogger()

        monkeypatch.setenv("PATH", str(real_dir))

        resolution = resolve_codex_cli_path(
            explicit_cli_path=wrapper,
            configured_cli_path=None,
            logger=logger,
            log_namespace="codex_cli_adapter",
        )

        assert resolution.cli_path == str(real_cli)
        assert resolution.wrapper_path == str(wrapper)
        assert resolution.fallback_path == str(real_cli)
        assert logger.events == [
            (
                "warning",
                "codex_cli_adapter.cli_wrapper_detected",
                {
                    "wrapper_path": str(wrapper),
                    "hint": "Searching PATH for the real Codex CLI.",
                },
            ),
            (
                "info",
                "codex_cli_adapter.cli_resolved_via_fallback",
                {"fallback_path": str(real_cli)},
            ),
        ]

    def test_keeps_wrapper_when_no_real_cli_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wrapper candidates should only pass through when no fallback exists."""
        wrapper = _write_wrapper(tmp_path / "codex-wrapper")
        logger = _FakeLogger()

        monkeypatch.setenv("PATH", "")

        resolution = resolve_codex_cli_path(
            explicit_cli_path=wrapper,
            configured_cli_path=None,
            logger=logger,
            log_namespace="codex_cli_runtime",
        )

        assert resolution.cli_path == str(wrapper)
        assert resolution.wrapper_path == str(wrapper)
        assert resolution.fallback_path is None
        assert logger.events[-1] == (
            "warning",
            "codex_cli_runtime.cli_no_fallback",
            {"wrapper_path": str(wrapper)},
        )


class TestBuildCodexChildEnv:
    def test_strips_recursive_markers_and_increments_depth(self) -> None:
        """Shared child-env policy should remove Ouroboros/Codex recursion markers."""
        env = build_codex_child_env(
            base_env={
                "OUROBOROS_AGENT_RUNTIME": "codex",
                "OUROBOROS_LLM_BACKEND": "codex",
                "CODEX_THREAD_ID": "thread-123",
                "CLAUDECODE": "1",
                "_OUROBOROS_DEPTH": "2",
                "KEEP_ME": "ok",
            },
            depth_error_factory=lambda depth, max_depth: RuntimeError(f"depth {depth}/{max_depth}"),
        )

        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env
        assert "CODEX_THREAD_ID" not in env
        assert "CLAUDECODE" not in env
        assert env["_OUROBOROS_DEPTH"] == "3"
        assert env["KEEP_ME"] == "ok"

    def test_uses_supplied_error_factory_for_depth_guard(self) -> None:
        """Callers can keep their own exception type while sharing policy."""

        class DepthExceededError(RuntimeError):
            pass

        with pytest.raises(DepthExceededError, match="depth 6/5"):
            build_codex_child_env(
                base_env={"_OUROBOROS_DEPTH": "5"},
                depth_error_factory=lambda depth, max_depth: DepthExceededError(
                    f"depth {depth}/{max_depth}"
                ),
            )
