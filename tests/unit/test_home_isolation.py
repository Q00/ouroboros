"""The test suite's hermetic-home contract.

conftest redirects the home environment used by ``Path.home()`` and
``os.path.expanduser("~")`` and clears inherited ``CODEX_HOME``. These tests
pin those paths and subprocess inheritance so tests do not use the invoking
developer's configuration by default.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from ouroboros.cli.commands import job, resume
from ouroboros.config.models import get_config_dir


def test_both_home_resolvers_agree_on_the_isolated_home() -> None:
    """``Path.home()`` and ``os.path.expanduser`` must resolve to the same
    isolated home — the bug was that only ``Path.home()`` was redirected while
    ``expanduser``-based CLI paths still reached the real home."""
    home = Path.home()
    assert Path(os.path.expanduser("~")) == home
    assert os.path.expanduser("~/.ouroboros/x") == str(home / ".ouroboros" / "x")
    # On Windows both resolvers instead use USERPROFILE.
    assert os.environ["HOME"] == str(home)
    if sys.platform == "win32":
        assert os.environ["USERPROFILE"] == str(home)


def test_expanduser_based_cli_db_paths_are_isolated() -> None:
    """The ``os.path.expanduser`` consumers flagged in review (default EventStore
    DB paths) resolve under the isolated home, not ``~``."""
    home = Path.home()
    assert Path(job._default_db_path()).is_relative_to(home)
    assert Path(resume._default_db_path()).is_relative_to(home)
    # ``Path.home()``-based resolution lands in the same tree.
    assert get_config_dir().is_relative_to(home)


def test_subprocess_inherits_the_isolated_home() -> None:
    """A subprocess spawned by a test must inherit the isolated ``$HOME`` so it
    cannot read or mutate the developer's real state either."""
    result = subprocess.run(
        [sys.executable, "-c", "import os; print(os.path.expanduser('~'))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == str(Path.home())


def test_inherited_codex_home_is_not_modified_by_setup_test(tmp_path: Path) -> None:
    """A test launched from Codex must not write into its inherited profile."""
    codex_home = tmp_path / "external-codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    original = b'[existing]\nvalue = "keep"\n'
    config.write_bytes(original)
    profile_config = tmp_path / "external-profile" / ".codex" / "config.toml"
    profile_config.parent.mkdir(parents=True)
    profile_config.write_bytes(original)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env["USERPROFILE"] = str(profile_config.parent.parent)
    env.pop("PYTEST_ADDOPTS", None)
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/cli/test_setup.py::TestCodexSetup::"
            "test_register_codex_mcp_server_writes_guidance_comment",
            "tests/unit/test_home_isolation.py::test_both_home_resolvers_agree_on_the_isolated_home",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == original
    assert profile_config.read_bytes() == original
