"""Windows process-liveness regressions for heartbeat leases."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ouroboros.orchestrator import heartbeat


def _install_windows_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    handle: int,
    last_error: int,
    wait_result: int = 258,
) -> SimpleNamespace:
    """Replace the Win32 boundary with a deterministic fake."""
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(return_value=handle),
        WaitForSingleObject=Mock(return_value=wait_result),
        CloseHandle=Mock(return_value=True),
    )
    api = SimpleNamespace(
        WinDLL=Mock(return_value=kernel32),
        get_last_error=Mock(return_value=last_error),
    )
    monkeypatch.setattr(heartbeat, "ctypes", api)
    return kernel32


@pytest.mark.parametrize(
    ("wait_result", "expected"),
    [
        (258, True),
        (0, False),
        (0xFFFFFFFF, True),
        (1, True),
    ],
    ids=["running", "terminated", "wait-failed", "unknown-wait-result"],
)
def test_windows_process_liveness_uses_wait_result_and_closes_handle(
    monkeypatch: pytest.MonkeyPatch,
    wait_result: int,
    expected: bool,
) -> None:
    kernel32 = _install_windows_api(
        monkeypatch,
        handle=1234,
        last_error=0,
        wait_result=wait_result,
    )

    assert heartbeat._is_windows_process_alive(42) is expected
    kernel32.OpenProcess.assert_called_once_with(0x101000, False, 42)
    kernel32.WaitForSingleObject.assert_called_once_with(1234, 0)
    kernel32.CloseHandle.assert_called_once_with(1234)


@pytest.mark.parametrize(
    ("last_error", "expected"),
    [
        (87, False),
        (5, True),
        (123, True),
    ],
    ids=["invalid-pid", "access-denied", "unexpected-open-process-error"],
)
def test_windows_process_liveness_handles_open_process_failures(
    monkeypatch: pytest.MonkeyPatch,
    last_error: int,
    expected: bool,
) -> None:
    kernel32 = _install_windows_api(monkeypatch, handle=0, last_error=last_error)

    assert heartbeat._is_windows_process_alive(42) is expected
    kernel32.OpenProcess.assert_called_once_with(0x101000, False, 42)
    kernel32.WaitForSingleObject.assert_not_called()
    kernel32.CloseHandle.assert_not_called()


def test_windows_process_liveness_preserves_lease_when_open_process_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _install_windows_api(monkeypatch, handle=0, last_error=0)
    kernel32.OpenProcess.side_effect = OSError("Win32 boundary unavailable")

    assert heartbeat._is_windows_process_alive(42) is True
    kernel32.WaitForSingleObject.assert_not_called()
    kernel32.CloseHandle.assert_not_called()


def test_windows_process_liveness_closes_handle_when_wait_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = _install_windows_api(monkeypatch, handle=1234, last_error=0)
    kernel32.WaitForSingleObject.side_effect = OSError("wait failed")

    assert heartbeat._is_windows_process_alive(42) is True

    kernel32.CloseHandle.assert_called_once_with(1234)


def test_process_identity_alive_uses_windows_api_instead_of_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alive = Mock(return_value=True)
    kill = Mock(side_effect=AssertionError("os.kill must not run on Windows"))
    monkeypatch.setattr(heartbeat, "_is_windows_process_alive", alive)
    monkeypatch.setattr(heartbeat, "os", SimpleNamespace(name="nt", kill=kill))

    assert heartbeat.is_process_identity_alive(42) is True
    alive.assert_called_once_with(42)
    kill.assert_not_called()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (None, True),
        (ProcessLookupError(), False),
        (PermissionError(), True),
    ],
)
def test_process_identity_alive_preserves_posix_kill_behavior(
    monkeypatch: pytest.MonkeyPatch,
    outcome: BaseException | None,
    expected: bool,
) -> None:
    kill = Mock(side_effect=outcome)
    monkeypatch.setattr(heartbeat, "os", SimpleNamespace(name="posix", kill=kill))

    assert heartbeat.is_process_identity_alive(42) is expected
    kill.assert_called_once_with(42, 0)


_HEARTBEAT_CHILD = """
import errno
import json
import os
from pathlib import Path
from queue import Queue
import sys
from threading import Thread

from ouroboros.orchestrator import heartbeat

source = Path(sys.argv[1]).resolve()
assert Path(heartbeat.__file__).resolve() == source / "ouroboros/orchestrator/heartbeat.py"
heartbeat.LOCK_DIR = Path(sys.argv[2])
session_id = "native-release-handoff"
commands = Queue()

