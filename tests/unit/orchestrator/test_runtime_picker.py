"""Tests for the cross-harness alternative-runtime picker (PR-X X0)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator import runtime_picker


@pytest.fixture
def three_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend claude, codex, and gemini are installed; nothing else."""
    installed = {
        "claude": "/usr/bin/claude",
        "codex": "/usr/bin/codex",
        "gemini": "/usr/bin/gemini",
        "opencode": None,  # not installed
    }
    monkeypatch.setattr(runtime_picker, "installed_backends", lambda: dict(installed))


class TestAvailability:
    def test_available_excludes_uninstalled(self, three_installed: None) -> None:
        assert runtime_picker.available_runtime_backends() == ("claude", "codex", "gemini")


class TestPickAlternative:
    def test_excludes_the_failed_backend(self, three_installed: None) -> None:
        result = runtime_picker.pick_alternative_runtime("claude")
        assert result is not None
        assert result != "claude"

    def test_excludes_extra_exclusions(self, three_installed: None) -> None:
        # Failed claude, already tried codex -> only gemini remains.
        result = runtime_picker.pick_alternative_runtime("claude", exclude={"codex"})
        assert result == "gemini"

    def test_returns_none_when_no_alternative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            runtime_picker, "installed_backends", lambda: {"claude": "/usr/bin/claude"}
        )
        assert runtime_picker.pick_alternative_runtime("claude") is None

    def test_deterministic_default_is_alphabetical(self, three_installed: None) -> None:
        # No weights: codex < gemini alphabetically, so codex wins over gemini.
        assert runtime_picker.pick_alternative_runtime("claude") == "codex"

    def test_weights_break_ties(self, three_installed: None) -> None:
        # Gemini outweighs codex despite losing alphabetically.
        result = runtime_picker.pick_alternative_runtime(
            "claude", weights={"codex": 0.1, "gemini": 0.9}
        )
        assert result == "gemini"

    def test_weights_never_admit_unavailable(self, three_installed: None) -> None:
        # A huge weight on an uninstalled backend must not resurrect it.
        result = runtime_picker.pick_alternative_runtime("claude", weights={"opencode": 99.0})
        assert result in {"codex", "gemini"}

    def test_equal_weights_fall_back_to_name(self, three_installed: None) -> None:
        result = runtime_picker.pick_alternative_runtime(
            "claude", weights={"codex": 0.5, "gemini": 0.5}
        )
        assert result == "codex"

    def test_alias_input_is_canonicalized(self, three_installed: None) -> None:
        # "claude_code" is an alias of claude and must still be excluded.
        result = runtime_picker.pick_alternative_runtime("claude_code")
        assert result != "claude"
