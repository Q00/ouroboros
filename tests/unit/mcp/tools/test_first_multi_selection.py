"""Multi-selection is explicit and persists only user-confirmed choices."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selected,expected,excluded",
    [
        ([], "[]", ["Doctors", "Teachers", "Gardeners"]),
        ([1], '["Gardeners"]', ["Doctors", "Teachers"]),
        ([1, 3], '["Gardeners", "Teachers"]', ["Doctors"]),
    ],
)
async def test_multiselect_roundtrip_and_projection(tmp_path, selected, expected, excluded):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Which audiences apply? Select all that apply, including none.","choices":["Gardeners","Doctors","Teachers"],"direct_input_label":"Or type your own answer","multi_select":true}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What should it do?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    result = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "multi_select_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    assert result.is_ok
    sid = result.value.meta["session_id"]
    assert result.value.meta["first_question_presentation"]["multi_select"] is True
    assert adapter.complete.await_count == 1
    resumed = await handler.handle({"session_id": sid})
    assert resumed.value.meta["first_question_presentation"]["multi_select"] is True
    answered = await handler.handle({"session_id": sid, "selected_first_choices": selected})
    assert answered.is_ok, answered.error if answered.is_err else ""
    loaded = (await engine.load_state(sid)).value
    assert loaded.rounds[0].user_response == "[from-user] Selected options (JSON): " + expected
    assert loaded.rounds[0].first_question_selected_indices == tuple(selected)
    projection = SeedGenerator._build_interview_context(object.__new__(SeedGenerator), loaded)
    for option in excluded:
        assert option not in projection


@pytest.mark.asyncio
async def test_multiselect_bad_or_conflicting_answers_keep_pending(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            return_value=Result.ok(
                SimpleNamespace(
                    content='{"question":"Which apply? Select all that apply.","choices":["Aardvarks","Bears"],"direct_input_label":"Type another answer","multi_select":true}'
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
            "multi_select_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    for selections in ([1, 1], [0], [3], [True]):
        result = await handler.handle({"session_id": sid, "selected_first_choices": selections})
        assert result.is_err
        assert (await engine.load_state(sid)).value.rounds[-1].user_response is None
    assert (
        await handler.handle({"session_id": sid, "selected_first_choices": [], "answer": "x"})
    ).is_err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"selected_first_choices": []}, "[from-user] Selected options (JSON): []"),
        (
            {"selected_first_choices_text": "a,b"},
            '[from-user] Selected options (JSON): ["Aardvarks", "Bears"]',
        ),
    ],
)
async def test_sdk_selection_transport(tmp_path, payload, expected):
    from unittest.mock import patch

    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Which apply? Select all that apply.","choices":["Aardvarks","Bears"],"direct_input_label":"Type another answer","multi_select":true}'
                    )
                ),
                Result.ok(SimpleNamespace(content="What else?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    server = MCPServerAdapter(name="multi-test")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        schema = (await client.list_tools()).tools[0].input_schema
        assert schema["properties"]["selected_first_choices"]["items"]["type"] == "integer"
        assert schema["properties"]["selected_first_choices_text"]["type"] == "string"
        start = await client.call_tool(
            "ouroboros_interview",
            {
                "initial_context": "Build a useful tool",
                "structured_first_question": True,
                "multi_select_first_question": True,
                "cwd": str(tmp_path),
            },
        )
        assert not start.is_error
        sid = start.meta["session_id"]
        assert not (
            await client.call_tool("ouroboros_interview", {"session_id": sid, **payload})
        ).is_error
    assert (await engine.load_state(sid)).value.rounds[0].user_response == expected


@pytest.mark.asyncio
async def test_multiselect_free_text_comma_is_preserved_as_user_input(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Which apply? Select all that apply.","choices":["Aardvarks","Bears"],"direct_input_label":"Type another answer","multi_select":true}'
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
            "multi_select_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    answered = await handler.handle({"session_id": sid, "answer": "1, 2"})
    assert answered.is_ok
    assert (await engine.load_state(sid)).value.rounds[0].user_response == "1, 2"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["false", '"true"', "null"])
async def test_multi_requested_but_model_returns_wrong_mode_fails_closed(tmp_path, flag):
    payload = (
        '{"question":"Which apply?","choices":["Aardvarks","Bears"],"direct_input_label":"Type another answer","multi_select":'
        + flag
        + "}"
    )
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=payload)))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    result = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_question": True,
            "multi_select_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    assert result.is_err or result.value.is_error


def test_selections_with_delimiters_cannot_collide():
    from ouroboros.interview_adapters.first_question import (
        FirstQuestionPresentation,
        selected_answers,
    )

    presentation = FirstQuestionPresentation(
        question="Choose options",
        choices=("A; B", "A", "B"),
        direct_input_label="Type something else",
        multi_select=True,
    )
    assert selected_answers(presentation, [1]) != selected_answers(presentation, [2, 3])


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,indices", [("a,c", (1, 3)), ("-", ()), (" B ", (2,))])
async def test_explicit_text_selection_is_persisted(tmp_path, raw, indices):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Which apply? Select all that apply.","choices":["Aardvarks","Bears","Cats"],"direct_input_label":"Type another answer","multi_select":true}'
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
            "multi_select_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    result = await handler.handle({"session_id": sid, "selected_first_choices_text": raw})
    assert result.is_ok
    state = (await engine.load_state(sid)).value
    assert state.rounds[0].first_question_selected_indices == indices


@pytest.mark.parametrize("raw", ["", "none", "a,a", "a,z", "1,2", "a,b,c,d,e"])
def test_invalid_text_selection_does_not_guess(raw):
    from ouroboros.interview_adapters.first_question import parse_selection_text

    with pytest.raises(ValueError):
        parse_selection_text(raw)


def test_multiselect_text_labels_match_rendered_presentation():
    from ouroboros.interview_adapters.first_question import FirstQuestionPresentation

    presentation = FirstQuestionPresentation(
        question="Choose",
        choices=("Aardvarks", "Bears"),
        direct_input_label="Type something else",
        multi_select=True,
    )
    assert "a. Aardvarks" in presentation.render()
    assert "b. Bears" in presentation.render()


@pytest.mark.asyncio
async def test_concurrent_multi_selections_keep_one_consistent_answer(tmp_path):
    import asyncio

    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(
                    SimpleNamespace(
                        content='{"question":"Which apply? Select all that apply.","choices":["Aardvarks","Bears"],"direct_input_label":"Type another answer","multi_select":true}'
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
            "multi_select_first_question": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    original_save = engine.save_state
    count = 0
    both = asyncio.Event()

    async def synchronized_save(state):
        nonlocal count
        if state.rounds and state.rounds[0].user_response is not None and len(state.rounds) == 1:
            count += 1
            if count == 2:
                both.set()
            await both.wait()
        return await original_save(state)

    engine.save_state = synchronized_save
    results = await asyncio.wait_for(
        asyncio.gather(
            handler.handle({"session_id": sid, "selected_first_choices": []}),
            handler.handle({"session_id": sid, "selected_first_choices": [1, 2]}),
        ),
        timeout=10,
    )
    assert sum(r.is_ok for r in results) == 1
    assert sum(r.is_err for r in results) == 1
    saved = (await engine.load_state(sid)).value.rounds[0]
    assert (saved.first_question_selected_indices, saved.user_response) in {
        ((), "[from-user] Selected options (JSON): []"),
        ((1, 2), '[from-user] Selected options (JSON): ["Aardvarks", "Bears"]'),
    }
