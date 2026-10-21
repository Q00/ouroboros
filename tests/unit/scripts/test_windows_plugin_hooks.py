"""Exercise packaged hook commands through Codex's native Windows shell boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

_HOOKS_PATH = Path(__file__).resolve().parents[3] / "hooks" / "hooks.json"
_EVENT_SCRIPTS = [
    ("SessionStart", "session-start.py"),
    ("UserPromptSubmit", "keyword-detector.py"),
    ("PostToolUse", "drift-monitor.py"),
]
_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="requires native Windows PowerShell")
_PAYLOAD = {"prompt": "한글과 emoji 🎯", "hook_event_name": "fixture"}
_ECHO_SCRIPT = """\
import json
from pathlib import Path
import sys

print(json.dumps({"payload": json.load(sys.stdin), "script": str(Path(__file__).resolve())}, ensure_ascii=False))
print("진단 메시지 🎯", file=sys.stderr)
"""


def _hook_handler(event_name: str) -> dict[str, object]:
    manifest = json.loads(_HOOKS_PATH.read_text(encoding="utf-8"))
    return manifest["hooks"][event_name][0]["hooks"][0]


@pytest.fixture(params=["powershell", "pwsh"])
def windows_shell(request: pytest.FixtureRequest) -> Path:
    if request.param == "powershell":
        shell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        assert shell.is_file(), "Windows PowerShell 5.1 is required"
        return shell
    path = shutil.which("pwsh")
    if path is None:
        pytest.skip("PowerShell 7 is not installed")
    return Path(path)


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "PLUGIN_ROOT",
        "CLAUDE_PLUGIN_ROOT",
        "PYTHONIOENCODING",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        env.pop(name, None)
    # Do not rely on the developer's Python aliases, activated environment, or Git Bash.
    env["PATH"] = str(Path(sys.executable).parent)
    return env


def _write_script(root: Path, name: str, body: str = _ECHO_SCRIPT) -> Path:
    script = root / "scripts" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    return script


def _run_hook(
    event_name: str,
    env: dict[str, str],
    cwd: Path,
    shell: Path,
) -> subprocess.CompletedProcess[bytes]:
    handler = _hook_handler(event_name)
    command = handler.get("commandWindows", handler["command"])
    # Codex invokes hooks using the session's shell; both Windows PowerShell
    # and PowerShell 7 receive the command as an argument to -Command.
    return subprocess.run(
        [str(shell), "-NoProfile", "-Command", str(command)],
        input=json.dumps(_PAYLOAD, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=cwd,
        env=env,
        timeout=10,
        check=False,
    )


def _assert_dispatched(result: subprocess.CompletedProcess[bytes], script: Path) -> None:
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout, result.stderr.decode("utf-8", errors="replace")
    output = json.loads(result.stdout.decode("utf-8"))
    assert output == {"payload": _PAYLOAD, "script": str(script.resolve())}
    assert "진단 메시지 🎯" in result.stderr.decode("utf-8")


@pytest.mark.parametrize(("event_name", "script_name"), _EVENT_SCRIPTS)
def test_packaged_hooks_provide_windows_commands(event_name: str, script_name: str) -> None:
    """The native host must have a Windows command rather than the POSIX fallback."""
    handler = _hook_handler(event_name)
    assert isinstance(handler.get("commandWindows"), str)
    assert handler["commandWindows"]


@_WINDOWS_ONLY
@pytest.mark.parametrize(("event_name", "script_name"), _EVENT_SCRIPTS)
@pytest.mark.parametrize("root_variable", ["PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"])
def test_windows_hooks_preserve_stdin_and_unicode(
    event_name: str, script_name: str, root_variable: str, tmp_path: Path, windows_shell: Path
) -> None:
    plugin_root = tmp_path / "플러그인 with spaces"
    script = _write_script(plugin_root, script_name)
    env = _environment()
    env[root_variable] = str(plugin_root)
    if root_variable == "CLAUDE_PLUGIN_ROOT":
        env["PLUGIN_ROOT"] = ""

    _assert_dispatched(_run_hook(event_name, env, tmp_path, windows_shell), script)


@_WINDOWS_ONLY
@pytest.mark.parametrize(("event_name", "script_name"), _EVENT_SCRIPTS)
def test_windows_plugin_root_takes_precedence(
    event_name: str, script_name: str, tmp_path: Path, windows_shell: Path
) -> None:
    plugin_root = tmp_path / "current"
    script = _write_script(plugin_root, script_name)
    fallback_root = tmp_path / "compatibility"
    _write_script(fallback_root, script_name, "raise RuntimeError('wrong root selected')\n")
    env = _environment()
    env["PLUGIN_ROOT"] = str(plugin_root)
    env["CLAUDE_PLUGIN_ROOT"] = str(fallback_root)

    _assert_dispatched(_run_hook(event_name, env, tmp_path, windows_shell), script)


@_WINDOWS_ONLY
@pytest.mark.parametrize(("event_name", "script_name"), _EVENT_SCRIPTS)
@pytest.mark.parametrize("interpreter", ["native", "python.cmd"])
def test_windows_plugin_root_metacharacters_are_literal(
    event_name: str, script_name: str, interpreter: str, tmp_path: Path, windows_shell: Path
) -> None:
    plugin_root = tmp_path / "한글 root %PATH% & ! literal"
    script = _write_script(plugin_root, script_name)
    env = _environment()
    env["PLUGIN_ROOT"] = str(plugin_root)
    if interpreter == "python.cmd":
        interpreter_dir = tmp_path / "실행기 with spaces"
        _python_shim(interpreter_dir, "python", functional=True)
        env["PATH"] = str(interpreter_dir)

    _assert_dispatched(_run_hook(event_name, env, tmp_path, windows_shell), script)


@_WINDOWS_ONLY
@pytest.mark.parametrize(("event_name", "script_name"), _EVENT_SCRIPTS)
@pytest.mark.parametrize("root_state", ["absent", "deleted", "missing-script"])
def test_windows_unavailable_root_fails_open_without_project_script(
    event_name: str, script_name: str, root_state: str, tmp_path: Path, windows_shell: Path
) -> None:
    """A removed plugin or empty root must never select a same-named project script."""
    project_root = tmp_path / "untrusted-project"
    marker = project_root / "executed"
    _write_script(project_root, script_name, "from pathlib import Path\nPath('executed').touch()\n")
    plugin_root = tmp_path / "plugin"
    env = _environment()
    if root_state != "absent":
        env["PLUGIN_ROOT"] = str(plugin_root)
        if root_state == "missing-script":
            plugin_root.mkdir()

    result = _run_hook(event_name, env, project_root, windows_shell)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b""
    assert "Ouroboros hook skipped:" in result.stderr.decode("utf-8")
    assert not marker.exists()


@_WINDOWS_ONLY
@pytest.mark.parametrize(("event_name", "script_name"), _EVENT_SCRIPTS)
def test_windows_child_error_remains_nonblocking(
    event_name: str, script_name: str, tmp_path: Path, windows_shell: Path
) -> None:
    plugin_root = tmp_path / "plugin"
    _write_script(
        plugin_root,
        script_name,
        "import sys\nprint('child-error', file=sys.stderr)\nraise SystemExit(7)\n",
    )
    env = _environment()
    env["PLUGIN_ROOT"] = str(plugin_root)

    result = _run_hook(event_name, env, tmp_path, windows_shell)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b""
    assert "child-error" in result.stderr.decode("utf-8")
    assert "failed open:" in result.stderr.decode("utf-8")


def _python_shim(directory: Path, name: str, *, functional: bool) -> None:
    """Supply deterministic applications to PowerShell's Get-Command lookup."""
    directory.mkdir(exist_ok=True)
    body = f'@"{sys.executable}" %*\n' if functional else "@exit /b 1\n"
    (directory / f"{name}.cmd").write_text(body, encoding="utf-8")


