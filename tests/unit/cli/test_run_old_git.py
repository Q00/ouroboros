"""``ooo run`` refuses a managed task worktree on Git older than 2.36.0.

The refusal happens before a runtime or runner exists and before any worktree
is created, instead of ``Cannot resolve project identity`` at session start.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from ouroboros.cli.commands.run import _run_orchestrator
from ouroboros.core import project_identity

SEED_DATA = {
    "goal": "Test task",
    "constraints": [],
    "acceptance_criteria": ["All tests pass"],
    "ontology_schema": {"name": "T", "description": "T", "fields": []},
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {"seed_id": "seed-old-git", "ambiguity_score": 0.1},
}


@pytest.mark.asyncio
async def test_run_refuses_task_worktree_on_old_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    seed_file = tmp_path / "seed.yaml"
    seed_file.write_text("goal: ignored\n", encoding="utf-8")
    root = tmp_path / "worktrees"
    real_run_git_command = project_identity._run_git_command

    def fake_git(*arguments: str) -> bytes:
        if arguments == ("--version",):
            return b"git version 2.34.1\n"
        return real_run_git_command(*arguments)

    runner_cls = MagicMock(side_effect=AssertionError("no runner may be built"))
    runtime_factory = MagicMock(side_effect=AssertionError("no runtime may be built"))
    with (
        patch("ouroboros.cli.commands.run._load_seed_from_yaml", return_value=SEED_DATA),
        patch("ouroboros.orchestrator.create_agent_runtime", runtime_factory),
        patch("ouroboros.orchestrator.runner_factory.OrchestratorRunner", runner_cls),
        patch("ouroboros.persistence.event_store.EventStore") as event_store_cls,
        patch("ouroboros.core.project_identity._run_git_command", side_effect=fake_git),
        patch("ouroboros.core.worktree._worktree_root", return_value=root),
        patch("ouroboros.core.worktree._worktrees_enabled", return_value=True),
        patch("ouroboros.cli.commands.run.print_error") as print_error,
        pytest.raises(typer.Exit) as exited,
    ):
        event_store_cls.return_value.initialize = AsyncMock()
        await _run_orchestrator(seed_file, project_dir=repo, no_qa=True)

    assert exited.value.exit_code == 1
    message = print_error.call_args.args[0]
    assert message.startswith("Task workspace error: ")
    assert "found 2.34.1" in message
    assert "orchestrator.use_worktrees: false" in message
    runner_cls.assert_not_called()
    runtime_factory.assert_not_called()
    assert not root.exists() or not any(root.rglob("*"))
