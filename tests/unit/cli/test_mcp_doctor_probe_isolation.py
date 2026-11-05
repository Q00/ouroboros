"""Real subprocess regressions for the opt-in local probe's isolation boundary."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ouroboros.cli.commands import mcp_doctor


def _run_isolated(code, home, cwd, extra_env=None):
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), DO_NOT_TRACK="1")
    env.update(extra_env or {})
    source = str(Path(mcp_doctor.__file__).resolve().parents[3])
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {source!r}); " + code],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_manifest_import_does_not_create_user_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = _run_isolated(
        "from ouroboros.mcp.tool_manifest import BUILTIN_TOOL_NAMES; "
        "import json; print(json.dumps(sorted(BUILTIN_TOOL_NAMES)))",
        home,
        tmp_path,
    )
    assert "ouroboros_ac_dashboard" in result
    assert not list(home.iterdir())


def test_real_probe_ignores_poisoned_working_directory_and_environment(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "shadow-executed"
    keylog = tmp_path / "tls-keylog"
    shadow = project / "ouroboros"
    shadow.mkdir()
    payload = f"from pathlib import Path; Path({str(marker)!r}).write_text('executed')"
    (shadow / "__init__.py").write_text(payload)
    (shadow / "__main__.py").write_text(payload)
    (project / ".env").write_text(f"SSLKEYLOGFILE={keylog}\n")
    result = _run_isolated(
        "import asyncio,json,pathlib; "
        "from ouroboros.cli.commands.mcp_doctor import _probe_local_stdio; "
        "before=set(pathlib.Path.home().rglob('*')); "
        "results=asyncio.run(_probe_local_stdio()); "
        "print(json.dumps({'statuses':[r.status for r in results], "
        "'messages':[r.message for r in results], "
        "'new_home_files':[str(p) for p in set(pathlib.Path.home().rglob('*'))-before]}))",
        home,
        project,
        {"PYTHONPATH": str(project), "SSLKEYLOGFILE": str(keylog)},
    )
    assert result["statuses"] == ["pass", "pass", "pass"], result
    assert result["new_home_files"] == []
    assert not marker.exists()
    assert not keylog.exists()


def test_probe_child_blocks_network_and_commands_and_shuts_down(tmp_path):
    code = """
import asyncio, json, os, socket, subprocess
from unittest.mock import patch
from ouroboros.cli.commands.mcp_doctor import _serve_local_stdio_probe
from ouroboros.mcp.server import adapter
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
checks = {}
def attempt_fork():
    if not hasattr(os, 'fork'):
        raise RuntimeError('disabled for the local doctor probe: fork unavailable')
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
def attempt_forkpty():
    if not hasattr(os, 'forkpty'):
        raise RuntimeError('disabled for the local doctor probe: forkpty unavailable')
    pid, fd = os.forkpty()
    if pid == 0:
        os._exit(0)
    os.close(fd)
    os.waitpid(pid, 0)
class Server:
    async def serve(self, transport):
        operations = {
            'socket': lambda: socket.socket(),
            'dns': lambda: socket.getaddrinfo('localhost', 9),
            'sendto': lambda: udp.sendto(b'guard-test', ('127.0.0.1', 9)),
            'command': lambda: subprocess.run([sys.executable, '-c', 'pass']),
            'shell': lambda: os.system('exit 77'),
            'fork': attempt_fork,
            'forkpty': attempt_forkpty,
        }
        for key, operation in operations.items():
            try:
                operation()
            except RuntimeError as exc:
                checks[key] = str(exc)
            else:
                checks[key] = 'NOT_BLOCKED'
    async def shutdown(self):
        checks['shutdown'] = 'called'
async def main():
    with patch.object(adapter, 'create_ouroboros_server', return_value=Server()):
        await _serve_local_stdio_probe()
