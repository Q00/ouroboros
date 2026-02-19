"""JSON-based storage for tasks.

This module provides a simple file-based storage system using JSON.
No external database is required.
"""

import json
from pathlib import Path
from typing import Any

from .models import Task


class TaskStorage:
    """Manages task persistence using a JSON file.

    Attributes:
        file_path: Path to the JSON file for storing tasks.
    """

    DEFAULT_FILE = Path.home() / ".task_manager" / "tasks.json"

    def __init__(self, file_path: Path | None = None) -> None:
        """Initialize the storage with a file path.

        Args:
            file_path: Optional path to the JSON file. Defaults to ~/.task_manager/tasks.json
        """
        self.file_path = file_path or self.DEFAULT_FILE
        self._ensure_storage_exists()

    def _ensure_storage_exists(self) -> None:
        """Ensure the storage directory and file exist."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self._write_data({"tasks": []})

    def _read_data(self) -> dict[str, Any]:
        """Read data from the JSON file."""
        with open(self.file_path, encoding="utf-8") as f:
            return json.load(f)

    def _write_data(self, data: dict[str, Any]) -> None:
        """Write data to the JSON file."""
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def create(self, task: Task) -> Task:
        """Create a new task in storage.

        Args:
            task: The task to create.

        Returns:
            The created task.
        """
        data = self._read_data()
        data["tasks"].append(task.to_dict())
        self._write_data(data)
        return task

    def get_all(self) -> list[Task]:
        """Get all tasks from storage.

        Returns:
            List of all tasks.
        """
        data = self._read_data()
        return [Task.from_dict(t) for t in data["tasks"]]

    def get_by_id(self, task_id: str) -> Task | None:
        """Get a task by its ID.

        Args:
            task_id: The ID of the task to retrieve.

        Returns:
            The task if found, None otherwise.
        """
        tasks = self.get_all()
        for task in tasks:
            if task.id == task_id:
                return task
        return None

    def update(self, task: Task) -> Task | None:
        """Update an existing task.

        Args:
            task: The task with updated values.

        Returns:
            The updated task if found, None otherwise.
        """
        data = self._read_data()
        for i, t in enumerate(data["tasks"]):
            if t["id"] == task.id:
                data["tasks"][i] = task.to_dict()
                self._write_data(data)
                return task
        return None

    def delete(self, task_id: str) -> bool:
        """Delete a task by its ID.

        Args:
            task_id: The ID of the task to delete.

        Returns:
            True if the task was deleted, False otherwise.
        """
        data = self._read_data()
        original_length = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]
        if len(data["tasks"]) < original_length:
            self._write_data(data)
            return True
        return False
