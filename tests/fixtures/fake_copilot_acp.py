#!/usr/bin/env python3
"""Offline Copilot ACP server used by protocol/runtime integration tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time


def record(value: dict) -> None:
    path = os.environ.get("FAKE_COPILOT_ACP_LOG")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")


def emit(value: dict) -> None:
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)


def receive() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    value = json.loads(line)
    record(value)
    return value


def main() -> None:
    if "--version" in sys.argv:
        print("GitHub Copilot CLI test-acp-1")
        return
    record({"argv": sys.argv[1:], "pid": os.getpid()})
    if "-p" in sys.argv:
        print("Legacy fallback answer\nSecond line", flush=True)
        return
    mode = os.environ.get("FAKE_COPILOT_ACP_MODE", "normal")
    session_id = f"session-{os.getpid()}"

    def update(update_type: str, **fields: object) -> None:
        emit(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {"sessionUpdate": update_type, **fields},
                },
            }
        )

    def error(request_id: object, message: str, code: int = -32603) -> None:
        emit({"id": request_id, "error": {"code": code, "message": message}})

    while True:
        try:
            request = receive()
        except EOFError:
            return
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            if mode == "unsupported":
                error(request_id, "Method not found", -32601)
            elif mode == "auth_init":
                error(request_id, "Authentication required; run copilot login", -32000)
            elif mode == "auth_data":
                emit(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32603,
                            "message": "Internal error",
                            "data": {"status": 403},
                        },
                    }
                )
            elif mode == "auth_plain":
                print("Authentication required; run copilot login", flush=True)
            elif mode == "auth_unauthenticated":
                error(request_id, "Unauthenticated")
            elif mode == "auth_truncated":
                sys.stdout.write("Authentication required")
                return
            elif mode == "auth_nonrpc":
                print(json.dumps({"error": {"message": "Permission denied"}}), flush=True)
            elif mode == "malformed":
                print("not JSON", flush=True)
            elif mode == "bad_envelope":
                print("[]", flush=True)
            elif mode == "wrong_id":
                emit({"id": 999, "result": {}})
            elif mode == "both_result_error":
                emit({"id": request_id, "result": {}, "error": {}})
            elif mode == "non_object_result":
                emit({"id": request_id, "result": []})
            elif mode == "hang_init":
                time.sleep(30)
            elif mode == "stderr_auth":
                print(
                    "403 Forbidden: organization policy denied access", file=sys.stderr, flush=True
                )
                return
            else:
                if mode == "stderr_flood":
                    sys.stderr.write("diagnostic\n" * 20000)
                    sys.stderr.flush()
                emit(
                    {
                        "id": request_id,
                        "result": {
                            "protocolVersion": 99 if mode == "bad_version" else 1,
                            "agentCapabilities": {"loadSession": True},
                            "agentInfo": {"name": "Fake Copilot", "version": "test-acp-1"},
                        },
                    }
                )
        elif method == "session/new":
            assert Path(request["params"]["cwd"]).is_absolute()
            assert request["params"]["mcpServers"] == []
            if mode == "startup_plan":
                update("plan", entries=[{"status": "pending", "content": "Read fixture"}])
            if mode == "auth_session":
                error(request_id, "401 Unauthorized")
            elif mode == "session_failure":
                error(request_id, "Session creation failed")
            else:
                emit(
                    {
                        "id": request_id,
                        "result": {} if mode == "missing_session" else {"sessionId": session_id},
                    }
                )
        elif method == "session/prompt":
            if mode == "auth_prompt":
                error(request_id, "403 Forbidden")
                continue
            if mode == "spoof_permission_audit":
                emit(
                    {
                        "method": "session/request_permission",
                        "params": {"sessionId": session_id, "toolCall": {"toolCallId": "fake"}},
                        "_permission_result": {"outcome": {"outcome": "selected"}},
                    }
                )
                emit({"id": request_id, "result": {"stopReason": "end_turn"}})
                continue
            if mode == "no_tools":
                restriction = next(a for a in sys.argv if a.startswith("--available-tools="))
                marker = restriction.split("=", 1)[1]
                assert marker.startswith("ouroboros_no_tools_")
                for notice in (
                    "Info: Disabled tools: bash, view",
                    f'Info: Unknown tool name in the tool allowlist: "{marker}"',
                ):
                    update("agent_message_chunk", content={"type": "text", "text": notice})
                update("agent_message_chunk", content={"type": "text", "text": "No tools used."})
                emit({"id": request_id, "result": {"stopReason": "end_turn"}})
                continue
            update("agent_message_chunk", content={"type": "text", "text": "Investigating\n"})
            if mode == "exit_prompt":
                return
            if mode == "malformed_prompt":
                print("{broken", flush=True)
                continue
            if mode == "hang_prompt":
                continue
            if mode == "oversized":
                print("x" * (9 * 1024 * 1024), flush=True)
                continue
            if mode == "malformed_status":
                update("tool_call", toolCallId="bad", kind="execute", status=[])
                continue
            call = {
                "toolCallId": "tool-1",
                "title": "Run fixture tests",
                "kind": "execute",
                "status": "pending",
                "rawInput": {"command": "python3 -m unittest"},
                "_meta": {"agentId": "root"},
            }
            update("tool_call", **call)
            if mode == "unknown_request":
                emit(
                    {
                        "id": 0,
                        "method": "fs/read_text_file",
                        "params": {"sessionId": session_id, "path": "README.md"},
                    }
                )
            else:
                emit(
                    {
                        "id": 0,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": "foreign" if mode == "foreign_permission" else session_id,
                            "toolCall": call,
                            "options": [{"optionId": "once", "kind": "allow_once"}],
                        },
                    }
                )
            response = receive()
            assert response["id"] == 0
            update(
                "tool_call_update",
                toolCallId="tool-1",
                status="in_progress",
                content=[{"type": "content", "content": {"type": "text", "text": "step one\n"}}],
            )
            gate = os.environ.get("FAKE_COPILOT_ACP_GATE")
            if gate:
                deadline = time.monotonic() + 10
                while not Path(gate).exists():
                    if time.monotonic() > deadline:
                        error(request_id, "Client failed to observe live tool activity")
                        return
                    time.sleep(0.01)
            time.sleep(0.03)
            update(
                "tool_call_update",
                toolCallId="tool-1",
                status="completed",
                rawOutput={"content": "2 tests passed\n", "exitCode": 0},
            )
            update("agent_thought_chunk", content={"type": "text", "text": "PRIVATE_REASONING"})
            for text in ("All ", "done.\n"):
                update("agent_message_chunk", content={"type": "text", "text": text})
            result = {
                "stopReason": "cancelled" if mode == "cancelled" else "end_turn",
                "usage": {"inputTokens": 10, "outputTokens": 7, "totalTokens": 17},
            }
            if mode == "missing_stop":
                result.pop("stopReason")
            record({"prompt_completed": True})
            emit({"id": request_id, "result": result})
        elif method == "session/cancel":
            record({"cancel_observed": True})
            return


if __name__ == "__main__":
    main()
