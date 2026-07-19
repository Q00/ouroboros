"""Installer runtime-selection regression tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _run_installer(
    tmp_path: Path,
    *,
    include_uv: bool = True,
    local_repo: bool = True,
    env: dict[str, str] | None = None,
    fake_commands: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool_bin_dir = tmp_path / "uv-tool-bin"
    tool_bin_dir.mkdir()
    calls = tmp_path / "calls.log"

    if include_uv:
        _write_executable(
            bin_dir / "uv",
            f"""#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "uv 0.0.0-test"
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "dir" ] && [ "$3" = "--bin" ]; then
  echo "{tool_bin_dir!s}"
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  cat > "{tool_bin_dir!s}/ouroboros" <<'SH'
#!/bin/sh
printf 'ouroboros %s\\n' "$*" >> "{calls!s}"
exit 0
SH
  chmod 755 "{tool_bin_dir!s}/ouroboros"
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

    if not include_uv:
        # Keep pipx/pip interpreter-selection tests independent of Python
        # binaries provided by the host runner. Individual tests opt candidates
        # in via fake_commands.
        for name in ("python3.14", "python3.13", "python3.12", "python3", "python"):
            _write_executable(bin_dir / name, "#!/bin/sh\nexit 1\n")

    if fake_commands:
        for name, content in fake_commands.items():
            _write_executable(bin_dir / name, content)

    install_sh = INSTALL_SH
    cwd = REPO_ROOT
    if not local_repo:
        install_sh = tmp_path / "install.sh"
        install_sh.write_text(INSTALL_SH.read_text(encoding="utf-8"), encoding="utf-8")
        install_sh.chmod(0o755)
        cwd = tmp_path

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
        ["bash", str(install_sh)],
        cwd=cwd,
        env=run_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _expected_pins_for_extras(*extra_names: str) -> list[str]:
    extras = _read_pyproject_extras()
    expected_pins: list[str] = []
    for extra_name in extra_names:
        for dep in extras.get(extra_name, []):
            stripped = dep.strip()
            if stripped:
                expected_pins.append(stripped.partition(";")[0].strip())
    return expected_pins


def _assert_calls_include_pyproject_pins(calls: str, *extra_names: str) -> None:
    expected_pins = _expected_pins_for_extras(*extra_names)
    assert expected_pins, "no pyproject pins discovered — parity check inert"

    drifted = sorted(pin for pin in expected_pins if f"--with {pin}" not in calls)
    assert not drifted, (
        "install.sh uv --with list has drifted from pyproject pins.\n"
        f"Missing or mismatched for extras {extra_names}: {drifted}\n"
        "Update the case statement in scripts/install.sh so each "
        "`--with <spec>` string matches pyproject [project.optional-dependencies]."
    )


