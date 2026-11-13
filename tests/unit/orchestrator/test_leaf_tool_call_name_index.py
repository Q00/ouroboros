"""Claude tool results are named from an index built once per leaf stream.

Claude's ToolResultBlock carries the call id but no tool name, so the leaf
stream resolves the name from the prior tool call with that id. It used to
rescan (and copy) the whole history for every result. These tests pin that
the incremental ``_ToolCallNameIndex`` answers exactly like that rescan, kept
below as a reference copy, also when the list is replaced or truncated, and
that each message is read once.
"""

from __future__ import annotations

from datetime import UTC, datetime
import random
from typing import Any
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator import leaf_dispatcher
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence import claims
from ouroboros.orchestrator.leaf_dispatcher import _correlated_tool_result_name, _ToolCallNameIndex
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message


def _reference_result_name(
    messages: list[AgentMessage],
    result_message: AgentMessage,
) -> str | None:
    """The full-history rescan this change replaced, verbatim."""
    result_call_id = claims._runtime_message_tool_call_id(result_message)
    if result_call_id is None or claims._runtime_message_has_conflicting_tool_call_ids(
        result_message
    ):
        return None
    if any(
        claims._runtime_message_has_conflicting_tool_call_ids(message)
        and result_call_id in claims._runtime_message_tool_call_ids(message)
        for message in messages[:-1]
    ):
        return None
    names = {
        message.tool_name
        for message in messages[:-1]
        if message.tool_name is not None
        and not claims._runtime_message_is_tool_completion(message)
        and claims._runtime_message_tool_call_id(message) == result_call_id
    }
    return next(iter(names)) if len(names) == 1 else None


_IDS = ("a", "b", "c", " a ", "d")
_ALIAS_KEYS = ("tool_call_id", "tool_use_id", "call_id")


def _random_ids(rng: random.Random) -> dict[str, Any]:
    """Correlation aliases: absent, one, agreeing, conflicting, blank or malformed."""
    data: dict[str, Any] = {}
    roll = rng.random()
    if roll < 0.2:
        return data
    data[rng.choice(_ALIAS_KEYS)] = rng.choice(_IDS)
    if roll > 0.75:
        # A second alias, nested or not, that may agree or conflict.
        value = rng.choice((*_IDS, "", "  ", 7))
        where = rng.choice(("top", "meta", "tool_result", "tool_result_meta"))
        key = rng.choice(_ALIAS_KEYS)
        if where == "top":
            data[key] = value
        elif where == "meta":
            data["meta"] = {key: value}
        elif where == "tool_result":
            data["tool_result"] = {key: value, "is_error": False}
        else:
            data["tool_result"] = {"is_error": False, "meta": {key: value}}
    return data


def _random_message(rng: random.Random) -> AgentMessage:
    data = _random_ids(rng)
    kind = rng.random()
    if kind < 0.45:
        return AgentMessage(
            type=rng.choice(("assistant", "tool")),
            content="Calling tool",
            tool_name=rng.choice(("Edit", "Write", "Read", "", None)),
            data={"tool_input": {"file_path": "x.py"}, **data},
        )
    if kind < 0.85:
        completion = rng.choice(
            (
                {"subtype": "tool_result"},
                {"runtime_event_type": "tool.completed"},
                {"tool_result": {"is_error": False}},
            )
        )
        merged = {**completion, **data}
        return AgentMessage(
            type=rng.choice(("tool_result", "user")),
            content="ok",
            tool_name=rng.choice((None, None, "Edit")),
            data=merged,
        )
    return AgentMessage(type="assistant", content="thinking", data=data)


@pytest.mark.parametrize("seed", range(40))
def test_index_matches_full_rescan_on_random_streams(seed: int) -> None:
    """Every append, as the stream loop does, then a lookup for that message."""
    rng = random.Random(seed)
    messages: list[AgentMessage] = []
    index = _ToolCallNameIndex()
    resolved = 0
    for _ in range(120):
        message = _random_message(rng)
        messages.append(message)
        expected = _reference_result_name(messages, message)
        assert index.result_name(messages, message) == expected, (seed, len(messages))
        assert _correlated_tool_result_name(messages, message) == expected
        resolved += expected is not None
    # The history is ``messages[:-1]`` even when the result is not the last entry.
    probe = AgentMessage(type="tool_result", content="", data={"tool_call_id": "a"})
    assert index.result_name(messages, probe) == _reference_result_name(messages, probe)


def test_random_streams_resolve_and_fail_closed() -> None:
    """The generated streams exercise both a resolved name and every refusal."""
    outcomes: set[str] = set()
    for seed in range(40):
        rng = random.Random(seed)
        messages: list[AgentMessage] = []
        for _ in range(120):
            message = _random_message(rng)
            messages.append(message)
            outcomes.add(str(_reference_result_name(messages, message)))
    assert {"None", "Edit", "Write", "Read", ""} <= outcomes


def _call(call_id: str, name: str) -> AgentMessage:
    return AgentMessage(
        type="assistant", content="", tool_name=name, data={"tool_call_id": call_id}
    )


def _result(call_id: str) -> AgentMessage:
    return AgentMessage(
        type="tool_result",
        content="ok",
        data={"subtype": "tool_result", "tool_call_id": call_id, "is_error": False},
    )


