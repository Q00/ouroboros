"""Unit tests for Rich table formatters."""

from io import StringIO

from rich.console import Console
from rich.table import Table

from ouroboros.cli.formatters.tables import (
    _get_status_style,
    create_key_value_table,
    create_status_table,
    create_table,
)


class TestCreateTable:
    """Tests for create_table function."""

    def test_creates_table_instance(self) -> None:
        """Test that create_table returns a Table instance."""
        table = create_table()
        assert isinstance(table, Table)

    def test_table_with_title(self) -> None:
        """Test that table title is set."""
        table = create_table("My Table")
        assert table.title == "My Table"

    def test_table_without_title(self) -> None:
        """Test that table without title is valid."""
        table = create_table()
        assert table.title is None

    def test_table_with_show_header_false(self) -> None:
        """Test that show_header can be disabled."""
        table = create_table(show_header=False)
        assert table.show_header is False

    def test_table_with_show_lines(self) -> None:
        """Test that show_lines can be enabled."""
        table = create_table(show_lines=True)
        assert table.show_lines is True

    def test_table_border_style(self) -> None:
        """Test that border style is applied."""
        table = create_table(border_style="green")
        assert table.border_style == "green"

    def test_default_border_style_is_blue(self) -> None:
        """Test that default border style is blue."""
        table = create_table()
        assert table.border_style == "blue"

    def test_custom_row_styles(self) -> None:
        """Test that custom row styles are applied."""
        table = create_table(row_styles=["red", "blue"])
        assert table.row_styles == ["red", "blue"]


class TestCreateKeyValueTable:
    """Tests for create_key_value_table function."""

    def test_creates_table_with_data(self) -> None:
        """Test that key-value table is populated correctly."""
        data = {"Key1": "Value1", "Key2": "Value2"}
        table = create_key_value_table(data)
        assert isinstance(table, Table)
        assert len(table.rows) == 2

    def test_table_with_title(self) -> None:
        """Test that title is set."""
        data = {"Key": "Value"}
        table = create_key_value_table(data, "Test Title")
        assert table.title == "Test Title"

    def test_empty_data(self) -> None:
        """Test that empty dict creates empty table."""
        table = create_key_value_table({})
        assert len(table.rows) == 0

    def test_non_string_values_converted(self) -> None:
        """Test that non-string values are converted."""
        data = {"Number": 42, "Bool": True, "None": None}
        table = create_key_value_table(data)
        assert len(table.rows) == 3


class TestCreateStatusTable:
    """Tests for create_status_table function."""

    def test_creates_status_table(self) -> None:
        """Test that status table is created correctly."""
        items = [
            {"name": "Task 1", "status": "complete"},
            {"name": "Task 2", "status": "running"},
        ]
        table = create_status_table(items)
        assert isinstance(table, Table)
        assert len(table.rows) == 2

    def test_table_with_title(self) -> None:
        """Test that title is set."""
        items = [{"name": "Item", "status": "ok"}]
        table = create_status_table(items, "Status Report")
        assert table.title == "Status Report"

    def test_empty_items(self) -> None:
        """Test that empty list creates empty table."""
        table = create_status_table([])
        assert len(table.rows) == 0

    def test_custom_keys(self) -> None:
        """Test that custom name/status keys work."""
        items = [{"id": "001", "state": "active"}]
        table = create_status_table(items, name_key="id", status_key="state")
        assert len(table.rows) == 1


class TestGetStatusStyle:
    """Tests for _get_status_style function."""

    def test_success_statuses(self) -> None:
        """Test success status styling."""
        success_statuses = ["success", "complete", "completed", "running", "active", "ok"]
        for status in success_statuses:
            assert _get_status_style(status) == "success"
            assert _get_status_style(status.upper()) == "success"

    def test_warning_statuses(self) -> None:
        """Test warning status styling."""
        warning_statuses = ["warning", "pending", "waiting", "paused"]
        for status in warning_statuses:
            assert _get_status_style(status) == "warning"
            assert _get_status_style(status.upper()) == "warning"

    def test_error_statuses(self) -> None:
        """Test error status styling."""
        error_statuses = ["error", "failed", "failure", "critical"]
        for status in error_statuses:
            assert _get_status_style(status) == "error"
            assert _get_status_style(status.upper()) == "error"

    def test_unknown_status(self) -> None:
        """Test that unknown status returns empty string."""
        assert _get_status_style("unknown") == ""
        assert _get_status_style("custom") == ""


class TestPrintTable:
    """Tests for print_table function."""

    def test_print_table_outputs_to_console(self) -> None:
        """Test that print_table outputs table content."""
        table = create_table("Test")
        table.add_column("Col1")
        table.add_row("Value1")

        # Use a separate console to capture output
        output = StringIO()
        test_console = Console(file=output, force_terminal=True)
        test_console.print(table)

        result = output.getvalue()
        assert "Value1" in result
