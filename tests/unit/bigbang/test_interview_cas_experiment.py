"""Concurrent interview persistence must not silently overwrite newer state."""

from unittest.mock import MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine, InterviewState
from ouroboros.mcp.tools.authoring_handlers import _plugin_load_state, _plugin_save_state


@pytest.mark.asyncio
async def test_stale_engine_save_conflicts_instead_of_losing_newer_answer(tmp_path):
    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    assert (await engine.save_state(InterviewState(interview_id="cas"))).is_ok
    first = (await engine.load_state("cas")).value
    stale = (await engine.load_state("cas")).value
    first.initial_context = "first"
    stale.initial_context = "stale"
    assert (await engine.save_state(first)).is_ok
    assert (await engine.save_state(stale)).is_err
    assert (await engine.load_state("cas")).value.initial_context == "first"


@pytest.mark.asyncio
async def test_stale_plugin_save_conflicts_with_engine_writer(tmp_path):
    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    assert (await engine.save_state(InterviewState(interview_id="cas"))).is_ok
    stale = (await _plugin_load_state(tmp_path, "cas")).value
    current = (await engine.load_state("cas")).value
    current.initial_context = "engine"
    stale.initial_context = "plugin"
    assert (await engine.save_state(current)).is_ok
    assert (await _plugin_save_state(tmp_path, stale)).is_err
    assert (await engine.load_state("cas")).value.initial_context == "engine"


@pytest.mark.asyncio
async def test_simultaneous_saves_of_same_object_allow_only_one(tmp_path):
    import asyncio

    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    state = InterviewState(interview_id="cas")
    assert (await engine.save_state(state)).is_ok
    state.initial_context = "new"
    results = await asyncio.gather(engine.save_state(state), engine.save_state(state))
    assert sum(result.is_ok for result in results) == 1
    assert sum(result.is_err for result in results) == 1


@pytest.mark.asyncio
async def test_cancelled_save_that_commits_later_requires_reload(tmp_path, monkeypatch):
    import asyncio
    import threading

    import ouroboros.bigbang.interview as interview

    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    state = InterviewState(interview_id="cas")
    assert (await engine.save_state(state)).is_ok
    original = interview._write_interview_snapshot
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()

    def stalled(*args):
        entered.set()
        assert resume.wait(5)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(interview, "_write_interview_snapshot", stalled)
    state.initial_context = "committed after cancellation"
    task = asyncio.create_task(engine.save_state(state))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 5)
    assert (await engine.save_state(state)).is_ok
    assert (await engine.load_state("cas")).value.initial_context == "committed after cancellation"


@pytest.mark.asyncio
async def test_write_failure_keeps_revision_and_prior_file(tmp_path, monkeypatch):
    import ouroboros.bigbang.interview as interview

    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    state = InterviewState(interview_id="cas")
    assert (await engine.save_state(state)).is_ok
    revision = state.persistence_revision
    state.initial_context = "unsaved"

    def fail(*_args):
        raise OSError("disk failure")

    monkeypatch.setattr(interview, "write_owner_only", fail)
    assert (await engine.save_state(state)).is_err
    assert state.persistence_revision == revision
    assert (await engine.load_state("cas")).value.initial_context == ""


@pytest.mark.asyncio
async def test_legacy_state_can_be_updated_and_stale_copy_rejected(tmp_path):
    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    (tmp_path / "interview_cas.json").write_text('{"interview_id":"cas"}', encoding="utf-8")
    first = (await engine.load_state("cas")).value
    stale = (await _plugin_load_state(tmp_path, "cas")).value
    first.initial_context = "migrated"
    assert (await engine.save_state(first)).is_ok
    stale.initial_context = "stale"
    assert (await _plugin_save_state(tmp_path, stale)).is_err
    assert (await engine.load_state("cas")).value.initial_context == "migrated"


