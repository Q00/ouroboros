"""Windows installer extras ownership, parity and syntax checks."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def test_installers_delegate_optional_versions_to_target_metadata() -> None:
    for path in (INSTALL_SH, INSTALL_PS1):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b[\w.-]+==[0-9]", text), path
    sh_text = INSTALL_SH.read_text(encoding="utf-8")
    ps1_text = INSTALL_PS1.read_text(encoding="utf-8")
    assert '"$INSTALL_SPEC" --with "$CLICK_SPEC"' in sh_text
    assert "\"$PackageName$extrasLabel\", '--with', $ClickSpec" in ps1_text
    for extras in ("claude,tui", "claude-sdk,tui", "mcp,tui", "all"):
        assert f"[{extras}]" in sh_text
        assert f"[{extras}]" in ps1_text


def test_ps1_shares_python_and_click_contract_with_install_sh() -> None:
    sh_text = INSTALL_SH.read_text(encoding="utf-8")
    ps1_text = INSTALL_PS1.read_text(encoding="utf-8")
    for var in ("CLICK_SPEC", "DEFAULT_PYTHON_SPEC", "LITELLM_PYTHON_SPEC"):
        match = re.search(rf'^{var}="([^"]+)"', sh_text, re.MULTILINE)
        assert match is not None, f"{var} missing from install.sh"
        assert f"'{match.group(1)}'" in ps1_text, (
            f"install.ps1 does not carry install.sh's {var}={match.group(1)!r}"
        )


def test_ps1_emits_no_installer_events() -> None:
    """TELEMETRY.md: install.ps1 emits neither installer event itself.

    The `ouroboros setup` subprocesses it runs are ordinary CLI invocations and
    keep their normal telemetry controls, so this only pins the installer's own
    behavior: no PostHog client, no capture call, and no event name outside the
    contract comment.
    """
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "emits no installer events of its own" in text
    assert "posthog" not in text.lower()
    assert "Invoke-RestMethod" not in text.replace(
        "Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1'", ""
    ), "install.ps1 must not make network calls beyond fetching the uv installer"
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "install_started" not in body
    assert "install_completed" not in body


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_ps1_parses_under_pwsh() -> None:
    script = (
        "$errors = $null; $tokens = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$tokens, [ref]$errors) | Out-Null; "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
@pytest.mark.parametrize(
    ("runtime", "extras"),
    [
        ("", "tui"),
        ("claude", "claude,tui"),
        ("claude-sdk", "claude-sdk,tui"),
        ("claude-cli", "claude-cli,tui"),
        ("hermes", "mcp,tui"),
        ("all", "all"),
    ],
)
@pytest.mark.parametrize("pre", [False, True])
def test_ps1_install_phase_selects_target_metadata(runtime: str, extras: str, pre: bool) -> None:
    """Execute the checked-in selection and argv code, stopping before setup."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    phase = text.split("# Map the backend", 1)[1].split("# Put the tool bin directory", 1)[0]
    phase = "# Map the backend" + phase
    constants = "\n".join(
        line
        for line in text.splitlines()
        if line.startswith(
            ("$PackageName =", "$ClickSpec =", "$DefaultPythonSpec =", "$LiteLLMPythonSpec =")
        )
    )
    script = (
        constants
        + f"\n$selected = '{runtime}'\n$Pre = ${str(pre).lower()}\n"
        + r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
function Write-Ok { }
function Write-Info { }
function Write-Step { }
function Write-Err { throw 'unexpected install failure' }
function Invoke-Native([string]$Command, [string[]]$NativeArgs) {
    $script:captured = @($Command) + $NativeArgs
    return $true
}
"""
        + phase
        + "\nConvertTo-Json -Compress -InputObject @($script:captured)"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    args = json.loads(result.stdout)
    assert args == [
        "uv",
        "tool",
        "install",
        "--upgrade",
        "--python",
        ">=3.12,<3.14" if runtime == "all" else ">=3.12",
        f"ouroboros-ai[{extras}]",
        "--with",
        "click>=8.1.0,<9.0.0",
        *(["--prerelease=allow"] if pre else []),
    ]
