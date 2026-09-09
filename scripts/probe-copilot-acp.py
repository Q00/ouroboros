#!/usr/bin/env python3
"""Inspect Copilot's real ACP wire stream without importing Ouroboros.

Run from the checkout:
    python3 scripts/probe-copilot-acp.py --workspace . --log .acp-artifacts/probe.ndjson

Only local read/search tools are exposed. Permission requests fail closed unless
they are read/search operations within the selected workspace. Raw logs contain
prompt and tool output: keep them private and do not commit them.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
import contextlib
import json
import os
from pathlib import Path
import time
from typing import Any, TextIO


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.workspace = Path(args.workspace).resolve()
        self.started = time.monotonic()
        self.session_id: str | None = None
        self.events: Counter[str] = Counter()
        self.process: asyncio.subprocess.Process | None = None
        self.stderr: deque[str] = deque(maxlen=30)
        self.log: TextIO | None = None

    def record(self, direction: str, message: Any) -> None:
        elapsed = round(time.monotonic() - self.started, 3)
        entry = {"elapsed": elapsed, "direction": direction, "message": message}
        line = json.dumps(entry, ensure_ascii=False)
        print(line, flush=True)
        if self.log is not None:
            self.log.write(line + "\n")
            self.log.flush()

    async def send(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.record("send", message)
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    def permission_outcome(self, params: dict[str, Any]) -> dict[str, Any]:
        outcome: dict[str, Any] = {"outcome": "cancelled"}
        tool = params.get("toolCall", {})
        if tool.get("kind") not in {"read", "search"}:
            return {"outcome": outcome}
        for location in tool.get("locations", []):
            path = Path(location.get("path", "")).resolve()
            if not path.is_relative_to(self.workspace):
                return {"outcome": outcome}
        for option in params.get("options", []):
            if option.get("kind") == "allow_once":
                outcome = {"outcome": "selected", "optionId": option["optionId"]}
                break
        return {"outcome": outcome}

    async def request(
        self, request_id: int, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        assert self.process is not None and self.process.stdout is not None
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        async with asyncio.timeout(timeout):
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    await asyncio.sleep(0)
                    raise RuntimeError(f"ACP closed stdout: {' '.join(self.stderr)}")
                message = json.loads(line)
                self.record("receive", message)
                if "method" in message:
                    notification = message["method"]
                    notification_params = message.get("params", {})
                    update = notification_params.get("update", {})
                    self.events[update.get("sessionUpdate", notification)] += 1
                    if "id" in message:
                        response: dict[str, Any] = {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                        }
                        if notification == "session/request_permission":
                            response["result"] = self.permission_outcome(notification_params)
                        else:
                            response["error"] = {
                                "code": -32601,
                                "message": "Client method not supported by read-only probe",
                            }
                        await self.send(response)
                    continue
                if message.get("id") != request_id:
                    raise RuntimeError(f"Unexpected ACP response id: {message.get('id')!r}")
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]

    async def drain_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        async for line in self.process.stderr:
            text = line.decode(errors="replace").rstrip()
            self.stderr.append(text)
            self.record("stderr", text)

    async def run(self) -> None:
        log_path = Path(self.args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = log_path.open("x", encoding="utf-8")
        log_path.chmod(0o600)
        env = os.environ.copy()
        for key in ("COPILOT_ALLOW_ALL", "COPILOT_SESSION_ID", "COPILOT_RESUME"):
            env.pop(key, None)
        if self.args.ignore_github_token:
            env.pop("GITHUB_TOKEN", None)
        env["TMPDIR"] = str(log_path.parent.resolve())
        command = [
            self.args.cli,
            "--acp",
            "--stdio",
            "--no-color",
            "--log-level",
            "none",
            "--log-dir",
            str(log_path.parent.resolve()),
            "--no-auto-update",
            "--disable-builtin-mcps",
            "--no-remote-export",
            f"--available-tools={self.args.tools}",
            "--add-dir",
            str(self.workspace),
        ]
        if self.args.model:
            command.extend(["--model", self.args.model])
        self.record("command", command)
        stderr_task: asyncio.Task[None] | None = None
        try:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.workspace,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024,
            )
            stderr_task = asyncio.create_task(self.drain_stderr())
            await self.request(
                1,
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "ouroboros-acp-probe", "version": "1"},
                },
                30,
            )
            session = await self.request(
                2, "session/new", {"cwd": str(self.workspace), "mcpServers": []}, 60
            )
            self.session_id = session["sessionId"]
            result = await self.request(
                3,
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": self.args.prompt}],
                },
                self.args.timeout,
            )
            self.record("summary", {"result": result, "events": dict(self.events)})
        finally:
            if self.process is not None:
                if self.process.stdin is not None:
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        self.process.stdin.close()
                        await self.process.stdin.wait_closed()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), 5)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            self.process.kill()
                        await self.process.wait()
                self.record("exit", {"returncode": self.process.returncode})
            if stderr_task is not None:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            self.log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", default="copilot")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--log", default=".acp-artifacts/probe.ndjson")
    parser.add_argument("--model")
    parser.add_argument("--tools", default="glob,grep,view")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument(
        "--ignore-github-token",
        action="store_true",
        help="Explicitly omit a known-invalid GITHUB_TOKEN from this child only",
    )
    parser.add_argument(
        "--prompt",
        default=(
            "Read-only diagnostic: first list files with glob, then search for TODO with grep, "
            "then read a matching file with view, then read README.md separately. "
            "Do these as sequential tool calls, briefly describe progress between steps, "
            "and summarize what you found. Do not edit files, execute shell commands, "
            "inspect credentials, access the network, or read outside the working directory."
        ),
    )
    asyncio.run(Probe(parser.parse_args()).run())


if __name__ == "__main__":
    main()
