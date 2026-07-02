"""Unit tests for cancelled execution display in TUI dashboard."""

from __future__ import annotations

from ouroboros.tui.screens.dashboard_v3 import STATUS_ICONS


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
