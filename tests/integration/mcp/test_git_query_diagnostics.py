"""Persist Git recheck outcomes from a fresh background Seed run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import closing
from contextvars import ContextVar
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.core import project_identity, worktree
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.tools import execution_handlers
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler, StartExecuteSeedHandler
from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, AgentMessage
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.persistence.event_store import EventStore


@pytest.mark.integration
@pytest.mark.parametrize(
    ("failure", "expected_stage", "exception_type"),
    [
        ("timeout", "git_wait", "TimeoutExpired"),
        ("rev_parse_timeout", "git_wait", "TimeoutExpired"),
        ("process", "git_process", "FileNotFoundError"),
        ("temp_file", "temporary_output_open", "FileNotFoundError"),
        pytest.param(None, None, None, id="success"),
    ],
)
async def test_background_git_recheck_diagnostics_survive_sqlite_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
    expected_stage: str | None,
    exception_type: str | None,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("Git is required to prove the managed worktree identity")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "Initial commit",
        ],
        cwd=repo,
        check=True,
    )
    monkeypatch.setattr(worktree, "_worktree_root", lambda: tmp_path / "worktrees")
    monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", "off")

    checking_prepared_session = ContextVar("checking_prepared_session", default=False)
    restore = OrchestratorRunner._restore_execution_contract_snapshot

    def restore_contract(runner: OrchestratorRunner, *args: Any, **kwargs: Any) -> Any:
        token = checking_prepared_session.set(kwargs.get("prepared_live_execution", False))
        try:
            return restore(runner, *args, **kwargs)
        finally:
            checking_prepared_session.reset(token)

    rev_parse_args = ["rev-parse", "--path-format=absolute", "--git-common-dir"]
    recheck_commands: list[list[str]] = []
    temp_file_failures = 0

    def temporary_file() -> Any:
        nonlocal temp_file_failures
        if checking_prepared_session.get() and failure == "temp_file":
            temp_file_failures += 1
            raise FileNotFoundError(2, "Cannot create the temporary Git output")
        return tempfile.TemporaryFile()

    def git_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if checking_prepared_session.get():
            recheck_commands.append(args)
            if failure == "rev_parse_timeout" and args[4:] == rev_parse_args:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            if args == ["git", "--version"]:
                if failure == "process":
                    missing_git = FileNotFoundError(2, "Git executable is missing")
                    missing_git.winerror = 2
                    raise missing_git
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.run(args, **kwargs)

    monkeypatch.setattr(
        OrchestratorRunner, "_restore_execution_contract_snapshot", restore_contract
    )
    monkeypatch.setattr(project_identity, "tempfile", SimpleNamespace(TemporaryFile=temporary_file))
    monkeypatch.setattr(
        project_identity,
        "subprocess",
        SimpleNamespace(
            run=git_run,
            DEVNULL=subprocess.DEVNULL,
            SubprocessError=subprocess.SubprocessError,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )

    execution_calls = 0

    async def execute_task(**_kwargs: Any) -> AsyncIterator[AgentMessage]:
        nonlocal execution_calls
        execution_calls += 1
        yield AgentMessage(type="result", content="completed", data={"subtype": "success"})

    async def make_runtime(_factory: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            runtime_backend="codex_cli",
            llm_backend="test",
            permission_mode="bypassPermissions",
            capabilities=FULL_CAPABILITIES,
            _model="synthetic-test",
            working_directory=str(kwargs["cwd"]),
            execute_task=execute_task,
        )

    monkeypatch.setattr(execution_handlers, "create_agent_runtime_async", make_runtime)
    monkeypatch.setattr(execution_handlers, "preflight_agent_runtime", lambda _adapter: None)
    monkeypatch.setattr(execution_handlers, "get_auto_evaluate_enabled", lambda: False)
    monkeypatch.setattr(execution_handlers, "get_auto_evolve_enabled", lambda: False)

    seed_yaml = """
goal: Exercise the Git identity recheck
acceptance_criteria:
  - The synthetic fixture completes
ontology_schema:
  name: GitDiagnosticFixture
  description: Isolated test
metadata:
  seed_id: synthetic-git-recheck