asyncio.run(main())
udp.close()
print(json.dumps(checks))
"""
    result = _run_isolated(code, tmp_path, tmp_path)
    assert result.pop("shutdown") == "called"
    assert len(result) == 7
    assert all("disabled for the local doctor probe" in value for value in result.values())


def test_probe_bootstrap_does_not_write_bytecode_to_trusted_installation(tmp_path, monkeypatch):
    trusted = tmp_path / "trusted"
    package = trusted / "ouroboros"
    for directory in (package, package / "cli", package / "cli" / "commands"):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("")
    entry = package / "cli" / "commands" / "mcp_doctor.py"
    entry.write_text(
        "import json, sys\n"
        "async def _serve_local_stdio_probe():\n"
        "    print(json.dumps({'no_bytecode': sys.flags.dont_write_bytecode}))\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mcp_doctor, "__file__", str(entry))
    config = mcp_doctor._local_stdio_probe_config(home)
    result = subprocess.run(
        [config.command, *config.args],
        env=config.env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["no_bytecode"] == 1
    assert not list(trusted.rglob("__pycache__"))
    assert not list(trusted.rglob("*.pyc"))


def test_probe_guards_network_before_venv_sitecustomize(tmp_path, monkeypatch):
    """A venv startup hook must run only after the child installs its guard."""
    import socket
    import venv

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(venv_dir)
    executable = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    purelib = subprocess.check_output(
        [str(executable), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
        timeout=15,
    ).strip()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    marker = tmp_path / "startup-result"
    try:
        (Path(purelib) / "sitecustomize.py").write_text(
            "import socket\n"
            "from pathlib import Path\n"
            "try:\n"
            f"    socket.create_connection(('127.0.0.1', {listener.getsockname()[1]}), timeout=2)\n"
            f"    Path({str(marker)!r}).write_text('connected')\n"
            "except Exception:\n"
            f"    if not Path({str(marker)!r}).exists(): Path({str(marker)!r}).write_text('blocked')\n"
        )
        trusted = tmp_path / "trusted"
        package = trusted / "ouroboros" / "cli" / "commands"
        package.mkdir(parents=True)
        for directory in (trusted / "ouroboros", trusted / "ouroboros" / "cli", package):
            (directory / "__init__.py").write_text("")
        entry = package / "mcp_doctor.py"
        entry.write_text("async def _serve_local_stdio_probe():\n    pass\n")
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(mcp_doctor, "__file__", str(entry))
        monkeypatch.setattr(mcp_doctor.sys, "executable", str(executable))
        config = mcp_doctor._local_stdio_probe_config(home)
        # Without -S, Python imports sitecustomize before our bootstrap.
        baseline_args = tuple(arg for arg in config.args if arg != "-S")
        baseline = subprocess.run(
            [config.command, *baseline_args],
            env=config.env,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert baseline.returncode == 0, baseline.stderr
        assert marker.read_text() == "connected"
        marker.unlink()
        result = subprocess.run(
            [config.command, *config.args],
            env=config.env,
            cwd=tmp_path,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert marker.read_text() == "blocked"
    finally:
        listener.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork")
def test_probe_guards_fork_before_venv_sitecustomize(tmp_path, monkeypatch):
    """An installed startup hook cannot create an unmanaged child process."""
    import venv

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(venv_dir)
    executable = venv_dir / "bin/python"
    purelib = subprocess.check_output(
        [str(executable), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
        timeout=15,
    ).strip()
    marker = tmp_path / "fork-result"
    (Path(purelib) / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "try:\n"
        "    pid = os.fork()\n"
        "except RuntimeError:\n"
        f"    Path({str(marker)!r}).write_text('blocked')\n"
        "else:\n"
        "    if pid == 0:\n"
        "        os._exit(0)\n"
        "    os.waitpid(pid, 0)\n"
        f"    Path({str(marker)!r}).write_text('forked')\n"
    )
    trusted = tmp_path / "trusted"
    package = trusted / "ouroboros" / "cli" / "commands"
    package.mkdir(parents=True)
    for directory in (trusted / "ouroboros", trusted / "ouroboros" / "cli", package):
        (directory / "__init__.py").write_text("")
    entry = package / "mcp_doctor.py"
    entry.write_text("async def _serve_local_stdio_probe():\n    pass\n")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mcp_doctor, "__file__", str(entry))
    monkeypatch.setattr(mcp_doctor.sys, "executable", str(executable))
    config = mcp_doctor._local_stdio_probe_config(home)
    result = subprocess.run(
        [config.command, *config.args],
        env=config.env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "blocked"