def test_index_rebuilds_when_list_is_replaced_truncated_or_regrown() -> None:
    index = _ToolCallNameIndex()
    first = [_call("x", "Edit"), _result("x")]
    assert index.result_name(first, first[-1]) == "Edit"

    # Another list object: nothing from the first one may leak.
    other = [_call("x", "Write"), _result("x")]
    assert index.result_name(other, other[-1]) == "Write"

    # Truncated below the watermark, then a different call for the same id.
    other.extend([_call("y", "Read"), _result("y")])
    assert index.result_name(other, other[-1]) == "Read"
    del other[1:]
    other.extend([_result("x")])
    assert index.result_name(other, other[-1]) == _reference_result_name(other, other[-1])
    del other[:]
    other.extend([_call("y", "Edit"), _result("y")])
    assert index.result_name(other, other[-1]) == "Edit"

    # Truncated and regrown past the watermark: the last indexed entry changed.
    grown = [_call("z", "Edit"), _call("q", "Read"), _result("z")]
    assert index.result_name(grown, grown[-1]) == "Edit"
    del grown[1:]
    grown.extend([_call("z", "Write"), _call("w", "Read"), _result("z")])
    expected = _reference_result_name(grown, grown[-1])
    assert expected is None  # two names for ``z``
    assert index.result_name(grown, grown[-1]) == expected

    # An empty list has no history.
    assert index.result_name([], _result("z")) is None


def _counting_tool_call_ids() -> tuple[dict[str, int], Any]:
    real = claims._runtime_message_tool_call_ids
    calls = {"count": 0}

    def counting(message: AgentMessage) -> frozenset[str]:
        calls["count"] += 1
        return real(message)

    return calls, counting


@pytest.mark.parametrize("pairs", [10, 100, 400])
def test_each_message_is_read_once(pairs: int) -> None:
    stream = []
    for i in range(pairs):
        stream.extend([_call(f"t{i}", "Edit"), _result(f"t{i}")])
    calls, counting = _counting_tool_call_ids()
    with (
        mock.patch.object(claims, "_runtime_message_tool_call_ids", counting),
        mock.patch.object(leaf_dispatcher, "_runtime_message_tool_call_ids", counting),
    ):
        index = _ToolCallNameIndex()
        messages: list[AgentMessage] = []
        for message in stream:
            messages.append(message)
            if message.type == "tool_result":
                assert index.result_name(messages, message) == "Edit"
        # One read per message folded in, one per result looked up.
        assert calls["count"] <= len(stream) + pairs

        calls["count"] = 0
        messages = []
        for message in stream:
            messages.append(message)
            if message.type == "tool_result":
                assert _reference_result_name(messages, message) == "Edit"
        # The rescan reads the whole history for every result.
        assert calls["count"] >= pairs * (pairs - 1)


def _claude_shaped_stream(rng: random.Random, length: int) -> list[AgentMessage]:
    """Claude-adapter-shaped calls and nameless results with messy ids."""
    stream: list[AgentMessage] = []
    for _ in range(length):
        call_id = rng.choice(("toolu_1", "toolu_2", "toolu_3", "toolu_4"))
        if rng.random() < 0.5:
            data: dict[str, Any] = {"tool_input": {"file_path": "notes.md"}}
            if rng.random() < 0.85:
                data["tool_call_id"] = call_id
            stream.append(
                AgentMessage(
                    type="assistant",
                    content="Calling tool",
                    tool_name=rng.choice(("Edit", "Write", "Read")),
                    data=data,
                )
            )
            continue
        meta_id = call_id if rng.random() < 0.85 else "toolu_conflict"
        stream.append(
            AgentMessage(
                type="tool_result",
                content="done",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": call_id,
                    "is_error": False,
                    "tool_result": {
                        "content": [],
                        "text_content": "done",
                        "is_error": False,
                        "meta": {"tool_call_id": meta_id},
                    },
                },
            )
        )
    return stream


def _expected_completed_names(stream: list[AgentMessage]) -> list[str]:
    """What the stream loop emitted with the full-history rescan."""
    history: list[AgentMessage] = []
    names: list[str] = []
    for message in stream:
        history.append(message)
        projected = project_runtime_message(message)
        if projected.message_type != "tool_result":
            continue
        name = projected.tool_name or _reference_result_name(history, message)
        if name is not None:
            names.append(name)
    return names


@pytest.mark.parametrize("seed", range(6))
async def test_leaf_stream_emits_the_same_completed_tool_names(seed: int) -> None:
    stream = _claude_shaped_stream(random.Random(seed), 80)
    expected = _expected_completed_names(stream)

    class StubClaudeRuntime:
        runtime_backend = "claude"
        working_directory = None
        permission_mode = "bypassPermissions"

        async def execute_task(self, **kwargs: Any):
            for message in stream:
                yield message
            yield AgentMessage(
                type="result", content="[TASK_COMPLETE]", data={"subtype": "success"}
            )

    event_store = AsyncMock()
    executor = ParallelACExecutor(
        adapter=StubClaudeRuntime(),
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
    )
    real_observe = _ToolCallNameIndex._observe
    with (
        mock.patch.object(
            _ToolCallNameIndex, "_observe", autospec=True, side_effect=real_observe
        ) as observe,
        mock.patch.object(
            leaf_dispatcher,
            "_correlated_tool_result_name",
            wraps=leaf_dispatcher._correlated_tool_result_name,
        ) as rescan,
    ):
        await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Edit the notes",
            session_id="sess_tool_names",
            tools=["Edit", "Write", "Read"],
            system_prompt="test",
            seed_goal="Name Claude tool results",
            depth=0,
            start_time=datetime.now(UTC),
        )

    appended = [call.args[0] for call in event_store.append.await_args_list]
    completed = [
        event.data["tool_name"] for event in appended if event.type == "execution.tool.completed"
    ]
    assert completed == expected
    assert expected, "the stream must resolve at least one result name"
    # Each streamed message is folded into the index at most once.
    assert observe.call_count <= len(stream)
    assert not rescan.called
