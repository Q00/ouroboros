"""Tests for the per-instance MCP server PID registry and client resolution.

The registry replaces the single-slot ``mcp-server.pid`` file, which was
last-writer-wins under the routine N-concurrent-servers steady state: any
exiting server deleted whichever record was written last, and the kill-advice
built on it could target a healthy server owned by a live session.
"""

from __future__ import annotations

import os

import pytest

from ouroboros.cli.commands import mcp


@pytest.fixture
def registry(tmp_path, monkeypatch):
    registry_dir = tmp_path / "mcp-servers"
    monkeypatch.setattr(mcp, "_PID_REGISTRY_DIR", registry_dir)
    monkeypatch.setattr(mcp, "_LEGACY_PID_FILE", tmp_path / "mcp-server.pid")
    monkeypatch.setattr(mcp, "_own_pid_file", None)
    monkeypatch.setattr(mcp, "_own_pid_payload", None)
    return registry_dir


class TestPidRegistry:
    def test_write_creates_own_record(self, registry):
        assert mcp._write_pid_file() is True
        record = registry / f"{os.getpid()}.pid"
        assert record.exists()
        parsed = mcp._parse_pid_record(record.read_text(encoding="utf-8"))
        assert parsed is not None
        assert parsed[0] == os.getpid()

    def test_cleanup_removes_only_own_record(self, registry):
        mcp._write_pid_file()
        peer = registry / "99999999.pid"
        peer.write_text("99999999 1700000000.0", encoding="utf-8")

        mcp._cleanup_pid_file()

        assert not (registry / f"{os.getpid()}.pid").exists()
        assert peer.exists()

    def test_cleanup_compare_and_delete_spares_recycled_record(self, registry):
        mcp._write_pid_file()
        record = registry / f"{os.getpid()}.pid"
        # Simulate a successor (recycled pid) re-registering after a sweep.
        record.write_text(f"{os.getpid()} 1.0", encoding="utf-8")

        mcp._cleanup_pid_file()

        assert record.exists(), "another instance's record must never be deleted"

    def test_cleanup_is_idempotent_without_write(self, registry):
        mcp._cleanup_pid_file()  # no record written — must be a no-op

    def test_sweep_removes_dead_records_keeps_live(self, registry, monkeypatch):
        registry.mkdir(parents=True)
        live = registry / "111.pid"
        live.write_text("111 1700000000.0", encoding="utf-8")
        dead = registry / "222.pid"
        dead.write_text("222 1700000000.0", encoding="utf-8")
        unparseable = registry / "garbage.pid"
        unparseable.write_text("not-a-record", encoding="utf-8")

        monkeypatch.setattr(
            mcp,
            "is_process_identity_alive",
            lambda pid, _start_time=None: pid == 111,
        )

        removed = mcp._sweep_stale_instances()

        assert removed == 2
        assert live.exists()
        assert not dead.exists()
        assert not unparseable.exists()

    def test_sweep_removes_stale_legacy_single_slot_file(self, registry, monkeypatch):
        legacy = mcp._LEGACY_PID_FILE
        legacy.write_text("31337", encoding="utf-8")
        monkeypatch.setattr(mcp, "is_process_identity_alive", lambda _pid, _start_time=None: False)

        removed = mcp._sweep_stale_instances()

        assert removed == 1
        assert not legacy.exists()

    def test_sweep_keeps_live_legacy_file(self, registry, monkeypatch):
        legacy = mcp._LEGACY_PID_FILE
        legacy.write_text("31337", encoding="utf-8")
        monkeypatch.setattr(mcp, "is_process_identity_alive", lambda _pid, _start_time=None: True)

        removed = mcp._sweep_stale_instances()

        assert removed == 0
        assert legacy.exists()

    def test_live_instances_lists_only_alive(self, registry, monkeypatch):
        registry.mkdir(parents=True)
        (registry / "111.pid").write_text("111 None", encoding="utf-8")
        (registry / "222.pid").write_text("222 None", encoding="utf-8")
        monkeypatch.setattr(
            mcp,
            "is_process_identity_alive",
            lambda pid, _start_time=None: pid == 222,
        )

        assert mcp._live_instances() == [222]

    def test_record_is_stale_treats_windows_oserror_as_stale(self, monkeypatch):
        def raise_oserror(_pid, _start_time=None):
            raise OSError(87, "signal 0 unsupported")

        monkeypatch.setattr(mcp, "is_process_identity_alive", raise_oserror)
        assert mcp._record_is_stale(123, None) is True


