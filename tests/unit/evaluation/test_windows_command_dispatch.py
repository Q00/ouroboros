"""Real Windows regressions for mechanical executable resolution and batch safety.

The fixtures execute only local scripts under pytest's temporary directory. One
smoke check uses an existing npm installation; no dependency installation, real
project build, network service, or system mutation is needed. Batch argument
checks exercise Windows rather than mock CMD's argument rules.
"""

import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import Mock

import pytest

from ouroboros.evaluation import command_dispatch
from ouroboros.evaluation.languages import build_mechanical_config
from ouroboros.evaluation.mechanical import MechanicalVerifier, run_command
from ouroboros.evaluation.models import CheckType

pytestmark = pytest.mark.asyncio
_REQUIRES_WINDOWS = pytest.mark.skipif(
    sys.platform != "win32", reason="Requires real Windows batch dispatch"
)


@pytest.fixture
def batch_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Provide a PATH-installed npm shim and a separate Unicode workspace."""
    tool_dir = tmp_path / "도구 모음"
    workspace = tmp_path / "작업 공간"
    tool_dir.mkdir()
    workspace.mkdir()
    (tool_dir / "capture.py").write_text(
        "import json, os, pathlib, sys\n"
        "payload = {'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'nested': os.environ.get('_OUROBOROS_NESTED'), "
        "'kept': os.environ.get('OOO_DISPATCH_KEEP')}\n"
        "pathlib.Path('ran.json').write_text(json.dumps(payload), encoding='utf-8')\n"
        "print(json.dumps(payload))\n"
        "sys.exit(7 if '--fixture-exit=7' in sys.argv else 0)\n",
        encoding="utf-8",
    )
    (tool_dir / "npm.CMD").write_text(
        "@echo off\n"
        "setlocal DisableDelayedExpansion\n"
        f'"{sys.executable}" "%~dp0capture.py" %*\n'
        "exit /b %errorlevel%\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PATH", str(tool_dir))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setenv("OOO_DISPATCH_KEEP", "keep-this-value")
    monkeypatch.setenv("OOO_DISPATCH_EXPANSION", "expanded-not-literal")
    monkeypatch.setenv("_OUROBOROS_NESTED", "1")
    return tool_dir, workspace


@_REQUIRES_WINDOWS
async def test_bare_npm_resolves_cmd_and_preserves_arguments_cwd_and_environment(
    batch_workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default validated npm spelling must launch without shell=True."""
    tool_dir, workspace = batch_workspace
    original_exec = asyncio.create_subprocess_exec
    calls: list[tuple[object, ...]] = []

    async def checked_exec(*args, **kwargs):
        assert not kwargs.get("shell", False)
        calls.append(args)
        return await original_exec(*args, **kwargs)

    async def forbidden_shell(*_args, **_kwargs):
        pytest.fail("Mechanical commands must not use create_subprocess_shell")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", checked_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_shell)
    arguments = ("run", "build", "--", "--record-evidence", "two words", "한글", "")

    result = await run_command(("npm", *arguments), timeout=10, working_dir=workspace)

    assert result.return_code == 0, result.stderr
    assert result.executed_command == (str(tool_dir / "npm.CMD"), *arguments)
    payload = json.loads(result.stdout)
    assert payload["args"] == list(arguments)
    assert Path(payload["cwd"]) == workspace
    assert payload["nested"] is None
    assert payload["kept"] == "keep-this-value"
    assert os.environ["_OUROBOROS_NESTED"] == "1"
    assert len(calls) == 1


@_REQUIRES_WINDOWS
async def test_bare_command_can_resolve_bat_extension(
    batch_workspace: tuple[Path, Path],
) -> None:
    tool_dir, workspace = batch_workspace
    (tool_dir / "npm.BAT").write_text(
        (tool_dir / "npm.CMD").read_text(encoding="utf-8"), encoding="utf-8"
    )

    result = await run_command(("npm", "run", "build"), timeout=10, working_dir=workspace)

    assert result.return_code == 0, result.stderr
    assert result.executed_command == (str(tool_dir / "npm.BAT"), "run", "build")
    assert json.loads(result.stdout)["args"] == ["run", "build"]