@pytest.mark.asyncio
async def test_replace_then_error_reconciles_state_before_error(tmp_path, monkeypatch):
    import ouroboros.bigbang.interview as interview

    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    state = InterviewState(interview_id="cas")
    assert (await engine.save_state(state)).is_ok
    original = interview.write_owner_only

    def commit_then_fail(*args):
        original(*args)
        raise OSError("failure after replace")

    monkeypatch.setattr(interview, "write_owner_only", commit_then_fail)
    state.initial_context = "written"
    assert (await engine.save_state(state)).is_err
    saved = (await engine.load_state("cas")).value
    assert saved.initial_context == "written"
    assert state.persistence_revision == saved.persistence_revision


@pytest.mark.asyncio
async def test_mcp_answer_does_not_report_success_on_conflict(tmp_path):
    from unittest.mock import AsyncMock

    from ouroboros.bigbang.interview import InterviewRound
    from ouroboros.core.errors import ValidationError
    from ouroboros.core.types import Result
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler

    state = InterviewState(
        interview_id="cas",
        rounds=[InterviewRound(round_number=1, question="Question?", user_response=None)],
    )
    engine = MagicMock()
    engine.load_state = AsyncMock(return_value=Result.ok(state))

    async def record(current, answer, question):
        current.record_answer(question, answer)
        return Result.ok(current)

    engine.record_response = AsyncMock(side_effect=record)
    engine.save_state = AsyncMock(
        return_value=Result.err(ValidationError("interview state changed; reload"))
    )
    engine.ask_next_question = AsyncMock()
    handler = InterviewHandler(interview_engine=engine, llm_adapter=MagicMock(), data_dir=tmp_path)
    handler._emit_event_bg = MagicMock()
    result = await handler.handle({"session_id": "cas", "answer": "a specific requirement"})
    assert result.is_err
    assert "reload" in str(result.error)
    handler._emit_event_bg.assert_not_called()
    engine.ask_next_question.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_cancel_after_replace_error_settles_reconciliation(tmp_path, monkeypatch):
    import asyncio
    import threading

    import ouroboros.bigbang.interview as interview

    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    state = InterviewState(interview_id="cas")
    assert (await engine.save_state(state)).is_ok
    original_write = interview.write_owner_only
    original_check = interview._interview_revision_matches
    started, release = threading.Event(), threading.Event()

    def commit_then_fail(*args):
        original_write(*args)
        raise OSError("failed after replace")

    def slow_check(*args):
        started.set()
        assert release.wait(5)
        return original_check(*args)

    monkeypatch.setattr(interview, "write_owner_only", commit_then_fail)
    monkeypatch.setattr(interview, "_interview_revision_matches", slow_check)
    state.initial_context = "written"
    task = asyncio.create_task(engine.save_state(state))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = (await engine.load_state("cas")).value
    assert saved.initial_context == "written"
    assert state.persistence_revision == saved.persistence_revision


@pytest.mark.asyncio
async def test_cancelled_worker_future_terminates_with_unknown_outcome(tmp_path, monkeypatch):
    import asyncio

    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    state = InterviewState(interview_id="cas")
    assert (await engine.save_state(state)).is_ok
    loop = asyncio.get_running_loop()

    def cancelled_future(*_args):
        future = loop.create_future()
        future.cancel()
        return future

    monkeypatch.setattr(loop, "run_in_executor", cancelled_future)
    result = await asyncio.wait_for(engine.save_state(state), timeout=1)
    assert result.is_err
    assert "outcome unknown" in str(result.error)


@pytest.mark.asyncio
async def test_simultaneous_worker_and_caller_cancellation_propagates(tmp_path, monkeypatch):
    import asyncio

    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path, model="test")
    state = InterviewState(interview_id="cas")
    assert (await engine.save_state(state)).is_ok
    loop = asyncio.get_running_loop()

    def cancelled_future(*_args):
        future = loop.create_future()
        future.cancel()
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return future

    monkeypatch.setattr(loop, "run_in_executor", cancelled_future)
    task = asyncio.create_task(engine.save_state(state))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
