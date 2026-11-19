"""The run card of a Codex Auto run updates while the run executes (#3060).

An ``ooo auto`` Seed runs one worker attempt (root 0) and judges every other
criterion from it (``orchestrator.shared_attempt``). The card used to read
"0 of N done" with one executing tile for the whole run: the other criteria
stayed ``pending`` until the run ended, although root 0's attempt executes
them, and they settled together in the last snapshot.

These tests drive the real executor into a real EventStore with a runtime
shaped like Codex (``codex_cli``, with Codex's own thread id as the session id
of its rows), and the console's own follower (``Harness._follow_run``) reads
that store while the run is in flight. No agent CLI runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import sqlite3
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata, SourcedScope, ac_texts
from ouroboros.dashboard_web.reader import default_db_path
from ouroboros.events.base import BaseEvent
from ouroboros.gui import harness, jobs
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.atomic_prompt_builder import install_sourced_scope
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.shared_attempt import shared_attempt_graph
from ouroboros.persistence.event_store import EventStore

SESSION = "orch_add322989299"
EXECUTION = "exec_58174f317577"
# Codex's own thread id: a Codex run writes it as ``session_id`` on its
# ``execution.session.*``, ``execution.ac.completed`` and progress rows.
CODEX_THREAD = "01a1f449-e0ed-7082-8e97-75408d3e48a8"
REQUEST = "Make a 3D galaxy page I can rotate with the mouse."
CRITERIA = (
    "The page renders the galaxy in real time with WebGL",
    "Dragging with the mouse rotates the galaxy",
    "The rotation stays smooth",
)
EVIDENCE = (
    "```json\n"
    '{"files_touched":["src/app.py"],"commands_run":["pytest"],"tests_passed":["pytest"],'
    '"entry_points":[{"symbol":"app.run","call_kind":"function"}]}'
    "\n```"
)
WAIT_SECONDS = 15.0

P, E, C = "pending", "executing", "completed"


class _CodexRuntime:
    """Replays a Codex worker: init, an edit, a passing test run, then its evidence.

    ``during`` runs before the final message, while the worker is still in
    flight, which is when a console poll has to show the run executing.
    """

    runtime_backend = "codex_cli"
    permission_mode = "bypassPermissions"

    def __init__(self, cwd: str, during: Callable[[str], Awaitable[None]]) -> None:
        self.working_directory = cwd
        self._during = during

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Any:
        del tools, system_prompt, resume_handle, resume_session_id
        handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id=CODEX_THREAD,
            cwd=self.working_directory,
            metadata={},
        )
        yield AgentMessage(
            type="system",
            content=f"Session initialized: {CODEX_THREAD}",
            data={"subtype": "init", "session_id": CODEX_THREAD},
            resume_handle=handle,
        )
        yield AgentMessage(
            type="tool",
            content="Edit src/app.py",
            tool_name="Edit",
            data={
                "input": {"file_path": "src/app.py"},
                "subtype": "success",
                "runtime_event_type": "tool.completed",
            },
            resume_handle=handle,
        )
        yield AgentMessage(
            type="tool",
            content="pytest passed\n1 passed in 0.01s",
            tool_name="Bash",
            data={"input": {"command": "pytest"}, "output": "1 passed in 0.01s"},
            resume_handle=handle,
        )
        await self._during(prompt)
        yield AgentMessage(
            type="result", content=EVIDENCE, data={"subtype": "success"}, resume_handle=handle
        )


def _seed(scope: SourcedScope | None) -> Seed:
    return Seed(
        goal="A 3D galaxy page that rotates with the mouse.",
        acceptance_criteria=CRITERIA,
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
        sourced_scope=scope,
    )


def _card_tiles(progress: dict[str, Any] | None) -> tuple[str, ...]:
    """The card's tile statuses in criterion order (tiles are titled by criterion)."""
    if not progress:
        return ()
    by_title = {tile["title"]: tile["status"] for tile in progress["tiles"]}
    return tuple(by_title.get(criterion, "-") for criterion in CRITERIA)


