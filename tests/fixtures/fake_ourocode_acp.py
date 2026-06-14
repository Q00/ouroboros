#!/usr/bin/env python3
"""A CI-safe fake of ``ourocode --acp`` for OurocodeAcpClient tests.

Speaks ourocode's newline-delimited JSON-RPC 2.0 ACP protocol on stdio so the
real client lifecycle (spawn → initialize → session/new → session/prompt →
streamed agent_message_chunk notifications → result) is exercised without
ourocode, Claude, or any network. Behaviour is switched via ``FAKE_ACP_MODE``:

- ``ok`` (default): streams two text chunks then ``stopReason: end_turn``.
- ``not_signed_in``: errors ``session/prompt`` like ourocode's unsigned backend.
- ``malformed``: emits a non-JSON line during the prompt turn.
- ``no_session_id``: ``session/new`` returns a result without ``sessionId``.
- ``hang``: never answers ``session/prompt`` (to exercise the turn timeout).
- ``die_mid_turn``: streams one chunk then exits (stdout EOF mid-turn).
"""

from __future__ import annotations

import json
import os
import sys
import time


def _send(obj: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(req_id: object, result: dict[str, object]) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: object, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _chunk(session_id: str, text: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }
    )


def main() -> int:
    mode = os.environ.get("FAKE_ACP_MODE", "ok")
    session_id = "sess_fake01"
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = frame.get("method")
        req_id = frame.get("id")

        if method == "initialize":
            _result(
                req_id,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {"loadSession": False},
                    "agentInfo": {"name": "fake-ourocode", "version": "0.0.0"},
                    "authMethods": [],
                },
            )
        elif method == "session/new":
            if "cwd" not in frame.get("params", {}):
                _error(req_id, -32602, "session/new requires an absolute cwd")
            elif mode == "no_session_id":
                _result(req_id, {})
            else:
                _result(req_id, {"sessionId": session_id})
        elif method == "session/prompt":
            if mode == "not_signed_in":
                _error(
                    req_id,
                    -32603,
                    "model backend unavailable; run ourocode interactively to sign in",
                )
            elif mode == "malformed":
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
            elif mode == "hang":
                # Never answer the prompt; the client's turn timeout must fire.
                time.sleep(60)
            elif mode == "die_mid_turn":
                _chunk(session_id, "partial...")
                return 0  # exit → stdout EOF before the prompt result
            else:
                _chunk(session_id, "Hello, ")
                _chunk(session_id, "world!")
                _result(req_id, {"stopReason": "end_turn"})
        elif method == "session/cancel":
            # one-way notification; nothing to answer
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