def read_commands():
    for line in sys.stdin:
        commands.put(line.strip())
    commands.put("exit")

def reply(**payload):
    print(json.dumps(payload), flush=True)

Thread(target=read_commands, daemon=True).start()
reply(event="ready", pid=os.getpid())
try:
    while True:
        command = commands.get(timeout=30)
        if command == "exit":
            break
        if command == "acquire":
            heartbeat.acquire(session_id)
            descriptor = heartbeat._HELD_LEASE_FDS[session_id]
            os.fstat(descriptor)
            reply(event="acquired", pid=os.getpid())
        elif command == "try-acquire":
            try:
                heartbeat.acquire(session_id)
            except OSError:
                path = heartbeat.lock_path(session_id)
                reply(event="denied", held=session_id in heartbeat._HELD_LEASE_FDS,
                      owner=path.read_text().split(":", 1)[0])
            else:
                reply(event="unexpectedly-acquired")
        elif command == "release":
            descriptor = heartbeat._HELD_LEASE_FDS.get(session_id)
            owned = heartbeat.release_if_owned_by_current_process(session_id)
            closed = None
            if descriptor is not None:
                try:
                    os.fstat(descriptor)
                except OSError as exc:
                    assert exc.errno == errno.EBADF
                    closed = True
                else:
                    closed = False
            path = heartbeat.lock_path(session_id)
            reply(event="released", owned=owned, closed=closed,
                  exists=path.exists(), held=session_id in heartbeat._HELD_LEASE_FDS,
                  owner=path.read_text().split(":", 1)[0] if path.exists() else None)
        else:
            raise AssertionError(command)
finally:
    heartbeat.release_if_owned_by_current_process(session_id)
"""


def _child_reply(process: subprocess.Popen[str]) -> dict[str, object]:
    """Read one protocol line with a deadline, including on Windows pipes."""
    stdout = process.stdout
    assert stdout is not None
    replies: Queue[str] = Queue()
    reader = Thread(target=lambda: replies.put(stdout.readline()), daemon=True)
    reader.start()
    try:
        line = replies.get(timeout=30)
    except Empty:
        pytest.fail(f"Heartbeat child {process.pid} did not reply within 30 seconds")
    reader.join(timeout=1)
    assert line, f"Heartbeat child {process.pid} exited without a reply"
    payload = json.loads(line)
    assert isinstance(payload, dict), line
    return payload


def _child_command(process: subprocess.Popen[str], command: str) -> dict[str, object]:
    assert process.stdin is not None
    # One short command is sent only after the previous reply, so the pipe cannot fill.
    process.stdin.write(command + "\n")
    process.stdin.flush()
    return _child_reply(process)


@contextmanager
def _heartbeat_child(lock_dir: Path) -> Iterator[tuple[subprocess.Popen[str], int]]:
    source = Path(__file__).resolve().parents[3] / "src"
    env = {**os.environ, "PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen(
        [sys.executable, "-u", "-B", "-c", _HEARTBEAT_CHILD, str(source), str(lock_dir)],
        cwd=source.parent,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        ready = _child_reply(process)
        assert ready["event"] == "ready"
        owner_pid = ready["pid"]
        assert isinstance(owner_pid, int) and owner_pid > 0
        # Windows virtualenvs can launch a redirector with a different Popen PID.
        yield process, owner_pid
    finally:
        assert process.stdin is not None
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        assert process.stdout is not None
        process.stdout.close()


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows file sharing")
def test_windows_release_allows_other_processes_to_reacquire_before_and_after_owner_exit(
    tmp_path: Path,
) -> None:
    """A released live owner cannot block or remove a successor's real lease."""
    with _heartbeat_child(tmp_path) as (original, original_pid):
        assert _child_command(original, "acquire")["event"] == "acquired"
        with _heartbeat_child(tmp_path) as (successor, successor_pid):
            assert _child_command(successor, "try-acquire") == {
                "event": "denied",
                "held": False,
                "owner": str(original_pid),
            }
            assert _child_command(successor, "release") == {
                "event": "released",
                "owned": False,
                "closed": None,
                "exists": True,
                "held": False,
                "owner": str(original_pid),
            }
            assert _child_command(original, "release") == {
                "event": "released",
                "owned": True,
                "closed": True,
                "exists": False,
                "held": False,
                "owner": None,
            }
            assert original.poll() is None
            assert _child_command(successor, "acquire")["event"] == "acquired"
            assert _child_command(original, "release") == {
                "event": "released",
                "owned": False,
                "closed": None,
                "exists": True,
                "held": False,
                "owner": str(successor_pid),
            }
            assert _child_command(successor, "release")["closed"] is True
            assert not (tmp_path / "native-release-handoff").exists()
        assert successor.returncode == 0
        assert original.poll() is None
    assert original.returncode == 0

    with _heartbeat_child(tmp_path) as (after_exit, _):
        assert _child_command(after_exit, "acquire")["event"] == "acquired"
        assert _child_command(after_exit, "release")["closed"] is True
        assert not (tmp_path / "native-release-handoff").exists()
    assert after_exit.returncode == 0


