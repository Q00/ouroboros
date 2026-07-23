"""The test suite's hermetic-home contract.

conftest redirects ``$HOME`` — the single chokepoint both ``Path.home()`` and
``os.path.expanduser("~")`` read — so no test resolves state under the
developer's real home. These tests pin that contract for both resolution
mechanisms and for subprocess inheritance, so a regression (e.g. isolating only
one resolver again) fails loudly.
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
    # ``$HOME`` is the underlying source both resolvers read.
    assert os.environ["HOME"] == str(home)


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
