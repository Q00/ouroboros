"""A Seed with ``sourced_scope`` runs without model-proposed sub-AC splits.

Every ``ooo auto`` Seed carries ``sourced_scope``, and its worker sees only
content with a known source. A bounce split would hand the worker children the
model wrote, so the start-execute handler builds the runner with decomposition
off for such a Seed. A normal-interview Seed keeps decomposition, and a resumed
session keeps what its execution contract recorded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
import yaml

from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler
from ouroboros.orchestrator.decomposition_limits import seed_allows_decomposition
from ouroboros.orchestrator.session import SessionTracker
from ouroboros.persistence.event_store import EventStore

SEED_YAML = """\
goal: Test task
constraints:
  - Python 3.14+
acceptance_criteria:
  - Task completes successfully
ontology_schema:
  name: TestOntology
  description: Test ontology
  fields:
    - name: test_field
      field_type: string
      description: A test field
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: test-seed-123
  version: "1.0.0"
  created_at: "2024-01-01T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
"""
SOURCED_SEED_YAML = SEED_YAML + "sourced_scope:\n  goal: The user's request, verbatim.\n"


@pytest.fixture
async def memory_event_store() -> AsyncIterator[EventStore]:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


def _contract(enable_decomposition: bool) -> dict[str, object]:
    return {
        "execution_contract": {
            "execution_semantics": {"enable_decomposition": enable_decomposition}
        }
    }


def _tracker(name: str, progress: dict[str, object] | None = None) -> SessionTracker:
    tracker = SessionTracker.create(name, "test-seed-123")
    return tracker.with_progress(progress) if progress is not None else tracker


def test_sourced_seed_runs_without_decomposition() -> None:
    sourced = Seed.from_dict(yaml.safe_load(SOURCED_SEED_YAML))
    normal = Seed.from_dict(yaml.safe_load(SEED_YAML))

    assert seed_allows_decomposition(sourced, None) is False
    assert seed_allows_decomposition(normal, None) is True
    # A resumed session keeps what its contract recorded, either way.
    assert seed_allows_decomposition(sourced, _tracker("a", _contract(True))) is True
    assert seed_allows_decomposition(normal, _tracker("b", _contract(False))) is False
    # Without a recorded contract the fresh rule applies.
    assert seed_allows_decomposition(sourced, _tracker("c")) is False
    assert seed_allows_decomposition(normal, _tracker("d")) is True


async def test_handler_builds_the_runner_without_decomposition_for_a_sourced_seed(
    memory_event_store: EventStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[bool] = []
    fresh_tracker = _tracker("exec_fresh")
    trackers = {
        # A sourced run started before this rule recorded decomposition on.
        "sess_old_sourced": _tracker("exec_old", _contract(True)),
        "sess_new_sourced": _tracker("exec_new", _contract(False)),
    }
    workspace = SimpleNamespace(
        effective_cwd="/tmp/ouroboros-worktree",
        worktree_path="/tmp/ouroboros-worktree",
        branch="ooo/test",
        lock_path="/tmp/ouroboros.lock",
    )

    class FakeSessionRepository:
        def __init__(self, _event_store: EventStore) -> None:
            pass

        async def reconstruct_session(self, session_id: str) -> Result:
            return Result.ok(trackers.get(session_id, fresh_tracker))

        async def mark_failed(self, session_id: str, *, error_message: str) -> None:
            raise AssertionError(f"unexpected failure mark for {session_id}: {error_message}")

    class FakeRunner:
        def __init__(self, *args: object, enable_decomposition: bool, **kwargs: object) -> None:
            captured.append(enable_decomposition)

        def _has_live_process_local_authority(self, *args: object, **kwargs: object) -> bool:
            return False

        async def prepare_session(self, *args: object, **kwargs: object) -> Result:
            return Result.ok(fresh_tracker)

        async def execute_precreated_session(self, *args: object, **kwargs: object) -> Result:
            return Result.ok(
                SimpleNamespace(
                    success=True, execution_id="exec_fresh", summary={}, final_message="done"
                )
            )

        async def resume_session(self, *args: object, **kwargs: object) -> Result:
            return Result.ok(
                SimpleNamespace(
                    success=True, execution_id="exec_resume", summary={}, final_message="resumed"
                )
            )

    prefix = "ouroboros.mcp.tools.execution_handlers"
    monkeypatch.setattr(f"{prefix}.SessionRepository", FakeSessionRepository)
    monkeypatch.setattr(f"{prefix}.OrchestratorRunner", FakeRunner)
    monkeypatch.setattr(
        f"{prefix}.create_agent_runtime", lambda **_kwargs: SimpleNamespace(runtime_backend="test")
    )
    monkeypatch.setattr(
        f"{prefix}.maybe_prepare_task_workspace", lambda *_args, **_kwargs: workspace
    )
    monkeypatch.setattr(
        f"{prefix}.maybe_restore_task_workspace", lambda *_args, **_kwargs: workspace
    )
    monkeypatch.setattr(f"{prefix}.release_lock", lambda *_args: None)

    handler = ExecuteSeedHandler(event_store=memory_event_store)
    calls = (
        {"seed_content": SEED_YAML},
        {"seed_content": SOURCED_SEED_YAML},
        {"seed_content": SOURCED_SEED_YAML, "session_id": "sess_old_sourced"},
        {"seed_content": SOURCED_SEED_YAML, "session_id": "sess_new_sourced"},
    )
    for arguments in calls:
        result = await handler.handle({**arguments, "skip_qa": True}, synchronous=True)
        assert result.is_ok

    # Normal Seed: on. Sourced Seed: off. Resume: the recorded setting, so an
    # older sourced run resumes with decomposition on, as it started.
    assert captured == [True, False, True, False]
