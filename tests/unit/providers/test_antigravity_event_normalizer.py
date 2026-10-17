"""Antigravity nested envelopes, based on task-owned CLI captures and fixtures."""

import json

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.antigravity_cli_runtime import AntigravityCLIRuntime
from ouroboros.providers.antigravity_event_normalizer import (
    AntigravityEventNormalizer,
    normalize_antigravity_tool,
)
from ouroboros.providers.gemini_event_normalizer import GeminiEventNormalizer


def event(kind, payload):
    return AntigravityEventNormalizer().normalize_line(json.dumps({"event": kind, kind: payload}))


@pytest.mark.parametrize(
    "name,inputs,canonical,key,value",
    [
        ("view_file", {"AbsolutePath": "/work/a.py"}, "Read", "file_path", "/work/a.py"),
        ("write_to_file", {"TargetFile": "/work/b.py"}, "Write", "file_path", "/work/b.py"),
        ("replace_file_content", {"TargetFile": "/work/b.py"}, "Edit", "file_path", "/work/b.py"),
        (
            "multi_replace_file_content",
            {"TargetFile": "/work/b.py"},
            "Edit",
            "file_path",
            "/work/b.py",
        ),
        ("grep_search", {"SearchPath": "/work", "Query": "foo"}, "Grep", "pattern", "foo"),
        ("find_by_name", {"SearchDirectory": "/work", "Pattern": "*.py"}, "Glob", "path", "/work"),
        ("run_command", {"CommandLine": "pytest", "Cwd": "/work"}, "Bash", "command", "pytest"),
    ],
)
def test_native_tool_hints(name, inputs, canonical, key, value):
    tool, actual = normalize_antigravity_tool(name, inputs)
    assert tool == canonical
    assert actual[key] == value
    assert all(actual[k] == v for k, v in inputs.items())


def test_unknown_and_malformed_tools_do_not_invent_hints():
    assert normalize_antigravity_tool("custom", {"AbsolutePath": "/a"}) == (
        "custom",
        {"AbsolutePath": "/a"},
    )
    assert normalize_antigravity_tool("run_command", "pytest") == ("Bash", {})
    assert normalize_antigravity_tool(None, ["/a"]) == ("", {})
    assert "file_path" not in normalize_antigravity_tool("view_file", {"AbsolutePath": []})[1]


def test_delta_and_terminal_response_keep_whitespace_and_session():
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy")
    content = ""
    for delta in ("Hello", " world", "\n"):
        parsed = event("step_update", {"step_type": "agent_response", "text_delta": delta})
        assert parsed["content"] == delta
        messages = runtime._convert_event(parsed, None)
        content = runtime._update_last_content(content, messages[0])
    assert content == "Hello world\n"
    terminal = event(
        "result",
        {"conversation_id": "conversation-1", "status": "SUCCESS", "response": "Hello world\n"},
    )
    assert runtime._extract_event_session_id(terminal) == "conversation-1"
    content = runtime._update_last_content(content, runtime._convert_event(terminal, None)[0])
    assert content == "Hello world\n"


@pytest.mark.parametrize("step_type", ["tool", "tool_call"])
def test_tool_envelopes_keep_name_inputs_and_failures(step_type):
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy")
    payload = {
        "step_type": step_type,
        "state": "ACTIVE",
        "tool_name": "run_command",
        "tool_info": {"parameters": {"CommandLine": "pytest"}},
    }
    parsed = event("step_update", payload)
    message = runtime._convert_event(parsed, None)[0]
    assert message.tool_name == "Bash"
    assert message.data["tool_input"]["command"] == "pytest"
    assert runtime._update_last_content("answer", message) == "answer"
    payload.update(state="ERROR", tool_info={"error": {"message": "failed"}})
    result = runtime._convert_event(event("step_update", payload), None)[0]
    assert result.type == "tool_result"
    assert result.data["is_error"] is True


def test_tool_call_fixture_spelling():
    parsed = event(
        "step_update",
        {
            "step_type": "tool_call",
            "tool_name": "run_command",
            "tool_input": {"CommandLine": "pytest"},
        },
    )
    assert parsed["type"] == "tool_use"
    assert parsed["metadata"]["input"]["command"] == "pytest"


def test_successful_tool_completion_preserves_native_output():
    parsed = event(
        "step_update",
        {
            "step_type": "tool",
            "state": "DONE",
            "tool_name": "view_file",
            "tool_info": {
                "parameters": {"AbsolutePath": "/work/probe.txt"},
                "output": "2 lines, 21 bytes",
            },
        },
    )

    assert parsed["type"] == "tool_result"
    assert parsed["content"] == "2 lines, 21 bytes"
    assert parsed["is_error"] is False
    assert parsed["metadata"]["name"] == "Read"
    assert parsed["metadata"]["input"]["file_path"] == "/work/probe.txt"


def test_failed_terminal_does_not_become_success():
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy")
    message = runtime._convert_event(event("result", {"status": "ERROR", "error": "quota"}), None)[
        0
    ]
    assert message.is_final and message.is_error
    assert "quota" in message.content


@pytest.mark.parametrize(
    "line", ['{"event":"step_update","step_update":[]}', '{"event":"unknown"}', "[1,2]", "{broken"]
)
def test_malformed_or_unknown_never_fabricates_tool_use(line):
    parsed = AntigravityEventNormalizer().normalize_line(line)
    assert parsed["type"] not in {"tool_use", "tool_result"}


def test_tool_and_thinking_content_do_not_pollute_final_answer():
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy")
    for m in [
        AgentMessage(type="tool_result", content="secret", tool_name="Read"),
        AgentMessage(type="assistant", content="thinking", data={"thinking": "thinking"}),
    ]:
        assert runtime._update_last_content("answer", m) == "answer"