def test_install_script_syntax_is_valid() -> None:
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_fresh_install_keeps_direct_model_settings_optional() -> None:
    """The installer should start with the runtime default instead of forcing pins."""
    text = INSTALL_SH.read_text(encoding="utf-8")

    assert "Codex's current default model is ready to use." in text
    assert 'GUI_DEFAULT="n"' in text
    assert "Open direct model settings" in text
    assert "Using the runtime default model." in text


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
    assert "Installing .[tui] ..." in result.stdout
    assert (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines() == [
        "uv tool install --upgrade --python >=3.12 . --with click>=8.1.0,<9.0.0 --with textual==8.2.8 --with textual-serve==1.1.3",
        "ouroboros setup --runtime opencode --non-interactive",
        "ouroboros setup refresh",
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
        "uv tool install --upgrade --python >=3.12 . --with click>=8.1.0,<9.0.0 --with mcp==1.28.1 --with claude-agent-sdk==0.2.110 --with anthropic==0.116.0 --with textual==8.2.8 --with textual-serve==1.1.3"
        in calls
    )
    _assert_calls_include_pyproject_pins(calls, "mcp", "claude")
    assert "ouroboros setup --runtime claude --non-interactive" in calls
    mcp_config = json.loads(
        (tmp_path / "home" / ".claude" / "mcp.json").read_text(encoding="utf-8")
    )
    assert mcp_config["mcpServers"]["ouroboros"] == {
        "command": "uvx",
        "args": [
            "--python",
            ">=3.12",
            "--from",
            "ouroboros-ai[mcp,claude]",
            "ouroboros",
            "mcp",
            "serve",
        ],
    }


def test_explicit_hermes_mcp_extra_matches_pyproject_pins(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "hermes"},
        fake_commands={"hermes": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "Runtime: hermes (from --runtime / OUROBOROS_INSTALL_RUNTIME)" in result.stdout
    _assert_calls_include_pyproject_pins(calls, "mcp")
    assert "ouroboros setup --runtime hermes --non-interactive" in calls


def test_explicit_pi_installs_base_and_runs_pi_setup(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "pi"},
        fake_commands={"pi": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "Runtime: pi (from --runtime / OUROBOROS_INSTALL_RUNTIME)" in result.stdout
    assert calls == [
        "uv tool install --upgrade --python >=3.12 . --with click>=8.1.0,<9.0.0 --with textual==8.2.8 --with textual-serve==1.1.3",
        "ouroboros setup --runtime pi --non-interactive",
        "ouroboros setup refresh",
    ]


def test_explicit_goose_installs_base_and_runs_goose_setup(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "goose"},
        fake_commands={"goose": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "Runtime: goose (from --runtime / OUROBOROS_INSTALL_RUNTIME)" in result.stdout
    assert "ouroboros setup --runtime goose --non-interactive" in calls


def test_explicit_gjc_installs_base_and_runs_gjc_setup(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "gjc"},
        fake_commands={"gjc": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "Runtime: gjc (from --runtime / OUROBOROS_INSTALL_RUNTIME)" in result.stdout
    assert "ouroboros setup --runtime gjc --non-interactive" in calls


def test_uv_install_setup_prefers_fresh_tool_bin_over_stale_path_command(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "pi"},
        fake_commands={
            "pi": "#!/bin/sh\nexit 0\n",
            "ouroboros": f"#!/bin/sh\nprintf 'stale-ouroboros %s\\n' \"$*\" >> {tmp_path / 'calls.log'}\nexit 0\n",
        },
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "ouroboros setup --runtime pi --non-interactive" in calls
    assert not any(call.startswith("stale-ouroboros setup") for call in calls)


def test_uv_install_setup_prefers_fresh_tool_bin_over_stale_home_local_bin(
    tmp_path: Path,
) -> None:
    home_local_bin = tmp_path / "home" / ".local" / "bin"
    home_local_bin.mkdir(parents=True)
    _write_executable(
        home_local_bin / "ouroboros",
        f"#!/bin/sh\nprintf 'stale-local-ouroboros %s\\n' \"$*\" >> {tmp_path / 'calls.log'}\nexit 0\n",
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "pi"},
        fake_commands={"pi": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "ouroboros setup --runtime pi --non-interactive" in calls
    assert not any(call.startswith("stale-local-ouroboros setup") for call in calls)


def test_pipx_install_setup_prefers_existing_path_command_over_stale_home_local_bin(
    tmp_path: Path,
) -> None:
    home_local_bin = tmp_path / "home" / ".local" / "bin"
    home_local_bin.mkdir(parents=True)
    _write_executable(
        home_local_bin / "ouroboros",
        f"#!/bin/sh\nprintf 'stale-home-ouroboros %s\\n' \"$*\" >> {tmp_path / 'calls.log'}\nexit 0\n",
    )

    python = '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.12; exit 0; fi\necho \'Python 3.12.0\'\n'
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "pi"},
        fake_commands={
            "pipx": '#!/bin/sh\nif [ "$1" = "--version" ]; then echo \'pipx 0.0.0-test\'; exit 0; fi\nprintf \'pipx %s\\n\' "$*" >> __CALLS__\nexit 0\n'.replace(
                "__CALLS__", str(tmp_path / "calls.log")
            ),
            "python3.12": python,
            "pi": "#!/bin/sh\nexit 0\n",
        },
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "ouroboros setup --runtime pi --non-interactive" in calls
    assert not any(call.startswith("stale-home-ouroboros setup") for call in calls)


def test_all_runtime_uv_install_uses_litellm_python_range(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "all"},
        fake_commands={"claude": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert ("uv tool install --upgrade --python >=3.12,<3.14 . --with click>=8.1.0,<9.0.0") in calls
    assert "--with litellm==1.91.0" in calls

    mcp_config = json.loads(
        (tmp_path / "home" / ".claude" / "mcp.json").read_text(encoding="utf-8")
    )
    assert mcp_config["mcpServers"]["ouroboros"]["args"][:2] == [
        "--python",
        ">=3.12,<3.14",
    ]


def test_non_litellm_uv_install_retains_python_312_floor(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "codex"},
        fake_commands={"codex": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "uv tool install --upgrade --python >=3.12 ." in calls
    assert ">=3.12,<3.14" not in calls


def test_all_runtime_pipx_selects_python_313_when_314_is_available(tmp_path: Path) -> None:
    python_314 = (
        '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.14; exit 0; fi\necho \'Python 3.14.0\'\n'
    )
    python_313 = (
        '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.13; exit 0; fi\necho \'Python 3.13.0\'\n'
    )
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "all"},
        fake_commands={
            "pipx": '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "pipx 0.0.0-test"; exit 0; fi\nprintf "pipx %s\\n" "$*" >> __CALLS__\nexit 0\n'.replace(
                "__CALLS__", str(tmp_path / "calls.log")
            ),
            "python3.14": python_314,
            "python3.13": python_313,
        },
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert f"pipx install --force --python {tmp_path}/bin/python3.13 .[all]" in calls
    assert f"--python {tmp_path}/bin/python3.14" not in calls


def test_all_runtime_pipx_fails_before_install_when_only_python_314_exists(
    tmp_path: Path,
) -> None:
    python_314 = (
        '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.14; exit 0; fi\necho \'Python 3.14.0\'\n'
    )
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "all"},
        fake_commands={
            "pipx": '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "pipx 0.0.0-test"; exit 0; fi\nprintf "pipx %s\\n" "$*" >> __CALLS__\nexit 0\n'.replace(
                "__CALLS__", str(tmp_path / "calls.log")
            ),
            "python3.14": python_314,
        },
    )

    assert result.returncode == 1
    assert "Python >=3.12,<3.14" in result.stdout
    assert "Python 3.13" in result.stdout
    calls_path = tmp_path / "calls.log"
    assert not calls_path.exists() or "pipx install" not in calls_path.read_text(encoding="utf-8")


def test_all_runtime_pipx_rejects_python_315_for_litellm_range(tmp_path: Path) -> None:
    python_315 = (
        '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.15; exit 0; fi\necho \'Python 3.15.0\'\n'
    )
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "all"},
        fake_commands={
            "pipx": '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "pipx 0.0.0-test"; exit 0; fi\nprintf "pipx %s\\n" "$*" >> __CALLS__\nexit 0\n'.replace(
                "__CALLS__", str(tmp_path / "calls.log")
            ),
            "python3.15": python_315,
            "python3": python_315,
        },
    )

    assert result.returncode == 1
    assert "Python >=3.12,<3.14" in result.stdout
    calls_path = tmp_path / "calls.log"
    assert not calls_path.exists() or "pipx install" not in calls_path.read_text(encoding="utf-8")


def test_all_runtime_pip_fallback_fails_before_install_when_only_python_314_exists(
    tmp_path: Path,
) -> None:
    python_314 = (
        '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.14; exit 0; fi\necho \'Python 3.14.0\'\n'
    )
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "all"},
        fake_commands={"python3": python_314},
    )

    assert result.returncode == 1
    assert "Python >=3.12,<3.14" in result.stdout
    assert "Python 3.13" in result.stdout


