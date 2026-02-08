"""Tests for task storage."""

import pytest
import tempfile
from pathlib import Path

from task_manager.models import Task
from task_manager.storage import TaskStorage


class TestTaskStorage:
    """Tests for the TaskStorage class."""

    @pytest.fixture
    def temp_storage(self) -> TaskStorage:
        """Create a temporary storage for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = TaskStorage(Path(tmpdir) / "tasks.json")
            yield storage

    def test_create_task(self, temp_storage: TaskStorage) -> None:
        """Test creating a task in storage."""
        task = Task(title="Test Task", description="Test description")
        created = temp_storage.create(task)

        assert created.title == task.title
        assert created.description == task.description

        # Verify it's persisted
        tasks = temp_storage.get_all()
        assert len(tasks) == 1
        assert tasks[0].title == "Test Task"

    def test_get_by_id(self, temp_storage: TaskStorage) -> None:
        """Test retrieving a task by ID."""
        task = Task(title="Find Me", description="I can be found")
        temp_storage.create(task)

        found = temp_storage.get_by_id(task.id)
        assert found is not None
        assert found.title == "Find Me"

    def test_get_by_id_not_found(self, temp_storage: TaskStorage) -> None:
        """Test retrieving a non-existent task."""
        found = temp_storage.get_by_id("non-existent-id")
        assert found is None

    def test_delete_task(self, temp_storage: TaskStorage) -> None:
        """Test deleting a task from storage."""
        task = Task(title="Delete Me", description="I will be deleted")
        temp_storage.create(task)

        # Verify the task exists
        tasks = temp_storage.get_all()
        assert len(tasks) == 1

        # Delete the task
        result = temp_storage.delete(task.id)
        assert result is True

        # Verify it's gone
        tasks = temp_storage.get_all()
        assert len(tasks) == 0

    def test_delete_task_not_found(self, temp_storage: TaskStorage) -> None:
        """Test deleting a non-existent task returns False."""
        result = temp_storage.delete("non-existent-id")
        assert result is False

    def test_delete_one_of_multiple_tasks(self, temp_storage: TaskStorage) -> None:
        """Test deleting one task doesn't affect others."""
        task1 = Task(title="Keep Me", description="I should remain")
        task2 = Task(title="Delete Me", description="I will be deleted")
        task3 = Task(title="Keep Me Too", description="I should also remain")
        temp_storage.create(task1)
        temp_storage.create(task2)
        temp_storage.create(task3)

        # Delete the middle task
        result = temp_storage.delete(task2.id)
        assert result is True

        # Verify only the deleted task is gone
        tasks = temp_storage.get_all()
        assert len(tasks) == 2
        task_ids = [t.id for t in tasks]
        assert task1.id in task_ids
        assert task2.id not in task_ids
        assert task3.id in task_ids
