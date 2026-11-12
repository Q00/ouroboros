"""The installer selects target extras; dependency versions belong to that target."""

from pathlib import Path

import pytest

from tests.unit.scripts.test_install_runtime_selection import _run_installer


@pytest.mark.parametrize(
    ("runtime", "extras"),
    [("", "mcp,tui"), ("claude", "claude,tui"), ("claude-sdk", "claude-sdk,tui"), ("all", "all")],
)
@pytest.mark.parametrize("local", [False, True])
def test_uv_selects_target_extras_without_overriding_optional_pins(
    tmp_path: Path, runtime: str, extras: str, local: bool
) -> None:
    result = _run_installer(tmp_path, local_repo=local, env={"OUROBOROS_INSTALL_RUNTIME": runtime})
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    install = next(line for line in calls if line.startswith("uv tool install "))
    target = "." if local else "ouroboros-ai"
    assert f" {target}[{extras}] --with click>=8.1.0,<9.0.0" in install
    assert install.count("--with") == 1, install
    assert "--upgrade" in install
    if runtime and runtime != "all":
        assert f"ouroboros setup --runtime {runtime} --non-interactive" in calls


@pytest.mark.parametrize("manager", ["pip", "pipx"])
@pytest.mark.parametrize(
    ("runtime", "extras"),
    [("", "mcp,tui"), ("claude-sdk", "claude-sdk,tui"), ("all", "all")],
)
def test_fallback_managers_keep_the_same_target_extras(
    tmp_path: Path, manager: str, runtime: str, extras: str
) -> None:
    from tests.unit.scripts.test_install_runtime_selection import CALLS_LOG_REF

    python = f"""#!/bin/sh
if [ "$1" = "-c" ]; then echo 3.12; exit 0; fi
printf 'python %s\\n' "$*" >> {CALLS_LOG_REF}
"""
    commands = {"python3": python, "python3.12": python}
    if manager == "pipx":
        commands["pipx"] = f"#!/bin/sh\nprintf 'pipx %s\\n' \"$*\" >> {CALLS_LOG_REF}\n"
    result = _run_installer(
        tmp_path,
        include_uv=False,
        local_repo=False,
        env={"OUROBOROS_INSTALL_RUNTIME": runtime},
        fake_commands=commands,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert f"ouroboros-ai[{extras}]" in calls
    assert "==" not in calls
    if manager == "pipx":
        assert "pipx install --force --python" in calls
        assert "pipx inject ouroboros-ai click>=8.1.0,<9.0.0" in calls
    else:
        assert "-m pip install --user --upgrade" in calls
