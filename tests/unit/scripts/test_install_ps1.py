"""Windows installer (scripts/install.ps1) parity and syntax checks.

The PowerShell installer cannot run in CI (no Windows runner), so these tests
pin the two things that drift silently: the dependency pins it passes to
`uv tool install` must equal install.sh's, and the file must still parse.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest

from tests.unit.scripts.test_install_runtime_selection import _expected_pins_for_extras

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

_PIN_RE = re.compile(r"\b([A-Za-z0-9][A-Za-z0-9_.-]*==[0-9][0-9A-Za-z.]*)")


def _pins(path: Path) -> set[str]:
    return set(_PIN_RE.findall(path.read_text(encoding="utf-8")))


def test_ps1_pins_match_install_sh() -> None:
    sh_pins = _pins(INSTALL_SH)
    ps1_pins = _pins(INSTALL_PS1)
    assert sh_pins, "no pins found in install.sh — parity check inert"
    assert ps1_pins == sh_pins, (
        "install.ps1 pins drifted from install.sh.\n"
        f"only in install.sh:  {sorted(sh_pins - ps1_pins)}\n"
        f"only in install.ps1: {sorted(ps1_pins - sh_pins)}"
    )


def test_ps1_pins_match_pyproject() -> None:
    ps1_text = INSTALL_PS1.read_text(encoding="utf-8")
    expected = _expected_pins_for_extras("claude", "claude-sdk", "mcp", "tui", "litellm")
    assert expected, "no pyproject pins discovered — parity check inert"
    drifted = sorted(pin for pin in expected if pin not in ps1_text)
    assert not drifted, f"install.ps1 is missing pyproject pins: {drifted}"


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
