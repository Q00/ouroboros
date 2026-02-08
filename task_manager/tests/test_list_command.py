"""Tests for the task manager list command with status filtering."""

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from task_manager.cli import app
from task_manager.models import TaskStatus
from task_manager.storage import TaskStorage


runner = CliRunner()


class TestListCommand:
    """Tests for the list command."""

    @pytest.fixture(autouse=True)
    def setup_temp_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set up temporary storage for each test."""
        self.temp_dir = tempfile.mkdtemp()
        temp_path = Path(self.temp_dir) / "tasks.json"
        self.temp_storage = TaskStorage(temp_path)
        monkeypatch.setattr("task_manager.cli.storage", self.temp_storage)

    def test_list_empty(self) -> None:
        """Test listing when no tasks exist."""
        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "No tasks found." in result.stdout

    def test_list_all_tasks(self) -> None:
        """Test listing all tasks without filtering."""
        # Create multiple tasks
        runner.invoke(app, ["create", "Task One"])
        runner.invoke(app, ["create", "Task Two"])
        runner.invoke(app, ["create", "Task Three"])

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "Task One" in result.stdout
        assert "Task Two" in result.stdout
        assert "Task Three" in result.stdout
        assert "Tasks" in result.stdout  # Table title

    def test_list_filter_by_pending_status(self) -> None:
        """Test listing tasks filtered by pending status."""
        # Create a task (default status is pending)
        runner.invoke(app, ["create", "Pending Task"])

        result = runner.invoke(app, ["list", "--status", "pending"])

        assert result.exit_code == 0
        assert "Pending Task" in result.stdout
        assert "pending" in result.stdout

    def test_list_filter_by_status_short_option(self) -> None:
        """Test listing tasks using short -s option."""
        runner.invoke(app, ["create", "My Task"])

        result = runner.invoke(app, ["list", "-s", "pending"])

        assert result.exit_code == 0
        assert "My Task" in result.stdout

    def test_list_filter_no_matching_status(self) -> None:
        """Test listing with a status filter that matches no tasks."""
        # Create a task (default status is pending)
        runner.invoke(app, ["create", "Pending Task"])

        # Filter by completed (no tasks should match)
        result = runner.invoke(app, ["list", "--status", "completed"])

        assert result.exit_code == 0
        assert "No tasks found." in result.stdout

    def test_list_filter_by_in_progress_status(self) -> None:
        """Test filtering by in_progress status."""
        # Create a task
        runner.invoke(app, ["create", "Progress Task"])

        # Update it to in_progress
        tasks = self.temp_storage.get_all()
        task = tasks[0]
        task.status = TaskStatus.IN_PROGRESS
        self.temp_storage.update(task)

        # Filter by in_progress
        result = runner.invoke(app, ["list", "--status", "in_progress"])

        assert result.exit_code == 0
        assert "Progress Task" in result.stdout
        assert "in_progress" in result.stdout

    def test_list_filter_by_completed_status(self) -> None:
        """Test filtering by completed status."""
        # Create a task
        runner.invoke(app, ["create", "Done Task"])

        # Update it to completed
        tasks = self.temp_storage.get_all()
        task = tasks[0]
        task.status = TaskStatus.COMPLETED
        self.temp_storage.update(task)

        # Filter by completed
        result = runner.invoke(app, ["list", "--status", "completed"])

        assert result.exit_code == 0
        assert "Done Task" in result.stdout
        assert "completed" in result.stdout

    def test_list_with_mixed_statuses(self) -> None:
        """Test listing shows all statuses when no filter applied."""
        # Create tasks with different statuses
        runner.invoke(app, ["create", "Pending Task"])
        runner.invoke(app, ["create", "In Progress Task"])
        runner.invoke(app, ["create", "Completed Task"])

        tasks = self.temp_storage.get_all()
        tasks[1].status = TaskStatus.IN_PROGRESS
        tasks[2].status = TaskStatus.COMPLETED
        self.temp_storage.update(tasks[1])
        self.temp_storage.update(tasks[2])

        # List all (no filter)
        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "Pending Task" in result.stdout
        assert "In Progress Task" in result.stdout
        assert "Completed Task" in result.stdout

    def test_list_filter_excludes_non_matching(self) -> None:
        """Test that filtered list excludes non-matching tasks."""
        # Create tasks with different statuses
        runner.invoke(app, ["create", "Pending Task"])
        runner.invoke(app, ["create", "Completed Task"])

        tasks = self.temp_storage.get_all()
        tasks[1].status = TaskStatus.COMPLETED
        self.temp_storage.update(tasks[1])

        # Filter by pending - should NOT show completed
        result = runner.invoke(app, ["list", "--status", "pending"])

        assert result.exit_code == 0
        assert "Pending Task" in result.stdout
        assert "Completed Task" not in result.stdout

    def test_list_shows_table_columns(self) -> None:
        """Test that list command shows table with correct columns."""
        runner.invoke(app, ["create", "Test Task", "-d", "Test description"])

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        # Check that table contains expected content
        assert "Test Task" in result.stdout
        assert "Test description" in result.stdout
        assert "pending" in result.stdout

    def test_list_invalid_status(self) -> None:
        """Test listing with an invalid status value."""
        result = runner.invoke(app, ["list", "--status", "invalid"])

        # Typer should handle invalid enum values
        assert result.exit_code != 0

    def test_list_filter_only_shows_matching_status(self) -> None:
        """Test that filter shows only tasks with the exact matching status."""
        # Create tasks with all three statuses
        runner.invoke(app, ["create", "Task A"])
        runner.invoke(app, ["create", "Task B"])
        runner.invoke(app, ["create", "Task C"])

        tasks = self.temp_storage.get_all()
        # Keep Task A as pending (default)
        tasks[1].status = TaskStatus.IN_PROGRESS
        tasks[2].status = TaskStatus.COMPLETED
        self.temp_storage.update(tasks[1])
        self.temp_storage.update(tasks[2])

        # Filter by in_progress
        result = runner.invoke(app, ["list", "--status", "in_progress"])

        assert result.exit_code == 0
        assert "Task B" in result.stdout
        assert "Task A" not in result.stdout
        assert "Task C" not in result.stdout

    def test_list_with_descriptions(self) -> None:
        """Test that list shows task descriptions."""
        runner.invoke(app, ["create", "My Task", "-d", "A short description"])

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "My Task" in result.stdout
        assert "A short description" in result.stdout

    def test_list_truncates_long_descriptions(self) -> None:
        """Test that list truncates descriptions longer than 40 chars."""
        long_desc = "This is a very long description that exceeds forty characters"
        runner.invoke(app, ["create", "Long Desc Task", "-d", long_desc])

        result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "Long Desc Task" in result.stdout
        # The description should be truncated with "..."
        assert "..." in result.stdout
