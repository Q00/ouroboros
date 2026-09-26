"""The check package on the MCP ``ouroboros_execute_seed`` path (plugin ``ooo run``)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.boundary.events import (
    ACCEPTANCE_RECONCILED,
    ACTOR_STARTED,
    ADMISSION_COMPLETED,
    BOUNDARY_AGGREGATE_TYPE,
    PACKAGE_FROZEN,
)
from ouroboros.core.types import Result
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.orchestrator.session import SessionStatus, SessionTracker
from ouroboros.persistence.event_store import EventStore

from .test_run_wiring import BUGFIX_SCRIPT, FIXED, _constructor_factory, _parallel_result

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
  seed_id: seed-mcp-boundary
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


async def _run_handler(
    store: EventStore,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    switch: str | None,
    legacy_success: bool,
    worker_edit: str | None,
    calls: list[dict[str, Any]],
) -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {}
    running = SessionTracker.create("exec_mcp_cp", "seed-mcp-boundary", session_id="orch_mcp_cp")
    workspace = SimpleNamespace(
        effective_cwd=str(repo),
        worktree_path=str(repo),
        branch="ooo/test",
        lock_path=str(tmp_path / "lock"),
    )

    class FakeSessionRepository:
        def __init__(self, _event_store: EventStore) -> None:
            pass

        async def reconstruct_session(self, _session_id: str) -> Result:
            status = SessionStatus.COMPLETED if seen.get("success") else SessionStatus.FAILED
            return Result.ok(running.with_status(status))

    class FakeRunner:
        """Honors the runner contract: calls the acceptance authority on the executor result."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.acceptance_authority: Any = None

        def _has_live_process_local_authority(self, *args: object, **kwargs: object) -> bool:
            return False

        async def prepare_session(self, *args: object, **kwargs: object) -> Result:
            return Result.ok(running)

        async def execute_precreated_session(self, *, seed: Any, **kwargs: object) -> Result:
            seen["events_at_dispatch"] = [
                event.type
                for event in await store.replay(
                    BOUNDARY_AGGREGATE_TYPE, f"{running.execution_id}/check_package/v1"
                )
            ]
            seen["authority_installed"] = self.acceptance_authority is not None
            if worker_edit is not None:
                (repo / "calc.py").write_text(worker_edit)
            parallel_result = _parallel_result(legacy_success)
            if self.acceptance_authority is not None:
                parallel_result = await self.acceptance_authority(
                    seed=seed, execution_id=running.execution_id, parallel_result=parallel_result
                )
            seen["success"] = parallel_result.all_succeeded
            return Result.ok(
                SimpleNamespace(
                    success=parallel_result.all_succeeded,
                    execution_id=running.execution_id,
                    summary={},
                    final_message="done",
                )
            )

    if switch is None:
        monkeypatch.delenv("OUROBOROS_CHECK_PACKAGE", raising=False)
    else:
        monkeypatch.setenv("OUROBOROS_CHECK_PACKAGE", switch)
    prefix = "ouroboros.mcp.tools.execution_handlers"
    monkeypatch.setattr(f"{prefix}.SessionRepository", FakeSessionRepository)
    monkeypatch.setattr(f"{prefix}.OrchestratorRunner", FakeRunner)
    monkeypatch.setattr(
        f"{prefix}.create_agent_runtime", lambda **_kwargs: SimpleNamespace(runtime_backend="codex")
    )
    monkeypatch.setattr(f"{prefix}.maybe_prepare_task_workspace", lambda *_a, **_k: workspace)
    monkeypatch.setattr(f"{prefix}.release_lock", lambda *_args: None)
    monkeypatch.setattr(
        "ouroboros.boundary.constructor.CheckConstructor",
        _constructor_factory(BUGFIX_SCRIPT, calls),
    )
    monkeypatch.setattr(
        "ouroboros.boundary.run_wiring.default_store_dir",
        lambda execution_id: tmp_path / "store" / execution_id,
    )

    handler = ExecuteSeedHandler(event_store=store, agent_runtime_backend="codex")
    result = await handler.handle(
        {"seed_content": SEED_YAML, "skip_qa": True},
        execution_id=running.execution_id,
        session_id_override=running.session_id,
        synchronous=True,
    )
    return result, seen


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    return root


async def test_mcp_execute_seed_prepares_verifies_and_reconciles(
    memory_event_store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    result, seen = await _run_handler(
        memory_event_store,
        repo,
        tmp_path,
        monkeypatch,
        switch="on",
        legacy_success=False,
        worker_edit=FIXED,
        calls=calls,
    )

    assert result.is_ok
    assert len(calls) == 1 and calls[0]["runtime_backend"] == "codex"
    assert seen["events_at_dispatch"] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, ACTOR_STARTED]
    assert seen["authority_installed"] is True
    events = await memory_event_store.replay(
        BOUNDARY_AGGREGATE_TYPE, "exec_mcp_cp/check_package/v1"
    )
    assert events[-1].type == ACCEPTANCE_RECONCILED
    tool = result.value
    assert tool.is_error is False
    assert tool.meta["status"] == "completed"
    assert "Check package verdict: pass" in tool.text_content
    assert {key: tool.meta[key] for key in ("check_package_arm", "check_package_assignment")} == {
        "check_package_arm": "on",
        "check_package_assignment": "user_forced_on",
    }
    assert tool.meta["check_package_status"] == "admitted"
    assert tool.meta["package_verdict"] == "pass"
    assert tool.meta["legacy_verdict"] == "reject"
    assert tool.meta["reconciliation"] == "package_accepted_over_legacy_reject"


async def test_mcp_execute_seed_with_the_arm_off_is_the_legacy_run(
    memory_event_store: EventStore, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    result, seen = await _run_handler(
        memory_event_store,
        repo,
        tmp_path,
        monkeypatch,
        switch="off",
        legacy_success=False,
        worker_edit=FIXED,
        calls=calls,
    )

    assert result.is_ok
    assert calls == []
    assert seen["events_at_dispatch"] == []
    assert seen["authority_installed"] is False
    tool = result.value
    assert tool.is_error is True
    assert "Check package" not in tool.text_content
    assert tool.meta["check_package_arm"] == "off"
    assert tool.meta["check_package_assignment"] == "user_forced_off"
    assert tool.meta["reconciliation"] == "none"
    assert tool.meta["legacy_verdict"] == "reject"
    assert not (tmp_path / "store").exists()
