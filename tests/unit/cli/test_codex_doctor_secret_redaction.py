"""Codex doctor must not echo credentials from configured launchers."""

import sys

import pytest

from ouroboros.cli.commands.codex import (
    _check_codex_runtime_env,
    _check_live_mcp_tool_exposure,
    _CodexMCPCommandEntry,
)


@pytest.mark.parametrize("mode", ["stderr", "rpc_error"])
def test_live_mcp_failure_does_not_disclose_configured_secret(tmp_path, mode):
    secret = "synthetic-doctor-sentinel-7a7e"
    script = tmp_path / "server.py"
    script.write_text(
        "import json, os, sys\n"
        "request = json.loads(sys.stdin.buffer.readline())\n"
        f"mode = {mode!r}\n"
        "if mode == 'stderr':\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 'test', 'version': '1'}}}), flush=True)\n"
        "    sys.stdin.buffer.readline()\n"
        "    sys.stdin.buffer.readline()\n"
        "    print(os.environ['TEST_CREDENTIAL'], file=sys.stderr, flush=True)\n"
        "    import time; time.sleep(0.05)\n"
        "else:\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], "
        "'error': {'code': -32000, 'message': os.environ['TEST_CREDENTIAL']}}), flush=True)\n",
        encoding="utf-8",
    )
    failures = []
    _check_live_mcp_tool_exposure(
        _CodexMCPCommandEntry(sys.executable, (str(script),), {"TEST_CREDENTIAL": secret}),
        failures,
        5.0,
    )
    assert failures
    assert secret not in "\n".join(failures)


def test_conflicting_selector_does_not_echo_configured_value():
    failures = []
    secret = "synthetic-selector-sentinel-49ff"
    _check_codex_runtime_env({"OUROBOROS_AGENT_RUNTIME": secret}, failures, required=False)
    assert failures
    assert "OUROBOROS_AGENT_RUNTIME" in "\n".join(failures)
    assert secret not in "\n".join(failures)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("synthetic-secret"), "timed out"),
        (FileNotFoundError("synthetic-secret"), "not found"),
        (PermissionError("synthetic-secret"), "permission denied"),
        (
            RuntimeError("MCP stdio process exited before response: synthetic-secret"),
            "exited before responding",
        ),
        (RuntimeError("synthetic-secret"), "rejected"),
    ],
)
def test_live_mcp_failure_categories_omit_details(monkeypatch, error, expected):
    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("ouroboros.cli.commands.codex._list_stdio_mcp_tool_names", fail)
    failures = []
    _check_live_mcp_tool_exposure(_CodexMCPCommandEntry("test", (), {}), failures, 1.0)
    assert expected in failures[0]
    assert "synthetic-secret" not in failures[0]
