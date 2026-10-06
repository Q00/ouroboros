"""Tests for MCP-owned registry lookup and its diagnostic privacy boundary."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from ouroboros.cli.runtime_doctor import app
from ouroboros.mcp import machine_runtime, registry_paths


@pytest.mark.skipif(os.name == "nt", reason="HOME is the POSIX home-location override")
def test_doctor_inspects_real_registry_under_server_home_override(tmp_path, monkeypatch):
    # Resolve macOS's /var alias so the existing no-symlink scanner can inspect it.
    home = tmp_path.resolve() / "PRIVATE_HOME_LOCATION_SENTINEL"
    registry = home / ".ouroboros" / "mcp-servers"
    registry.mkdir(parents=True)
    (registry / "123.pid").write_text("PRIVATE_REGISTRY_CONTENT_SENTINEL")
    (home / ".ouroboros" / "config.yaml").write_text("PRIVATE_CONFIG_SENTINEL")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")

    assert registry_paths.owned_mcp_pid_registry_dir() == registry
    assert registry_paths.diagnostic_mcp_pid_registry_dir() == registry
    monkeypatch.setattr(Path, "read_text", lambda *_a, **_k: pytest.fail("content read"))
    monkeypatch.setattr(Path, "read_bytes", lambda *_a, **_k: pytest.fail("content read"))
    before = set(home.rglob("*"))
    runner = CliRunner()
    structured = runner.invoke(app, ["--json"])
    human = runner.invoke(app, [])

    assert structured.exit_code == human.exit_code == 0
    payload = json.loads(structured.output)
    assert payload["registry"]["status"] == "available"
    assert [record["pid"] for record in payload["registry"]["records"]] == [123]
    assert payload["registry"]["directory"] == "~/.ouroboros/mcp-servers"
    assert "pid=123" in human.output
    for output in (structured.output, human.output):
        assert str(home) not in output
        assert "PRIVATE_HOME_LOCATION_SENTINEL" not in output
        assert "PRIVATE_REGISTRY_CONTENT_SENTINEL" not in output
        assert "PRIVATE_CONFIG_SENTINEL" not in output
    assert set(home.rglob("*")) == before


@pytest.mark.skipif(os.name == "nt", reason="HOME is the POSIX home-location override")
def test_server_and_doctor_resolve_same_registry_in_fresh_process(tmp_path):
    home = tmp_path.resolve() / "OVERRIDE_HOME_SENTINEL"
    home.mkdir()
    source = str(Path(registry_paths.__file__).resolve().parents[2])
    code = (
        f"import sys; sys.path.insert(0, {source!r}); "
        "from ouroboros.cli.commands.mcp import _PID_REGISTRY_DIR; "
        "from ouroboros.mcp.registry_paths import diagnostic_mcp_pid_registry_dir; "
        "assert _PID_REGISTRY_DIR == diagnostic_mcp_pid_registry_dir(); "
        "import pathlib; assert _PID_REGISTRY_DIR == pathlib.Path.home()/'.ouroboros'/'mcp-servers'; "
        "print('same-owned-registry')"
    )
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    environment.update(HOME=str(home), DO_NOT_TRACK="1", OUROBOROS_TELEMETRY="0")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        env=environment,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "same-owned-registry"
    assert str(home) not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX Path.home reads HOME")
def test_diagnostic_collection_reads_only_path_and_home_location(tmp_path, monkeypatch):
    home = tmp_path.resolve() / "PRIVATE_LOCATION_SENTINEL"
    (home / ".ouroboros" / "mcp-servers").mkdir(parents=True)
    reads = set()

    class ScopedEnvironment(dict):
        def __getitem__(self, key):
            assert key in {"PATH", "HOME"}, f"Unexpected environment read: {key}"
            reads.add(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            assert key in {"PATH", "HOME"}, f"Unexpected environment read: {key}"
            reads.add(key)
            return super().get(key, default)

    environment = ScopedEnvironment(
        HOME=str(home), PATH="", PRIVATE_CREDENTIAL="SECRET_CREDENTIAL_SENTINEL"
    )
    monkeypatch.setattr(os, "environ", environment)
    snapshot = machine_runtime.collect_runtime_snapshot(
        registry_dir=registry_paths.diagnostic_mcp_pid_registry_dir()
    )

    assert reads == {"PATH", "HOME"}
    assert snapshot.registry.status == "available"
    serialized = json.dumps(snapshot.to_dict())
    assert str(home) not in serialized
    assert "SECRET_CREDENTIAL_SENTINEL" not in serialized


@pytest.mark.parametrize("home", ["relative/private-location", "/private/bad\x00location"])
@pytest.mark.skipif(os.name == "nt", reason="HOME is the POSIX home-location override")
def test_invalid_home_location_is_not_checked_without_echoing_value(home, monkeypatch):
    monkeypatch.setattr(os, "environ", {**os.environ, "HOME": home, "PATH": ""})

    assert registry_paths.diagnostic_mcp_pid_registry_dir() is None
    for arguments in (["--json"], []):
        result = CliRunner().invoke(app, arguments)
        assert result.exit_code == 0, result.output
        assert "owner_unavailable" in result.output
        assert "private-location" not in result.output
        assert "bad" not in result.output
        if arguments:
            payload = json.loads(result.output)
            assert payload["registry"]["status"] == "not_checked"
            assert payload["registry"]["reason"] == "owner_unavailable"


@pytest.mark.parametrize("error", [KeyError, OSError, RuntimeError, ValueError])
def test_unresolvable_home_is_not_checked_without_exception_details(error, monkeypatch):
    def unavailable():
        raise error("PRIVATE_LOCATION_EXCEPTION_SENTINEL")

    monkeypatch.setattr(Path, "home", unavailable)
    monkeypatch.setenv("PATH", "")

    assert registry_paths.diagnostic_mcp_pid_registry_dir() is None
    result = CliRunner().invoke(app, ["--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["registry"]["status"] == "not_checked"
    assert payload["registry"]["reason"] == "owner_unavailable"
    assert "PRIVATE_LOCATION_EXCEPTION_SENTINEL" not in result.output


def test_owned_server_registry_keeps_standard_home_resolution(monkeypatch):
    home = Path.cwd() / "normal_server_home"
    monkeypatch.setattr(Path, "home", lambda: home)

    assert registry_paths.owned_mcp_pid_registry_dir() == home / ".ouroboros" / "mcp-servers"


@pytest.mark.skipif(os.name == "nt", reason="POSIX Path.home normalizes empty HOME to root")
def test_empty_home_keeps_the_servers_effective_location(monkeypatch):
    monkeypatch.setenv("HOME", "")

    assert registry_paths.diagnostic_mcp_pid_registry_dir() == (
        registry_paths.owned_mcp_pid_registry_dir()
    )
