"""Tests for MCP-owned filesystem locations."""

import json
import os
from pathlib import Path

import pytest

from ouroboros.mcp import registry_paths


@pytest.mark.skipif(os.name == "nt", reason="/etc/passwd is a POSIX account database")
def test_posix_home_comes_from_local_passwd_entry(tmp_path):
    passwd_file = tmp_path / "passwd"
    local_home = tmp_path / "account_home"
    passwd_file.write_text(f"test:x:4242:20:test:{local_home}:/bin/sh\n")

    assert registry_paths._posix_home_directory(passwd_file, user_id=4242) == local_home


@pytest.mark.skipif(os.name == "nt", reason="/etc/passwd is a POSIX account database")
def test_posix_home_ignores_non_utf8_gecos_fields(tmp_path):
    passwd_file = tmp_path / "passwd"
    local_home = tmp_path / "account_home"
    passwd_file.write_bytes(b"test:x:4242:20:\xff:" + os.fsencode(local_home) + b":/bin/sh\n")

    assert registry_paths._posix_home_directory(passwd_file, user_id=4242) == local_home


@pytest.mark.parametrize(
    "contents",
    [
        b"",
        b"malformed\n",
        b"other:x:9999:20:test:/home/other:/bin/sh\n",
        b"test:x:4242:20:test:relative:/bin/sh\n",
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="/etc/passwd is a POSIX account database")
def test_posix_home_rejects_missing_or_malformed_local_records(tmp_path, contents):
    passwd_file = tmp_path / "passwd"
    passwd_file.write_bytes(contents)

    with pytest.raises(OSError):
        registry_paths._posix_home_directory(passwd_file, user_id=4242)


@pytest.mark.skipif(os.name == "nt", reason="/etc/passwd is a POSIX account database")
def test_posix_home_rejects_oversized_local_passwd_file(tmp_path):
    passwd_file = tmp_path / "passwd"
    passwd_file.write_bytes(b"x" * (1_048_577))

    with pytest.raises(OSError, match="read limit"):
        registry_paths._posix_home_directory(passwd_file, user_id=4242)


def test_windows_home_helper_uses_profile_known_folder_api(tmp_path):
    profile_path = tmp_path / "windows-profile"

    class FakeFolderApi:
        argtypes = None
        restype = None

        def __init__(self):
            self.folder_id = None

        def __call__(self, _window, folder_id, _token, _flags, buffer):
            self.folder_id = folder_id
            buffer.value = str(profile_path)
            return 0

    function = FakeFolderApi()
    shell32 = type("FakeShell32", (), {"SHGetFolderPathW": function})()

    assert registry_paths._windows_home_directory(shell32) == profile_path
    assert function.folder_id == 40


@pytest.mark.parametrize("result,profile", [(1, "/unused"), (0, "")])
def test_windows_home_helper_reports_profile_resolution_failure(result, profile):
    class FakeFolderApi:
        argtypes = None
        restype = None

        def __call__(self, _window, _folder_id, _token, _flags, buffer):
            buffer.value = profile
            return result

    shell32 = type("FakeShell32", (), {"SHGetFolderPathW": FakeFolderApi()})()
    with pytest.raises(OSError):
        registry_paths._windows_home_directory(shell32)


def test_registry_path_does_not_read_home_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/private/sentinel")
    monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(AssertionError("HOME read")))
    local_home = Path.cwd() / "local_account_home"
    monkeypatch.setattr(registry_paths, "_user_home_directory", lambda: local_home)

    assert registry_paths.diagnostic_mcp_pid_registry_dir() == (
        local_home / ".ouroboros" / "mcp-servers"
    )


def test_owned_server_registry_keeps_standard_home_resolution(monkeypatch):
    home = Path.cwd() / "normal_server_home"
    monkeypatch.setattr(Path, "home", lambda: home)

    assert registry_paths.owned_mcp_pid_registry_dir() == home / ".ouroboros" / "mcp-servers"


def test_registry_path_is_unavailable_without_local_account_home(monkeypatch):
    monkeypatch.setattr(
        registry_paths,
        "_user_home_directory",
        lambda: (_ for _ in ()).throw(OSError("no local account record")),
    )

    assert registry_paths.diagnostic_mcp_pid_registry_dir() is None


def test_runtime_doctor_reports_unavailable_when_account_home_cannot_be_resolved(monkeypatch):
    from typer.testing import CliRunner

    from ouroboros.cli.runtime_doctor import app

    monkeypatch.setattr(
        registry_paths,
        "_user_home_directory",
        lambda: (_ for _ in ()).throw(KeyError("missing account record")),
    )
    result = CliRunner().invoke(app, ["--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["registry"]["status"] == "not_checked"
    assert payload["registry"]["reason"] == "owner_unavailable"