class _Console:
    """One Auto chat whose run card the real follower keeps up to date."""

    def __init__(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(harness, "_RUN_POLL_SEC", 0.02)
        self.h = harness.Harness(jobs.JobRegistry())
        self.job = jobs.Job(id="j-auto", kind="auto", title="t", argv=["ouroboros", "auto"])
        self.job.status = "running"
        self.h._jobs._jobs[self.job.id] = self.job
        self.h._sessions["c1"] = {
            "id": "c1",
            "title": "galaxy",
            "goal": REQUEST,
            "cwd": str(tmp_path),
            "mode": "auto",
            "status": "running",
            "version": 0,
            "messages": [
                {
                    "id": "r1",
                    "role": "assistant",
                    "kind": "run",
                    "text": "",
                    "ts": 0,
                    "execution_id": EXECUTION,
                    "run_status": "running",
                    "progress": None,
                }
            ],
        }
        # Every card state the follower published, in order.
        self.shown: list[tuple[int, int, tuple[str, ...]]] = []
        update = self.h._update

        def recording(session_id: str, fn: Callable[[dict[str, Any]], None]) -> None:
            update(session_id, fn)
            progress = self.card().get("progress")
            if progress:
                self.shown.append((progress["done"], progress["total"], _card_tiles(progress)))

        monkeypatch.setattr(self.h, "_update", recording)
        self.thread = threading.Thread(
            target=self.h._follow_run,
            args=("c1", EXECUTION, None),
            kwargs={"auto_job_id": self.job.id},
            daemon=True,
        )

    def card(self) -> dict[str, Any]:
        session = self.h.get("c1") or {}
        return next(m for m in session["messages"] if m["kind"] == "run")

    async def shows(self, tiles: tuple[str, ...]) -> None:
        """Wait, bounded, until a follower poll has put ``tiles`` on the card."""
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            if _card_tiles(self.card().get("progress")) == tiles:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"the card never showed {tiles}; it showed {self.shown}")


async def _run(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    seed: Seed,
    plan: Any,
    *,
    in_flight: Callable[[_Console, str], Awaitable[None]],
    judging: Callable[[_Console, int], Awaitable[None]],
) -> _Console:
    console = _Console(tmp_path, monkeypatch)
    store = EventStore(f"sqlite+aiosqlite:///{default_db_path()}")
    await store.initialize()
    try:
        # The runner's start row, as a real Codex Auto run writes it (without
        # ``acceptance_root_indices``, so the end row below needs no acceptance plan).
        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id=SESSION,
                data={
                    "execution_id": EXECUTION,
                    "seed_id": "seed_fcc7e95d0bda",
                    "seed_goal": seed.goal,
                    "runtime_backend": "codex_cli",
                    "llm_backend": "codex",
                },
            )
        )
        console.thread.start()
        await console.shows(())  # the run has started; no board row yet
        append = store.append

        async def held(event: BaseEvent) -> None:
            await append(event)
            if event.type == "workflow.progress.updated" and event.data.get("activity") == (
                "Starting parallel execution"
            ):
                await console.shows((P, P, P))  # a poll sees the plan before any dispatch

        store.append = held  # type: ignore[method-assign]

        executor = ParallelACExecutor(
            adapter=_CodexRuntime(str(tmp_path), lambda prompt: in_flight(console, prompt)),  # type: ignore[arg-type]
            event_store=store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            transcript_verifier="decide",
            run_verify_commands=False,
            ac_retry_attempts=0,
            task_cwd=str(tmp_path),
        )

        async def gate(*, ac_index: int, result: ACExecutionResult, **_: Any) -> Any:
            await judging(console, ac_index)
            return result

        executor.check_package_gate = gate  # type: ignore[attr-defined]
        install_sourced_scope(executor, seed.sourced_scope, ac_texts(seed.acceptance_criteria))
        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=plan,
            session_id=SESSION,
            execution_id=EXECUTION,
            tools=["Read", "Edit", "Bash"],
            tool_catalog=None,
            system_prompt="system",
        )
        assert result.all_succeeded
        await store.append(
            BaseEvent(
                type="orchestrator.session.completed",
                aggregate_type="session",
                aggregate_id=SESSION,
                data={"summary": {"success_count": len(CRITERIA)}},
            )
        )
        await console.shows((C, C, C))
    finally:
        console.job.status = "succeeded"
        console.thread.join(timeout=WAIT_SECONDS)
        await store.close()
    assert not console.thread.is_alive()
    assert console.card()["run_status"] == "completed"
    return console


