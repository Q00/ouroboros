"""The canonical live opt-in changes only its own provider test guards."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

PLUGIN = """
import os
from pathlib import Path
import pytest

def pytest_collection_modifyitems(items):
    import ouroboros.orchestrator.runner
    import ouroboros.mcp.tools.auto_handler
    import ouroboros.cli.commands.auto

def pytest_sessionstart(session):
    import socket, sys
    def audit(event, args):
        if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
            raise RuntimeError("network disabled in fixture test")
        if event == "socket.__new__" and args[1] in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("network disabled in fixture test")
    sys.addaudithook(audit)

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    import ouroboros.orchestrator.runner as runner
    import ouroboros.providers.factory as factory
    import ouroboros.auto.adapters as adapters
    import ouroboros.auto.investigator as investigator
    import ouroboros.mcp.tools.auto_handler as handler
    import ouroboros.cli.commands.auto as cli
    expected = os.environ["EXPECT_LIVE"]
    live = expected == "1" or (expected == "mixed" and item.originalname == "test_scenario_live_run_or_skip")
    assert (runner.create_llm_adapter is factory.create_llm_adapter) == live
    assert (handler.build_answer_refiner is adapters.build_answer_refiner) == live
    assert (handler.build_repo_investigator is investigator.build_repo_investigator) == live
    assert (cli.build_answer_refiner is adapters.build_answer_refiner) == live
    assert (cli.build_repo_investigator is investigator.build_repo_investigator) == live
    assert Path.home() != Path(os.environ["OUTER_HOME"])
    assert os.environ["OUROBOROS_TELEMETRY"] == "0"
    assert os.environ["OUROBOROS_DASHBOARD"] == "0"
    print("fixture_scope_verified")
    pytest.skip("fixture-only test: no provider call")
"""


@pytest.mark.parametrize(
    ("opt_in", "test_name", "expect_live"),
    [
        ("1", "test_scenario_live_run_or_skip", "1"),
        ("", "test_scenario_live_run_or_skip", "0"),
        ("1", "test_scenario_has_nonempty_goal", "0"),
        ("1", "test_live_run_opt_in_invokes_auto_handler", "0"),
    ],
)
def test_canonical_fixture_scoping(
    tmp_path: Path, opt_in: str, test_name: str, expect_live: str
) -> None:
    root = Path(__file__).resolve().parents[2]
    plugin = tmp_path / "fixture_probe.py"
    plugin.write_text(PLUGIN, encoding="utf-8")
    home = tmp_path / "outer-home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "CODEX_HOME": str(tmp_path / "isolated-codex"),
        "PYTHONPATH": os.pathsep.join((str(root / "src"), str(tmp_path))),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "OUROBOROS_RUN_CANONICAL": opt_in,
        "EXPECT_LIVE": expect_live,
        "OUTER_HOME": str(home),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "fixture_probe",
            "-s",
            "-q",
            f"tests/canonical/test_canonical.py::{test_name}[cli-todo]",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "fixture_scope_verified" in completed.stdout
    assert "fixture-only test: no provider call" not in completed.stderr


@pytest.mark.parametrize("live_first", [True, False])
def test_live_and_unit_fixtures_do_not_leak_between_tests(tmp_path: Path, live_first: bool) -> None:
    root = Path(__file__).resolve().parents[2]
    (tmp_path / "fixture_probe.py").write_text(PLUGIN, encoding="utf-8")
    home = tmp_path / "outer-home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "CODEX_HOME": str(tmp_path / "isolated-codex"),
        "PYTHONPATH": os.pathsep.join((str(root / "src"), str(tmp_path))),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "OUROBOROS_RUN_CANONICAL": "1",
        "EXPECT_LIVE": "mixed",
        "OUTER_HOME": str(home),
    }
    live = "tests/canonical/test_canonical.py::test_scenario_live_run_or_skip[cli-todo]"
    unit = "tests/unit/test_home_isolation.py::test_both_home_resolvers_agree_on_the_isolated_home"
    targets = [live, unit] if live_first else [unit, live]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "fixture_probe",
            "-s",
            "-q",
            *targets,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.count("fixture_scope_verified") == 2