@_REQUIRES_WINDOWS
async def test_bare_npm_prefers_path_over_implicit_current_directory(
    batch_workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-named repository shim must not shadow the PATH-installed tool."""
    _, workspace = batch_workspace
    (workspace / "npm.CMD").write_text(
        "@echo off\necho unexpected>cwd-shadow-ran.txt\nexit /b 0\n", encoding="utf-8"
    )
    monkeypatch.chdir(workspace)

    result = await run_command(("npm", "run", "build"), timeout=10, working_dir=workspace)

    assert result.return_code == 0, result.stderr
    assert json.loads(result.stdout)["args"] == ["run", "build"]
    assert not (workspace / "cwd-shadow-ran.txt").exists()


@_REQUIRES_WINDOWS
async def test_batch_nonzero_exit_is_not_skipped_or_passed(
    batch_workspace: tuple[Path, Path],
) -> None:
    _, workspace = batch_workspace

    result = await run_command(
        ("npm", "run", "test", "--", "--fixture-exit=7"),
        timeout=10,
        working_dir=workspace,
    )

    assert result.return_code == 7
    assert (workspace / "ran.json").exists()
    assert "--fixture-exit=7" in json.loads(result.stdout)["args"]
    assert not result.timed_out


@_REQUIRES_WINDOWS
async def test_missing_bare_executable_is_an_explicit_failure(
    batch_workspace: tuple[Path, Path],
) -> None:
    _, workspace = batch_workspace

    result = await run_command(
        ("ouroboros-fixture-does-not-exist", "run", "build"),
        timeout=10,
        working_dir=workspace,
    )

    assert result.return_code != 0
    assert result.stderr
    assert not result.timed_out
    assert not (workspace / "ran.json").exists()


@_REQUIRES_WINDOWS
@pytest.mark.parametrize("explicit_path", [False, True], ids=["bare", "explicit-cmd"])
@pytest.mark.parametrize(
    "argument",
    [
        pytest.param("value&echo injected>injected.txt", id="ampersand"),
        pytest.param("%OOO_DISPATCH_EXPANSION%", id="percent-expansion"),
        pytest.param("!OOO_DISPATCH_EXPANSION!", id="delayed-expansion"),
        pytest.param("caret^value", id="caret"),
        pytest.param('embedded"quote', id="embedded-quote"),
        pytest.param("line\necho injected>injected.txt", id="newline"),
        pytest.param("line\recho injected>injected.txt", id="carriage-return"),
        pytest.param("value|echo injected>injected.txt", id="pipe"),
        pytest.param("value>injected.txt", id="redirection"),
        pytest.param("(parenthesized)", id="parentheses"),
        pytest.param("two words\\", id="quoted-trailing-backslash"),
    ],
)
async def test_batch_sensitive_arguments_remain_literal_or_fail_before_execution(
    batch_workspace: tuple[Path, Path], argument: str, explicit_path: bool
) -> None:
    """A batch-specific rejection is a failure, never execution or a silent pass."""
    tool_dir, workspace = batch_workspace
    arguments = ("run", "build", "--", argument)
    executable = str(tool_dir / "npm.CMD") if explicit_path else "npm"

    result = await run_command((executable, *arguments), timeout=10, working_dir=workspace)

    assert not (workspace / "injected.txt").exists()
    if result.return_code == 0:
        assert json.loads(result.stdout)["args"] == list(arguments)
    else:
        assert result.stderr, "Unsupported batch arguments need an explicit diagnostic"
        assert not (workspace / "ran.json").exists()


@_REQUIRES_WINDOWS
async def test_explicit_native_executable_keeps_shell_characters_literal(
    batch_workspace: tuple[Path, Path],
) -> None:
    """Batch restrictions must not change normal .exe argv semantics."""
    _, workspace = batch_workspace
    argument = 'literal & %OOO_DISPATCH_EXPANSION% ! ^ " text\nnext line'

    result = await run_command(
        (sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))", argument),
        timeout=10,
        working_dir=workspace,
    )

    assert result.return_code == 0, result.stderr
    assert json.loads(result.stdout) == [argument]


@_REQUIRES_WINDOWS
@pytest.mark.parametrize("directory_name", ["tools & extra", "tools %OOO_DISPATCH_EXPANSION%"])
@pytest.mark.parametrize("explicit_path", [False, True], ids=["bare", "explicit-cmd"])
async def test_batch_sensitive_executable_path_is_rejected_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory_name: str, explicit_path: bool
) -> None:
    tool_dir = tmp_path / directory_name
    tool_dir.mkdir()
    executable = tool_dir / "npm.CMD"
    executable.write_text("@echo off\necho ran>path-executed.txt\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tool_dir))
    monkeypatch.setenv("PATHEXT", ".CMD")

    result = await run_command(
        (str(executable) if explicit_path else "npm", "run", "build"),
        timeout=5,
        working_dir=tmp_path,
    )

    assert result.return_code == -1
    assert "Unsafe Windows batch" in result.stderr
    assert result.executed_command is None
    assert not (tmp_path / "path-executed.txt").exists()


@_REQUIRES_WINDOWS
async def test_installed_npm_runs_validated_build_and_test_without_installing_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise config validation through real npm, with a local-only fixture."""
    if shutil.which("npm.cmd") is None or shutil.which("node") is None:
        pytest.skip("Requires an existing Windows Node/npm installation")
    workspace = tmp_path / "npm 검증 공간"
    config_dir = workspace / ".ouroboros"
    config_dir.mkdir(parents=True)
    (workspace / "package.json").write_text(
        json.dumps(
            {
                "name": "ouroboros-windows-dispatch-fixture",
                "version": "1.0.0",
                "private": True,
                "scripts": {
                    "build": "node verify.mjs",
                    "test:unit": "node verify.mjs --fixture-exit=7",
                },
            }
        ),
        encoding="utf-8",
    )
    (workspace / "verify.mjs").write_text(
        "console.log('local-verification-fixture-ran');\n"
        "process.exit(process.argv.includes('--fixture-exit=7') ? 7 : 0);\n",
        encoding="utf-8",
    )
    (config_dir / "mechanical.toml").write_text(
        'build = "npm run build"\ntest = "npm run test:unit"\ntimeout = 20\n',
        encoding="utf-8",
    )
    npmrc = workspace / ".npmrc"
    npmrc.write_text("", encoding="utf-8")
    for key, value in {
        "npm_config_userconfig": str(npmrc),
        "npm_config_cache": str(tmp_path / "npm-cache"),
        "npm_config_offline": "true",
        "npm_config_update_notifier": "false",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
    }.items():
        monkeypatch.setenv(key, value)
    config = build_mechanical_config(workspace)
    assert config.build_command == ("npm", "run", "build")
    assert config.test_command == ("npm", "run", "test:unit")

    result = await MechanicalVerifier(config).verify(
        "windows-npm-fixture", checks=[CheckType.BUILD, CheckType.TEST]
    )

    assert result.is_ok
    mechanical_result, _ = result.value
    build, test = mechanical_result.checks
    assert build.passed, build.details
    assert build.details["return_code"] == 0
    assert not test.passed
    assert test.details["return_code"] == 7
    assert not mechanical_result.passed
    for check in (build, test):
        assert not check.details.get("skipped", False)
        assert check.details["command"][0] == "npm"
        assert check.details["executed_command"][0].lower().endswith("npm.cmd")
        assert check.details["working_dir"] == str(workspace)
        assert "local-verification-fixture-ran" in check.details["stdout_preview"]


async def test_posix_preparation_does_not_lookup_or_rewrite_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform-independent proof that the new boundary leaves POSIX alone."""
    monkeypatch.setattr(command_dispatch, "_WINDOWS", False)
    lookup = Mock(side_effect=AssertionError("POSIX commands must not be resolved here"))
    monkeypatch.setattr(command_dispatch.shutil, "which", lookup)
    command = ("npm", "run", "test", "--", "literal & % ! ^", 'embedded"quote')

    assert command_dispatch.prepare_command(command, {"PATH": "/usr/bin"}) is command
    lookup.assert_not_called()


@pytest.mark.parametrize(
    "executable",
    [r"C:\도구 모음\node.exe", r".\tools\node.exe", "tools/node.exe", r"C:node.exe"],
)
async def test_windows_explicit_native_path_is_not_resolved_or_rewritten(
    monkeypatch: pytest.MonkeyPatch, executable: str
) -> None:
    monkeypatch.setattr(command_dispatch, "_WINDOWS", True)
    lookup = Mock(side_effect=AssertionError("Explicit executable paths must not be resolved"))
    monkeypatch.setattr(command_dispatch.shutil, "which", lookup)
    command = (executable, "--", "two words", 'literal & % ! ^ "', "")

    assert command_dispatch.prepare_command(command, {"PATH": r"C:\other-tools"}) == command
    lookup.assert_not_called()


async def test_windows_lookup_uses_absolute_path_entries_without_implicit_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-independent lookup proof without patching os.name/Path semantics."""
    monkeypatch.setattr(command_dispatch, "_WINDOWS", True)
    monkeypatch.setattr(command_dispatch.os, "pathsep", ";")
    resolved = r"D:\도구 모음\npm.CMD"
    lookup = Mock(side_effect=[None, resolved])
    monkeypatch.setattr(command_dispatch.shutil, "which", lookup)
    command = ("npm", "run", "test", "--", "two words", "")
    env = {"PATH": ';.;relative-tools;\\rooted-tools;C:\\tools;"D:\\도구 모음"'}

    assert command_dispatch.prepare_command(command, env) == (resolved, *command[1:])
    assert [call.args for call in lookup.call_args_list] == [
        (r"C:\tools\npm",),
        (r"D:\도구 모음\npm",),
    ]


async def test_windows_path_miss_preserves_native_spawn_failure_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(command_dispatch, "_WINDOWS", True)
    monkeypatch.setattr(command_dispatch.os, "pathsep", ";")
    lookup = Mock(return_value=None)
    monkeypatch.setattr(command_dispatch.shutil, "which", lookup)
    command = ("missing-fixture-tool", "run", "test")

    assert command_dispatch.prepare_command(command, {"PATH": r"C:\tools"}) == command
