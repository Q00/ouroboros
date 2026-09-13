"""Run the installer's optional Python probe through real Windows native processes.

Only the two functions under test are loaded; installing packages and changing
user configuration are deliberately outside this harness. Other platforms and
unavailable shells skip these tests.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

INSTALL_PS1 = Path(__file__).resolve().parents[3] / "scripts" / "install.ps1"
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows")

_PROBE_SCRIPT = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
if ($env:OOO_TEST_LEGACY -eq '1') { $PSNativeCommandArgumentPassing = 'Legacy' }
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:OOO_TEST_INSTALLER, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors -join '; ') }
foreach ($name in @('Get-CommandPath', 'Test-PythonAtLeast')) {
    $functions = @($ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true))
    if ($functions.Count -ne 1) { throw "Expected exactly one $name function" }
    . ([scriptblock]::Create($functions[0].Extent.Text))
}
$MinPython = [version]$env:OOO_TEST_MIN_PYTHON
$errorsBefore = $Error.Count
$answer = Test-PythonAtLeast $env:OOO_TEST_PYTHON
[pscustomobject]@{
    result = $answer
    is_boolean = ($answer -is [bool])
    error_preference = [string]$ErrorActionPreference
    errors_added = ($Error.Count - $errorsBefore)
} | ConvertTo-Json -Compress
"""


@pytest.fixture(params=["powershell.exe", "pwsh.exe", "pwsh-legacy"])
def shell(request: pytest.FixtureRequest) -> tuple[str, bool]:
    legacy = request.param == "pwsh-legacy"
    executable = shutil.which("pwsh.exe" if legacy else request.param)
    if executable is None:
        pytest.skip(f"{request.param} not installed")
    return executable, legacy


def _probe(
    shell: tuple[str, bool],
    executable: str | Path,
    minimum: str = "3.12",
    extra_env: dict[str, str] | None = None,
) -> dict:
    # Pass paths through the environment and code as UTF-16LE so the harness
    # does not introduce a second layer of native argument quoting.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    env.update(
        OOO_TEST_INSTALLER=str(INSTALL_PS1),
        OOO_TEST_PYTHON=str(executable),
        OOO_TEST_MIN_PYTHON=minimum,
        OOO_TEST_LEGACY="1" if shell[1] else "0",
    )
    env.update(extra_env or {})
    encoded = base64.b64encode(_PROBE_SCRIPT.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [shell[0], "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["is_boolean"], result
    assert result["error_preference"] == "Stop", result
    return result


@pytest.mark.parametrize(
    ("minimum", "expected"),
    [
        ("3.12", True),
        (f"{sys.version_info.major}.{sys.version_info.minor}", True),
        (f"{sys.version_info.major}.{sys.version_info.minor + 1}", False),
    ],
    ids=["supported", "equal-minimum", "too-old"],
)
def test_real_python_version(shell: tuple[str, bool], minimum: str, expected: bool) -> None:
    assert _probe(shell, sys.executable, minimum)["result"] is expected


def test_real_python_path_with_spaces_and_unicode(shell: tuple[str, bool], tmp_path: Path) -> None:
    python_dir = tmp_path / "Python 설치 경로"
    venv.EnvBuilder(with_pip=False).create(python_dir)
    assert _probe(shell, python_dir / "Scripts" / "python.exe")["result"] is True


def test_missing_python_is_optional(shell: tuple[str, bool], tmp_path: Path) -> None:
    assert _probe(shell, tmp_path / "missing-python.exe")["result"] is False


def test_python_start_failure_is_optional(shell: tuple[str, bool], tmp_path: Path) -> None:
    invalid_executable = tmp_path / "broken-python.exe"
    invalid_executable.write_bytes(b"not a Windows executable")
    assert _probe(shell, invalid_executable)["result"] is False


def test_python_stderr_and_nonzero_exit_are_optional(
    shell: tuple[str, bool], tmp_path: Path
) -> None:
    # CPython really fails during startup when its standard library is absent.
    missing_home = tmp_path / "missing-python-home"
    assert (
        _probe(shell, sys.executable, extra_env={"PYTHONHOME": str(missing_home)})["result"]
        is False
    )


def test_python_stderr_with_successful_exit(shell: tuple[str, bool], tmp_path: Path) -> None:
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\nsys.stderr.write('startup diagnostic\\n')\n", encoding="utf-8"
    )
    assert _probe(shell, sys.executable, extra_env={"PYTHONPATH": str(tmp_path)})["result"] is True


def test_windows_store_alias_is_skipped(shell: tuple[str, bool], tmp_path: Path) -> None:
    # Resolution-only fixture: an invalid executable proves the existing alias
    # guard returns before invocation, without opening the user's Store app.
    alias = tmp_path / "WindowsApps" / "python.exe"
    alias.parent.mkdir()
    alias.write_bytes(b"must not execute")
    result = _probe(shell, alias)
    assert result["result"] is False
    assert result["errors_added"] == 0