def test_process_identity_alive_preserves_unexpected_posix_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OSError("unexpected signal-zero failure")
    kill = Mock(side_effect=error)
    monkeypatch.setattr(heartbeat, "os", SimpleNamespace(name="posix", kill=kill))

    with pytest.raises(OSError, match="unexpected signal-zero failure"):
        heartbeat.is_process_identity_alive(42)

    kill.assert_called_once_with(42, 0)


def _release_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str,
    close_error: OSError | None = None,
    unlink_error: OSError | None = None,
    unlock_error: OSError | None = None,
) -> SimpleNamespace:
    """Model resource ownership without changing the interpreter's global os module."""
    events: list[str] = []
    descriptor = 73

    def close(fd: int) -> None:
        assert fd == descriptor
        events.append("close")
        if close_error is not None:
            raise close_error

    def unlink(*, missing_ok: bool) -> None:
        assert missing_ok is True
        events.append("unlink")
        if unlink_error is not None:
            raise unlink_error

    def unlock(fd: int, operation: int) -> None:
        assert (fd, operation) == (descriptor, 8)
        events.append("unlock")
        if unlock_error is not None:
            raise unlock_error

    path = Mock(unlink=Mock(side_effect=unlink))
    path.name = "release-test"
    release_path = Mock(unlink=Mock(side_effect=unlink))
    path.with_name.return_value = release_path

    def replace(target: object) -> object:
        assert target is release_path
        events.append("replace")
        return target

    path.replace.side_effect = replace
    os_api = SimpleNamespace(name=platform, close=Mock(side_effect=close))
    fcntl_api = SimpleNamespace(LOCK_UN=8, flock=Mock(side_effect=unlock))
    descriptors = {"release-test": descriptor}
    monkeypatch.setattr(heartbeat, "os", os_api)
    monkeypatch.setattr(heartbeat, "fcntl", fcntl_api if platform == "posix" else None)
    monkeypatch.setattr(heartbeat, "lock_path", Mock(return_value=path))
    monkeypatch.setattr(heartbeat, "_HELD_LEASE_FDS", descriptors)
    return SimpleNamespace(
        events=events,
        path=path,
        release_path=release_path,
        os=os_api,
        fcntl=fcntl_api,
        descriptors=descriptors,
    )


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("nt", ["close", "replace", "unlink"]), ("posix", ["unlink", "unlock", "close"])],
)
def test_release_preserves_platform_resource_order(
    monkeypatch: pytest.MonkeyPatch, platform: str, expected: list[str]
) -> None:
    state = _release_state(monkeypatch, platform=platform)

    heartbeat.release("release-test")

    assert state.events == expected
    state.os.close.assert_called_once_with(73)
    assert state.descriptors == {}


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_release_attempts_close_only_once_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    platform: str,
) -> None:
    state = _release_state(monkeypatch, platform=platform, close_error=OSError("close failed"))

    with caplog.at_level(logging.WARNING):
        heartbeat.release("release-test")

    state.os.close.assert_called_once_with(73)
    unlink = state.release_path.unlink if platform == "nt" else state.path.unlink
    unlink.assert_called_once_with(missing_ok=True)
    assert state.descriptors == {}
    assert any(
        record.levelno >= logging.WARNING
        and getattr(record, "session_id", None) == "release-test"
        and getattr(record, "operation", None) == "close"
        for record in caplog.records
    )


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_release_closes_descriptor_and_warns_when_unlink_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    platform: str,
) -> None:
    state = _release_state(
        monkeypatch, platform=platform, unlink_error=PermissionError("unlink denied")
    )

    with caplog.at_level(logging.INFO):
        heartbeat.release("release-test")

    state.os.close.assert_called_once_with(73)
    assert state.descriptors == {}
    assert any(
        record.levelno >= logging.WARNING
        and getattr(record, "session_id", None) == "release-test"
        and getattr(record, "operation", None) == "unlink"
        for record in caplog.records
    )
    assert all(record.getMessage() != "session_lock.released" for record in caplog.records)


