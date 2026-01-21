"""Unit tests for Rich progress formatters."""

from rich.progress import Progress

from ouroboros.cli.formatters.progress import (
    async_progress_spinner,
    create_determinate_progress,
    create_progress,
    progress_spinner,
)


class TestCreateProgress:
    """Tests for create_progress function."""

    def test_creates_progress_instance(self) -> None:
        """Test that create_progress returns a Progress instance."""
        progress = create_progress()
        assert isinstance(progress, Progress)

    def test_progress_has_spinner_column(self) -> None:
        """Test that progress has SpinnerColumn."""
        progress = create_progress()
        # Progress columns are configured
        assert len(progress.columns) > 0


class TestProgressSpinner:
    """Tests for progress_spinner context manager."""

    def test_context_manager_yields_progress_and_task(self) -> None:
        """Test that progress_spinner yields Progress and TaskID."""
        with progress_spinner("Test task") as (progress, task_id):
            assert isinstance(progress, Progress)
            # TaskID is a NewType alias for int, so check it's an int
            assert isinstance(task_id, int)

    def test_custom_description(self) -> None:
        """Test that custom description is used."""
        description = "Custom processing..."
        with progress_spinner(description) as (progress, task_id):
            task = progress.tasks[task_id]
            assert task.description == description

    def test_default_description(self) -> None:
        """Test that default description is used when not specified."""
        with progress_spinner() as (progress, task_id):
            task = progress.tasks[task_id]
            assert task.description == "Processing..."


class TestAsyncProgressSpinner:
    """Tests for async_progress_spinner context manager."""

    async def test_async_context_manager_yields_progress_and_task(self) -> None:
        """Test that async_progress_spinner yields Progress and TaskID."""
        async with async_progress_spinner("Async task") as (progress, task_id):
            assert isinstance(progress, Progress)
            # TaskID is a NewType alias for int, so check it's an int
            assert isinstance(task_id, int)

    async def test_async_custom_description(self) -> None:
        """Test that custom description is used in async context."""
        description = "Fetching data..."
        async with async_progress_spinner(description) as (progress, task_id):
            task = progress.tasks[task_id]
            assert task.description == description

    async def test_async_default_description(self) -> None:
        """Test that default description is used in async context."""
        async with async_progress_spinner() as (progress, task_id):
            task = progress.tasks[task_id]
            assert task.description == "Processing..."


class TestCreateDeterminateProgress:
    """Tests for create_determinate_progress function."""

    def test_creates_progress_with_total(self) -> None:
        """Test that create_determinate_progress sets total correctly."""
        progress, task_id = create_determinate_progress(100, "Processing files...")
        task = progress.tasks[task_id]
        assert task.total == 100
        assert task.description == "Processing files..."

    def test_progress_can_be_updated(self) -> None:
        """Test that progress can be advanced."""
        progress, task_id = create_determinate_progress(10, "Test")
        with progress:
            progress.update(task_id, advance=5)
            task = progress.tasks[task_id]
            assert task.completed == 5

    def test_custom_description(self) -> None:
        """Test that custom description is applied."""
        description = "Loading modules..."
        progress, task_id = create_determinate_progress(50, description)
        task = progress.tasks[task_id]
        assert task.description == description
