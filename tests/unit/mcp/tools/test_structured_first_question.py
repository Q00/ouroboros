"""A single opt-in question survives the real MCP engine's persisted round trip."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.answer_provenance import extraction_rounds
from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


@pytest.mark.asyncio
async def test_structured_first_question_resume_and_single_selection(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Who is this for?","choices":["Gardeners","Doctors"],"direct_input_label":"Or type your own answer"}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What should it do?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    start = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    assert start.is_ok and not start.value.is_error
    sid = start.value.meta["session_id"]
    assert start.value.meta["first_question_presentation"]["choices"] == ["Gardeners", "Doctors"]
    assert adapter.complete.await_count == 1
    resumed = await handler.handle({"session_id": sid})
    assert resumed.is_ok
    assert (
        resumed.value.meta["first_question_presentation"]
        == start.value.meta["first_question_presentation"]
    )
    assert adapter.complete.await_count == 1
    answered = await handler.handle(
        {
            "session_id": sid,
            "selected_first_choice": 1,
            "last_question": "Who? 1. Gardeners 2. Doctors",
        }
    )
    assert answered.is_ok
    loaded = await engine.load_state(sid)
    assert loaded.is_ok
    first = loaded.value.rounds[0]
    assert first.user_response == "[from-user] Gardeners"
    assert first.question == "Who is this for?"
    projected = extraction_rounds(loaded.value)[0]
    assert "Doctors" not in str(projected)
    seed_context = SeedGenerator._build_interview_context(
        object.__new__(SeedGenerator), loaded.value
    )
    assert "Gardeners" in seed_context
    assert "Doctors" not in seed_context
    assert first.first_question_presentation.choices == ("Gardeners", "Doctors")


@pytest.mark.asyncio
async def test_malformed_structured_output_does_not_show_raw_json(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            return_value=Result.ok(SimpleNamespace(content='{"secret":"synthetic"}'))
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    result = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    assert "synthetic" not in str(result)
    assert result.is_err or result.value.is_error


@pytest.mark.asyncio
async def test_free_text_answer_retains_only_user_answer(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Who is this for?","choices":["Gardeners","Doctors"],"direct_input_label":"Or type your own answer"}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What should it do?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    start = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = start.value.meta["session_id"]
    result = await handler.handle({"session_id": sid, "answer": "[from-user] Teachers"})
    assert result.is_ok
    loaded = await engine.load_state(sid)
    first = extraction_rounds(loaded.value)[0]
    assert first.question == "Who is this for?"
    assert first.answer == "[from-user] Teachers"
    assert "Gardeners" not in str(first) and "Doctors" not in str(first)


@pytest.mark.asyncio
async def test_invalid_number_preserves_pending_round(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            return_value=Result.ok(
                SimpleNamespace(
                    content='{"question":"Who is this for?","choices":["Gardeners","Doctors"],"direct_input_label":"Or type your own answer"}'
                )
            )
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    start = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = start.value.meta["session_id"]
    result = await handler.handle({"session_id": sid, "selected_first_choice": 3})
    assert result.is_err
    loaded = await engine.load_state(sid)
    assert loaded.value.rounds[0].user_response is None
    assert loaded.value.rounds[0].first_question_presentation is not None
    assert adapter.complete.await_count == 1


@pytest.mark.asyncio
async def test_opt_in_rejected_in_plugin_mode_before_dispatch(tmp_path):
    adapter = SimpleNamespace(complete=AsyncMock())
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(
        interview_engine=engine,
        llm_adapter=adapter,
        data_dir=tmp_path,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    result = await handler.handle(
        {"initial_context": "Build a useful tool", "structured_first_question": True}
    )
    assert result.is_err
    assert adapter.complete.await_count == 0
    assert not list(tmp_path.glob("interview_*.json"))


@pytest.mark.parametrize(
    "content",
    [
        '{"question":"Who?","choices":["one","one"],"direct_input_label":"Other"}',
        '{"question":"Who?","choices":["1","2"],"direct_input_label":"Other"}',
        '{"question":"Who?\n1. x","choices":["x","y"],"direct_input_label":"Other"}',
        '{"question":"Who?","choices":["one","two"],"direct_input_label":"Other","unexpected":true}',
        '{"question":"Who?","choices":["one"],"direct_input_label":"Other"}',
    ],
)
@pytest.mark.asyncio
async def test_invalid_presentations_are_not_accepted_or_exposed(tmp_path, content):
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=content)))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    result = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    assert result.is_err or result.value.is_error
    assert content not in str(result)
    assert adapter.complete.await_count == 1


@pytest.mark.asyncio
async def test_legacy_start_remains_plain_question(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content="Who is it for?")))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    result = await handler.handle({"initial_context": "Build a useful tool", "cwd": str(tmp_path)})
    assert result.is_ok
    assert "first_question_presentation" not in result.value.meta
    assert "Who is it for?" in result.value.content[0].text
    assert adapter.complete.await_count == 1


@pytest.mark.asyncio
async def test_text_number_stays_literal_and_not_an_option(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"How many?","choices":["A few","Several"],"direct_input_label":"Type another answer"}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What else?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    result = await handler.handle({"session_id": sid, "answer": "[from-user] 1"})
    assert result.is_ok
    saved = await engine.load_state(sid)
    assert saved.value.rounds[0].user_response == "[from-user] 1"


@pytest.mark.parametrize("text", ["line\rnext", "line\x1b[2J", "line\u2028next", "line\u202eevil"])
def test_untrusted_presentation_cannot_inject_terminal_or_lines(text):
    from pydantic import ValidationError

    from ouroboros.interview_adapters.first_question import FirstQuestionPresentation

    for field in ("question", "direct_input_label", "choice"):
        obj = {
            "question": "Who?",
            "choices": ["Gardeners", "Doctors"],
            "direct_input_label": "Type another",
        }
        if field == "choice":
            obj["choices"] = [text, "Doctors"]
        else:
            obj[field] = text
        with pytest.raises(ValidationError):
            FirstQuestionPresentation.model_validate(obj)


@pytest.mark.asyncio
async def test_structured_answer_conflict_does_not_emit_success_or_advance(tmp_path):
    from ouroboros.core.errors import ValidationError

    adapter = SimpleNamespace(
        complete=AsyncMock(
            return_value=Result.ok(
                SimpleNamespace(
                    content='{"question":"Who is this for?","choices":["Gardeners","Doctors"],"direct_input_label":"Or type your own answer"}'
                )
            )
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    engine.save_state = AsyncMock(return_value=Result.err(ValidationError("stale revision")))
    result = await handler.handle({"session_id": sid, "selected_first_choice": 1})
    assert result.is_err
    assert adapter.complete.await_count == 1
    saved = await engine.load_state(sid)
    assert saved.value.rounds[0].user_response is None


@pytest.mark.asyncio
async def test_selected_completion_like_choice_is_an_answer(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Which operation?","choices":["Create a seed catalog","Track gardens"],"direct_input_label":"Type another answer"}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What else?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    result = await handler.handle({"session_id": sid, "selected_first_choice": 1})
    assert result.is_ok
    saved = await engine.load_state(sid)
    assert saved.value.rounds[0].user_response == "[from-user] Create a seed catalog"
    assert len(saved.value.rounds) == 2


@pytest.mark.parametrize(
    "selection,answer", [(True, None), (0, None), (2, "different answer"), ("1", None)]
)
@pytest.mark.asyncio
async def test_invalid_or_conflicting_selections_preserve_pending(tmp_path, selection, answer):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            return_value=Result.ok(
                SimpleNamespace(
                    content='{"question":"Who is this for?","choices":["Gardeners","Doctors"],"direct_input_label":"Type another answer"}'
                )
            )
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    args = {"session_id": sid, "selected_first_choice": selection}
    if answer is not None:
        args["answer"] = answer
    result = await handler.handle(args)
    assert result.is_err
    saved = await engine.load_state(sid)
    assert saved.value.rounds[0].user_response is None
    assert adapter.complete.await_count == 1


@pytest.mark.asyncio
async def test_real_mcp_sdk_transport_round_trip(tmp_path):
    from unittest.mock import patch

    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    model = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Who is this for?","choices":["Gardeners","Doctors"],"direct_input_label":"Type another answer"}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What else?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=model, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=model, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, model)
    server = MCPServerAdapter(name="structured-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        listed = (await client.list_tools()).tools[0]
        assert listed.input_schema["properties"]["selected_first_choice"]["type"] == "integer"
        started = await client.call_tool(
            "ouroboros_interview",
            {
                "initial_context": "Build a useful tool",
                "structured_first_question": True,
                "cwd": str(tmp_path),
            },
        )
        assert not started.is_error
        sid = started.meta["session_id"]
        assert started.meta["first_question_presentation"]["choices"] == ["Gardeners", "Doctors"]
        resumed = await client.call_tool("ouroboros_interview", {"session_id": sid})
        assert (
            resumed.meta["first_question_presentation"]
            == started.meta["first_question_presentation"]
        )
        result = await client.call_tool(
            "ouroboros_interview", {"session_id": sid, "selected_first_choice": 1}
        )
        assert not result.is_error
    saved = await engine.load_state(sid)
    assert saved.value.rounds[0].user_response == "[from-user] Gardeners"
    assert "Doctors" not in SeedGenerator._build_interview_context(
        object.__new__(SeedGenerator), saved.value
    )


@pytest.mark.asyncio
async def test_two_simultaneous_selections_one_saved_one_conflict(tmp_path):
    import asyncio

    model = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Who is this for?","choices":["Gardeners","Doctors"],"direct_input_label":"Type another answer"}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What else?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=model, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=model, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, model)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    original_save = engine.save_state
    arrived = 0
    both_arrived = asyncio.Event()
    events = []
    handler._emit_event_bg = events.append

    async def synchronized_save(state):
        nonlocal arrived
        if state.rounds and state.rounds[0].user_response is not None and len(state.rounds) == 1:
            arrived += 1
            if arrived == 2:
                both_arrived.set()
            await both_arrived.wait()
        return await original_save(state)

    engine.save_state = synchronized_save
    one, two = await asyncio.wait_for(
        asyncio.gather(
            handler.handle({"session_id": sid, "selected_first_choice": 1}),
            handler.handle({"session_id": sid, "selected_first_choice": 2}),
        ),
        timeout=10,
    )
    assert sum(result.is_ok for result in (one, two)) == 1
    assert sum(result.is_err for result in (one, two)) == 1
    assert model.complete.await_count == 2
    assert sum(event.type == "interview.response.recorded" for event in events) == 1
    saved = await engine.load_state(sid)
    assert saved.value.rounds[0].user_response in {"[from-user] Gardeners", "[from-user] Doctors"}
    assert len(saved.value.rounds) == 2


@pytest.mark.asyncio
async def test_calibration_preserves_choice_identity_and_resume(tmp_path):
    model = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Who is this for?","choices":["Gardeners","Doctors"],"direct_input_label":"Type another answer"}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What else?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=model, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=model, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, model)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    calibrated = await handler.handle(
        {"session_id": sid, "calibration_input": "I do not know what gardening means"}
    )
    assert calibrated.is_ok
    assert calibrated.value.meta["question_rephrased"] is False
    assert (
        calibrated.value.meta["first_question_presentation"]
        == started.value.meta["first_question_presentation"]
    )
    assert "Gardeners" in calibrated.value.content[0].text
    assert model.complete.await_count == 1
    resumed = await handler.handle({"session_id": sid})
    assert (
        resumed.value.meta["first_question_presentation"]
        == started.value.meta["first_question_presentation"]
    )
    answered = await handler.handle({"session_id": sid, "selected_first_choice": 1})
    assert answered.is_ok
    saved = await engine.load_state(sid)
    assert saved.value.rounds[0].user_response == "[from-user] Gardeners"
