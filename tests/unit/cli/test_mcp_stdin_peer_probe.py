"""Tests for the stdin dead-peer probe and the extracted orphan watchdog loop.

Production zombies (2026-08-31) held a socketpair stdin whose peer was gone
(lsof ``->(none)``) without ever delivering a readline EOF, so the serve loop
outlived its client indefinitely. The probe detects exactly that state; the
watchdog loop turns it — and parent-death signals — into a stop request.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys

import pytest

from ouroboros.cli.commands import mcp as mcp_module

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the probe is POSIX-only by design")


class TestMakeStdinPeerProbe:
    def test_live_peer_reports_alive(self) -> None:
        ours, theirs = socket.socketpair()
        try:
            probe = mcp_module._make_stdin_peer_probe(ours.fileno())
            assert probe is not None
            assert probe() is False
        finally:
            ours.close()
            theirs.close()

    def test_dead_peer_is_detected(self) -> None:
        ours, theirs = socket.socketpair()
        try:
            probe = mcp_module._make_stdin_peer_probe(ours.fileno())
            assert probe is not None
            theirs.close()
            assert probe() is True
        finally:
            ours.close()

    def test_peek_does_not_consume_protocol_bytes(self) -> None:
        ours, theirs = socket.socketpair()
        try:
            probe = mcp_module._make_stdin_peer_probe(ours.fileno())
            assert probe is not None
            theirs.sendall(b"x")
            assert probe() is False, "queued bytes mean the peer was alive to send them"
            assert ours.recv(1) == b"x", "MSG_PEEK must leave the byte in the queue"
        finally:
            ours.close()
            theirs.close()

    def test_probe_survives_fd_diversion(self) -> None:
        """The probe watches a duplicate, immune to later dup2 games on the fd.

        mcp>=2.0 diverts fd 0 to /dev/null while serving; the probe must keep
        watching the original wire regardless.
        """
        ours, theirs = socket.socketpair()
        devnull = os.open(os.devnull, os.O_RDONLY)
        # Sacrifice a duplicate as the "fd 0" stand-in so real stdin is safe.
        stand_in = os.dup(ours.fileno())
        try:
            probe = mcp_module._make_stdin_peer_probe(stand_in)
            assert probe is not None
            os.dup2(devnull, stand_in)  # the SDK's diversion
            assert probe() is False
            theirs.close()
            assert probe() is True
        finally:
            os.close(stand_in)
            os.close(devnull)
            ours.close()

    def test_non_socket_stdin_returns_none(self, tmp_path) -> None:
        read_fd, write_fd = os.pipe()
        try:
            assert mcp_module._make_stdin_peer_probe(read_fd) is None
        finally:
            os.close(read_fd)
            os.close(write_fd)
        plain = os.open(tmp_path / "plain", os.O_CREAT | os.O_RDWR)
        try:
            assert mcp_module._make_stdin_peer_probe(plain) is None
        finally:
            os.close(plain)

    def test_invalid_fd_returns_none(self) -> None:
        assert mcp_module._make_stdin_peer_probe(999_999) is None


class TestOrphanWatchdogLoop:
    @pytest.mark.asyncio
    async def test_reparented_to_init_stops_server(self, monkeypatch) -> None:
        """getppid() drifting to 1 (parent died) must request a stop."""
        monkeypatch.setattr(mcp_module.os, "getppid", lambda: 1)
        stop = asyncio.Event()
        await asyncio.wait_for(
            mcp_module._orphan_watchdog_loop(
                stop=stop,
                orig_ppid=4242,
                client_identity=None,
                stdin_peer_dead=None,
                poll_seconds=0.05,
            ),
            timeout=5.0,
        )
        assert stop.is_set()

    @pytest.mark.asyncio
    async def test_dead_stdin_peer_stops_server(self, monkeypatch) -> None:
        current_ppid = os.getppid()
        monkeypatch.setattr(mcp_module.os, "getppid", lambda: current_ppid)
        stop = asyncio.Event()
        await asyncio.wait_for(
            mcp_module._orphan_watchdog_loop(
                stop=stop,
                orig_ppid=current_ppid,
                client_identity=None,
                stdin_peer_dead=lambda: True,
                poll_seconds=0.05,
            ),
            timeout=5.0,
        )
        assert stop.is_set()

    @pytest.mark.asyncio
    async def test_detached_start_with_probe_is_still_watched(self) -> None:
        """orig_ppid == 1 no longer disables the watchdog when a wire exists.

        A stdio server spawned already-detached (the launchd-orphan zombie
        case) still has its stdin socket to watch; a dead peer must stop it.
        """
        stop = asyncio.Event()
        await asyncio.wait_for(
            mcp_module._orphan_watchdog_loop(
                stop=stop,
                orig_ppid=1,
                client_identity=None,
                stdin_peer_dead=lambda: True,
                poll_seconds=0.05,
            ),
            timeout=5.0,
        )
        assert stop.is_set()

    @pytest.mark.asyncio
    async def test_detached_start_without_probe_never_self_terminates(self) -> None:
        """A deliberate service (ppid 1, no stdio wire) keeps no tether."""
        stop = asyncio.Event()
        await asyncio.wait_for(
            mcp_module._orphan_watchdog_loop(
                stop=stop,
                orig_ppid=1,
                client_identity=None,
                stdin_peer_dead=None,
                poll_seconds=0.05,
            ),
            timeout=5.0,
        )
        assert not stop.is_set()

    @pytest.mark.asyncio
    async def test_live_peer_keeps_polling_until_stop(self, monkeypatch) -> None:
        current_ppid = os.getppid()
        monkeypatch.setattr(mcp_module.os, "getppid", lambda: current_ppid)
        stop = asyncio.Event()
        task = asyncio.create_task(
            mcp_module._orphan_watchdog_loop(
                stop=stop,
                orig_ppid=current_ppid,
                client_identity=None,
                stdin_peer_dead=lambda: False,
                poll_seconds=0.05,
            )
        )
        await asyncio.sleep(0.2)
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


class TestFlushAndHardExit:
    def test_flushes_before_exit(self, monkeypatch) -> None:
        calls: list[tuple[str, int | None]] = []
        monkeypatch.setattr(mcp_module.sys.stdout, "flush", lambda: calls.append(("flush", None)))
        monkeypatch.setattr(mcp_module.os, "_exit", lambda code: calls.append(("exit", code)))
        mcp_module._flush_and_hard_exit(0)
        assert ("exit", 0) in calls
        assert calls.index(("flush", None)) < calls.index(("exit", 0))
