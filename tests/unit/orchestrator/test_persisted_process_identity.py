"""Versioned cross-process owner identity regressions for #1699."""

from __future__ import annotations

import os
from pathlib import Path
import platform

import pytest

from ouroboros.orchestrator import persisted_process_identity as identity_module


def _stat_payload(comm: bytes, ticks: int) -> bytes:
    """Build a proc stat row whose field 22 is ``ticks``."""
    tail = b" ".join([b"S", *[str(value).encode() for value in range(4, 22)], str(ticks).encode()])
    return b"1234 (" + comm + b") " + tail + b" 0 0\n"


def _install_linux_procfs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid: int,
    ticks: int,
    boot_id: bytes = b"11111111-2222-3333-4444-555555555555",
    comm: bytes = b"python",
) -> None:
    real_read_bytes = Path.read_bytes

    def fake_read_bytes(path: Path, *args, **kwargs):
        if str(path) == f"/proc/{pid}/stat":
            return _stat_payload(comm, ticks)
        if path == identity_module._LINUX_BOOT_ID_PATH:
            return boot_id + b"\n"
        return real_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)


def _identity(pid: int, ticks: int) -> dict[str, object]:
    return {
        "version": 1,
        "platform": "linux",
        "pid": pid,
        "boot_id": "11111111-2222-3333-4444-555555555555",
        "start_ticks": ticks,
    }


def test_linux_identity_is_stable_while_btime_drifts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh observer must never recompute identity from drifting btime."""
    pid = os.getpid()
    _install_linux_procfs(
        monkeypatch,
        pid=pid,
        ticks=4242,
        comm=b"bad-\xff (nested) name",
    )
    monkeypatch.setattr(identity_module.os, "kill", lambda _pid, _signal: None)

    real_read_text = Path.read_text
    btimes = iter(range(1_700_000_000, 1_700_000_020))

    def drifting_read_text(path: Path, *args, **kwargs):
        if str(path) == "/proc/stat":
            return f"btime {next(btimes)}\n"
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", drifting_read_text)
    identity = _identity(pid, 4242)

    for _ in range(10):
        assert (
            identity_module.persisted_process_identity_alive(
                identity,
                legacy_pid=pid,
                legacy_start_time=1_700_000_000.0,
            )
            is True
        )


def test_linux_identity_rejects_dead_process(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = 424242
    _install_linux_procfs(monkeypatch, pid=pid, ticks=12)

    def missing(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(identity_module.os, "kill", missing)

    assert (
        identity_module.persisted_process_identity_alive(
            _identity(pid, 12), legacy_pid=pid, legacy_start_time=1.0
        )
        is False
    )


def test_linux_identity_rejects_pid_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = 424242
    _install_linux_procfs(monkeypatch, pid=pid, ticks=99)
    monkeypatch.setattr(identity_module.os, "kill", lambda _pid, _signal: None)

    assert (
        identity_module.persisted_process_identity_alive(
            _identity(pid, 12), legacy_pid=pid, legacy_start_time=1.0
        )
        is False
    )


def test_linux_identity_rejects_prior_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = 424242
    _install_linux_procfs(
        monkeypatch,
        pid=pid,
        ticks=12,
        boot_id=b"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    )

    assert (
        identity_module.persisted_process_identity_alive(
            _identity(pid, 12), legacy_pid=pid, legacy_start_time=1.0
        )
        is False
    )


def test_linux_owner_pid_disagreement_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A torn redundant owner record cannot authorize recovery."""
    pid = 424242
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        identity_module.os,
        "kill",
        lambda _pid, _signal: pytest.fail("mismatched owner PID must not probe liveness"),
    )

    assert (
        identity_module.persisted_process_owner_alive(
            {
                "owner_pid": pid + 1,
                "owner_start_time": 1.0,
                "owner_identity": _identity(pid, 12),
            }
        )
        is None
    )


def test_linux_malformed_persisted_boot_id_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An arbitrary boot token is corruption, not proof of a prior boot."""
    pid = 424242
    identity = _identity(pid, 12)
    identity["boot_id"] = "corrupt-not-a-linux-boot-uuid"
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        identity_module.os,
        "kill",
        lambda _pid, _signal: pytest.fail("malformed boot ID must not probe liveness"),
    )

    assert (
        identity_module.persisted_process_owner_alive(
            {
                "owner_pid": pid,
                "owner_start_time": 1.0,
                "owner_identity": identity,
            }
        )
        is None
    )


@pytest.mark.parametrize(
    "boot_id",
    [
        b"corrupt-not-a-linux-boot-uuid",
        b"11111111222233334444555555555555",
        b"11111111-2222-3333-4444-55555555555g",
    ],
)
def test_linux_boot_id_reader_rejects_non_uuid_values(
    monkeypatch: pytest.MonkeyPatch,
    boot_id: bytes,
) -> None:
    _install_linux_procfs(monkeypatch, pid=os.getpid(), ticks=12, boot_id=boot_id)

    assert identity_module._linux_boot_id() is None
    assert identity_module.current_persisted_process_identity() is None


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {},
        {"version": 2},
        {
            "version": 1,
            "platform": "linux",
            "pid": True,
            "boot_id": "boot",
            "start_ticks": 1,
        },
    ],
)
def test_linux_legacy_or_malformed_identity_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    identity: object,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    assert (
        identity_module.persisted_process_identity_alive(
            identity,
            legacy_pid=424242,
            legacy_start_time=1.0,
        )
        is None
    )


def test_non_linux_behavior_delegates_to_legacy_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    calls: list[tuple[int, float | None]] = []

    def legacy_alive(pid: int, start_time: float | None = None) -> bool:
        calls.append((pid, start_time))
        return False

    monkeypatch.setattr(identity_module, "is_process_identity_alive", legacy_alive)

    assert identity_module.current_persisted_process_identity() is None
    assert (
        identity_module.persisted_process_identity_alive(
            None,
            legacy_pid=77,
            legacy_start_time=12.5,
        )
        is False
    )
    assert calls == [(77, 12.5)]


def test_non_linux_owner_record_preserves_legacy_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    calls: list[tuple[int, float | None]] = []

    def legacy_alive(pid: int, start_time: float | None = None) -> bool:
        calls.append((pid, start_time))
        return False

    monkeypatch.setattr(identity_module, "is_process_identity_alive", legacy_alive)

    assert (
        identity_module.persisted_process_owner_alive({"owner_pid": 77, "owner_start_time": 0.0})
        is False
    )
    assert calls == [(77, 0.0)]
