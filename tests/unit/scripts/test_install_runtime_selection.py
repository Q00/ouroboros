"""Installer runtime-selection regression tests."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

_SHELL_QUOTE_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.,:/@%+=-"
)


def _expected_shell_quote(value: str) -> str:
    """Mirror install.sh's `_shell_quote` — ASCII-only, one shell word.

    The installer cannot delegate to `printf %q`: bash 3.2, the macOS system
    bash, escapes only part of a multibyte character and emits invalid UTF-8.
    So the expectation is spelled out here rather than read back out of bash.
    """
    if not value:
        return "$''"
    if all(char in _SHELL_QUOTE_SAFE for char in value):
        return value
    body = "".join(
        char
        if char in _SHELL_QUOTE_SAFE
        else "".join(f"\\{byte:03o}" for byte in char.encode("utf-8"))
        for char in value
    )
    return f"$'{body}'"


def _shell_word_reads_back_as(quoted: str, expected: str) -> bool:
    """A quoted word must survive the shell as exactly the original name."""
    read_back = subprocess.run(
        ["bash", "-c", f"printf '%sEND' {quoted}"],
        check=True,
        capture_output=True,
    )
    return read_back.stdout == expected.encode("utf-8") + b"END"


# Test-only variables read by the fake commands below (never by install.sh):
# keeping the stub bodies free of per-test paths makes their content identical
# across tests, which is what lets _write_executable share one on-disk copy.
CALLS_LOG_VAR = "OUROBOROS_TEST_CALLS_LOG"
TOOL_BIN_VAR = "OUROBOROS_TEST_TOOL_BIN"
OUROBOROS_STUB_VAR = "OUROBOROS_TEST_OUROBOROS_STUB"
# Where the fake curl records the requests it was asked to send.
CAPTURES_LOG_VAR = "OUROBOROS_TEST_CURL_LOG"
# Shell reference the fake commands append their call log to.
CALLS_LOG_REF = f'"${CALLS_LOG_VAR}"'

_STUB_CACHE: dict[str, Path] = {}
_STUB_CACHE_DIR: Path | None = None

_OUROBOROS_STUB = f"""#!/bin/sh
printf 'ouroboros %s\\n' "$*" >> {CALLS_LOG_REF}
exit 0
"""


def _cached_executable(content: str) -> Path:
    """Return a warm executable holding `content`, creating it at most once.

    macOS scans every newly created executable the first time it is exec'd
    (~0.3s per file), and the installer harness runs a handful of stubs per
    test. Writing each distinct stub body once and reusing that file keeps the
    scan to a single occurrence per body instead of one per test.
    """
    global _STUB_CACHE_DIR

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cached = _STUB_CACHE.get(digest)
    if cached is not None:
        return cached

    if _STUB_CACHE_DIR is None:
        _STUB_CACHE_DIR = Path(tempfile.mkdtemp(prefix="install-sh-stubs-"))
        atexit.register(shutil.rmtree, _STUB_CACHE_DIR, ignore_errors=True)

    cached = _STUB_CACHE_DIR / digest
    cached.write_text(content, encoding="utf-8")
    cached.chmod(0o755)
    _STUB_CACHE[digest] = cached
    return cached


def _write_executable(path: Path, content: str) -> None:
    path.unlink(missing_ok=True)
    path.symlink_to(_cached_executable(content))


def _run_installer(
    tmp_path: Path,
    *,
    include_uv: bool = True,
    local_repo: bool = True,
    piped: bool = False,
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
  echo "${TOOL_BIN_VAR}"
  exit 0
fi
if [ "$1" = "tool" ] && [ "$2" = "install" ]; then
  ln -sf "${OUROBOROS_STUB_VAR}" "${TOOL_BIN_VAR}/ouroboros"
fi
printf 'uv %s\\n' "$*" >> {CALLS_LOG_REF}
exit 0
""",
        )
    _write_executable(bin_dir / "ouroboros", _OUROBOROS_STUB)

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
            CALLS_LOG_VAR: str(calls),
            TOOL_BIN_VAR: str(tool_bin_dir),
            OUROBOROS_STUB_VAR: str(_cached_executable(_OUROBOROS_STUB)),
            CAPTURES_LOG_VAR: str(tmp_path / "telemetry.log"),
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

    command = ["bash"] if piped else ["bash", str(install_sh)]
    return subprocess.run(
        command,
        cwd=cwd,
        env=run_env,
        input=install_sh.read_text(encoding="utf-8") if piped else None,
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


def _telemetry_probe_curl() -> str:
    return f"""#!/bin/sh
case "$*" in
  *"/capture/"*) ;;
  *) printf '{{"info":{{"version":"0.50.0"}}}}\n'; exit 0 ;;
esac
state="$HOME/.ouroboros/telemetry.json"
if ! grep -q '"notice_shown"[[:space:]]*:[[:space:]]*true' "$state" 2>/dev/null; then
  printf 'capture-before-notice\\n' >> "${CAPTURES_LOG_VAR}"
  exit 0
fi
printf '%s\\n' "$*" >> "${CAPTURES_LOG_VAR}"
exit 0
"""


def _telemetry_fake_commands() -> dict[str, str]:
    """Use the test environment's schema while recording installer captures."""
    return {
        "curl": _telemetry_probe_curl(),
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
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "capture-before-notice" not in captures
    assert '"event":"install_completed"' in captures
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1


def test_piped_installer_does_not_require_bash_source(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("telemetry:\n  enabled: true\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        piped=True,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert "BASH_SOURCE" not in result.stderr


def test_piped_installer_defaults_to_mcp_v2_and_settings_gui(tmp_path: Path) -> None:
    result = _run_installer(tmp_path, local_repo=False, piped=True)

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "installing MCP v2 + settings GUI" in result.stdout
    assert "--with mcp==2.0.0" in calls
    assert "--with textual==8.2.8" in calls
    assert "--with textual-serve==1.1.3" in calls


def test_runtime_less_mcp_v2_guidance_lists_only_compatible_backends(tmp_path: Path) -> None:
    result = _run_installer(tmp_path, local_repo=False, piped=True)

    assert result.returncode == 0, result.stderr
    guidance = next(
        line for line in result.stdout.splitlines() if "Pick an MCP v2-compatible backend" in line
    )
    assert "claude-cli" in guidance
    assert "|claude|" not in guidance
    assert "claude-sdk" not in guidance


def test_installer_old_schema_without_telemetry_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("telemetry:\n  enabled: true\n", encoding="utf-8")

    old_package = tmp_path / "old-package" / "ouroboros" / "config"
    old_package.mkdir(parents=True)
    (old_package.parent / "__init__.py").write_text("", encoding="utf-8")
    (old_package / "__init__.py").write_text("", encoding="utf-8")
    (old_package / "models.py").write_text(
        "class OuroborosConfig:\n"
        "    @classmethod\n"
        "    def model_validate(cls, raw):\n"
        "        return cls()\n",
        encoding="utf-8",
    )
    python_wrapper = (
        "#!/bin/sh\n"
        "shift\n"
        f"PYTHONPATH={shlex.quote(str(old_package.parents[1]))} "
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )

    result = _run_installer(
        tmp_path,
        local_repo=False,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands={
            "curl": _telemetry_probe_curl(),
            "python3": python_wrapper,
            "python": python_wrapper,
        },
    )

    assert result.returncode == 0, result.stderr
    assert "AttributeError" not in result.stderr
    assert not (tmp_path / "telemetry.log").exists()


def test_readme_mcp_v2_default_selects_claude_cli_worker() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "pip install 'ouroboros-ai[mcp,tui]' && ouroboros setup --runtime claude-cli" in readme
    assert "ouroboros-ai[claude]" in readme


def test_copied_installer_dangling_config_symlink_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.symlink_to(config.parent / "missing-config.yaml")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands={"curl": _telemetry_probe_curl()},
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_uppercase_telemetry_off_flag(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": "OFF"},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_uppercase_do_not_track_yes_flag(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"DO_NOT_TRACK": "YES", "OUROBOROS_TELEMETRY": "1"},
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_fails_closed_on_unclosed_quote_at_eof(tmp_path: Path) -> None:
    """Round-7 (`test_installer_user_env_unclosed_quote_is_skipped`)
    established this as a plain skip -- telemetry stayed on, since the
    binding was just "unrecognized". Round-21 flips that contract: a
    missing closing quote is not a dotenv parse error at all -- it's the
    start of a value dotenv keeps reading across subsequent physical
    lines (a multiline value), which this single-line shell reader cannot
    follow. Silently skipping left telemetry ON while the application
    could resolve a real (stripped) opt-out from the exact same file, so
    this now fails closed instead.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="0\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_fails_closed_on_genuinely_multiline_telemetry_opt_out(
    tmp_path: Path,
) -> None:
    """A REAL two-physical-line dotenv value (`OUROBOROS_TELEMETRY="0` then
    a bare `"` on the next line) is exactly the shape dotenv_values()
    decodes to `"0\\n"`, strips, and resolves to disabled -- the reviewer's
    reproduction. Same fail-closed outcome as the truncated-at-EOF case.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="0\n"\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_fails_closed_on_genuinely_multiline_do_not_track(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('DO_NOT_TRACK="1\n"\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        drop_env=("OUROBOROS_TELEMETRY", "DO_NOT_TRACK"),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_fails_closed_on_multiline_value_that_looks_enabling(
    tmp_path: Path,
) -> None:
    """Safe-direction asymmetry (matches round-20's escape-bearing case):
    a multiline value that LOOKS like an enable (`"1` / `"`) still
    disables the installer run rather than being trusted as an enable.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="1\n"\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_user_env_trailing_garbage_after_quote_is_skipped(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="0"x\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        local_repo=False,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "https://telemetry-process.invalid/capture/" in captures
    assert "telemetry-envfile.invalid" not in captures


def test_installer_user_env_duplicate_telemetry_key_resolves_last_wins(tmp_path: Path) -> None:
    """A key repeated in the trusted .env must resolve to its LAST valid
    occurrence -- matching python-dotenv's dotenv_values(), which the
    application loader delegates to. An installer that instead kept the
    FIRST occurrence (a plain per-line "export once, then skip" bug) would
    disagree with the application about whether telemetry is enabled, from
    the exact same file.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("OUROBOROS_TELEMETRY=1\nOUROBOROS_TELEMETRY=0\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_user_env_duplicate_do_not_track_resolves_last_wins(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("DO_NOT_TRACK=0\nDO_NOT_TRACK=1\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY", "DO_NOT_TRACK"),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_user_env_duplicate_telemetry_key_reverse_order_enables(
    tmp_path: Path,
) -> None:
    """Reverse order of the previous case (disable, then enable): last-wins
    means the file's own final value is `1`, and there's no OTHER disabling
    control in play here, so telemetry ends up enabled -- proving the fix
    didn't accidentally make a duplicated key sticky-disabled regardless of
    the order its occurrences appear in.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("OUROBOROS_TELEMETRY=0\nOUROBOROS_TELEMETRY=1\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1


def test_installer_real_process_env_wins_over_duplicated_user_env_file(
    tmp_path: Path,
) -> None:
    """Real process-env precedence must survive the last-wins rework: a
    process-set `OUROBOROS_TELEMETRY=0` still wins even when the file
    contains a duplicated, enabling `OUROBOROS_TELEMETRY=1`.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("OUROBOROS_TELEMETRY=1\nOUROBOROS_TELEMETRY=1\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": "0"},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_fails_closed_on_escape_bearing_quoted_value(tmp_path: Path) -> None:
    """`dotenv_values()` decodes backslash escapes inside a double-quoted
    value (the two source characters `\\n` become an actual newline, then
    the application's `.strip()` removes it, resolving to disabled); this
    shell parser copies them literally (`0\\n`, matching no disabling
    value). Rather than risk agreeing with neither interpretation, the
    installer must refuse to trust this value and fail closed -- telemetry
    off for the whole run, no notice, no events.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="0\\n"\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_single_quoted_key(tmp_path: Path) -> None:
    """A key wrapped in one layer of matching quotes -- `'OUROBOROS_TELEMETRY'=0`
    -- must parse identically to the bare form, matching python-dotenv.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("'OUROBOROS_TELEMETRY'=0\n", encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_honors_double_quoted_key(tmp_path: Path) -> None:
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('"DO_NOT_TRACK"=1\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY", "DO_NOT_TRACK"),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_fails_closed_on_escape_bearing_value_even_when_it_looks_enabling(
    tmp_path: Path,
) -> None:
    """The fail-closed rule is deliberately asymmetric: an ambiguous value
    that LOOKS like an enable (`"1\\n"`) must still disable the installer
    run, not just an ambiguous-looking disable. This diverges from
    whatever the application resolves only in the safe (under-collecting)
    direction.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="1\\n"\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout


def test_installer_plain_quoted_value_without_backslash_still_enables(
    tmp_path: Path,
) -> None:
    """Control for the previous two cases: a double-quoted value with NO
    backslash anywhere in it is not ambiguous at all and must not trip the
    fail-closed path -- only genuine escape-bearing values do.
    """
    user_env = tmp_path / "home" / ".ouroboros" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text('OUROBOROS_TELEMETRY="1"\n', encoding="utf-8")

    result = _run_installer(
        tmp_path,
        drop_env=("OUROBOROS_TELEMETRY",),
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1


def test_installer_do_not_track_precedes_explicit_enable(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"DO_NOT_TRACK": "1", "OUROBOROS_TELEMETRY": "1"},
        fake_commands=_telemetry_fake_commands(),
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
            fake_commands=_telemetry_fake_commands(),
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
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert "capture-before-notice" not in captures
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
        fake_commands=_telemetry_fake_commands(),
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


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _extract_distinct_id(text: str) -> str:
    match = re.search(r'"distinct_id"\s*:\s*"([^"]*)"', text)
    assert match is not None, f"no distinct_id field found: {text!r}"
    return match.group(1)


def test_installer_replaces_non_uuid_distinct_id_with_fresh_uuid(tmp_path: Path) -> None:
    """A non-UUID value (valid JSON, but not an identity we ever minted) must
    never be emitted under or persisted verbatim -- it's corrupt state, not
    a salvageable identity, so a fresh canonical UUID replaces it.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    state.write_text(
        '{"distinct_id": "person@example.com", "created_at": "2020-01-01T00:00:00Z", '
        '"notice_shown": false}\n',
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    repaired_id = _extract_distinct_id(state.read_text(encoding="utf-8"))
    assert _UUID_RE.fullmatch(repaired_id)
    assert repaired_id != "person@example.com"

    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert f'"distinct_id":"{repaired_id}"' in captures


def test_installer_salvages_uuid_from_malformed_json_and_lowercases_it(tmp_path: Path) -> None:
    """Malformed JSON around an otherwise-valid UUID must be SALVAGED, not
    discarded for a fresh mint -- the same identity keeps being used across
    the repair instead of fragmenting into a new one.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    state.write_text(
        '{"distinct_id": "3F2504E0-4F89-11D3-9A0C-0305E82C3301", garbage', encoding="utf-8"
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    repaired_text = state.read_text(encoding="utf-8")
    assert _extract_distinct_id(repaired_text) == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    json.loads(repaired_text)  # the surrounding JSON must be repaired too, not just the field

    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert '"distinct_id":"3f2504e0-4f89-11d3-9a0c-0305e82c3301"' in captures


def test_installer_canonicalizes_uppercase_uuid_in_valid_json(tmp_path: Path) -> None:
    """A well-formed file with an uppercase UUID must be lowercased and
    persisted -- same identity, canonical case, so the Python loader
    (which now applies the same canonicalization) converges on it too.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    state.write_text(
        '{"distinct_id": "3F2504E0-4F89-11D3-9A0C-0305E82C3301", '
        '"created_at": "2020-01-01T00:00:00Z", "notice_shown": false}\n',
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert (
        _extract_distinct_id(state.read_text(encoding="utf-8"))
        == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    )

    captures = _wait_for_telemetry(tmp_path)
    assert '"distinct_id":"3f2504e0-4f89-11d3-9a0c-0305e82c3301"' in captures


_TELEMETRY_SHAPE_RE = re.compile(
    r'^\{"distinct_id": "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", '
    r'"created_at": "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", "notice_shown": (?:true|false)\}\n$'
)
_TELEMETRY_SKELETON_RE = re.compile(r'"distinct_id": "[^"]*"|"created_at": "[^"]*"')


def test_installer_repaired_telemetry_json_matches_fresh_install_shape(tmp_path: Path) -> None:
    """The repair path (`mv`) must write the exact same single-line shape as
    the create path (`ln`, on a fresh install) -- no format drift between
    the two code paths that can produce this file, so Python's json.loads
    and canonical-UUID check accept either one verbatim.
    """
    fresh_home = tmp_path / "fresh"
    fresh_home.mkdir()
    fresh_result = _run_installer(
        fresh_home,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )
    assert fresh_result.returncode == 0, fresh_result.stderr
    fresh_text = (fresh_home / "home" / ".ouroboros" / "telemetry.json").read_text(encoding="utf-8")
    assert _TELEMETRY_SHAPE_RE.match(fresh_text), fresh_text
    json.loads(fresh_text)

    repaired_home = tmp_path / "repaired"
    state_dir = repaired_home / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    corrupt_state = state_dir / "telemetry.json"
    corrupt_state.write_text("not-json{{{\n", encoding="utf-8")
    repaired_result = _run_installer(
        repaired_home,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )
    assert repaired_result.returncode == 0, repaired_result.stderr
    repaired_text = corrupt_state.read_text(encoding="utf-8")
    assert _TELEMETRY_SHAPE_RE.match(repaired_text), repaired_text
    json.loads(repaired_text)

    # Strip the id/timestamp so the static skeleton -- key names, order,
    # spacing, quoting, trailing newline -- can be compared verbatim.
    assert _TELEMETRY_SKELETON_RE.sub("<X>", fresh_text) == _TELEMETRY_SKELETON_RE.sub(
        "<X>", repaired_text
    )


_skip_if_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permission modes",
)


@_skip_if_root
def test_installer_drops_telemetry_when_identity_storage_is_unwritable(tmp_path: Path) -> None:
    """An invalid identity file in an unwritable directory can't be
    repaired. The installer must still succeed -- installation itself is
    unrelated to telemetry -- but must drop telemetry for this run rather
    than emit under a local candidate that never actually got persisted (an
    ephemeral identity no other process, or a later run of this one, could
    ever reproduce).
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    state.write_text("not-json{{{\n", encoding="utf-8")
    state_dir.chmod(0o500)
    try:
        result = _run_installer(
            tmp_path,
            env={"OUROBOROS_TELEMETRY": ""},
            fake_commands=_telemetry_fake_commands(),
        )
    finally:
        state_dir.chmod(0o700)  # restore write access so tmp cleanup can proceed

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "telemetry.log").exists()


@_skip_if_root
def test_installer_sends_telemetry_from_valid_identity_in_unwritable_dir(tmp_path: Path) -> None:
    """A directory being unwritable must not block telemetry when the
    existing identity is already fully valid -- adopting it needs no write
    at all, only a read.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    # notice_shown is pre-set to true: flipping it also needs a write, which
    # this scenario cannot do, and that's an orthogonal concern from what
    # this test is checking (event capture using the existing identity).
    # This is also a literal top-level `true` fixture, so it doubles as the
    # "notice correctly skipped, no duplicate" regression for the
    # structural notice_shown check.
    state.write_text(
        '{"distinct_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301", '
        '"created_at": "2020-01-01T00:00:00Z", "notice_shown": true}\n',
        encoding="utf-8",
    )
    state_dir.chmod(0o500)
    try:
        result = _run_installer(
            tmp_path,
            env={"OUROBOROS_TELEMETRY": ""},
            fake_commands=_telemetry_fake_commands(),
        )
    finally:
        state_dir.chmod(0o700)

    assert result.returncode == 0, result.stderr
    assert "Anonymous usage stats help improve Ouroboros" not in result.stdout
    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert '"distinct_id":"3f2504e0-4f89-11d3-9a0c-0305e82c3301"' in captures


def test_installer_shows_notice_when_only_nested_and_persists_top_level(
    tmp_path: Path,
) -> None:
    """A `notice_shown` sitting only inside another top-level object (not
    the document's own top level) must not suppress the disclosure --
    mirrors the reviewer's exact document and the distinct_id top-level-only
    rule from the previous round. Also verifies the notice is actually
    persisted at the top level afterward, not just printed once and lost.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    top_level_uuid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    state.write_text(
        f'{{"distinct_id": "{top_level_uuid}", "wrapper": {{"notice_shown": true}}}}',
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1

    captures = _wait_for_telemetry(tmp_path)
    assert "capture-before-notice" not in captures
    assert '"event":"install_completed"' in captures
    assert f'"distinct_id":"{top_level_uuid}"' in captures

    final_state = json.loads(state.read_text(encoding="utf-8"))
    assert final_state.get("distinct_id") == top_level_uuid
    assert final_state.get("notice_shown") is True


def test_installer_shows_notice_when_top_level_value_is_a_string_not_bool(
    tmp_path: Path,
) -> None:
    """A top-level `notice_shown` that isn't the literal boolean `true` (a
    string "true", here) must not suppress the disclosure either -- the
    check requires an actual bool, matching telemetry.py's `is True`.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    top_level_uuid = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    state.write_text(
        f'{{"distinct_id": "{top_level_uuid}", "created_at": "2020-01-01T00:00:00Z", '
        f'"notice_shown": "true"}}',
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Anonymous usage stats help improve Ouroboros") == 1

    final_state = json.loads(state.read_text(encoding="utf-8"))
    assert final_state.get("notice_shown") is True


_HOSTILE_UNAME_SCRIPT = (
    "#!/bin/sh\n"
    'if [ "$1" = "-s" ]; then printf \'Linux","leaked":"project-secret\'; exit 0; fi\n'
    'if [ "$1" = "-m" ]; then printf \'x86_64\\ninjected\\ttab\'; exit 0; fi\n'
    "exit 1\n"
)


def _capture_dashd_curl() -> str:
    """Fake curl that logs ONLY the raw `-d` argument of each call, one per
    line -- lets a test `json.loads()` the exact payload instead of
    substring-matching the whole curl invocation (which also contains
    unrelated flags like `-fsS -X POST ...`).
    """
    return f"""#!/bin/sh
prev=""
for arg in "$@"; do
  if [ "$prev" = "-d" ]; then
    printf '%s\\n' "$arg" >> "${CAPTURES_LOG_VAR}"
  fi
  prev="$arg"
done
exit 0
"""


def _build_no_python3_path(target_dir: Path) -> str:
    """Symlink every real executable from /usr/bin and /bin into
    `target_dir`, except anything named python*/pip* -- gives a PATH with
    normal coreutils (sed, tr, mkdir, uuidgen, mv, ln, rm, date, ...) but no
    Python interpreter anywhere on it, for exercising an installer code
    path's NO_PYTHON3 fallback in real isolation rather than just hoping no
    python3 happens to be first on some CI runner's PATH.
    """
    target_dir.mkdir(exist_ok=True)
    for source_dir in (Path("/usr/bin"), Path("/bin")):
        if not source_dir.is_dir():
            continue
        for entry in source_dir.iterdir():
            name = entry.name
            if name.startswith("python") or name.startswith("pip"):
                continue
            link = target_dir / name
            if link.exists() or link.is_symlink():
                continue
            try:
                link.symlink_to(entry)
            except OSError:
                continue
    return str(target_dir)


def _drive_telemetry_ping(
    tmp_path: Path, *, home: Path, path_env: str, captures: Path, extra_setup: str = ""
) -> subprocess.CompletedProcess[str]:
    """Extract `_telemetry_distinct_id` + `_telemetry_ping` verbatim from
    install.sh and run them in an isolated driver, bypassing `_telemetry_enabled`
    (irrelevant to payload construction) and giving full control over PATH --
    the standard `_run_installer` harness can't hide python3 since its PATH
    always includes the real /usr/bin.
    """
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/bin/bash\nset -u\n"
        + _extract_function("_telemetry_distinct_id")
        + _extract_function("_telemetry_ping")
        + f"""
PH_API_KEY="phc_test_key_000000000000000000"
PH_HOST="https://telemetry.invalid"
_telemetry_enabled() {{ return 0; }}
{extra_setup}
_telemetry_ping install_started "is_local=true" "pre=no" "version=1.2.3"
wait
""",
        encoding="utf-8",
    )
    driver.chmod(0o755)

    run_env = os.environ.copy()
    run_env["HOME"] = str(home)
    run_env["PATH"] = path_env
    run_env[CAPTURES_LOG_VAR] = str(captures)
    return subprocess.run(
        ["bash", str(driver)],
        env=run_env,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_installer_ping_escapes_hostile_uname_output_with_python3(tmp_path: Path) -> None:
    """A hostile executable named `uname` earlier on PATH -- returning a
    value crafted to break out of its declared JSON string and inject an
    undeclared `"leaked":"project-secret"` property, plus a literal control
    character (newline/tab) in the other field -- must never be able to
    alter the payload's structure. python3's real encoder is on PATH here,
    so this exercises the structural-encoding path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    captures = tmp_path / "captures.log"
    _write_executable(bin_dir / "uname", _HOSTILE_UNAME_SCRIPT)
    _write_executable(bin_dir / "curl", _capture_dashd_curl())

    result = _drive_telemetry_ping(
        tmp_path,
        home=tmp_path / "home",
        path_env=f"{bin_dir}:/usr/bin:/bin",
        captures=captures,
    )

    assert result.returncode == 0, result.stderr
    lines = [line for line in captures.read_text(encoding="utf-8").splitlines() if line]
    assert lines, f"no payload captured; stderr={result.stderr!r}"
    payload = json.loads(lines[0])
    assert "leaked" not in payload
    assert "leaked" not in payload.get("properties", {})
    assert payload["properties"]["os"] == "unknown"
    assert payload["properties"]["is_local"] == "true"


def test_installer_ping_escapes_hostile_uname_output_without_python3(tmp_path: Path) -> None:
    """Same hostile-uname probe, forced down the NO_PYTHON3 shell-sanitizer
    fallback (a curated PATH with no python3/python anywhere on it): every
    value must instead survive a strict safe-token filter before string
    concatenation, so structure mutation is still impossible either way.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    captures = tmp_path / "captures.log"
    _write_executable(bin_dir / "uname", _HOSTILE_UNAME_SCRIPT)
    _write_executable(bin_dir / "curl", _capture_dashd_curl())
    no_python_dir = _build_no_python3_path(tmp_path / "no-python-bin")

    home = tmp_path / "home"
    state_dir = home / ".ouroboros"
    state_dir.mkdir(parents=True)
    # Pre-seed an already-valid canonical identity so _telemetry_distinct_id
    # succeeds via its own sed-only NO_PYTHON3 fallback, independent of
    # whether `uuidgen` happens to exist on this platform.
    (state_dir / "telemetry.json").write_text(
        '{"distinct_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301", '
        '"created_at": "2020-01-01T00:00:00Z", "notice_shown": true}\n',
        encoding="utf-8",
    )

    result = _drive_telemetry_ping(
        tmp_path,
        home=home,
        path_env=f"{bin_dir}:{no_python_dir}",
        captures=captures,
    )

    assert result.returncode == 0, result.stderr
    assert "command not found" not in result.stderr.lower(), result.stderr
    lines = [line for line in captures.read_text(encoding="utf-8").splitlines() if line]
    assert lines, f"no payload captured; stderr={result.stderr!r}"
    payload = json.loads(lines[0])
    assert "leaked" not in payload
    assert "leaked" not in payload.get("properties", {})
    assert payload["properties"]["os"] == "unknown"
    assert payload["distinct_id"] == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


def test_installer_ping_uses_exact_declared_property_structure(tmp_path: Path) -> None:
    """A normal install run (real uname, no hostile injection): every
    captured event still parses as JSON with EXACTLY the declared top-level
    and `properties` keys -- the installer-side structural whitelist,
    verified end-to-end through the real installer harness.
    """
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands={
            **_telemetry_fake_commands(),
            "curl": _capture_dashd_curl(),
        },
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    lines = [line for line in captures.splitlines() if line]
    events = {}
    for line in lines:
        payload = json.loads(line)
        assert set(payload.keys()) == {"api_key", "event", "distinct_id", "properties"}
        events[payload["event"]] = payload

    assert set(events) == {"install_started", "install_completed"}
    assert set(events["install_started"]["properties"].keys()) == {
        "os",
        "version",
        "ref",
    }
    assert set(events["install_completed"]["properties"].keys()) == {
        "os",
        "runtime",
        "version",
        "ref",
    }


def test_installer_ping_ref_defaults_to_direct_when_unset(tmp_path: Path) -> None:
    """No `OUROBOROS_INSTALL_REF` in the environment -- both telemetry events
    must carry `ref=direct`, the documented fallback for an install with no
    channel attribution. `drop_env` (not just omitting it from `env`) is
    required here: a set-but-empty variable still counts as "set" for shell
    parameter expansion, so this must genuinely unset the key rather than
    rely on it being absent from the test's own `env` dict.
    """
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        drop_env=("OUROBOROS_INSTALL_REF",),
        fake_commands={
            **_telemetry_fake_commands(),
            "curl": _capture_dashd_curl(),
        },
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    events = {}
    for line in captures.splitlines():
        if not line:
            continue
        payload = json.loads(line)
        events[payload["event"]] = payload

    assert events["install_completed"]["properties"]["ref"] == "direct"


def test_installer_ping_ref_carries_approved_channel_token(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": "", "OUROBOROS_INSTALL_REF": "readme-hero"},
        fake_commands={
            **_telemetry_fake_commands(),
            "curl": _capture_dashd_curl(),
        },
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    events = {}
    for line in captures.splitlines():
        if not line:
            continue
        payload = json.loads(line)
        events[payload["event"]] = payload

    assert events["install_completed"]["properties"]["ref"] == "readme-hero"


def test_installer_ping_ref_folds_valid_shaped_unknown_to_direct(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": "", "OUROBOROS_INSTALL_REF": "private-project-2278"},
        fake_commands={
            **_telemetry_fake_commands(),
            "curl": _capture_dashd_curl(),
        },
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    payload = json.loads(next(line for line in captures.splitlines() if line))
    assert payload["properties"]["ref"] == "direct"
    assert "private-project-2278" not in captures


def test_installer_ping_ref_degrades_hostile_value_to_direct(tmp_path: Path) -> None:
    """Invalid shell-shaped values also fold to the closed `direct` token."""
    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": "", "OUROBOROS_INSTALL_REF": "evil; rm -rf /"},
        fake_commands={
            **_telemetry_fake_commands(),
            "curl": _capture_dashd_curl(),
        },
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    events = {}
    for line in captures.splitlines():
        if not line:
            continue
        payload = json.loads(line)
        events[payload["event"]] = payload

    assert events["install_completed"]["properties"]["ref"] == "direct"


def test_installer_ignores_nested_distinct_id_in_valid_json_and_mints_fresh(
    tmp_path: Path,
) -> None:
    """A UUID nested somewhere other than the TOP LEVEL of a valid JSON
    document is not a real identity -- telemetry.py's loader only ever looks
    at `data["distinct_id"]` on the top-level dict, so the installer must
    match that instead of a text search that finds a match anywhere. Salvage
    (reusing an in-file value) also must NOT apply here: telemetry.py only
    salvages from genuinely unparseable text, never from valid-but-wrong-
    shaped JSON, so this must mint an unrelated fresh id, not the nested one.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    nested_uuid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    # notice_shown deliberately omitted: this state needs a real repair (a
    # fresh mint, since there's no usable top-level id), and the repair
    # candidate always starts `notice_shown: false` -- pre-seeding `true`
    # here would race `_telemetry_notice`'s own raw-text "already shown"
    # check against that overwrite for no reason relevant to this test.
    state.write_text(
        f'{{"wrapper":{{"distinct_id":"{nested_uuid}"}}}}',
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    repaired_text = state.read_text(encoding="utf-8")
    repaired_id = _extract_distinct_id(repaired_text)
    assert _UUID_RE.fullmatch(repaired_id)
    assert repaired_id != nested_uuid
    assert _TELEMETRY_SHAPE_RE.match(repaired_text), repaired_text  # top-level, canonical shape

    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert f'"distinct_id":"{repaired_id}"' in captures
    assert f'"distinct_id":"{nested_uuid}"' not in captures


def test_installer_adopts_last_value_for_duplicate_top_level_distinct_id(
    tmp_path: Path,
) -> None:
    """Duplicate top-level `distinct_id` keys: a JSON parser (and
    telemetry.py's loader, which uses one) keeps the LAST occurrence. A
    sed-based first-match would silently disagree with what Python adopts
    from the exact same file -- fragmenting one installation across two
    identities. The installer must match Python's last-wins semantics.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    uuid_a = "11111111-1111-1111-1111-111111111111"
    uuid_b = "22222222-2222-2222-2222-222222222222"
    state.write_text(
        f'{{"distinct_id":"{uuid_a}","distinct_id":"{uuid_b}",'
        f'"created_at":"2020-01-01T00:00:00Z","notice_shown":true}}',
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert f'"distinct_id":"{uuid_b}"' in captures
    assert f'"distinct_id":"{uuid_a}"' not in captures


def test_installer_still_salvages_uuid_from_unparseable_text_with_nested_shape(
    tmp_path: Path,
) -> None:
    """Unparseable text containing what LOOKS like a nested `distinct_id`
    field must still be salvaged via the lenient text search -- the
    top-level-only rule applies to VALID JSON; telemetry.py's own salvage
    path for genuinely unparseable text is the same raw-text regex search,
    unaware of "nesting" since there's no successfully parsed structure to
    speak of.
    """
    state_dir = tmp_path / "home" / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    state.write_text(
        'garbage {"distinct_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301"} more garbage',
        encoding="utf-8",
    )

    result = _run_installer(
        tmp_path,
        env={"OUROBOROS_TELEMETRY": ""},
        fake_commands=_telemetry_fake_commands(),
    )

    assert result.returncode == 0, result.stderr
    repaired_text = state.read_text(encoding="utf-8")
    assert _extract_distinct_id(repaired_text) == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    json.loads(repaired_text)

    captures = _wait_for_telemetry(tmp_path)
    assert '"event":"install_completed"' in captures
    assert '"distinct_id":"3f2504e0-4f89-11d3-9a0c-0305e82c3301"' in captures


def _extract_function(name: str) -> str:
    """Pull a bash function definition verbatim out of install.sh.

    Lets a driver script exercise real installer logic directly -- e.g. to
    race the repair-lock protocol, or to isolate `_telemetry_ping` under a
    controlled PATH -- without paying for a full installer subprocess tree
    per test.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?\n\}}\n", text)
    assert match is not None, f"could not locate {name}() in install.sh"
    return match.group(0)


def _race_distinct_id_drivers(
    tmp_path: Path, telemetry_json_content: str, *, n_procs: int = 6
) -> tuple[list[str], Path, Path]:
    """Launch N processes that all call `_telemetry_distinct_id` against the
    same pre-seeded telemetry.json at once, released from a shared barrier
    to maximize the actual race window. Returns (outputs, state file path,
    state dir path) for the caller to assert convergence on.
    """
    home = tmp_path / "home"
    state_dir = home / ".ouroboros"
    state_dir.mkdir(parents=True)
    state = state_dir / "telemetry.json"
    state.write_text(telemetry_json_content, encoding="utf-8")

    barrier = tmp_path / "go"
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "#!/bin/bash\nset -u\n"
        + _extract_function("_telemetry_distinct_id")
        + f"""
while [ ! -f {shlex.quote(str(barrier))} ]; do
  sleep 0.01
done
_telemetry_distinct_id
""",
        encoding="utf-8",
    )
    driver.chmod(0o755)

    driver_env = os.environ.copy()
    driver_env["HOME"] = str(home)
    procs = [
        subprocess.Popen(
            ["bash", str(driver)],
            env=driver_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(n_procs)
    ]
    # All drivers are already spawned and spinning on the barrier file before
    # it's created, so releasing them here maximizes the actual race window.
    barrier.write_text("go", encoding="utf-8")

    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=10)
        assert proc.returncode == 0, err
        outputs.append(out.strip())
    return outputs, state, state_dir


def test_concurrent_corrupt_telemetry_json_repair_converges_on_one_id(tmp_path: Path) -> None:
    """N processes racing a corrupt telemetry.json must all adopt the SAME id.

    Regression for a last-writer-wins race: an unconditional `mv` in the
    repair path let every process that saw the corrupt file mint and persist
    its own uuid, so different processes emitted events under different
    distinct_ids while only the last writer's id survived on disk. The
    repair-lock protocol (mkdir "$f.repair.lock") must make every racer
    converge on exactly one id — whoever's file is actually on disk after
    all of them finish.
    """
    outputs, state, state_dir = _race_distinct_id_drivers(tmp_path, "not-json{{{\n")

    assert all(outputs), f"a driver returned an empty distinct_id: {outputs}"
    assert len(set(outputs)) == 1, f"driver processes disagreed on distinct_id: {outputs}"

    repaired = state.read_text(encoding="utf-8")
    match = re.search(r'"distinct_id"\s*:\s*"([^"]*)"', repaired)
    assert match is not None, repaired
    assert match.group(1) == outputs[0], (
        "the id every process adopted does not match what's actually persisted"
    )

    leftovers = [p.name for p in state_dir.iterdir() if p.name != "telemetry.json"]
    assert not leftovers, f"repair left stray tmp/lock artifacts behind: {leftovers}"


def test_concurrent_salvage_of_malformed_json_converges_on_the_salvaged_id(
    tmp_path: Path,
) -> None:
    """N processes racing a malformed-but-UUID-bearing telemetry.json must all
    salvage and converge on the SAME canonicalized id -- specifically the one
    already in the file, never a freshly minted substitute.

    Regression for a bug found while implementing UUID canonicalization: the
    winner-side re-validation under the repair lock initially used a lenient
    check that treated ANY pattern-shaped id in the file as "already
    repaired", even while the file was still malformed (pre-`mv`). That made
    every racer skip its own `mv`, permanently leaving the malformed JSON on
    disk while each process still returned a clean canonical id in memory --
    so a later reader (this file, or the Python loader) would never actually
    see a repaired, parseable state.
    """
    outputs, state, state_dir = _race_distinct_id_drivers(
        tmp_path, '{"distinct_id": "3F2504E0-4F89-11D3-9A0C-0305E82C3301", garbage'
    )

    assert all(outputs), f"a driver returned an empty distinct_id: {outputs}"
    assert len(set(outputs)) == 1, f"driver processes disagreed on distinct_id: {outputs}"
    assert outputs[0] == "3f2504e0-4f89-11d3-9a0c-0305e82c3301", (
        f"processes converged on {outputs[0]!r} instead of the salvaged id"
    )

    repaired = state.read_text(encoding="utf-8")
    json.loads(repaired)  # the surrounding JSON must actually be repaired, not just the field
    assert _extract_distinct_id(repaired) == outputs[0]

    leftovers = [p.name for p in state_dir.iterdir() if p.name != "telemetry.json"]
    assert not leftovers, f"repair left stray tmp/lock artifacts behind: {leftovers}"


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
    assert "Installing .[mcp,tui] ..." in result.stdout
    assert (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines() == [
        "uv tool install --upgrade --python >=3.12 . --with click>=8.1.0,<9.0.0 --with mcp==2.0.0 --with textual==8.2.8 --with textual-serve==1.1.3",
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
    assert "Installing .[mcp,tui]" in result.stdout
    assert "--with claude-agent-sdk" not in calls
    assert "--with mcp==2.0.0" in calls
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
    assert "Installing .[mcp,tui]" in result.stdout
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
        "uv tool install --upgrade --python >=3.12 . --with click>=8.1.0,<9.0.0 --with mcp==2.0.0 --with textual==8.2.8 --with textual-serve==1.1.3",
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
                "__CALLS__", CALLS_LOG_REF
            ),
            "python3.12": '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.12; exit 0; fi\necho \'Python 3.12.0\'\n',
            "pi": "#!/bin/sh\nexit 0\n",
            "ouroboros": f'#!/bin/sh\nprintf \'ouroboros %s\\n\' "$*" >> {CALLS_LOG_REF}\nif [ "$1" = "setup" ] && [ "$2" = "--runtime" ]; then exit 42; fi\nexit 0\n',
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
            "ouroboros": f"#!/bin/sh\nprintf 'stale-ouroboros %s\\n' \"$*\" >> {CALLS_LOG_REF}\nexit 0\n",
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
        f"#!/bin/sh\nprintf 'stale-local-ouroboros %s\\n' \"$*\" >> {CALLS_LOG_REF}\nexit 0\n",
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
        f"#!/bin/sh\nprintf 'stale-home-ouroboros %s\\n' \"$*\" >> {CALLS_LOG_REF}\nexit 0\n",
    )

    python = '#!/bin/sh\nif [ "$1" = "-c" ]; then echo 3.12; exit 0; fi\necho \'Python 3.12.0\'\n'
    result = _run_installer(
        tmp_path,
        include_uv=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "pi"},
        fake_commands={
            "pipx": '#!/bin/sh\nif [ "$1" = "--version" ]; then echo \'pipx 0.0.0-test\'; exit 0; fi\nprintf \'pipx %s\\n\' "$*" >> __CALLS__\nexit 0\n'.replace(
                "__CALLS__", CALLS_LOG_REF
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

    assert "--with claude-agent-sdk==0.2.139" in calls
    assert "--with anthropic==0.122.0" in calls


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
                "__CALLS__", CALLS_LOG_REF
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
                "__CALLS__", CALLS_LOG_REF
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
                "__CALLS__", CALLS_LOG_REF
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
    ).replace("__CALLS__", CALLS_LOG_REF)
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
    ).replace("__CALLS__", CALLS_LOG_REF)
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
        "uv tool install --upgrade --python >=3.12 . --with click>=8.1.0,<9.0.0 --with mcp==2.0.0 --with textual==8.2.8 --with textual-serve==1.1.3",
        "ouroboros setup --runtime pi --non-interactive",
        "ouroboros setup refresh",
    ]


def test_installer_configures_omp_after_runtime_setup_source_order() -> None:
    source = INSTALL_SH.read_text(encoding="utf-8")
    assert source.rfind("_ensure_omp_tool_call_timeout") > source.index(
        "setup --runtime $RUNTIME --non-interactive"
    )


def test_installer_preserves_higher_omp_tool_timeout(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        fake_commands={
            "omp": '#!/bin/sh\nif [ "$2" = "get" ]; then echo 120000; else printf \'omp %s\\n\' "$*" >> "$OUROBOROS_TEST_CALLS_LOG"; fi\nexit 0\n',
        },
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "omp config set extensionHandlers.toolCallTimeoutMs 60000" not in calls


def test_installer_preserves_oversized_omp_tool_timeout(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        fake_commands={
            "omp": '#!/bin/sh\nif [ "$2" = "get" ]; then echo 9223372036854775808; else printf \'omp %s\\n\' "$*" >> "$OUROBOROS_TEST_CALLS_LOG"; fi\nexit 0\n',
        },
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "omp config set extensionHandlers.toolCallTimeoutMs 60000" not in calls


def test_installer_sets_omp_tool_call_timeout_when_omp_is_available(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        fake_commands={
            "omp": '#!/bin/sh\nprintf \'omp %s\\n\' "$*" >> "$OUROBOROS_TEST_CALLS_LOG"\nexit 0\n',
        },
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "omp config set extensionHandlers.toolCallTimeoutMs 60000" in calls


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
            "claude": (f'#!/bin/sh\nprintf "claude %s\\n" "$*" >> {CALLS_LOG_REF}\nexit 0\n'),
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
        "uv tool install --upgrade --python >=3.12 ouroboros-ai --with click>=8.1.0,<9.0.0 --with mcp==2.0.0 --with textual==8.2.8 --with textual-serve==1.1.3"
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
    assert "--with claude-agent-sdk==0.2.139" in calls
    assert "--with anthropic==0.122.0" in calls


_DSH_STUB = f"""#!/bin/sh
printf 'dsh %s\\n' "$*" >> {CALLS_LOG_REF}
exit 0
"""


def _dsh_calls(tmp_path: Path) -> list[str]:
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    return [line for line in calls if line.startswith("dsh ")]


def test_installer_without_dsh_touches_no_profile(tmp_path: Path) -> None:
    """The bundle install is conditional on the host actually being installed."""
    result = _run_installer(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not _dsh_calls(tmp_path)
    assert "DeepSeek Harness plugin" not in result.stdout


def test_installer_covers_the_profile_a_dsh_user_boots(tmp_path: Path) -> None:
    """`dsh web` scaffolds `web`, so that is the profile worth covering blind."""
    result = _run_installer(tmp_path, fake_commands={"dsh": _DSH_STUB})

    assert result.returncode == 0, result.stderr
    assert _dsh_calls(tmp_path) == [
        "dsh plugin --profile web add github:Q00/ouroboros#main&path:integrations/dsh-plugin"
    ]


def test_installer_refreshes_profiles_that_already_opted_in(tmp_path: Path) -> None:
    """An existing install picks up a release; an unrelated profile is left alone."""
    profiles = tmp_path / "home" / ".dsh" / "profiles"
    (profiles / "tui").mkdir(parents=True)
    (profiles / "tui" / "package.json").write_text(
        '{"dependencies": {"dsh-ouroboros": "github:Q00/ouroboros"}}', encoding="utf-8"
    )
    (profiles / "unrelated").mkdir(parents=True)
    (profiles / "unrelated" / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"not-dsh-ouroboros": "1.0.0"},
                "description": "mentions dsh-ouroboros without installing it",
            }
        ),
        encoding="utf-8",
    )
    (profiles / "malformed").mkdir(parents=True)
    (profiles / "malformed" / "package.json").write_text(
        '{"dependencies": {"dsh-ouroboros":', encoding="utf-8"
    )

    result = _run_installer(
        tmp_path,
        fake_commands={
            "dsh": _DSH_STUB,
            "python3": f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
        },
    )

    assert result.returncode == 0, result.stderr
    profiles_called = sorted(
        line.split("--profile ")[1].split()[0] for line in _dsh_calls(tmp_path)
    )
    assert profiles_called == ["tui", "web"]


def test_installer_preserves_an_opted_in_profile_name_as_one_argument(tmp_path: Path) -> None:
    """Profile discovery must not split or expand user-owned directory names."""
    profile = tmp_path / "home" / ".dsh" / "profiles" / "team alpha"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        '{"dependencies": {"dsh-ouroboros": "github:Q00/ouroboros"}}', encoding="utf-8"
    )
    dsh_stub = f"""#!/bin/sh
printf 'dsh-profile:%s\\n' "$3" >> {CALLS_LOG_REF}
exit 0
"""

    result = _run_installer(
        tmp_path,
        fake_commands={
            "dsh": dsh_stub,
            "python3": f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
        },
    )

    assert result.returncode == 0, result.stderr
    profiles_called = [
        line.removeprefix("dsh-profile:")
        for line in (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
        if line.startswith("dsh-profile:")
    ]
    assert profiles_called == ["web", "team alpha"]


@pytest.mark.parametrize(
    "profile_name",
    [
        "team alpha",
        "team's alpha",
        "team$alpha*",
        "team$(printf PWNED)",
        "team`printf PWNED`",
        "team;printf PWNED",
        "team\nalpha",
        "team\n",
        "team\ralpha",
        "team\x1b[31mred",
        "team\u202etxt",
        "team\u200btxt",
        "팀 alpha",
    ],
)
def test_installer_quotes_profile_in_manual_recovery_command(
    tmp_path: Path, profile_name: str
) -> None:
    """The printed fallback command must preserve a profile name as one shell argument."""
    profile = tmp_path / "home" / ".dsh" / "profiles" / profile_name
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        '{"dependencies": {"dsh-ouroboros": "github:Q00/ouroboros"}}', encoding="utf-8"
    )

    result = _run_installer(
        tmp_path,
        fake_commands={
            "dsh": "#!/bin/sh\nexit 1\n",
            "python3": f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
        },
    )

    assert result.returncode == 0, result.stderr
    manual_commands = [
        line.split("Manual install: ", 1)[1]
        for line in result.stdout.splitlines()
        if "Manual install: " in line
    ]
    quoted_profile = _expected_shell_quote(profile_name)
    assert _shell_word_reads_back_as(quoted_profile, profile_name)
    assert manual_commands == [
        'dsh plugin --profile web add "github:Q00/ouroboros#main&path:integrations/dsh-plugin"',
        f'dsh plugin --profile {quoted_profile} add "github:Q00/ouroboros#main&path:integrations/dsh-plugin"',
    ]
    expected_warning = f"dsh profile {quoted_profile}: install skipped"
    assert sum(line.endswith(expected_warning) for line in result.stdout.splitlines()) == 1
    assert profile_name not in result.stdout


@pytest.mark.parametrize(
    "profile_name",
    ["team\n", "team\x1b[31mred", "team\u202etxt", "team\u200btxt", "팀 alpha"],
)
def test_installer_quotes_profile_in_success_log(tmp_path: Path, profile_name: str) -> None:
    """Successful installs must not emit raw profile control characters."""
    profile = tmp_path / "home" / ".dsh" / "profiles" / profile_name
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        '{"dependencies": {"dsh-ouroboros": "github:Q00/ouroboros"}}', encoding="utf-8"
    )

    result = _run_installer(
        tmp_path,
        fake_commands={
            "dsh": "#!/bin/sh\nexit 0\n",
            "python3": f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
        },
    )

    assert result.returncode == 0, result.stderr
    quoted_profile = _expected_shell_quote(profile_name)
    assert _shell_word_reads_back_as(quoted_profile, profile_name)
    expected_status = f"dsh profile {quoted_profile}: Ouroboros tools installed"
    assert sum(line.endswith(expected_status) for line in result.stdout.splitlines()) == 1
    assert profile_name not in result.stdout


def test_installer_survives_a_failing_dsh(tmp_path: Path) -> None:
    """A broken host cannot fail an install that already put Ouroboros on disk."""
    result = _run_installer(
        tmp_path,
        fake_commands={"dsh": "#!/bin/sh\nexit 1\n"},
    )

    assert result.returncode == 0, result.stderr
    assert "install skipped" in result.stdout
    assert "Manual install: dsh plugin --profile web add" in result.stdout