def test_all_runtime_pip_fallback_selects_313_when_generic_python3_is_314(
    tmp_path: Path,
) -> None:
    python_314 = (
        '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.14; exit 0; fi\necho \'Python 3.14.0\'\n'
    )
    python_313 = (
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then echo 3.13; exit 0; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then printf \'pip313 %s\\n\' "$*" >> __CALLS__; exit 0; fi\n'
        "echo 'Python 3.13.0'\n"
    ).replace("__CALLS__", str(tmp_path / "calls.log"))
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "all"},
        fake_commands={
            "python3": python_314,
            "python3.13": python_313,
        },
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "pip313 -m pip install --user --upgrade .[all] click>=8.1.0,<9.0.0" in calls


def test_all_runtime_pip_fallback_uses_compatible_python(tmp_path: Path) -> None:
    python_313 = (
        "#!/bin/sh\n"
        'if [ "$1" = "-c" ]; then echo 3.13; exit 0; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then printf \'pip %s\\n\' "$*" >> __CALLS__; exit 0; fi\n'
        "echo 'Python 3.13.0'\n"
    ).replace("__CALLS__", str(tmp_path / "calls.log"))
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "all"},
        fake_commands={"python3": python_313},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "pip -m pip install --user --upgrade .[all] click>=8.1.0,<9.0.0" in calls