class TestResolveClientIdentity:
    """The watchdog must watch the real client, not the uvx wrapper.

    Shipped topology: ``claude -> uv tool uvx ... -> python ouroboros mcp
    serve``. The wrapper blocks on waitpid() and survives the client's death,
    so a getppid()-only watchdog can never fire there.
    """

    def test_walks_past_uvx_wrapper_to_client(self, monkeypatch):
        monkeypatch.delenv("OUROBOROS_CLIENT_PID", raising=False)
        tree = {
            100: {"comm": "/Users/u/.local/bin/uv", "ppid": "50"},
            50: {"comm": "claude", "ppid": "1"},
        }

        def fake_ps(pid, column):
            entry = tree.get(pid)
            return entry[column] if entry else None

        monkeypatch.setattr(mcp, "_ps_value", fake_ps)
        monkeypatch.setattr(mcp, "process_start_time", lambda _pid: 1700000000.0)

        resolved = mcp._resolve_client_identity(100)

        assert resolved == (50, 1700000000.0)

    def test_direct_spawn_returns_parent(self, monkeypatch):
        monkeypatch.delenv("OUROBOROS_CLIENT_PID", raising=False)
        monkeypatch.setattr(
            mcp, "_ps_value", lambda _pid, column: {"comm": "codex", "ppid": "1"}[column]
        )
        monkeypatch.setattr(mcp, "process_start_time", lambda _pid: None)

        resolved = mcp._resolve_client_identity(77)

        assert resolved == (77, None)

    def test_chain_dead_ending_at_pid1_returns_none(self, monkeypatch):
        monkeypatch.delenv("OUROBOROS_CLIENT_PID", raising=False)
        tree = {100: {"comm": "uvx", "ppid": "1"}}

        def fake_ps(pid, column):
            entry = tree.get(pid)
            return entry[column] if entry else None

        monkeypatch.setattr(mcp, "_ps_value", fake_ps)

        assert mcp._resolve_client_identity(100) is None

    def test_ps_failure_returns_none(self, monkeypatch):
        monkeypatch.delenv("OUROBOROS_CLIENT_PID", raising=False)
        monkeypatch.setattr(mcp, "_ps_value", lambda _pid, _column: None)

        assert mcp._resolve_client_identity(100) is None

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("OUROBOROS_CLIENT_PID", "4242")
        monkeypatch.setattr(mcp, "process_start_time", lambda _pid: 5.0)
        monkeypatch.setattr(
            mcp,
            "_ps_value",
            lambda _pid, _column: pytest.fail("override must skip the ancestor walk"),
        )

        assert mcp._resolve_client_identity(100) == (4242, 5.0)


class TestClientIsAlive:
    def test_defunct_zombie_client_counts_as_dead(self, monkeypatch):
        """kill(pid, 0) succeeds on an unreaped zombie; ps stat 'Z' must catch it."""
        monkeypatch.setattr(mcp, "is_process_identity_alive", lambda _pid, _start=None: True)
        monkeypatch.setattr(mcp, "_ps_value", lambda _pid, _column: "Z+")
        assert mcp._client_is_alive(123, None) is False

    def test_live_client_stays_alive(self, monkeypatch):
        monkeypatch.setattr(mcp, "is_process_identity_alive", lambda _pid, _start=None: True)
        monkeypatch.setattr(mcp, "_ps_value", lambda _pid, _column: "S+")
        assert mcp._client_is_alive(123, None) is True

    def test_dead_identity_short_circuits(self, monkeypatch):
        monkeypatch.setattr(mcp, "is_process_identity_alive", lambda _pid, _start=None: False)
        monkeypatch.setattr(
            mcp, "_ps_value", lambda _pid, _column: pytest.fail("must short-circuit")
        )
        assert mcp._client_is_alive(123, None) is False
