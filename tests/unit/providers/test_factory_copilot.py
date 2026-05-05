"""Tests for Copilot CLI integration into the LLM provider factory."""

from __future__ import annotations

import os

import pytest

from ouroboros.providers.copilot_cli_adapter import CopilotCliLLMAdapter
from ouroboros.providers.factory import (
    create_llm_adapter,
    resolve_llm_backend,
    resolve_llm_permission_mode,
)


class TestResolveBackendAliases:
    @pytest.mark.parametrize("alias", ["copilot", "copilot_cli", "COPILOT", " Copilot_CLI "])
    def test_aliases_normalize_to_copilot(self, alias: str) -> None:
        assert resolve_llm_backend(alias) == "copilot"


class TestPermissionMode:
    def test_interview_use_case_bypasses_for_copilot(self) -> None:
        mode = resolve_llm_permission_mode("copilot", use_case="interview")
        assert mode == "bypassPermissions"


class TestCreateAdapter:
    def test_returns_copilot_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Avoid touching real config / PATH discovery during construction.
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_runtime_profile",
            lambda: None,
        )
        monkeypatch.setattr(
            "ouroboros.providers.factory.get_llm_permission_mode",
            lambda backend=None: "default",  # noqa: ARG005
        )
        adapter = create_llm_adapter(backend="copilot", cwd=os.getcwd())
        assert isinstance(adapter, CopilotCliLLMAdapter)
        assert adapter._cwd == os.getcwd()