def test_detects_pi_as_single_runtime_and_runs_pi_setup(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        fake_commands={"pi": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "Pi:" in result.stdout
    assert calls == [
        "uv tool install --upgrade --python >=3.12 . --with click>=8.1.0,<9.0.0 --with textual==8.2.8 --with textual-serve==1.1.3",
        "ouroboros setup --runtime pi --non-interactive",
        "ouroboros setup refresh",
    ]


def test_explicit_codex_refreshes_runtime_artifacts(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "codex"},
        fake_commands={"codex": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "ouroboros setup --runtime codex --non-interactive" in calls
    assert "ouroboros setup refresh" in calls


def test_preserved_non_codex_runtime_still_refreshes_runtime_artifacts(tmp_path: Path) -> None:
    config_dir = tmp_path / "home" / ".ouroboros"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n  runtime_backend: opencode\n",
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        fake_commands={"codex": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "ouroboros setup --runtime opencode --non-interactive" in calls
    assert "ouroboros setup refresh" in calls


def test_preserved_codex_runtime_refreshes_claude_skills_when_detected(tmp_path: Path) -> None:
    config_dir = tmp_path / "home" / ".ouroboros"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n  runtime_backend: codex\n",
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        fake_commands={
            "claude": (
                f'#!/bin/sh\nprintf "claude %s\\n" "$*" >> {tmp_path / "calls.log"}\nexit 0\n'
            ),
        },
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "ouroboros setup --runtime codex --non-interactive" in calls
    assert "claude plugin marketplace update ouroboros" in calls
    assert "claude plugin install ouroboros@ouroboros" in calls
    assert not (tmp_path / "home" / ".claude" / "mcp.json").exists()


def test_all_runtime_install_refreshes_runtime_artifacts(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "all"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert "ouroboros setup refresh" in calls


def test_pypi_lookup_failure_stays_stable_only_for_remote_install(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        local_repo=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "codex"},
        fake_commands={"curl": "#!/bin/sh\nexit 22\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert (
        "uv tool install --upgrade --python >=3.12 ouroboros-ai --with click>=8.1.0,<9.0.0 --with textual==8.2.8 --with textual-serve==1.1.3"
        in calls
    )
    assert "--prerelease=allow" not in calls


# ---------------------------------------------------------------------------
# pyproject ↔ install.sh `[all]` extras parity
# ---------------------------------------------------------------------------
#
# Maps every [project.optional-dependencies] extra to the package names that
# the installer's `[all]` --with list MUST cover under uv. Update both this
# table and install.sh whenever pyproject extras change. The mapping is
# explicit (rather than parsed from pyproject) so a wrong rename or removal
# fails loudly here instead of silently desyncing.
_EXTRA_TO_PACKAGES: dict[str, tuple[str, ...]] = {
    "claude": ("claude-agent-sdk", "anthropic"),
    "copilot": (),  # pyproject declares copilot extras as []; nothing to install
    "litellm": ("litellm",),
    "dashboard": (),  # compatibility alias; no runtime payload
    "mcp": ("mcp",),
    "tui": ("textual",),
}


def _read_pyproject_extras() -> dict[str, list[str]]:
    """Parse [project.optional-dependencies] from pyproject.toml."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover — Python <3.11 fallback
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    pyproject = REPO_ROOT / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["optional-dependencies"]


def test_install_all_extras_match_pyproject(tmp_path: Path) -> None:
    """`[all]` under uv must install every extra that pyproject declares.

    Catches the contract drift flagged by ouroboros-agent on PR #654:
    install.sh's hand-maintained --with list silently dropped tui, so
    users picking 'All' got an incomplete tree.
    """
    extras = _read_pyproject_extras()
    # `all` is a single self-referential entry, e.g.
    # ``["ouroboros-ai[claude,copilot,litellm,mcp,tui,dashboard]"]``. Pull the
    # bracketed names back out so we can compare against our mapping.
    declared_in_all: set[str] = set()
    for entry in extras.get("all", []):
        match = re.search(r"\[([^\]]+)\]", entry)
        if match:
            declared_in_all.update(name.strip() for name in match.group(1).split(","))

    expected_extras = set(_EXTRA_TO_PACKAGES.keys())

    # Sanity: pyproject's `all` aggregates every extra we know about.
    assert declared_in_all == expected_extras, (
        "pyproject [all] no longer matches the test mapping — update "
        "_EXTRA_TO_PACKAGES and install.sh together."
    )

    result = _run_installer(tmp_path, env={"OUROBOROS_INSTALL_RUNTIME": "all"})
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    expected_packages = {pkg for pkgs in _EXTRA_TO_PACKAGES.values() for pkg in pkgs}
    missing = sorted(pkg for pkg in expected_packages if f"--with {pkg}" not in calls)
    assert not missing, (
        f"install.sh `[all]` is missing --with entries for: {missing}.\n"
        "Update the case statement in scripts/install.sh to mirror the "
        "pyproject extras."
    )


def test_install_all_extras_match_pyproject_pins(tmp_path: Path) -> None:
    """`[all]` under uv must mirror pyproject's full version specifiers, not
    just the package names.

    Bot follow-up on PR #660: the package-name check was insufficient — a
    silent change to a pin range (e.g. relaxing ``<1.0.0`` to ``<2.0.0`` in
    pyproject without updating install.sh, or vice versa) would have slipped
    past the existing test. Each pyproject pin string is checked verbatim
    against the captured ``--with`` arguments so any drift fails here.
    """
    result = _run_installer(tmp_path, env={"OUROBOROS_INSTALL_RUNTIME": "all"})
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    _assert_calls_include_pyproject_pins(calls, *_EXTRA_TO_PACKAGES)
