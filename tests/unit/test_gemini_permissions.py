"""Unit tests for Gemini CLI permission helpers."""

from __future__ import annotations

import pytest

from ouroboros.gemini_permissions import (
    build_gemini_exec_permission_args,
    resolve_gemini_permission_mode,
)


class TestResolveGeminiPermissionMode:
    """Tests for permission mode validation."""

    def test_valid_modes(self) -> None:
        assert resolve_gemini_permission_mode("default") == "default"
        assert resolve_gemini_permission_mode("acceptEdits") == "acceptEdits"
        assert resolve_gemini_permission_mode("bypassPermissions") == "bypassPermissions"

    def test_none_uses_default(self) -> None:
        assert resolve_gemini_permission_mode(None) == "default"

    def test_custom_default(self) -> None:
        assert resolve_gemini_permission_mode(None, default_mode="acceptEdits") == "acceptEdits"

    def test_strips_whitespace(self) -> None:
        assert resolve_gemini_permission_mode("  acceptEdits  ") == "acceptEdits"

    def test_rejects_invalid_mode(self) -> None:
        with pytest.raises(ValueError, match="Unsupported Gemini permission mode"):
            resolve_gemini_permission_mode("invalid")


class TestBuildGeminiExecPermissionArgs:
    """Tests for flag translation."""

    def test_default_mode_flags(self) -> None:
        args = build_gemini_exec_permission_args("default")
        assert "--sandbox" in args
        assert "--approval-mode" in args
        assert "default" in args

    def test_accept_edits_flags(self) -> None:
        args = build_gemini_exec_permission_args("acceptEdits")
        assert "--approval-mode" in args
        assert "auto_edit" in args
        assert "--sandbox" not in args

    def test_bypass_permissions_flags(self) -> None:
        args = build_gemini_exec_permission_args("bypassPermissions")
        assert "--yolo" in args
        assert "--sandbox" not in args
        assert "--approval-mode" not in args

    def test_none_uses_default_mode(self) -> None:
        args = build_gemini_exec_permission_args(None)
        assert "--sandbox" in args
