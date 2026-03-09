"""Unit tests for cancelled execution display in TUI dashboard."""

from __future__ import annotations

from ouroboros.tui.screens.dashboard import StatusPanel
from ouroboros.tui.screens.dashboard_v3 import STATUS_ICONS


class TestStatusPanelCancelled:
    """Tests for cancelled status display in StatusPanel."""

    def test_format_status_cancelled(self) -> None:
        """Test that cancelled status has a distinct format string."""
        panel = StatusPanel()
        result = panel._format_status("cancelled")
        assert result == "[!!] Cancelled"

    def test_format_status_all_statuses_have_entries(self) -> None:
        """Test that all expected statuses have format entries."""
        panel = StatusPanel()
        expected_statuses = ["idle", "running", "paused", "completed", "failed", "cancelled"]
        for status in expected_statuses:
            result = panel._format_status(status)
            assert result != status, f"Status '{status}' should have a formatted display"

    def test_cancelled_css_class_in_default_css(self) -> None:
        """Test that the cancelled CSS class exists in StatusPanel DEFAULT_CSS."""
        assert ".status.cancelled" in StatusPanel.DEFAULT_CSS
        assert "$warning" in StatusPanel.DEFAULT_CSS  # yellow is mapped to $warning


class TestStatusIconsCancelled:
    """Tests for cancelled status icon in dashboard_v3 STATUS_ICONS."""

    def test_cancelled_icon_exists(self) -> None:
        """Test that STATUS_ICONS includes a cancelled entry."""
        assert "cancelled" in STATUS_ICONS

    def test_cancelled_icon_is_yellow(self) -> None:
        """Test that the cancelled icon uses yellow (bold yellow) styling."""
        icon = STATUS_ICONS["cancelled"]
        assert "yellow" in icon

    def test_cancelled_icon_distinct_from_others(self) -> None:
        """Test that the cancelled icon is visually distinct from other statuses."""
        cancelled_icon = STATUS_ICONS["cancelled"]
        for status, icon in STATUS_ICONS.items():
            if status != "cancelled":
                assert cancelled_icon != icon, f"Cancelled icon should differ from {status} icon"
