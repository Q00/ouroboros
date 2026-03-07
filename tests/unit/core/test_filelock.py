"""Unit tests for ouroboros.core.filelock module."""

import threading
from pathlib import Path

import pytest

from ouroboros.core.filelock import _file_lock, _lock_fd, _locked_fd, _unlock_fd


@pytest.fixture
def sentinel_fd(tmp_path: Path):
    """An open binary file with a sentinel byte, yielding its file descriptor."""
    path = tmp_path / "sentinel.lock"
    f = open(path, "wb+")
    f.write(b"\x00")
    f.flush()
    yield f.fileno(), f
    f.close()


class TestFileLockContextManager:
    """Tests for the _file_lock() context manager."""

    def test_creates_companion_lock_file(self, tmp_path: Path) -> None:
        """_file_lock creates a .lock companion file."""
        target = tmp_path / "state.json"
        lock_file = tmp_path / "state.json.lock"

        assert not lock_file.exists()
        with _file_lock(target):
            assert lock_file.exists()

    def test_lock_file_persists_after_release(self, tmp_path: Path) -> None:
        """Lock file remains on disk after the context manager exits (intentional)."""
        target = tmp_path / "state.json"
        lock_file = tmp_path / "state.json.lock"

        with _file_lock(target):
            pass

        assert lock_file.exists()

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """_file_lock creates missing parent directories for the lock file."""
        target = tmp_path / "deep" / "nested" / "state.json"
        with _file_lock(target):
            assert (tmp_path / "deep" / "nested" / "state.json.lock").exists()

    def test_yields_inside_context(self, tmp_path: Path) -> None:
        """Code inside the context manager runs normally."""
        target = tmp_path / "state.json"
        executed = False

        with _file_lock(target):
            executed = True

        assert executed

    def test_exclusive_lock_releases_on_exception(self, tmp_path: Path) -> None:
        """Lock is released even if an exception occurs inside the block."""
        target = tmp_path / "state.json"

        try:
            with _file_lock(target, exclusive=True):
                raise ValueError("test error")
        except ValueError:
            pass

        # Should be able to acquire the lock again
        acquired = False
        with _file_lock(target, exclusive=True):
            acquired = True
        assert acquired

    def test_shared_lock_does_not_raise(self, tmp_path: Path) -> None:
        """exclusive=False (shared lock) works without error."""
        target = tmp_path / "data.json"
        with _file_lock(target, exclusive=False):
            pass  # no error

    def test_lock_is_reacquirable_after_release(self, tmp_path: Path) -> None:
        """The same lock can be acquired multiple times sequentially."""
        target = tmp_path / "state.json"

        for _ in range(3):
            with _file_lock(target, exclusive=True):
                pass  # no error

    def test_concurrent_exclusive_locks_serialize(self, tmp_path: Path) -> None:
        """Two threads cannot hold the exclusive lock simultaneously.

        Tracks the number of threads simultaneously inside the lock.
        With correct mutual exclusion, that count must never exceed 1.
        A separate threading.Lock protects the counter itself.
        """
        import time

        target = tmp_path / "state.json"
        active_count = 0
        max_active = 0
        count_lock = threading.Lock()

        def worker() -> None:
            nonlocal active_count, max_active
            with _file_lock(target, exclusive=True):
                with count_lock:
                    active_count += 1
                    max_active = max(max_active, active_count)
                time.sleep(0.02)  # Hold lock briefly to expose any overlap
                with count_lock:
                    active_count -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert max_active == 1, (
            f"Multiple threads held the lock simultaneously (max concurrent: {max_active})"
        )


class TestLockedFdContextManager:
    """Tests for the _locked_fd() context manager."""

    def test_exclusive_lock_does_not_raise(self, sentinel_fd) -> None:
        """_locked_fd with exclusive=True acquires and releases without error."""
        fd, _ = sentinel_fd
        with _locked_fd(fd, exclusive=True):
            pass

    def test_shared_lock_does_not_raise(self, sentinel_fd) -> None:
        """_locked_fd with exclusive=False acquires and releases without error."""
        fd, _ = sentinel_fd
        with _locked_fd(fd, exclusive=False):
            pass

    def test_releases_on_exception(self, sentinel_fd, tmp_path: Path) -> None:
        """_locked_fd releases the lock even when an exception is raised."""
        fd, _ = sentinel_fd
        try:
            with _locked_fd(fd, exclusive=True):
                raise ValueError("test")
        except ValueError:
            pass

        # Lock must be re-acquirable
        with _locked_fd(fd, exclusive=True):
            pass


class TestLockFdHelpers:
    """Tests for _lock_fd() and _unlock_fd() primitives."""

    def test_exclusive_lock_and_unlock(self, sentinel_fd) -> None:
        """_lock_fd / _unlock_fd work on a real file descriptor."""
        fd, _ = sentinel_fd
        _lock_fd(fd, exclusive=True)
        _unlock_fd(fd)

    def test_shared_lock_and_unlock(self, sentinel_fd) -> None:
        """_lock_fd with exclusive=False (shared) does not raise."""
        fd, _ = sentinel_fd
        _lock_fd(fd, exclusive=False)
        _unlock_fd(fd)
