"""Terminal verdicts reach board cards and the persisted recent-run picker."""

from pathlib import Path

import pytest

from ouroboros.dashboard.board import reduce_board
from ouroboros.dashboard_web.reader import EventTail, list_recent_executions
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore, acceptance_generation_id_for_session


@pytest.fixture
async def terminal_store(tmp_path: Path):
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'terminal.db'}")
    await store.initialize()
    await store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="orch-terminal",
            data={"execution_id": "exec-terminal", "acceptance_root_indices": [0]},
        )
    )
    await store.append(
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="exec-terminal",
            data={
                "acceptance_criteria": [
                    {
                        "node_id": "child",
                        "root_ac_number": 1,
                        "parent_node_id": "root",
                        "status": "completed",
                    },
                    {"node_id": "root", "root_ac_number": 1, "status": "completed"},
                ]
            },
        )
    )
    await store.append(
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="exec-terminal",
            data={"runtime_status": "running"},
        )
    )
    try:
        yield store, tmp_path / "terminal.db"
    finally:
        await store.close()


@pytest.mark.parametrize("final", [True, False, "true", 1, None])
async def test_verify_failure_reaches_tail_and_picker_only_when_final(terminal_store, final):
    store, db = terminal_store
    tail = EventTail(db, "exec-terminal")
    before = tail.fetch_new()
    await store.append(
        BaseEvent(
            type="execution.verify.failed",
            aggregate_type="execution",
            aggregate_id="exec-terminal",
            data={"ac_index": 0, "final_workspace_revalidation": final},
        )
    )
    after = tail.fetch_new()
    board = reduce_board(before + after)
    failed = [card["id"] for card in board["columns"]["failed"]]
    assert failed == (["root"] if final is True else [])
    run = list_recent_executions(db)[0]
    assert run["failed_count"] == (1 if final is True else 0)
    assert run["status"] == ("failed" if final is True else "running")


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_atomic_terminal_acceptance_updates_root_without_relabeling_child(
    terminal_store, status
):
    store, db = terminal_store
    accepted = status == "completed"
    outcome = "succeeded" if accepted else "failed"
    if accepted:
        await store.append(
            BaseEvent(
                type="execution.node.updated",
                aggregate_type="execution",
                aggregate_id="exec-terminal",
                data={"node_id": "root", "status": "failed"},
            )
        )
    await store.append(
        BaseEvent(
            type=f"orchestrator.session.{status}",
            aggregate_type="session",
            aggregate_id="orch-terminal",
            data={
                "execution_id": "exec-terminal",
                "acceptance_finalizations": [
                    {
                        "session_id": "orch-terminal",
                        "execution_id": "exec-terminal",
                        "acceptance_generation_id": acceptance_generation_id_for_session(
                            "orch-terminal", "exec-terminal"
                        ),
                        "root_ac_index": 0,
                        "final_retry_attempt": 0,
                        "accepted": accepted,
                        "disposition": "accepted" if accepted else status,
                        "outcome": outcome,
                        "terminal_status": status,
                    }
                ],
            },
        )
    )
    board = reduce_board(EventTail(db, "exec-terminal").fetch_all())
    assert [card["id"] for card in board["columns"]["failed"]] == ([] if accepted else ["root"])
    assert "child" in [card["id"] for card in board["columns"]["completed"]]
    run = list_recent_executions(db)[0]
    assert (run["status"], run["failed_count"]) == (status, 0 if accepted else 1)


@pytest.mark.parametrize("index", [None, True, -1, "0", 1])
def test_unmapped_or_malformed_root_does_not_change_card(index):
    board = reduce_board(
        [
            {
                "event_type": "workflow.progress.updated",
                "payload": {"acceptance_criteria": [{"node_id": "root", "status": "completed"}]},
            },
            {
                "event_type": "execution.ac.acceptance_finalized",
                "payload": {"root_ac_index": index, "accepted": False},
            },
        ]
    )
    assert [card["id"] for card in board["columns"]["completed"]] == ["root"]


@pytest.mark.parametrize("accepted", [None, 0, 1, "false"])
def test_malformed_acceptance_does_not_change_card(accepted):
    board = reduce_board(
        [
            {
                "event_type": "workflow.progress.updated",
                "payload": {"acceptance_criteria": ["root"]},
            },
            {
                "event_type": "execution.ac.acceptance_finalized",
                "payload": {"root_ac_index": 0, "accepted": accepted},
            },
        ]
    )
    assert [card["id"] for card in board["columns"]["pending"]] == ["ac_1"]


def test_root_verdict_does_not_fall_back_to_a_decomposed_child():
    board = reduce_board(
        [
            {
                "event_type": "execution.node.updated",
                "payload": {
                    "node_id": "child",
                    "root_ac_number": 1,
                    "parent_node_id": "root",
                    "status": "completed",
                },
            },
            {
                "event_type": "execution.ac.acceptance_finalized",
                "payload": {"root_ac_index": 0, "accepted": False},
            },
        ]
    )
    assert [card["id"] for card in board["columns"]["completed"]] == ["child"]
