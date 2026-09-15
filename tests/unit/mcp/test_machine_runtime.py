"""Focused safety and behavior tests for bounded runtime metadata."""

from __future__ import annotations

import os
from pathlib import Path
import socket
from unittest.mock import MagicMock, patch


class _RuntimeModuleProxy:
    """Delay the runtime module import until a selected runtime test executes."""

    @staticmethod
    def _module():
        from ouroboros.mcp import machine_runtime

        return machine_runtime

    def __getattr__(self, name: str):
        return getattr(self._module(), name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._module(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._module(), name)


runtime = _RuntimeModuleProxy()


def test_path_is_bounded_and_reports_collisions_without_raw_path(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        executable = directory / "ouroboros"
        executable.write_text("sentinel", encoding="utf-8")
        executable.chmod(0o755)

    facts = runtime.collect_path_facts(os.pathsep.join((str(first), str(second), "x" * 100)))

    assert [candidate.path for candidate in facts.candidates[:2]] == [
        str(first / "ouroboros"),
        str(second / "ouroboros"),
    ]
    assert facts.collisions == {"ouroboros": (str(first / "ouroboros"), str(second / "ouroboros"))}
    assert facts.characters_seen <= runtime.MAX_PATH_CHARS


def test_path_absent_is_empty(monkeypatch):
    monkeypatch.delenv("PATH", raising=False)

    facts = runtime.collect_path_facts()

    assert facts.candidates == ()
    assert facts.truncated is False
    assert facts.status == "not_checked"
    assert facts.reason == "missing"


def test_absent_path_does_not_scan_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    executable = tmp_path / "ouroboros"
    executable.write_text("PRIVATE_SENTINEL", encoding="utf-8")
    executable.chmod(0o755)

    facts = runtime.collect_path_facts("")

    assert facts.candidates == ()
    assert facts.status == "not_checked"
    assert facts.reason == "missing"


def test_duplicate_and_symlink_alias_do_not_create_collisions(tmp_path):
    executable = tmp_path / "ouroboros"
    executable.write_text("unused", encoding="utf-8")
    executable.chmod(0o755)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)

    facts = runtime.collect_path_facts(os.pathsep.join([str(tmp_path), str(tmp_path), str(alias)]))

    assert len(facts.candidates) == 1
    assert facts.collisions == {}


def test_shared_target_preserves_each_command_namespace(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    for executable in [first / "python", second / "python3"]:
        executable.write_text("unused", encoding="utf-8")
        executable.chmod(0o755)
    (first / "python3").symlink_to(first / "python")

    facts = runtime.collect_path_facts(os.pathsep.join([str(first), str(second)]))

    assert facts.collisions["python3"] == (str(first / "python3"), str(second / "python3"))


def test_windows_names_and_separator_are_synthetic(tmp_path):
    executable = tmp_path / "ouroboros.exe"
    executable.write_text("unused", encoding="utf-8")
    executable.chmod(0o755)

    with patch.object(runtime.sys, "platform", "win32"):
        facts = runtime.collect_path_facts(str(tmp_path) + ";" + str(tmp_path))

    assert [candidate.name for candidate in facts.candidates] == ["ouroboros.exe"]
    assert facts.collisions == {}


def test_path_limits_discard_partial_component(tmp_path):
    with patch.object(runtime, "MAX_PATH_CHARS", 3):
        facts = runtime.collect_path_facts("abcdef")
    assert facts.truncated
    assert facts.candidates == ()
    assert facts.entries_seen == 0

    with patch.object(runtime, "MAX_PATH_ENTRIES", 2):
        facts = runtime.collect_path_facts(os.pathsep.join([str(tmp_path)] * 5))
    assert facts.entries_seen == 2
    assert facts.truncated


def test_loopback_binds_ephemeral_ports_and_closes_sockets():
    probes = runtime.probe_loopback()

    assert probes[0].family == "ipv4"
    assert probes[0].status in {"available", "unavailable", "not_checked"}
    if probes[0].status == "available":
        assert probes[0].port and probes[0].port > 0


def test_ipv6_unsupported_is_explicitly_not_checked():
    with patch.object(
        runtime.socket, "socket", side_effect=OSError(runtime.errno.EAFNOSUPPORT, "sentinel")
    ):
        probe = runtime._probe(socket.AF_INET6, "ipv6")

    assert probe.status == "not_checked"
    assert probe.port is None
    assert probe.reason == "unsupported"


def test_loopback_permission_denied_still_closes_socket():
    sock = MagicMock()
    sock.bind.side_effect = PermissionError(runtime.errno.EACCES, "PRIVATE_SENTINEL")
    with patch.object(runtime.socket, "socket", return_value=sock):
        probe = runtime._probe(socket.AF_INET, "ipv4")

    assert probe.reason == "permission_denied"
    assert probe.status == "unavailable"
    assert "PRIVATE_SENTINEL" not in str(probe)
    sock.close.assert_called_once()


def test_loopback_unavailable_does_not_claim_probe_was_skipped():
    with patch.object(
        runtime.socket, "socket", side_effect=OSError(runtime.errno.EADDRNOTAVAIL, "sentinel")
    ):
        probe = runtime._probe(socket.AF_INET, "ipv4")

    assert probe.status == "unavailable"
    assert probe.reason == "unavailable"
    assert probe.port is None


def test_registry_skips_symlinks_malformed_and_oversized(tmp_path):
    (tmp_path / "123.pid").write_bytes(b"opaque")
    (tmp_path / "bad.pid").write_bytes(b"opaque")
    (tmp_path / ("9" * 100 + ".pid")).write_bytes(b"opaque")
    (tmp_path / "456.pid").write_bytes(b"x" * (runtime.MAX_REGISTRY_RECORD_BYTES + 1))
    (tmp_path / "link.pid").symlink_to(tmp_path / "123.pid")

    facts = runtime.collect_registry_facts(tmp_path)

    assert [record.pid for record in facts.records] == [123]
    assert facts.records[0].identity_verified is False
    assert facts.records[0].liveness == "not_checked"


def test_registry_metadata_never_reads_pid_record_contents(tmp_path):
    (tmp_path / "123.pid").write_text("PRIVATE_PROCESS_ARGUMENTS", encoding="utf-8")

    with patch.object(Path, "read_text", side_effect=AssertionError("must not read PID records")):
        facts = runtime.collect_registry_facts(tmp_path)

    assert [record.pid for record in facts.records] == [123]
    assert "PRIVATE_PROCESS_ARGUMENTS" not in str(facts)


def test_registry_permission_error_is_not_checked(tmp_path):
    with patch.object(runtime.os, "scandir", side_effect=PermissionError):
        facts = runtime.collect_registry_facts(tmp_path)

    assert facts.status == "not_checked"
    assert facts.records == ()


def test_registry_scan_is_bounded(tmp_path):
    for index in range(runtime.MAX_REGISTRY_ENTRIES + 2):
        (tmp_path / f"{index + 1}.pid").write_bytes(b"x")

    facts = runtime.collect_registry_facts(tmp_path)

    assert facts.entries_seen == runtime.MAX_REGISTRY_ENTRIES
    assert facts.truncated is True
    assert len(facts.records) == runtime.MAX_REGISTRY_ENTRIES


def test_registry_root_symlink_is_not_followed(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "123.pid").write_text("PRIVATE_CONTENT", encoding="utf-8")
    root = tmp_path / "mcp-servers"
    root.symlink_to(target, target_is_directory=True)

    facts = runtime.collect_registry_facts(root)

    assert facts.status == "not_checked"
    assert facts.reason == "symlink"
    assert facts.records == ()


def test_registry_scan_uses_open_directory_handle(tmp_path):
    (tmp_path / "123.pid").write_bytes(b"opaque")
    with patch.object(runtime.os, "scandir", wraps=runtime.os.scandir) as scandir:
        facts = runtime.collect_registry_facts(tmp_path)

    assert [record.pid for record in facts.records] == [123]
    assert isinstance(scandir.call_args.args[0], int)


def test_snapshot_has_no_privacy_sensitive_fields(tmp_path):
    snapshot = runtime.collect_runtime_snapshot(
        path_value="/private/sentinel", registry_dir=tmp_path
    )

    rendered = str(snapshot.to_dict())

    assert "argv" not in rendered
    assert "credential" not in rendered
    assert "/private/sentinel" not in rendered
