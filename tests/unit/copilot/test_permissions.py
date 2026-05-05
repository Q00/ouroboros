"""Tests for the GitHub Copilot CLI permission-flag translation table."""

from __future__ import annotations

import pytest

from ouroboros.copilot_permissions import (
    build_copilot_exec_args_for_sandbox,
    build_copilot_exec_permission_args,
    resolve_copilot_permission_mode,
)
from ouroboros.sandbox import SandboxClass


class TestResolveCopilotPermissionMode:
    def test_default_when_none(self) -> None:
        assert resolve_copilot_permission_mode(None) == "default"

    @pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypassPermissions"])
    def test_passes_valid_modes(self, mode: str) -> None:
        assert resolve_copilot_permission_mode(mode) == mode

    def test_rejects_unknown_mode(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Copilot permission mode"):
            resolve_copilot_permission_mode("nonsense")

    def test_respects_explicit_default_mode(self) -> None:
        assert (
            resolve_copilot_permission_mode(None, default_mode="acceptEdits") == "acceptEdits"
        )


class TestBuildCopilotExecArgsForSandbox:
    def test_read_only_uses_empty_allowlist(self) -> None:
        args = build_copilot_exec_args_for_sandbox(SandboxClass.READ_ONLY)
        assert args == ["--available-tools="]

    def test_workspace_write_skips_prompts(self) -> None:
        args = build_copilot_exec_args_for_sandbox(SandboxClass.WORKSPACE_WRITE)
        assert args == ["--allow-all-tools"]

    def test_unrestricted_uses_allow_all(self) -> None:
        args = build_copilot_exec_args_for_sandbox(SandboxClass.UNRESTRICTED)
        assert args == ["--allow-all"]

    def test_returns_a_copy(self) -> None:
        a = build_copilot_exec_args_for_sandbox(SandboxClass.READ_ONLY)
        a.append("--mutated")
        b = build_copilot_exec_args_for_sandbox(SandboxClass.READ_ONLY)
        assert "--mutated" not in b


class TestBuildCopilotExecPermissionArgs:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            ("default", ["--available-tools="]),
            ("acceptEdits", ["--allow-all-tools"]),
            ("bypassPermissions", ["--allow-all"]),
        ],
    )
    def test_string_to_flags(self, mode: str, expected: list[str]) -> None:
        assert build_copilot_exec_permission_args(mode) == expected

    def test_none_falls_back_to_default(self) -> None:
        assert build_copilot_exec_permission_args(None) == ["--available-tools="]
