"""Exercise hosting-profile authority through the real loader and runtime factory."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

_DISPATCH = """
import anyio
from pathlib import Path
from ouroboros.orchestrator.runtime_factory import create_agent_runtime

async def main():
    runtime = create_agent_runtime(
        backend="hermes", cli_path=Path("hermes-fixture").resolve(), cwd=".", llm_backend="hermes"
    )
    async for message in runtime.execute_task("Report launch arguments"):
        print(message.content)

anyio.run(main)
"""


@pytest.mark.parametrize(
    ("source", "relative_home", "relative_real_home", "explicit", "expected"),
    [
        pytest.param("project", False, False, None, None, id="project-cannot-select-profile"),
        pytest.param("trusted", False, False, None, "astraia", id="trusted-user-env"),
        pytest.param("process", False, False, None, "astraia", id="trusted-process-env"),
        pytest.param("process", True, False, None, None, id="relative-hermes-home"),
        pytest.param("process", False, True, None, None, id="relative-real-home"),
        pytest.param("process", False, False, "worker", "worker", id="explicit-wins"),
        pytest.param("process", True, False, "worker", "worker", id="explicit-wins-relative"),
    ],
)
def test_profile_authority_at_public_dispatch(
    tmp_path: Path,
    source: str,
    relative_home: bool,
    relative_real_home: bool,
    explicit: str | None,
    expected: str | None,
) -> None:
    # Given a canonical host profile and a recording CLI in an isolated process.
    home = tmp_path / "home"
    profile = home / ".hermes" / "profiles" / "astraia"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    project = profile if relative_home else home if relative_real_home else tmp_path / "project"
    project.mkdir(exist_ok=True)
    fixture = project / "hermes-fixture"
    fixture.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        'print(json.dumps({"argv": sys.argv[1:], '
        '"home": os.environ.get("HERMES_HOME"), '
        '"real_home": os.environ.get("HERMES_REAL_HOME")}))\n',
        encoding="utf-8",
    )
    fixture.chmod(0o700)
    host_env = {
        "HERMES_HOME": "." if relative_home else str(profile),
        "HERMES_REAL_HOME": "." if relative_real_home else str(home),
    }
    child_env = {
        "HOME": str(home),
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
    }
    if source == "process":
        child_env.update(host_env)
    else:
        env_dir = project if source == "project" else home / ".ouroboros"
        env_dir.mkdir(exist_ok=True)
        (env_dir / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in host_env.items()), encoding="utf-8"
        )
    if explicit:
        child_env["OUROBOROS_RUNTIME_PROFILE"] = explicit

    # When import-time .env loading, public construction, and actual dispatch run.
    result = subprocess.run(
        [sys.executable, "-c", _DISPATCH],
        cwd=project,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    # Then the child sees only the authorized profile selection and environment.
    assert result.returncode == 0, result.stderr
    launch = json.loads(result.stdout.splitlines()[-1])
    expected_prefix = ["--profile", expected, "chat"] if expected else ["chat"]
    assert launch["argv"][: len(expected_prefix)] == expected_prefix
    assert launch["home"] == (None if source == "project" else host_env["HERMES_HOME"])
    assert launch["real_home"] == (None if source == "project" else host_env["HERMES_REAL_HOME"])


@pytest.mark.parametrize(
    ("stderr", "exit_code", "expected_type", "expected_retry"),
    [
        (
            "Hermes isn't configured yet -- no API keys or providers found.",
            1,
            "HermesConfigurationError",
            False,
        ),
        (
            "Hermes isn't configured yet — no API keys or providers found.",
            1,
            "HermesConfigurationError",
            False,
        ),
        ("permission denied", 1, "RuntimeExecutionError", True),
        ("HTTP 429 rate limit exceeded", 1, "RateLimitError", True),
        (
            "Hermes isn't configured yet -- no API keys or providers found.\nHTTP 429",
            1,
            "RateLimitError",
            True,
        ),
        ("Internal worker failure", 1, "RuntimeExecutionError", True),
        (
            "Example: Hermes isn't configured yet -- no API keys or providers found.",
            1,
            "RuntimeExecutionError",
            True,
        ),
        ("Hermes isn't configured yet -- no API keys or providers found.", 0, None, False),
    ],
)
def test_hermes_configuration_retry_boundary_at_public_dispatch(
    tmp_path: Path, stderr: str, exit_code: int, expected_type: str | None, expected_retry: bool
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fixture = tmp_path / "hermes-fixture"
    fixture.write_text(
        f"#!{sys.executable}\nimport sys\nprint({stderr!r}, file=sys.stderr)\nsys.exit({exit_code})\n",
        encoding="utf-8",
    )
    fixture.chmod(0o700)
    script = """
import anyio, json
from pathlib import Path
from ouroboros.orchestrator.runtime_factory import create_agent_runtime
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult
from ouroboros.orchestrator.retry_hints import is_retryable_failure
async def main():
    runtime = create_agent_runtime(backend="hermes", cli_path=str(Path("hermes-fixture").resolve()), cwd=".", llm_backend="hermes")
    messages = tuple([message async for message in runtime.execute_task("Run the worker")])
    terminal = messages[-1]
    result = ACExecutionResult(ac_index=0, ac_content="Run the worker", success=not terminal.is_error, messages=messages, final_message=terminal.content)
    print(json.dumps({"metadata":terminal.data,"retry":is_retryable_failure(result)}))
anyio.run(main)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
            "OUROBOROS_TELEMETRY": "0",
            "OUROBOROS_DASHBOARD": "0",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    observation = json.loads(completed.stdout.splitlines()[-1])
    (tmp_path / "retry-observation.json").write_text(json.dumps(observation), encoding="utf-8")
    assert observation["metadata"].get("error_type") == expected_type
    if exit_code:
        assert observation["metadata"]["exit_code"] == exit_code
    assert observation["retry"] is expected_retry
