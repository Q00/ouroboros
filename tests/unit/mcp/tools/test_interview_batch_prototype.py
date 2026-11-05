"""Experimental batch protocol: real handler, persistence and fake provider."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


def _proposal():
    return json.dumps(
        {
            "questions": [
                {
                    "presentation": {
                        "question": "Who uses it?",
                        "choices": ["Gardeners", "Teachers"],
                        "direct_input_label": "Type another answer",
                        "multi_select": True,
                    },
                    "dimension": "goal",
                    "recommended_index": 1,
                    "depends_on": [],
                },
                {
                    "presentation": {
                        "question": "What size?",
                        "choices": ["Small", "Large"],
                        "direct_input_label": "Type another answer",
                        "multi_select": False,
                    },
                    "dimension": "constraint",
                    "recommended_index": 1,
                    "depends_on": [],
                },
                {
                    "presentation": {
                        "question": "How to measure success?",
                        "choices": ["Faster", "Cheaper"],
                        "direct_input_label": "Type another answer",
                        "multi_select": False,
                    },
                    "dimension": "success",
                    "recommended_index": 1,
                    "depends_on": [],
                },
            ]
        }
    )


@pytest.mark.asyncio
async def test_sdk_pages_preserve_batch_and_native_text_round_trip(tmp_path):
    from unittest.mock import patch

    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    proposal = json.loads(_proposal())
    for question, dimension in [("Where is it used?", "context"), ("What is excluded?", "scope")]:
        proposal["questions"].append(
            {
                "presentation": {
                    "question": question,
                    "choices": ["First", "Second"],
                    "direct_input_label": "Type another answer",
                    "multi_select": False,
                },
                "dimension": dimension,
                "recommended_index": 1,
                "depends_on": [],
            }
        )
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=json.dumps(proposal))))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    server = MCPServerAdapter(name="batch-capacity")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        assert (await client.list_tools()).tools[0].input_schema["properties"]["batch_page_size"][
            "type"
        ] == "integer"
        start = await client.call_tool(
            "ouroboros_interview",
            {
                "initial_context": "Build a tool",
                "structured_first_batch": True,
                "batch_page_size": 4,
            },
        )
        assert not start.is_error
        items = start.meta["questions"]
        assert list(map(len, start.meta["question_pages"])) == [4, 1]
        pages = [q for page in start.meta["question_pages"] for q in page]
        assert [q["batch_number"] for q in pages] == [1, 2, 3, 4, 5]
        assert [{k: v for k, v in q.items() if k != "batch_number"} for q in pages] == items
        sid, digest = start.meta["session_id"], start.meta["batch_digest"]
        resumed = await client.call_tool(
            "ouroboros_interview", {"session_id": sid, "batch_page_size": 2}
        )
        assert resumed.meta["batch_digest"] == digest
        assert resumed.meta["questions"] == items
        assert list(map(len, resumed.meta["question_pages"])) == [2, 2, 1]
        pages = [q for page in resumed.meta["question_pages"] for q in page]
        assert [q["batch_number"] for q in pages] == [1, 2, 3, 4, 5]
        assert [{k: v for k, v in q.items() if k != "batch_number"} for q in pages] == items
        base = {
            "session_id": sid,
            "batch_confirm_digest": digest,
            "batch_independence_confirmed": True,
        }
        native = await client.call_tool(
            "ouroboros_interview",
            {
                **base,
                "batch_page_size": 4,
                "batch_answers": [
                    {"question_id": items[0]["question_id"], "selected_choices": []},
                    {"question_id": items[1]["question_id"], "selected_choices": [2]},
                ],
            },
        )
        assert not native.is_error
        assert native.meta["batch_digest"] == digest
        partial = await client.call_tool(
            "ouroboros_interview", {"session_id": sid, "batch_page_size": 4}
        )
        assert not partial.is_error
        assert partial.meta["batch_digest"] == digest
        assert partial.meta["answered_question_ids"] == [
            items[0]["question_id"],
            items[1]["question_id"],
        ]
        assert [q["question_id"] for page in partial.meta["question_pages"] for q in page] == [
            q["question_id"] for q in items
        ]
        text_result = await client.call_tool(
            "ouroboros_interview",
            {**base, "batch_page_size": 4, "batch_answers_text": "3: a / 4: b / 5: free text"},
        )
        assert not text_result.is_error
        assert text_result.meta["batch_complete"] is True
    state = (await engine.load_state(sid)).value
    assert state.rounds[0].batch_selected_indices == ()
    assert state.rounds[1].batch_selected_indices == (2,)
    assert state.rounds[4].user_response == "free text"


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [0, -1, 6, True, "4"])
async def test_invalid_page_capacity_rejected_before_start(tmp_path, capacity):
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    result = await handler.handle(
        {
            "initial_context": "Build a tool",
            "structured_first_batch": True,
            "batch_page_size": capacity,
        }
    )
    assert result.is_err
    assert adapter.complete.await_count == 0


@pytest.mark.asyncio
async def test_first_batch_partial_replay_resume_and_projection(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    assert started.is_ok, started.error if started.is_err else ""
    assert adapter.complete.await_count == 1
    sid = started.value.meta["session_id"]
    digest = started.value.meta["batch_digest"]
    ids = [q["question_id"] for q in started.value.meta["questions"]]
    assert len(ids) == len(set(ids)) == 3
    resumed = await handler.handle({"session_id": sid})
    assert resumed.value.meta["batch_digest"] == digest
    assert (await handler.handle({"session_id": sid, "answer": "local"})).is_err
    payload = {
        "session_id": sid,
        "batch_confirm_digest": digest,
        "batch_independence_confirmed": True,
        "batch_answers": [
            {"question_id": ids[0], "selected_choices": [1]},
            {"question_id": ids[2], "answer": "Fast enough"},
        ],
    }
    assert (await handler.handle(payload)).is_ok
    assert (await handler.handle(payload)).is_ok
    state = (await engine.load_state(sid)).value
    assert state.rounds[0].user_response == '[from-user] Selected options (JSON): ["Gardeners"]'
    assert state.rounds[0].first_question_selected_indices == (1,)
    assert state.rounds[1].user_response is None
    assert state.rounds[2].user_response == "Fast enough"
    assert (
        await handler.handle(
            {**payload, "batch_answers": [{"question_id": ids[0], "answer": "Doctors"}]}
        )
    ).is_err
    assert (
        await handler.handle(
            {"session_id": sid, "batch_answers": [{"question_id": ids[1], "answer": "Small"}]}
        )
    ).is_err
    with pytest.raises(ValueError, match="Pending batch"):
        SeedGenerator._build_interview_context(object.__new__(SeedGenerator), state)
    final = {**payload, "batch_answers": [{"question_id": ids[1], "answer": "Small"}]}
    assert (await handler.handle(final)).is_ok
    state = (await engine.load_state(sid)).value
    projection = SeedGenerator._build_interview_context(object.__new__(SeedGenerator), state)
    assert "Teachers" not in projection


@pytest.mark.asyncio
async def test_pending_batch_blocks_seed_calibration_plugin_and_has_safe_rejection(tmp_path):
    from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler

    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=_proposal())),
                Result.ok(SimpleNamespace(content="What is your goal?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    digest = started.value.meta["batch_digest"]
    assert (
        await handler.handle({"session_id": sid, "calibration_input": "I am a beginner"})
    ).is_err
    seed_handler = GenerateSeedHandler(
        interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path
    )
    assert (await seed_handler.handle({"session_id": sid, "force": True})).is_err
    # Other backend cannot reinterpret the last member as a single pending question.
    plugin_handler = InterviewHandler(
        interview_engine=engine,
        llm_adapter=adapter,
        data_dir=tmp_path,
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )
    plugin_handler._create_interview_engine = lambda: (engine, adapter)
    assert (await plugin_handler.handle({"session_id": sid, "answer": "anything"})).is_err
    assert (
        await handler.handle(
            {"session_id": sid, "batch_reject": True, "batch_confirm_digest": "wrong"}
        )
    ).is_err
    rejected = await handler.handle(
        {"session_id": sid, "batch_reject": True, "batch_confirm_digest": digest}
    )
    assert rejected.is_ok and rejected.value.meta["batch_discarded"]
    assert not (await engine.load_state(sid)).value.rounds
    resumed = await handler.handle({"session_id": sid})
    assert resumed.is_ok


@pytest.mark.asyncio
async def test_discard_after_partial_answers_keeps_confirmed_ids_and_round_numbers(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=_proposal())),
                Result.ok(SimpleNamespace(content="What is next?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    sid, digest = started.value.meta["session_id"], started.value.meta["batch_digest"]
    ids = [q["question_id"] for q in started.value.meta["questions"]]
    answer = {
        "session_id": sid,
        "batch_confirm_digest": digest,
        "batch_independence_confirmed": True,
        "batch_answers": [
            {"question_id": ids[0], "selected_choices": [1]},
            {"question_id": ids[2], "answer": "Fast enough"},
        ],
    }
    assert (await handler.handle(answer)).is_ok
    discarded = await handler.handle(
        {"session_id": sid, "batch_reject": True, "batch_confirm_digest": digest}
    )
    assert discarded.is_ok
    state = (await engine.load_state(sid)).value
    assert [r.question_id for r in state.rounds] == [ids[0], ids[2]]
    assert [r.round_number for r in state.rounds] == [1, 3]
    assert state.rounds[0].first_question_selected_indices == (1,)
    assert state.rounds[1].user_response == "Fast enough"
    resumed = await handler.handle({"session_id": sid})
    assert resumed.is_ok
    state = (await engine.load_state(sid)).value
    assert [r.round_number for r in state.rounds] == [1, 3, 4]


@pytest.mark.asyncio
async def test_dangling_invalid_numeric_selection_does_not_save_an_answer(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {"initial_context": "Build a tool", "structured_first_batch": True}
    )
    assert started.is_ok
    sid = started.value.meta["session_id"]
    before = (await engine.load_state(sid)).value.model_dump(mode="json")
    rejected = await handler.handle(
        {
            "session_id": sid,
            "batch_confirm_digest": started.value.meta["batch_digest"],
            "batch_independence_confirmed": True,
            "batch_answers_text": "1: 5 /",
        }
    )
    assert rejected.is_err
    assert (await engine.load_state(sid)).value.model_dump(mode="json") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "last_answer"),
    [
        ('1: a / 2: b / 3: "fast / 2: must not split"', "fast / 2: must not split"),
        ("1: 1 / 2: 2 / 3: fast enough", "fast enough"),
    ],
)
async def test_sdk_batch_text_transport_and_explicit_independence(tmp_path, answers, last_answer):
    from unittest.mock import patch

    from mcp import Client
    from mcp.server import MCPServer

    from ouroboros.mcp.server.adapter import MCPServerAdapter

    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    server = MCPServerAdapter(name="batch-prototype")
    server.register_tool(handler)
    with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
        await server.serve(transport="stdio")
    async with Client(server._mcp_server, mode="auto") as client:
        schema = (await client.list_tools()).tools[0].input_schema["properties"]
        assert schema["batch_answers_text"]["type"] == "string"
        start = await client.call_tool(
            "ouroboros_interview",
            {
                "initial_context": "Build a useful tool",
                "structured_first_batch": True,
                "cwd": str(tmp_path),
            },
        )
        assert not start.is_error
        sid, digest = start.meta["session_id"], start.meta["batch_digest"]
        assert "a. Gardeners" in start.content[0].text
        result = await client.call_tool(
            "ouroboros_interview",
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_independence_confirmed": True,
                "batch_answers_text": answers,
            },
        )
        assert not result.is_error
        assert result.meta["batch_complete"] is True
    state = (await engine.load_state(sid)).value
    assert state.rounds[0].batch_selected_indices == (1,)
    assert state.rounds[1].batch_selected_indices == (2,)
    assert state.rounds[2].user_response == last_answer


@pytest.mark.asyncio
async def test_conflicting_batch_controls_fail_without_mutation(tmp_path):
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    assert (
        await handler.handle(
            {
                "initial_context": "Build a useful tool",
                "structured_first_batch": True,
                "batch_answers": [],
            }
        )
    ).is_err
    assert adapter.complete.await_count == 0
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    sid, digest = started.value.meta["session_id"], started.value.meta["batch_digest"]
    before = (await engine.load_state(sid)).value.model_dump_json()
    assert (
        await handler.handle(
            {
                "session_id": sid,
                "batch_reject": True,
                "batch_confirm_digest": digest,
                "answer": "I explicitly answered",
            }
        )
    ).is_err
    assert (await engine.load_state(sid)).value.model_dump_json() == before


@pytest.mark.asyncio
async def test_concurrent_batch_answer_and_discard_have_one_cas_winner(tmp_path):
    import asyncio

    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    sid, digest = started.value.meta["session_id"], started.value.meta["batch_digest"]
    qid = started.value.meta["questions"][0]["question_id"]
    save = engine.save_state
    count = 0
    both = asyncio.Event()

    async def barrier(state):
        nonlocal count
        count += 1
        if count == 2:
            both.set()
        await both.wait()
        return await save(state)

    engine.save_state = barrier
    accepted, rejected = await asyncio.wait_for(
        asyncio.gather(
            handler.handle(
                {
                    "session_id": sid,
                    "batch_confirm_digest": digest,
                    "batch_independence_confirmed": True,
                    "batch_answers": [{"question_id": qid, "answer": "gardeners"}],
                }
            ),
            handler.handle(
                {"session_id": sid, "batch_confirm_digest": digest, "batch_reject": True}
            ),
        ),
        timeout=10,
    )
    assert sum(r.is_ok for r in (accepted, rejected)) == 1
    state = (await engine.load_state(sid)).value
    if accepted.is_ok:
        assert state.rounds[0].question_id == qid and state.rounds[0].user_response == "gardeners"
        assert len(state.rounds) == 3
    else:
        assert state.rounds == []


@pytest.mark.asyncio
async def test_cli_resume_rejects_pending_batch_without_provider_or_write(tmp_path, monkeypatch):
    from unittest.mock import Mock, patch

    import typer

    from ouroboros.cli.commands import init as init_module
    from ouroboros.cli.commands import pm as pm_module

    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    sid = started.value.meta["session_id"]
    before = (await engine.load_state(sid)).value.model_dump_json()
    adapter.complete.reset_mock()
    monkeypatch.setattr(init_module, "_get_adapter", Mock(return_value=adapter))
    monkeypatch.setattr(init_module, "InterviewEngine", Mock(return_value=engine))
    monkeypatch.setattr(init_module, "AmbiguityScorer", Mock())
    loop = AsyncMock()
    monkeypatch.setattr(init_module, "_run_interview_loop", loop)
    with pytest.raises(typer.Exit) as exc:
        await init_module._run_interview("", resume_id=sid, state_dir=tmp_path)
    assert exc.value.exit_code == 1
    loop.assert_not_awaited()
    fake_pm = Mock()
    fake_pm.load_state = engine.load_state
    with (
        patch.object(pm_module, "create_llm_adapter", return_value=adapter),
        patch("ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=fake_pm),
    ):
        with pytest.raises(typer.Exit) as exc:
            await pm_module._run_pm_interview(
                resume_id=sid, model="synthetic", backend="codex", debug=False
            )
    assert exc.value.exit_code == 1
    adapter.complete.assert_not_awaited()
    assert (await engine.load_state(sid)).value.model_dump_json() == before


@pytest.mark.asyncio
async def test_partial_discard_preserves_event_round_identity(tmp_path):
    from ouroboros.persistence.event_store import EventStore

    adapter = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                Result.ok(SimpleNamespace(content=_proposal())),
                Result.ok(SimpleNamespace(content="What is next?")),
            ]
        )
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite'}")
    handler = InterviewHandler(
        interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path, event_store=store
    )
    handler._create_interview_engine = lambda: (engine, adapter)
    start = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    sid, digest = start.value.meta["session_id"], start.value.meta["batch_digest"]
    third_id = start.value.meta["questions"][2]["question_id"]
    await handler._drain_bg_tasks()
    result = await handler.handle(
        {
            "session_id": sid,
            "batch_confirm_digest": digest,
            "batch_independence_confirmed": True,
            "batch_answers": [{"question_id": third_id, "answer": "Fast enough"}],
        }
    )
    assert result.is_ok
    await handler._drain_bg_tasks()
    assert (
        await handler.handle(
            {"session_id": sid, "batch_reject": True, "batch_confirm_digest": digest}
        )
    ).is_ok
    assert (await handler.handle({"session_id": sid})).is_ok
    await handler._drain_bg_tasks()
    state = (await engine.load_state(sid)).value
    assert [(r.question_id, r.round_number) for r in state.rounds] == [(third_id, 3), (None, 4)]
    events = await store.replay("interview", sid)
    response = next(event for event in events if event.type == "interview.response.recorded")
    assert response.data["question_id"] == third_id
    assert response.data["round_number"] == 3
    await store.close()


@pytest.mark.asyncio
async def test_declared_dependency_is_rejected_and_original_session_recoverable(tmp_path):
    proposal = json.loads(_proposal())
    proposal["questions"][1]["depends_on"] = ["first"]
    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=json.dumps(proposal))))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    result = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    assert result.is_ok and result.value.is_error and result.value.meta["recoverable"]
    state = (await engine.load_state(result.value.meta["session_id"])).value
    assert state.rounds == []


@pytest.mark.asyncio
async def test_pending_batch_blocks_direct_scoring_and_question_generation(tmp_path):
    from ouroboros.bigbang.ambiguity import AmbiguityScorer

    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    started = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    state = (await engine.load_state(started.value.meta["session_id"])).value
    adapter.complete.reset_mock()
    assert engine.prepare_next_question(state).is_err
    scorer = AmbiguityScorer(llm_adapter=adapter, model="synthetic")
    assert (await scorer.score(state)).is_err
    assert (await scorer.score_per_dimension(state)).is_err
    adapter.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_only_host_can_recover_ids_and_answer(tmp_path):
    import re

    adapter = SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    result = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    text = result.value.content[0].text  # Simulate a host that discards metadata.
    sid = re.search(r"Session ID: (interview_[0-9a-f]{16})", text).group(1)
    digest = re.search(r"Batch digest: ([0-9a-f]{64})", text).group(1)
    ids = re.findall(r"question_id=([0-9a-f]{32})", text)
    assert len(ids) == 3
    answered = await handler.handle(
        {
            "session_id": sid,
            "batch_confirm_digest": digest,
            "batch_independence_confirmed": True,
            "batch_answers_text": '1: a / 2: b / 3: "fast"',
        }
    )
    assert answered.is_ok
    assert (await engine.load_state(sid)).value.rounds[0].question_id == ids[0]