"""
    if failure is None:
        seed_yaml += "orchestrator:\n  execution_mode: legacy\n"
    database = tmp_path / "events.sqlite"
    store = EventStore(f"sqlite+aiosqlite:///{database.as_posix()}")
    await store.initialize()
    manager = JobManager(store)
    handler = StartExecuteSeedHandler(
        execute_handler=ExecuteSeedHandler(event_store=store, agent_runtime_backend="codex"),
        event_store=store,
        job_manager=manager,
        agent_runtime_backend="codex",
    )
    try:
        started = await handler.handle(
            {"cwd": str(repo), "seed_content": seed_yaml, "auto_evaluate": False, "skip_qa": True}
        )
        assert started.is_ok, started.error if started.is_err else None
        assert started.value.meta is not None
        job_id = started.value.meta["job_id"]
        session_id = started.value.meta["session_id"]
        async with asyncio.timeout(30):
            snapshot = await manager.get_snapshot(job_id)
            while not snapshot.is_terminal:
                await asyncio.sleep(0.05)
                snapshot = await manager.get_snapshot(job_id)
        expected = JobStatus.COMPLETED if failure is None else JobStatus.FAILED
        assert snapshot.status is expected, snapshot
    finally:
        await manager.drain()
        await store.close()

    assert execution_calls == (1 if failure is None else 0)
    if failure is None:
        assert any(args[4:] == rev_parse_args for args in recheck_commands)
    elif failure == "temp_file":
        assert recheck_commands == []
        assert temp_file_failures == 1
    elif failure == "rev_parse_timeout":
        assert recheck_commands == [
            ["git", "--version"],
            [
                "git",
                "-C",
                str(repo.resolve()),
                f"--git-dir={repo.resolve() / '.git'}",
                *rev_parse_args,
            ],
        ]
    else:
        assert recheck_commands == [["git", "--version"]]
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as reopened:
        rows = reopened.execute(
            "SELECT event_type, payload FROM events WHERE aggregate_id IN (?, ?) ORDER BY rowid",
            (session_id, job_id),
        ).fetchall()

    event_types = [event_type for event_type, _ in rows]
    if failure is None:
        assert event_types.count("orchestrator.session.completed") == 1
        assert event_types.count("mcp.job.completed") == 1
        assert "orchestrator.session.failed" not in event_types
        assert "mcp.job.failed" not in event_types
        assert all("git_query" not in payload for _, payload in rows)
        return

    assert (
        event_types.index("orchestrator.session.started")
        < event_types.index("orchestrator.session.failed")
        < event_types.index("mcp.job.failed")
    )
    (session_failed,) = [
        json.loads(payload)
        for event_type, payload in rows
        if event_type == "orchestrator.session.failed"
    ]
    (job_failed,) = [
        json.loads(payload) for event_type, payload in rows if event_type == "mcp.job.failed"
    ]
    assert session_failed["messages_processed"] == 0
    assert job_failed["status"] == "failed"
    assert session_failed["error"] == (
        "Cannot resolve project identity (details: {'invalid': 'project_identity', "
        "'cause': 'Git query is temporarily unavailable', "
        "'resume_blocked': 'project_identity_unavailable', 'retryable': True})"
    )
    assert "git_query" not in json.dumps(job_failed)
    diagnostic = session_failed["error_details"]["git_query"]
    if failure == "rev_parse_timeout":
        assert diagnostic["command"] == [
            "git",
            "-C",
            "<path>",
            "--git-dir=<path>",
            *rev_parse_args,
        ]
        assert diagnostic["checkout"] == "source"
        serialized = json.dumps(diagnostic)
        assert json.dumps(str(repo.resolve()))[1:-1] not in serialized
        assert repo.resolve().as_posix() not in serialized
    else:
        assert diagnostic["command"] == ["git", "--version"]
        assert diagnostic["checkout"] == "git_version"
    assert diagnostic["stage"] == expected_stage
    assert diagnostic["exception_type"] == exception_type
    assert diagnostic["timeout_seconds"] == 5.0
    assert isinstance(diagnostic["elapsed_seconds"], float)
    assert diagnostic["elapsed_seconds"] >= 0.0
    if failure in {"process", "temp_file"}:
        assert diagnostic["errno"] == 2
    if failure == "process":
        assert diagnostic["winerror"] == 2
