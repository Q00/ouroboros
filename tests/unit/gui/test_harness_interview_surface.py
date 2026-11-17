"""The console back end labels the interviews it drives with the ``console`` surface."""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from ouroboros.auto.adapters import HandlerInterviewBackend
from ouroboros.bigbang.interview_closure import InterviewSurface, current_interview_surface
from ouroboros.gui.harness import Harness
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from tests.unit.mcp.tools.test_interview_closure_instrumentation import (
    _CapturingEventStore,
    _StubEngine,
)


def _handler(tmp_path: Path) -> tuple[_CapturingEventStore, InterviewHandler]:
    store = _CapturingEventStore()
    handler = InterviewHandler(
        interview_engine=_StubEngine(state_dir=tmp_path),  # type: ignore[arg-type]
        event_store=store,  # type: ignore[arg-type]
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )
    return store, handler


def test_interviews_driven_on_the_console_loop_are_labeled_console(tmp_path: Path) -> None:
    store, handler = _handler(tmp_path)
    backend = HandlerInterviewBackend(handler, cwd=str(tmp_path))
    loop = Harness()._loop_ready()

    turn = loop.submit(backend.start("Build a CLI", cwd=str(tmp_path))).result(timeout=30)
    loop.submit(
        backend.answer(turn.session_id, "It lints files.", last_question=turn.question)
    ).result(timeout=30)
    loop.submit(handler._drain_bg_tasks()).result(timeout=30)

    (started,) = store.of("interview.started")
    assert started.data["surface"] == "console"
    assert "host" not in started.data
    (recorded,) = store.of("interview.response.recorded")
    assert recorded.data["answer_source"] == "from_user"
    assert current_interview_surface() is None


def test_console_spawn_path_runs_work_under_the_console_label(tmp_path: Path) -> None:
    harness = Harness()
    seen: list[object] = []
    done = threading.Event()

    async def probe() -> None:
        seen.append(current_interview_surface())
        done.set()

    harness._spawn("chat1", probe())

    assert done.wait(timeout=30)
    assert seen == [InterviewSurface.CONSOLE]


def test_console_loop_returns_results_and_propagates_errors() -> None:
    loop = Harness()._loop_ready()

    async def value() -> int:
        return 7

    async def boom() -> None:
        raise RuntimeError("handler failed")

    assert loop.submit(value()).result(timeout=30) == 7
    with pytest.raises(RuntimeError, match="handler failed"):
        loop.submit(boom()).result(timeout=30)
