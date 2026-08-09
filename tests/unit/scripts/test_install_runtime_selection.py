"""Installer runtime-selection regression tests."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

import pytest

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
    drop_env: tuple[str, ...] = (),
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
    # A set-but-empty process variable still counts as "set" for trusted
    # ~/.ouroboros/.env precedence (mirrors config/loader.py), so tests that
    # exercise the user env file must genuinely unset the suite-level
    # OUROBOROS_TELEMETRY=0 rather than blank it.
    for key in drop_env:
        run_env.pop(key, None)

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


def _telemetry_probe_curl(tmp_path: Path) -> str:
    captures = tmp_path / "telemetry.log"
    return f'''#!/bin/sh
case "$*" in
  *"/capture/"*) ;;
  *) printf '{{"info":{{"version":"0.50.0"}}}}\n'; exit 0 ;;
esac
state="$HOME/.ouroboros/telemetry.json"
if ! grep -q '"notice_shown"[[:space:]]*:[[:space:]]*true' "$state" 2>/dev/null; then
  printf 'capture-before-notice\\n' >> "{captures!s}"
  exit 0
fi
printf '%s\\n' "$*" >> "{captures!s}"
exit 0
'''


def _telemetry_fake_commands(tmp_path: Path) -> dict[str, str]:
    """Use the test environment's schema while recording installer captures."""
    return {
        "curl": _telemetry_probe_curl(tmp_path),
        "python3": (f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'),
    }


def _wait_for_telemetry(tmp_path: Path) -> str:
    capture_path = tmp_path / "telemetry.log"
    deadline = time.monotonic() + 2.0
    captures = ""
    while time.monotonic() < deadline:
        if capture_path.exists():
            captures = capture_path.read_text(encoding="utf-8")
            if '"event":"install_completed"' in captures:
                break
        time.sleep(0.01)
    return captures


def test_installer_absent_config_retains_disclosed_default_on(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        local_repo=False,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "capture-before-notice" not in captures
    assert '"event":"install_started"' in captures
    assert '"event":"install_completed"' in captures
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1


def test_copied_installer_dangling_config_symlink_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.symlink_to(config.parent / "missing-config.yaml")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands={"curl": _telemetry_probe_curl(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    assert config.is_symlink()
    assert not config.exists()
    assert not (tmp_path / "telemetry.log").exists()
    assert not (tmp_path / "home" / ".ouroboros" / "telemetry.json").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_persisted_opt_out_suppresses_all_collection(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("telemetry:\n  enabled: false\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_malformed_config_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("telemetry: [\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()


@pytest.mark.parametrize(
    "config_text",
    (
        "telemetry:\n  enabled: true\nlogging:\n  level: verbose\n",
        "telemetry:\n  enabled: true\nbroken: [\n",
    ),
)
def test_installer_unrelated_invalid_config_fails_closed(
    tmp_path: Path,
    config_text: str,
) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(config_text, encoding="utf-8")

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_explicit_enable_cannot_override_persisted_opt_out(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("telemetry:\n  enabled: false\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": "1"},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_explicit_enable_cannot_override_malformed_config(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("telemetry: [\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": "1"},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_user_env_opt_out(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("OUROBOROS_TELEMETRY=0 # persisted opt-out\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_quoted_user_env_opt_out_with_comment(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="0" # persisted opt-out\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_quoted_do_not_track_with_comment(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('DO_NOT_TRACK="1" # off\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY", "DO_NOT_TRACK"),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_user_env_opt_out_two_spaces_after_export(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("export  OUROBOROS_TELEMETRY=0\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_user_env_opt_out_tab_after_export(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("export\tOUROBOROS_TELEMETRY=0\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_export_multi_space_quoted_do_not_track_with_comment(
    tmp_path: Path,
) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('export  DO_NOT_TRACK="1" # off\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY", "DO_NOT_TRACK"),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_uppercase_telemetry_off_flag(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": "OFF"},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_uppercase_do_not_track_yes_flag(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"DO_NOT_TRACK": "YES", "OUROBOROS_TELEMETRY": "1"},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_single_quoted_user_env_opt_out_with_comment(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("OUROBOROS_TELEMETRY='0' # off\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_quoted_user_env_opt_out_without_comment(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="0"\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_user_env_unclosed_quote_is_skipped(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="0\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "capture-before-notice" not in captures
    assert '"event":"install_completed"' in captures
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1


def test_installer_user_env_trailing_garbage_after_quote_is_skipped(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="0"x\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "capture-before-notice" not in captures
    assert '"event":"install_completed"' in captures
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1


def test_installer_honors_user_env_destination_override(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text(
        'export OUROBOROS_POSTHOG_HOST="https://telemetry-envfile.invalid"\n',
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        local_repo=False,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "https://telemetry-envfile.invalid/capture/" in captures
    assert "us.i.posthog.com" not in captures


def test_installer_process_env_wins_over_user_env_file(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text(
        "OUROBOROS_POSTHOG_HOST=https://telemetry-envfile.invalid\n",
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        local_repo=False,
        env={
            "OUROBOROS_TELEMETRY": "",
            "OUROBOROS_POSTHOG_HOST": "https://telemetry-process.invalid",
        },
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "https://telemetry-process.invalid/capture/" in captures
    assert "telemetry-envfile.invalid" not in captures


def test_installer_do_not_track_precedes_explicit_enable(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"DO_NOT_TRACK": "1", "OUROBOROS_TELEMETRY": "1"},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_unreadable_config_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("telemetry:\n  enabled: true\n", encoding="utf-8")
    config.chmod(0)
    if os.access(config, os.R_OK):
        config.chmod(0o600)
        pytest.skip("current user can still read mode-000 files")

    try:
        result = _run_installer(
            tmp_path,
            env={"OUROBOROS_TELEMETRY": ""},
            fake_commands=_telemetry_fake_commands(tmp_path),
        )
    finally:
        config.chmod(0o600)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()


def test_installer_notice_is_persisted_before_first_capture(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("telemetry:\n  enabled: true\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "capture-before-notice" not in captures
    assert '"event":"install_started"' in captures
    assert '"event":"install_completed"' in captures
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1
    state = (tmp_path / "home" / ".ouroboros" / "telemetry.json").read_text(encoding="utf-8")
    assert '"notice_shown": true' in state


def test_installer_repairs_corrupt_telemetry_json_and_persists_events(tmp_path: Path) -> None:
    """A pre-existing but unparseable telemetry.json must be repaired, not wedged.

    `ln` create-if-not-exists refuses forever once `$f` exists, so a corrupt
    file (partial write, disk error, garbage) would otherwise strand every
    future process on its own unpersisted uuid. The installer must instead
    replace it atomically (`mv`) and every process must adopt the survivor.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    state.write_text("not-json{{{\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    repaired = state.read_text(encoding="utf-8")
    match = re.search(r'"distinct_id"\s*:\s*"([^"]*)"', repaired)
    assert match is not None, f"telemetry.json was not repaired with a distinct_id: {repaired!r}"
    repaired_id = match.group(1)
    assert repaired_id

    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert f'"distinct_id":"{repaired_id}"' in captures


def test_fresh_install_keeps_direct_model_settings_optional() -> None:
    """The installer should start with the runtime default instead of forcing pins."""
    text = INSTALL_SH.read_text(encoding="utf-8")

    assert "Codex's current default model is ready to use." in text
    assert 'GUI_DEFAULT="n"' in text
    assert "Open direct model settings" in text
    assert "Using the runtime default model." in text


def test_installer_does_not_report_ready_after_runtime_setup_failure() -> None:
    """The activation command is a hard gate, not a best-effort side effect."""
    source = INSTALL_SH.read_text(encoding="utf-8")

    assert 'setup --runtime "$RUNTIME" --non-interactive || true' not in source
    assert 'if "$OUROBOROS_SETUP_CMD" setup --runtime "$RUNTIME" --non-interactive; then' in source
    assert 'exit "$setup_status"\n  fi' in source


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


def test_explicit_claude_uses_isolated_sdk_profile(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "claude"},
        fake_commands={"claude": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "Runtime: claude (from --runtime / OUROBOROS_INSTALL_RUNTIME)" in result.stdout
    assert "Installing .[claude,tui]" in result.stdout
    _assert_calls_include_pyproject_pins(calls, "claude")
    assert "--with mcp==" not in calls
    assert "ouroboros setup --runtime claude --non-interactive" in calls
    assert "Claude SDK is isolated on MCP 1.x" in result.stdout


def test_explicit_claude_cli_uses_dependency_free_mcp2_profile(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "claude-cli"},
        fake_commands={"claude": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "Installing .[claude-cli,tui]" in result.stdout
    assert "--with claude-agent-sdk" not in calls
    assert "--with mcp==" not in calls
    assert "ouroboros setup --runtime claude-cli --non-interactive" in calls


def test_explicit_claude_sdk_uses_isolated_sdk_profile(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_INSTALL_RUNTIME": "claude-sdk"},
        fake_commands={"claude": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "Installing .[claude-sdk,tui]" in result.stdout
    _assert_calls_include_pyproject_pins(calls, "claude-sdk")
    assert "--with mcp==" not in calls
    assert "ouroboros setup --runtime claude-sdk --non-interactive" in calls
    assert "Claude SDK is isolated on MCP 1.x" in result.stdout


def test_legacy_claude_config_preserves_sdk_profile_on_upgrade(tmp_path: Path) -> None:
    config_dir = tmp_path / "home" / ".ouroboros"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n  runtime_backend: claude\n",
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        fake_commands={"claude": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "Runtime: claude (preserved from" in result.stdout
    assert "Installing .[claude,tui]" in result.stdout
    assert "ouroboros setup --runtime claude --non-interactive" in calls


def test_cli_backed_claude_config_preserves_cli_profile_on_upgrade(tmp_path: Path) -> None:
    config_dir = tmp_path / "home" / ".ouroboros"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "orchestrator:\n  runtime_backend: claude_mcp\n",
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        fake_commands={"claude": "#!/bin/sh\nexit 0\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "Runtime: claude-cli (preserved from" in result.stdout
    assert "Installing .[claude-cli,tui]" in result.stdout
    assert "ouroboros setup --runtime claude-cli --non-interactive" in calls


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


def test_explicit_runtime_setup_failure_fails_install(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "pi"},
        fake_commands={
            "pipx": "#!/bin/sh\nprintf 'pipx %s\\n' \"$*\" >> __CALLS__\nexit 0\n".replace(
                "__CALLS__", str(tmp_path / "calls.log")
            ),
            "python3.12": '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.12; exit 0; fi\necho \'Python 3.12.0\'\n',
            "pi": "#!/bin/sh\nexit 0\n",
            "ouroboros": f'#!/bin/sh\nprintf \'ouroboros %s\\n\' "$*" >> {tmp_path / "calls.log"}\nif [ "$1" = "setup" ] && [ "$2" = "--runtime" ]; then exit 42; fi\nexit 0\n',
        },
    )

    assert result.returncode == 42
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "ouroboros setup --runtime pi --non-interactive" in calls
    assert "ouroboros setup refresh" not in calls


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

    assert "--with claude-agent-sdk==0.2.123" in calls


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
    "claude-cli": (),
    "claude-sdk": ("claude-agent-sdk", "anthropic"),
    "copilot": (),  # pyproject declares copilot extras as []; nothing to install
    "litellm": ("litellm",),
    "dashboard": (),  # compatibility alias; no runtime payload
    "mcp": ("mcp",),
    "tui": ("textual",),
}

_ALL_AGGREGATED_EXTRAS = {"claude", "copilot", "litellm", "dashboard", "tui"}


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
    """`[all]` mirrors co-installable payloads and omits isolated profiles.

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

    expected_extras = _ALL_AGGREGATED_EXTRAS

    # Sanity: pyproject's `all` aggregates every extra we know about.
    assert declared_in_all == expected_extras, (
        "pyproject [all] no longer matches the test mapping — update "
        "_EXTRA_TO_PACKAGES and install.sh together."
    )

    result = _run_installer(tmp_path, env={"OUROBOROS_INSTALL_RUNTIME": "all"})
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    expected_packages = {
        pkg
        for extra, pkgs in _EXTRA_TO_PACKAGES.items()
        if extra in expected_extras
        for pkg in pkgs
    }
    missing = sorted(pkg for pkg in expected_packages if f"--with {pkg}" not in calls)
    assert not missing, (
        f"install.sh `[all]` is missing --with entries for: {missing}.\n"
        "Update the case statement in scripts/install.sh to mirror the "
        "pyproject extras."
    )


def test_install_all_extras_match_pyproject_pins(tmp_path: Path) -> None:
    """`[all]` mirrors full pins for its compatible extras, not just names.

    Bot follow-up on PR #660: the package-name check was insufficient — a
    silent change to a pin range (e.g. relaxing ``<1.0.0`` to ``<2.0.0`` in
    pyproject without updating install.sh, or vice versa) would have slipped
    past the existing test. Each pyproject pin string is checked verbatim
    against the captured ``--with`` arguments so any drift fails here.
    """
    result = _run_installer(tmp_path, env={"OUROBOROS_INSTALL_RUNTIME": "all"})
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    _assert_calls_include_pyproject_pins(calls, *_ALL_AGGREGATED_EXTRAS)
    assert "--with mcp==" not in calls
    assert "--with claude-agent-sdk==0.2.123" in calls