@_WINDOWS_ONLY
@pytest.mark.parametrize("python_state", ["missing", "broken"])
def test_windows_hooks_fall_back_to_python3(
    python_state: str, tmp_path: Path, windows_shell: Path
) -> None:
    plugin_root = tmp_path / "plugin"
    script = _write_script(plugin_root, "keyword-detector.py")
    interpreter_dir = tmp_path / "interpreters"
    _python_shim(interpreter_dir, "python3", functional=True)
    if python_state == "broken":
        _python_shim(interpreter_dir, "python", functional=False)
    env = _environment()
    env["PATH"] = str(interpreter_dir)
    env["PLUGIN_ROOT"] = str(plugin_root)

    _assert_dispatched(_run_hook("UserPromptSubmit", env, tmp_path, windows_shell), script)


@_WINDOWS_ONLY
@pytest.mark.parametrize("python_state", ["missing", "broken"])
def test_windows_unavailable_python_fails_open(
    python_state: str, tmp_path: Path, windows_shell: Path
) -> None:
    plugin_root = tmp_path / "plugin"
    _write_script(plugin_root, "keyword-detector.py")
    interpreter_dir = tmp_path / "interpreters"
    interpreter_dir.mkdir()
    if python_state == "broken":
        _python_shim(interpreter_dir, "python", functional=False)
        _python_shim(interpreter_dir, "python3", functional=False)
    env = _environment()
    env["PATH"] = str(interpreter_dir)
    env["PLUGIN_ROOT"] = str(plugin_root)

    result = _run_hook("UserPromptSubmit", env, tmp_path, windows_shell)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b""
    diagnostic = result.stderr.decode("utf-8")
    assert "Ouroboros hook skipped:" in diagnostic
    assert "Python" in diagnostic and "unavailable" in diagnostic