def test_windows_release_retries_transient_rename_sharing_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _release_state(monkeypatch, platform="nt")
    state.path.replace = Mock(
        side_effect=[PermissionError("sharing violation"), state.release_path]
    )
    sleep = Mock()
    monkeypatch.setattr(heartbeat.time, "sleep", sleep)

    heartbeat.release("release-test")

    assert state.events == ["close", "unlink"]
    assert state.path.replace.call_count == 2
    state.release_path.unlink.assert_called_once_with(missing_ok=True)
    sleep.assert_called_once_with(0.01)


def test_windows_release_reports_only_rename_when_all_move_attempts_fail(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _release_state(monkeypatch, platform="nt")
    state.path.replace = Mock(side_effect=PermissionError("rename denied"))
    sleep = Mock()
    monkeypatch.setattr(heartbeat.time, "sleep", sleep)

    with caplog.at_level(logging.WARNING):
        heartbeat.release("release-test")

    assert state.path.replace.call_count == 3
    assert sleep.call_count == 2
    state.path.unlink.assert_not_called()
    state.release_path.unlink.assert_not_called()
    operations = [
        getattr(record, "operation", None)
        for record in caplog.records
        if record.getMessage() == "session_lock.release_failed"
    ]
    assert operations == ["rename"]


def test_windows_release_never_unlinks_the_successor_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _release_state(monkeypatch, platform="nt")

    heartbeat.release("release-test")

    assert state.events == ["close", "replace", "unlink"]
    state.path.unlink.assert_not_called()
    state.release_path.unlink.assert_called_once_with(missing_ok=True)


def test_posix_release_still_closes_descriptor_when_unlock_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = _release_state(monkeypatch, platform="posix", unlock_error=OSError("unlock failed"))

    with caplog.at_level(logging.WARNING):
        heartbeat.release("release-test")

    assert state.events == ["unlink", "unlock", "close"]
    state.os.close.assert_called_once_with(73)
    assert state.descriptors == {}
    assert any(
        getattr(record, "session_id", None) == "release-test"
        and getattr(record, "operation", None) == "unlock"
        for record in caplog.records
    )


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_repeated_release_never_closes_a_consumed_descriptor(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    state = _release_state(monkeypatch, platform=platform)

    heartbeat.release("release-test")
    heartbeat.release("release-test")

    state.os.close.assert_called_once_with(73)
    if platform == "nt":
        state.release_path.unlink.assert_called_once_with(missing_ok=True)
        state.path.unlink.assert_called_once_with(missing_ok=True)
    else:
        assert state.path.unlink.call_count == 2
    assert state.descriptors == {}


def test_release_of_missing_lease_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(heartbeat, "LOCK_DIR", tmp_path)
    monkeypatch.setattr(heartbeat, "_HELD_LEASE_FDS", {})

    heartbeat.release("missing-release")
    heartbeat.release("missing-release")
    assert heartbeat.release_if_owned_by_current_process("missing-release") is False
    assert not heartbeat.lock_path("missing-release").exists()


def test_owned_release_wrapper_preserves_foreign_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(heartbeat, "LOCK_DIR", tmp_path)
    monkeypatch.setattr(heartbeat, "_HELD_LEASE_FDS", {})
    path = heartbeat.lock_path("foreign-release")
    foreign_payload = f"{os.getpid() + 1}:0"
    path.write_text(foreign_payload)

    assert heartbeat.release_if_owned_by_current_process("foreign-release") is False
    assert path.read_text() == foreign_payload


def test_owned_release_wrapper_returns_ownership_match_even_when_unlink_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _release_state(
        monkeypatch, platform="nt", unlink_error=PermissionError("unlink denied")
    )
    monkeypatch.setattr(heartbeat, "is_owned_by_current_process", Mock(return_value=True))

    assert heartbeat.release_if_owned_by_current_process("release-test") is True

    state.os.close.assert_called_once_with(73)
    state.release_path.unlink.assert_called_once_with(missing_ok=True)
    assert state.descriptors == {}
