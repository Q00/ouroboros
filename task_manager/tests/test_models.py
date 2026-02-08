"""Tests for task models."""

import pytest
from datetime import datetime
from task_manager.models import Task, TaskStatus


class TestTask:
    """Tests for the Task model."""

    def test_create_task_with_title_and_description(self) -> None:
        """Test that tasks can be created with title and description."""
        task = Task(title="My Task", description="This is my task description")

        assert task.title == "My Task"
        assert task.description == "This is my task description"
        assert task.status == TaskStatus.PENDING
        assert task.id is not None
        assert isinstance(task.created_at, datetime)
        assert isinstance(task.updated_at, datetime)

    def test_create_task_with_title_only(self) -> None:
        """Test that tasks can be created with title only."""
        task = Task(title="Simple Task", description="")

        assert task.title == "Simple Task"
        assert task.description == ""

    def test_task_to_dict(self) -> None:
        """Test task serialization to dictionary."""
        task = Task(title="Test", description="Desc")
        data = task.to_dict()

        assert data["title"] == "Test"
        assert data["description"] == "Desc"
        assert data["status"] == "pending"
        assert "id" in data
        assert "created_at" in data
        assert "updated_at" in data

    def test_task_from_dict(self) -> None:
        """Test task deserialization from dictionary."""
        original = Task(title="Original", description="Original desc")
        data = original.to_dict()
        restored = Task.from_dict(data)

        assert restored.title == original.title
        assert restored.description == original.description
        assert restored.id == original.id
        assert restored.status == original.status
