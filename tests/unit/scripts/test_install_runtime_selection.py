"""Installer runtime-selection regression tests."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_installer(
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    fake_commands: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"

    _write_executable(
        bin_dir / "uv",
        f"""#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "uv 0.0.0-test"
  exit 0
fi
printf 'uv %s\\n' "$*" >> {calls!s}
exit 0
""",
    )
    _write_executable(
        bin_dir / "ouroboros",
        f"""#!/bin/sh
printf 'ouroboros %s\\n' "$*" >> {calls!s}
exit 0
""",
    )
    if fake_commands:
        for name, content in fake_commands.items():
            _write_executable(bin_dir / name, content)

    run_env = os.environ.copy()
    run_env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        }
    )
    if env:
        run_env.update(env)

    return subprocess.run(
        ["bash", str(INSTALL_SH)],
        cwd=REPO_ROOT,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_install_script_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_preserves_opencode_backend_from_existing_config(tmp_path: Path) -> None:
    config_dir = tmp_path / "home" / ".ouroboros"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n  runtime_backend: opencode\n",
        encoding="utf-8",
    )

    result = _run_installer(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Runtime: opencode (preserved from" in result.stdout
    assert "Installing . ..." in result.stdout
    assert (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines() == [
        "uv tool install --upgrade --python >=3.12 . --prerelease=allow",
        "ouroboros setup --runtime opencode --non-interactive",
    ]


def test_explicit_claude_installs_mcp_and_claude_extras(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "claude"},
        fake_commands={"claude": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "Runtime: claude (from --runtime / OUROBOROS_INSTALL_RUNTIME)" in result.stdout
    assert (
        "uv tool install --upgrade --python >=3.12 . --prerelease=allow --with mcp>=1.26.0,<2.0.0 --with claude-agent-sdk>=0.1.0 --with anthropic>=0.52.0"
        in calls
    )
    assert "ouroboros setup --runtime claude --non-interactive" in calls
