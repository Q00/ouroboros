"""Opt-in smoke test for the native Pi CLI runtime.

Skipped by default so regular CI does not require a real Pi installation,
credentials, or network access. To run locally, install/authenticate the Pi CLI
and set ``OUROBOROS_PI_NATIVE_SMOKE=1``.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import NamedTuple

import pytest

from ouroboros.config import get_pi_cli_path
from ouroboros.orchestrator.runtime_factory import create_agent_runtime

SMOKE_ENABLED = os.environ.get("OUROBOROS_PI_NATIVE_SMOKE", "").strip() == "1"
REFERENCE_REPO_ENV = "OUROBOROS_PI_REFERENCE_REPO"


class GitSnapshot(NamedTuple):
    """Minimal repository state used to detect smoke-test side effects."""

    head: str
    status: str


def _reference_repo_path() -> Path | None:
    raw_path = os.environ.get(REFERENCE_REPO_ENV, "").strip()
    if not raw_path:
        return None
    return Path(raw_path).expanduser()


def _git_snapshot(path: Path | None) -> GitSnapshot | None:
    if path is None or not path.exists():
        return None

    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return GitSnapshot(head=head, status=status)


def _configured_pi_cli_path() -> str | None:
    configured_path = get_pi_cli_path()
    if not configured_path:
        return None
    return configured_path


@pytest.fixture()
def reference_repo_snapshot() -> GitSnapshot | None:
    reference_repo = _reference_repo_path()
    before = _git_snapshot(reference_repo)
    yield before
    after = _git_snapshot(reference_repo)
    assert after == before, f"{reference_repo} was modified by the Pi smoke test"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not SMOKE_ENABLED,
    reason="set OUROBOROS_PI_NATIVE_SMOKE=1 to enable the real Pi CLI smoke test",
)
async def test_real_pi_cli_runtime_returns_response_without_reference_repo_side_effects(
    tmp_path: Path,
    reference_repo_snapshot: GitSnapshot | None,
) -> None:
    del reference_repo_snapshot
    if shutil.which("pi") is None and _configured_pi_cli_path() is None:
        pytest.skip("Pi CLI is not on PATH and no Pi CLI path is configured via env/config")

    runtime = create_agent_runtime(
        backend="pi",
        cwd=tmp_path,
        model=os.environ.get("OUROBOROS_EXECUTION_MODEL") or None,
        permission_mode="acceptEdits",
    )

    result = await runtime.execute_task_to_result(
        'Reply with exactly the word "ready" and nothing else.',
        tools=[],
        system_prompt="You are running a smoke test. Return only the requested word.",
    )

    assert runtime.runtime_backend == "pi"
    assert result.is_ok, f"Pi CLI returned error: {result.error}"
    task_result = result.value
    assert task_result.success is True
    assert task_result.final_message.strip()
    assert "ready" in task_result.final_message.lower()
    assert task_result.messages
