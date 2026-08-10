"""Windows process-liveness regressions for heartbeat leases."""

from __future__ import annotations

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
    monkeypatch.setattr(heartbeat.os, "name", "nt")
    alive = Mock(return_value=True)
    kill = Mock(side_effect=AssertionError("os.kill must not run on Windows"))
    monkeypatch.setattr(heartbeat, "_is_windows_process_alive", alive)
    monkeypatch.setattr(heartbeat.os, "kill", kill)

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
    monkeypatch.setattr(heartbeat.os, "name", "posix")
    kill = Mock(side_effect=outcome)
    monkeypatch.setattr(heartbeat.os, "kill", kill)

    assert heartbeat.is_process_identity_alive(42) is expected
    kill.assert_called_once_with(42, 0)


def test_process_identity_alive_preserves_unexpected_posix_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(heartbeat.os, "name", "posix")
    error = OSError("unexpected signal-zero failure")
    kill = Mock(side_effect=error)
    monkeypatch.setattr(heartbeat.os, "kill", kill)

    with pytest.raises(OSError, match="unexpected signal-zero failure"):
        heartbeat.is_process_identity_alive(42)

    kill.assert_called_once_with(42, 0)
