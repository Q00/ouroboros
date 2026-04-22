"""Tests for MCP shell environment loading."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys

from ouroboros.cli.commands import mcp


def test_shell_env_loader_preserves_mcp_stdin(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    initialize_message = '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    fake_stdin = io.StringIO(initialize_message)
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        if kwargs.get("stdin") != subprocess.DEVNULL:
            sys.stdin.read()
        stdout = json.dumps({"PATH": os.environ["PATH"], "OUROBOROS_TEST_ENV": "loaded"})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(mcp.subprocess, "run", fake_run)

    mcp._ensure_shell_env(timeout=1.25)

    assert fake_stdin.read() == initialize_message
    assert calls[0]["stdin"] == subprocess.DEVNULL
    assert os.environ["OUROBOROS_TEST_ENV"] == "loaded"
