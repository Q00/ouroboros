"""Fresh-start execution preferences honor the persistent default policy (#1733)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ouroboros.mcp.tools.execution_handlers import _resolve_execution_preferences_request

_CONFIG_TARGET = "ouroboros.mcp.tools.execution_handlers.default_execution_efficiency_mode"


class TestFreshStartDefaultPolicy:
    def test_config_default_fills_omitted_arguments(self) -> None:
        """A configured quality_first policy resolves omitted arguments to
        quality_first/off and flows into the raw efficiency value the runner
        receives."""
        with patch(_CONFIG_TARGET, return_value="quality_first"):
            preferences, raw_efficiency, raw_assurance = _resolve_execution_preferences_request(
                {}, is_resume=False
            )

        assert preferences.efficiency_mode.value == "quality_first"
        assert preferences.frugality_assurance.value == "off"
        assert preferences.frugality_assurance_explicit is False
        assert raw_efficiency == "quality_first"
        assert raw_assurance is None

    def test_config_efficient_maps_to_adaptive_observe(self) -> None:
        with patch(_CONFIG_TARGET, return_value="adaptive"):
            preferences, raw_efficiency, _raw_assurance = _resolve_execution_preferences_request(
                {}, is_resume=False
            )

        assert preferences.efficiency_mode.value == "adaptive"
        assert preferences.frugality_assurance.value == "observe"
        assert preferences.frugality_assurance_explicit is False
        assert raw_efficiency == "adaptive"

    def test_explicit_argument_beats_the_configured_default(self) -> None:
        with patch(_CONFIG_TARGET, return_value="quality_first"):
            preferences, raw_efficiency, _raw_assurance = _resolve_execution_preferences_request(
                {"efficiency_mode": "adaptive"}, is_resume=False
            )

        assert preferences.efficiency_mode.value == "adaptive"
        assert preferences.frugality_assurance.value == "observe"
        assert raw_efficiency == "adaptive"

    def test_explicit_assurance_composes_with_the_configured_mode(self) -> None:
        """An explicitly supplied assurance stays explicit — the configured
        default supplies only the efficiency mode, so strict remains a
        deliberate opt-in."""
        with patch(_CONFIG_TARGET, return_value="adaptive"):
            preferences, raw_efficiency, raw_assurance = _resolve_execution_preferences_request(
                {"frugality_assurance": "strict"}, is_resume=False
            )

        assert preferences.efficiency_mode.value == "adaptive"
        assert preferences.frugality_assurance.value == "strict"
        assert preferences.frugality_assurance_explicit is True
        assert raw_efficiency == "adaptive"
        assert raw_assurance == "strict"

    def test_ask_or_unset_config_preserves_the_engine_default(self) -> None:
        with patch(_CONFIG_TARGET, return_value=None):
            preferences, raw_efficiency, _raw_assurance = _resolve_execution_preferences_request(
                {}, is_resume=False
            )

        assert preferences.efficiency_mode.value == "adaptive"
        assert preferences.frugality_assurance.value == "observe"
        assert raw_efficiency is None

    def test_resume_never_consults_the_configured_default(self) -> None:
        """A resumed session keeps its persisted immutable contract; the
        persistent policy is not even read."""
        config_reader = MagicMock(return_value="quality_first")
        with patch(_CONFIG_TARGET, config_reader):
            preferences, raw_efficiency, _raw_assurance = _resolve_execution_preferences_request(
                {}, is_resume=True
            )

        config_reader.assert_not_called()
        assert preferences.efficiency_mode.value == "adaptive"
        assert raw_efficiency is None

    def test_resume_still_rejects_supplied_arguments(self) -> None:
        with (
            patch(_CONFIG_TARGET, return_value="quality_first"),
            pytest.raises(ValueError, match="cannot be changed on resume"),
        ):
            _resolve_execution_preferences_request({"efficiency_mode": "adaptive"}, is_resume=True)