def _session_ids(event_type: str) -> set[tuple[str | None, str | None, str]]:
    """``(execution_id, session_id, aggregate_id)`` of each ``event_type`` row in the store."""
    conn = sqlite3.connect(f"{default_db_path().as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT json_extract(payload, '$.execution_id'), "
            "json_extract(payload, '$.session_id'), aggregate_id "
            "FROM events WHERE event_type = ?",
            [event_type],
        ).fetchall()
    finally:
        conn.close()
    return {(row[0], row[1], row[2]) for row in rows}


@pytest.mark.asyncio
async def test_a_codex_auto_run_card_shows_each_criterion_execute_then_settle(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every criterion of the shared attempt executes with it, then settles as it is judged."""
    seed = _seed(SourcedScope(goal=REQUEST))
    plan = shared_attempt_graph(seed)
    assert plan is not None  # an ``ooo auto`` Seed: one worker attempt for every criterion

    async def in_flight(console: _Console, _prompt: str) -> None:
        # The one worker is executing every criterion: no tile waits as pending.
        await console.shows((E, E, E))

    async def judging(console: _Console, ac_index: int) -> None:
        # Root 0 is judged in its own batch; then each other criterion in turn,
        # and the card already shows every criterion judged before it.
        expected = {0: (E, E, E), 1: (C, E, E), 2: (C, C, E)}[ac_index]
        await console.shows(expected)

    console = await _run(
        tmp_path,
        monkeypatch,
        seed,
        plan.to_execution_plan(),
        in_flight=in_flight,
        judging=judging,
    )

    # The rows have the shape of the real Codex run exec_58174f317577: the
    # worker's own rows name Codex's thread as their session, and only the
    # board's snapshots name the run's session.
    node = f"{EXECUTION}_node_SZPONEEO45JBM"
    assert _session_ids("execution.session.started") == {(EXECUTION, CODEX_THREAD, node)}
    assert _session_ids("execution.ac.completed") == {(EXECUTION, CODEX_THREAD, EXECUTION)}
    assert _session_ids("orchestrator.progress.updated") == {(None, CODEX_THREAD, SESSION)}
    assert _session_ids("workflow.progress.updated") == {(None, SESSION, EXECUTION)}

    seen = [tiles for _done, _total, tiles in console.shown]
    for tiles in [(P, P, P), (E, E, E), (C, E, E), (C, C, E), (C, C, C)]:
        assert tiles in seen
    assert seen.index((P, P, P)) < seen.index((E, E, E)) < seen.index((C, E, E))
    assert seen.index((C, E, E)) < seen.index((C, C, E)) < seen.index((C, C, C))
    # The counter moved before the run ended, not only at its last snapshot.
    assert [done for done, _total, _tiles in console.shown if done][:2] == [1, 2]


@pytest.mark.asyncio
async def test_an_interview_hand_off_run_card_is_unchanged(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Seed without ``sourced_scope`` runs one leaf per criterion, as before."""
    seed = _seed(None)
    assert shared_attempt_graph(seed) is None
    plan = DependencyGraph(
        nodes=tuple(ACNode(index=i, content=c) for i, c in enumerate(CRITERIA)),
        execution_levels=((0,), (1, 2)),
    ).to_execution_plan()

    workers: list[str] = []

    async def in_flight(console: _Console, prompt: str) -> None:
        # Level 1's one leaf runs alone; level 2's two leaves wait for it.
        workers.append(prompt)
        await console.shows((E, P, P) if len(workers) == 1 else (C, E, E))

    async def judging(console: _Console, ac_index: int) -> None:
        del console, ac_index

    console = await _run(
        tmp_path,
        monkeypatch,
        seed,
        plan,
        in_flight=in_flight,
        judging=judging,
    )

    seen = [tiles for _done, _total, tiles in console.shown]
    assert seen.index((P, P, P)) < seen.index((E, P, P)) < seen.index((C, E, E))
    assert seen.index((C, E, E)) < seen.index((C, C, C))
