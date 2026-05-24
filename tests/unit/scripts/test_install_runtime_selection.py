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
    local_repo: bool = True,
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
        "uv tool install --upgrade --python >=3.12 .",
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
        "uv tool install --upgrade --python >=3.12 . --with mcp>=1.26.0,<2.0.0 --with claude-agent-sdk>=0.1.0,<1.0.0 --with anthropic>=0.52.0,<1.0.0"
        in calls
    )
    assert "ouroboros setup --runtime claude --non-interactive" in calls


def test_pypi_lookup_failure_stays_stable_only_for_remote_install(tmp_path: Path) -> None:
    result = _run_installer(
        tmp_path,
        local_repo=False,
        env={"OUROBOROS_INSTALL_RUNTIME": "codex"},
        fake_commands={"curl": "#!/bin/sh\nexit 22\n"},
    )

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "uv tool install --upgrade --python >=3.12 ouroboros-ai" in calls
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
    "dashboard": ("streamlit", "plotly", "pandas"),
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
    install.sh's hand-maintained --with list silently dropped tui and
    dashboard packages, so users picking 'All' got an incomplete tree.
    """
    extras = _read_pyproject_extras()
    # `all` is a single self-referential entry, e.g.
    # ``["ouroboros-ai[claude,copilot,litellm,mcp,tui,dashboard]"]``. Pull the
    # bracketed names back out so we can compare against our mapping.
    import re

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
    extras = _read_pyproject_extras()

    # Collect every dependency string declared by an extra we install via uv
    # --with (the ``copilot`` extra has no Python deps, so nothing to pin).
    expected_pins: list[str] = []
    for extra_name in _EXTRA_TO_PACKAGES:
        for dep in extras.get(extra_name, []):
            stripped = dep.strip()
            if stripped:
                expected_pins.append(stripped)

    # Sanity: pyproject must declare at least one pin we expect to enforce.
    # If this fails the parsing is broken or the extras went empty.
    assert expected_pins, "no pyproject pins discovered — parity check inert"

    result = _run_installer(tmp_path, env={"OUROBOROS_INSTALL_RUNTIME": "all"})
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")

    drifted = sorted(pin for pin in expected_pins if f"--with {pin}" not in calls)
    assert not drifted, (
        "install.sh `[all]` --with list has drifted from pyproject pins.\n"
        f"Missing or mismatched: {drifted}\n"
        "Update the case statement in scripts/install.sh so each "
        "`--with <spec>` string matches pyproject [project.optional-dependencies]."
    )
