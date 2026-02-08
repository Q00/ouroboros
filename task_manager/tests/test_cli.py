"""Tests for the task manager CLI."""

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from task_manager.cli import app
from task_manager.storage import TaskStorage


runner = CliRunner()


class TestCreateCommand:
    """Tests for the create command."""

    @pytest.fixture(autouse=True)
    def setup_temp_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set up temporary storage for each test."""
        self.temp_dir = tempfile.mkdtemp()
        temp_path = Path(self.temp_dir) / "tasks.json"
        temp_storage = TaskStorage(temp_path)
        monkeypatch.setattr("task_manager.cli.storage", temp_storage)

    def test_create_task_with_title(self) -> None:
        """Test creating a task with just a title."""
        result = runner.invoke(app, ["create", "My New Task"])

        assert result.exit_code == 0
        assert "Task created successfully!" in result.stdout
        assert "My New Task" in result.stdout

    def test_create_task_with_title_and_description(self) -> None:
        """Test creating a task with title and description."""
        result = runner.invoke(
            app, ["create", "Important Task", "-d", "This is very important"]
        )

        assert result.exit_code == 0
        assert "Task created successfully!" in result.stdout
        assert "Important Task" in result.stdout
        assert "This is very important" in result.stdout

    def test_create_task_with_long_description(self) -> None:
        """Test creating a task with a long description."""
        long_desc = "This is a very detailed description " * 10
        result = runner.invoke(
            app, ["create", "Detailed Task", "--description", long_desc]
        )

        assert result.exit_code == 0
        assert "Task created successfully!" in result.stdout


class TestDeleteCommand:
    """Tests for the delete command."""

    @pytest.fixture(autouse=True)
    def setup_temp_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set up temporary storage for each test."""
        self.temp_dir = tempfile.mkdtemp()
        temp_path = Path(self.temp_dir) / "tasks.json"
        self.temp_storage = TaskStorage(temp_path)
        monkeypatch.setattr("task_manager.cli.storage", self.temp_storage)

    def test_delete_task_with_force(self) -> None:
        """Test deleting a task with --force flag (skips confirmation)."""
        # First create a task
        result = runner.invoke(app, ["create", "Task to Delete"])
        assert result.exit_code == 0

        # Get the task ID from the output
        tasks = self.temp_storage.get_all()
        assert len(tasks) == 1
        task_id = tasks[0].id

        # Delete with --force
        result = runner.invoke(app, ["delete", task_id[:8], "--force"])

        assert result.exit_code == 0
        assert "Task deleted successfully!" in result.stdout

        # Verify it's deleted
        tasks = self.temp_storage.get_all()
        assert len(tasks) == 0

    def test_delete_task_with_confirmation(self) -> None:
        """Test deleting a task with confirmation prompt."""
        # First create a task
        result = runner.invoke(app, ["create", "Task to Delete"])
        assert result.exit_code == 0

        tasks = self.temp_storage.get_all()
        task_id = tasks[0].id

        # Delete with confirmation (input 'y')
        result = runner.invoke(app, ["delete", task_id[:8]], input="y\n")

        assert result.exit_code == 0
        assert "Task deleted successfully!" in result.stdout

        # Verify it's deleted
        tasks = self.temp_storage.get_all()
        assert len(tasks) == 0

    def test_delete_task_cancel_confirmation(self) -> None:
        """Test cancelling the delete confirmation."""
        # First create a task
        result = runner.invoke(app, ["create", "Task to Keep"])
        assert result.exit_code == 0

        tasks = self.temp_storage.get_all()
        task_id = tasks[0].id

        # Delete but cancel (input 'n')
        result = runner.invoke(app, ["delete", task_id[:8]], input="n\n")

        assert result.exit_code == 0
        assert "Cancelled" in result.stdout

        # Verify task is still there
        tasks = self.temp_storage.get_all()
        assert len(tasks) == 1

    def test_delete_nonexistent_task(self) -> None:
        """Test deleting a task that doesn't exist."""
        result = runner.invoke(app, ["delete", "nonexistent", "--force"])

        assert result.exit_code == 1
        assert "Task not found" in result.stdout

    def test_delete_with_partial_id(self) -> None:
        """Test deleting a task using partial ID."""
        # First create a task
        result = runner.invoke(app, ["create", "Partial ID Task"])
        assert result.exit_code == 0

        tasks = self.temp_storage.get_all()
        task_id = tasks[0].id

        # Delete using only the first 4 characters
        result = runner.invoke(app, ["delete", task_id[:4], "--force"])

        assert result.exit_code == 0
        assert "Task deleted successfully!" in result.stdout

    def test_delete_multiple_matching_tasks(self) -> None:
        """Test that deleting with ambiguous ID shows error."""
        # Create two tasks with mock UUIDs that share a prefix
        from task_manager.models import Task

        task1 = Task(title="Task 1", description="First task")
        task2 = Task(title="Task 2", description="Second task")

        # Manually set IDs with same prefix to simulate ambiguity
        task1.id = "abc12345-1111-1111-1111-111111111111"
        task2.id = "abc12345-2222-2222-2222-222222222222"

        self.temp_storage.create(task1)
        self.temp_storage.create(task2)

        # Try to delete with ambiguous prefix
        result = runner.invoke(app, ["delete", "abc12345", "--force"])

        assert result.exit_code == 1
        assert "Multiple tasks match" in result.stdout


