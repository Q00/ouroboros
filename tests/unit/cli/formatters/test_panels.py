"""Unit tests for Rich panel formatters."""

from io import StringIO
from unittest.mock import patch

from rich.console import Console
from rich.panel import Panel

from ouroboros.cli.formatters.panels import (
    error_panel,
    info_panel,
    print_error,
    print_info,
    print_success,
    print_warning,
    success_panel,
    warning_panel,
)


class TestInfoPanel:
    """Tests for info_panel function."""

    def test_creates_panel_instance(self) -> None:
        """Test that info_panel returns a Panel instance."""
        panel = info_panel("Test message")
        assert isinstance(panel, Panel)

    def test_panel_has_blue_border(self) -> None:
        """Test that info panel has blue border."""
        panel = info_panel("Test message")
        assert panel.border_style == "blue"

    def test_default_title(self) -> None:
        """Test that default title is 'Info'."""
        panel = info_panel("Test message")
        # Title contains formatting
        assert "Info" in str(panel.title)

    def test_custom_title(self) -> None:
        """Test that custom title is applied."""
        panel = info_panel("Test message", title="Custom")
        assert "Custom" in str(panel.title)

    def test_expand_option(self) -> None:
        """Test that expand option works."""
        panel_expanded = info_panel("Test", expand=True)
        panel_collapsed = info_panel("Test", expand=False)
        assert panel_expanded.expand is True
        assert panel_collapsed.expand is False


class TestWarningPanel:
    """Tests for warning_panel function."""

    def test_creates_panel_instance(self) -> None:
        """Test that warning_panel returns a Panel instance."""
        panel = warning_panel("Test message")
        assert isinstance(panel, Panel)

    def test_panel_has_yellow_border(self) -> None:
        """Test that warning panel has yellow border."""
        panel = warning_panel("Test message")
        assert panel.border_style == "yellow"

    def test_default_title(self) -> None:
        """Test that default title is 'Warning'."""
        panel = warning_panel("Test message")
        assert "Warning" in str(panel.title)

    def test_custom_title(self) -> None:
        """Test that custom title is applied."""
        panel = warning_panel("Test message", title="Caution")
        assert "Caution" in str(panel.title)


class TestErrorPanel:
    """Tests for error_panel function."""

    def test_creates_panel_instance(self) -> None:
        """Test that error_panel returns a Panel instance."""
        panel = error_panel("Test message")
        assert isinstance(panel, Panel)

    def test_panel_has_red_border(self) -> None:
        """Test that error panel has red border."""
        panel = error_panel("Test message")
        assert panel.border_style == "red"

    def test_default_title(self) -> None:
        """Test that default title is 'Error'."""
        panel = error_panel("Test message")
        assert "Error" in str(panel.title)

    def test_custom_title(self) -> None:
        """Test that custom title is applied."""
        panel = error_panel("Test message", title="Fatal")
        assert "Fatal" in str(panel.title)


class TestSuccessPanel:
    """Tests for success_panel function."""

    def test_creates_panel_instance(self) -> None:
        """Test that success_panel returns a Panel instance."""
        panel = success_panel("Test message")
        assert isinstance(panel, Panel)

    def test_panel_has_green_border(self) -> None:
        """Test that success panel has green border."""
        panel = success_panel("Test message")
        assert panel.border_style == "green"

    def test_default_title(self) -> None:
        """Test that default title is 'Success'."""
        panel = success_panel("Test message")
        assert "Success" in str(panel.title)

    def test_custom_title(self) -> None:
        """Test that custom title is applied."""
        panel = success_panel("Test message", title="Complete")
        assert "Complete" in str(panel.title)


class TestPrintFunctions:
    """Tests for print_* convenience functions."""

    def test_print_info_calls_console(self) -> None:
        """Test that print_info outputs to console."""
        output = StringIO()
        test_console = Console(file=output, force_terminal=True)

        with patch("ouroboros.cli.formatters.panels.console", test_console):
            print_info("Test info message")

        result = output.getvalue()
        assert "Test info message" in result

    def test_print_warning_calls_console(self) -> None:
        """Test that print_warning outputs to console."""
        output = StringIO()
        test_console = Console(file=output, force_terminal=True)

        with patch("ouroboros.cli.formatters.panels.console", test_console):
            print_warning("Test warning message")

        result = output.getvalue()
        assert "Test warning message" in result

    def test_print_error_calls_console(self) -> None:
        """Test that print_error outputs to console."""
        output = StringIO()
        test_console = Console(file=output, force_terminal=True)

        with patch("ouroboros.cli.formatters.panels.console", test_console):
            print_error("Test error message")

        result = output.getvalue()
        assert "Test error message" in result

    def test_print_success_calls_console(self) -> None:
        """Test that print_success outputs to console."""
        output = StringIO()
        test_console = Console(file=output, force_terminal=True)

        with patch("ouroboros.cli.formatters.panels.console", test_console):
            print_success("Test success message")

        result = output.getvalue()
        assert "Test success message" in result

    def test_print_info_custom_title(self) -> None:
        """Test that print_info accepts custom title."""
        output = StringIO()
        test_console = Console(file=output, force_terminal=True)

        with patch("ouroboros.cli.formatters.panels.console", test_console):
            print_info("Message", "Custom Title")

        result = output.getvalue()
        assert "Custom Title" in result