@pytest.mark.parametrize("invalid", [[], {}, ["tool"], {"type": "tool"}, 0, False])
@pytest.mark.parametrize("field", ["event", "step_type", "state"])
def test_malformed_discriminators_remain_non_actionable(field, invalid):
    raw = {
        "event": "step_update",
        "step_update": {
            "step_type": "tool",
            "state": "ACTIVE",
            "tool_name": "run_command",
            "tool_input": {"CommandLine": "pytest"},
        },
    }
    if field == "event":
        raw[field] = invalid
    else:
        raw["step_update"][field] = invalid

    parsed = AntigravityEventNormalizer().normalize_line(json.dumps(raw))

    assert parsed["type"] == "unknown"
    assert parsed["content"] == ""
    assert parsed["raw"] == raw
    assert parsed["is_error"] is False


@pytest.mark.parametrize("kind", ["unrecognized", "", None, [], {}, ["result"], {"type": "tool"}])
@pytest.mark.parametrize("top_type", ["tool_use", "tool_result", "result", "message"])
def test_unknown_native_envelopes_do_not_reinterpret_top_level_fields(kind, top_type):
    raw = {
        "event": kind,
        "type": top_type,
        "name": "run_command",
        "input": {"CommandLine": "echo test"},
        "response": "fabricated success",
        "status": "SUCCESS",
        "result": {"status": "SUCCESS", "response": "fabricated success"},
    }
    parsed = AntigravityEventNormalizer().normalize_line(json.dumps(raw))

    assert parsed == {
        "type": "unknown",
        "content": "",
        "raw": raw,
        "metadata": {},
        "is_error": False,
    }
    assert AntigravityCLIRuntime(cli_path="/usr/bin/agy")._convert_event(parsed, None) == []


@pytest.mark.parametrize("kind", ["init", "step_update", "result"])
@pytest.mark.parametrize("payload", [None, [], "invalid"])
def test_malformed_native_payload_ignores_conflicting_top_level_type(kind, payload):
    raw = {"event": kind, kind: payload, "type": "result", "response": "fabricated success"}
    parsed = AntigravityEventNormalizer().normalize_line(json.dumps(raw))

    assert parsed["type"] == "unknown"
    assert parsed["raw"] == raw
    assert parsed["metadata"] == {}
    assert parsed["content"] == ""
    assert AntigravityCLIRuntime(cli_path="/usr/bin/agy")._convert_event(parsed, None) == []


@pytest.mark.parametrize("top_type", ["tool_use", "tool_result", "result", "message", "init"])
def test_legacy_gemini_without_event_key_preserves_normalization_and_conversion(top_type):
    raw = {
        "type": top_type,
        "name": "run_command",
        "input": {"CommandLine": "echo test"},
        "response": "legacy answer",
    }
    line = json.dumps(raw)
    expected = GeminiEventNormalizer().normalize_line(line)
    parsed = AntigravityEventNormalizer().normalize_line(line)

    assert parsed == expected
    runtime = AntigravityCLIRuntime(cli_path="/usr/bin/agy")
    messages = runtime._convert_event(parsed, None)
    if top_type in {"tool_use", "tool_result"}:
        assert messages[0].tool_name == "run_command"
    elif top_type == "result":
        assert messages[0].data["terminal"] is True
        assert messages[0].content == "legacy answer"
    elif top_type == "message":
        assert messages[0].content == "legacy answer"
    else:
        assert messages == []


@pytest.mark.parametrize("name", [None, "", " ", [], {}, 0, False])
@pytest.mark.parametrize("state", [None, "ACTIVE", "DONE", "ERROR"])
def test_malformed_native_tool_identity_never_becomes_actionable(name, state):
    payload = {
        "step_type": "tool",
        "state": state,
        "tool_name": name,
        "tool_info": {"output": "finished"},
    }
    parsed = event("step_update", payload)

    assert parsed["type"] == "unknown"
    assert parsed["metadata"] == {}
    assert parsed["content"] == ""
    assert parsed["raw"]["step_update"] == payload
    assert AntigravityCLIRuntime(cli_path="/usr/bin/agy")._convert_event(parsed, None) == []


@pytest.mark.parametrize("status", [{"state": "ERROR"}, {}, [], ["ERROR"], None, 0, False])
def test_malformed_terminal_status_never_becomes_success(status):
    payload = {"status": status, "response": "finished"}
    parsed = event("result", payload)

    assert parsed["type"] == "unknown"
    assert parsed["metadata"] == {}
    assert parsed["content"] == ""
    assert parsed["raw"]["result"] == payload
    assert AntigravityCLIRuntime(cli_path="/usr/bin/agy")._convert_event(parsed, None) == []


@pytest.mark.parametrize("status", ["SUCCESS", "COMPLETED", "ERROR"])
def test_valid_terminal_status_retains_success_or_error(status):
    parsed = event("result", {"status": status, "response": "finished"})
    message = AntigravityCLIRuntime(cli_path="/usr/bin/agy")._convert_event(parsed, None)[0]

    assert parsed["type"] == "result"
    assert parsed["is_error"] is (status == "ERROR")
    assert message.data["terminal"] is True
    assert message.is_error is (status == "ERROR")
    assert message.content == "finished"


@pytest.mark.parametrize("payload", [{}, {"response": "finished"}])
def test_missing_native_terminal_status_never_implies_success(payload):
    parsed = event("result", payload)

    assert parsed["type"] == "unknown"
    assert parsed["raw"]["result"] == payload
    assert AntigravityCLIRuntime(cli_path="/usr/bin/agy")._convert_event(parsed, None) == []