class TestCompleteCommand:
    """Tests for the complete command."""

    @pytest.fixture(autouse=True)
    def setup_temp_storage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set up temporary storage for each test."""
        self.temp_dir = tempfile.mkdtemp()
        temp_path = Path(self.temp_dir) / "tasks.json"
        self.temp_storage = TaskStorage(temp_path)
        monkeypatch.setattr("task_manager.cli.storage", self.temp_storage)

    def test_complete_task(self) -> None:
        """Test marking a task as complete."""
        # First create a task
        result = runner.invoke(app, ["create", "Task to Complete"])
        assert result.exit_code == 0

        # Get the task ID from the created task
        tasks = self.temp_storage.get_all()
        assert len(tasks) == 1
        task_id = tasks[0].id

        # Complete the task
        result = runner.invoke(app, ["complete", task_id[:8]])

        assert result.exit_code == 0
        assert "Task completed!" in result.stdout
        assert "Task to Complete" in result.stdout

        # Verify the task is marked as completed
        updated_task = self.temp_storage.get_by_id(task_id)
        assert updated_task is not None
        assert updated_task.status.value == "completed"

    def test_complete_task_partial_id(self) -> None:
        """Test completing a task with partial ID."""
        # Create a task
        result = runner.invoke(app, ["create", "Another Task"])
        assert result.exit_code == 0

        tasks = self.temp_storage.get_all()
        task_id = tasks[0].id

        # Complete using just the first 4 characters
        result = runner.invoke(app, ["complete", task_id[:4]])

        assert result.exit_code == 0
        assert "Task completed!" in result.stdout

    def test_complete_nonexistent_task(self) -> None:
        """Test completing a task that doesn't exist."""
        result = runner.invoke(app, ["complete", "nonexistent123"])

        assert result.exit_code == 1
        assert "Task not found" in result.stdout

    def test_complete_already_completed_task(self) -> None:
        """Test completing an already completed task."""
        # Create and complete a task
        result = runner.invoke(app, ["create", "Already Done"])
        assert result.exit_code == 0

        tasks = self.temp_storage.get_all()
        task_id = tasks[0].id

        # Complete the task
        result = runner.invoke(app, ["complete", task_id[:8]])
        assert result.exit_code == 0

        # Try to complete again
        result = runner.invoke(app, ["complete", task_id[:8]])

        assert result.exit_code == 0
        assert "already marked as complete" in result.stdout

    def test_complete_multiple_matching_tasks_error(self) -> None:
        """Test completing with ambiguous ID shows error."""
        from task_manager.models import Task

        # Create two tasks with same ID prefix
        task1 = Task(title="Task 1", description="First task")
        task2 = Task(title="Task 2", description="Second task")

        # Manually set IDs with same prefix to simulate ambiguity
        task1.id = "xyz98765-1111-1111-1111-111111111111"
        task2.id = "xyz98765-2222-2222-2222-222222222222"

        self.temp_storage.create(task1)
        self.temp_storage.create(task2)

        # Try to complete with ambiguous prefix
        result = runner.invoke(app, ["complete", "xyz98765"])

        assert result.exit_code == 1
        assert "Multiple tasks match" in result.stdout
