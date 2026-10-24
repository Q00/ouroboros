"""``execute_seed`` refuses a managed task worktree on Git older than 2.36.0.

The refusal is the launch error the caller already knows
(``launch_workspace_unavailable``), raised before a runtime, runner or
worktree exists, instead of ``Cannot resolve project identity`` at session
start inside the background job.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from ouroboros.core import project_identity
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.persistence.event_store import EventStore

SEED_YAML = """\
goal: Fix add
constraints: []
acceptance_criteria:
  - add(2, 3) returns 5
ontology_schema:
  name: calc
  description: calculator
  fields: []
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: seed-old-git
  version: "1.0.0"
  created_at: "2024-01-01T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
"""


@pytest.fixture
async def memory_event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_execute_seed_refuses_task_worktree_on_old_git(
    memory_event_store: EventStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    root = tmp_path / "worktrees"
    real_run_git_command = project_identity._run_git_command

    def fake_git(*arguments: str) -> bytes:
        if arguments == ("--version",):
            return b"git version 2.34.1\n"
        return real_run_git_command(*arguments)

    def no_runner(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no runner may be built when the task worktree is refused")

    prefix = "ouroboros.mcp.tools.execution_handlers"
    monkeypatch.setattr("ouroboros.orchestrator.runner_factory.OrchestratorRunner", no_runner)
    monkeypatch.setattr(f"{prefix}.create_agent_runtime", no_runner)
    with (
        patch("ouroboros.core.project_identity._run_git_command", side_effect=fake_git),
        patch("ouroboros.core.worktree._worktree_root", return_value=root),
        patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
    ):
        result = await ExecuteSeedHandler(
            event_store=memory_event_store, agent_runtime_backend="codex"
        ).handle({"seed_content": SEED_YAML, "cwd": str(repo), "skip_qa": True})

    assert result.is_err
    message = str(result.error)
    assert "Task workspace error" in message
    assert "found 2.34.1" in message
    assert "orchestrator.use_worktrees: false" in message
    assert result.error.failure_meta == {"failure_cause": "launch_workspace_unavailable"}
    assert not root.exists() or not any(root.rglob("*"))
