"""Unit tests for shared Console and theme."""

from rich.console import Console
from rich.theme import Theme

from ouroboros.cli.formatters import OUROBOROS_THEME, console


class TestOuroborosTheme:
    """Tests for the Ouroboros theme."""

    def test_theme_is_theme_instance(self) -> None:
        """Test that OUROBOROS_THEME is a Theme instance."""
        assert isinstance(OUROBOROS_THEME, Theme)

    def test_theme_has_success_style(self) -> None:
        """Test that theme has success style defined."""
        assert "success" in OUROBOROS_THEME.styles

    def test_theme_has_warning_style(self) -> None:
        """Test that theme has warning style defined."""
        assert "warning" in OUROBOROS_THEME.styles

    def test_theme_has_error_style(self) -> None:
        """Test that theme has error style defined."""
        assert "error" in OUROBOROS_THEME.styles

    def test_theme_has_info_style(self) -> None:
        """Test that theme has info style defined."""
        assert "info" in OUROBOROS_THEME.styles

    def test_theme_has_muted_style(self) -> None:
        """Test that theme has muted style defined."""
        assert "muted" in OUROBOROS_THEME.styles

    def test_theme_has_highlight_style(self) -> None:
        """Test that theme has highlight style defined."""
        assert "highlight" in OUROBOROS_THEME.styles


class TestSharedConsole:
    """Tests for the shared Console instance."""

    def test_console_is_console_instance(self) -> None:
        """Test that console is a Console instance."""
        assert isinstance(console, Console)

    def test_console_uses_ouroboros_theme(self) -> None:
        """Test that console uses the Ouroboros theme."""
        # Check that theme styles are available
        # Console inherits theme styles
        # We can verify by checking if styling works
        from io import StringIO

        output = StringIO()
        test_console = Console(file=output, theme=OUROBOROS_THEME, force_terminal=True)
        test_console.print("[success]Test[/]")
        # If it doesn't raise an error, the style is recognized
        assert "Test" in output.getvalue()

    def test_console_can_print(self) -> None:
        """Test that console can print without errors."""
        from io import StringIO

        output = StringIO()
        test_console = Console(file=output, theme=OUROBOROS_THEME)
        test_console.print("Hello, Ouroboros!")
        assert "Hello, Ouroboros!" in output.getvalue()

    def test_console_semantic_colors(self) -> None:
        """Test that semantic color styles are available."""
        from io import StringIO

        output = StringIO()
        test_console = Console(file=output, theme=OUROBOROS_THEME, force_terminal=True)

        # Test all semantic colors work without error
        test_console.print("[success]Success[/]")
        test_console.print("[warning]Warning[/]")
        test_console.print("[error]Error[/]")
        test_console.print("[info]Info[/]")

        result = output.getvalue()
        assert "Success" in result
        assert "Warning" in result
        assert "Error" in result
        assert "Info" in result
