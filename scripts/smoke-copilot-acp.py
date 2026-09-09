#!/usr/bin/env python3
"""Run a credentialed ACP smoke test and persist normalized activity for replay.

    .venv/bin/python scripts/smoke-copilot-acp.py --workspace /path/to/repo

The default envelope is read-only. Mutating tests require an explicit --tools
selection (for example Read,Edit,Bash) and an explicitly scoped --prompt.
No unrestricted permission mode or network ACP listener is used.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import time
from uuid import uuid4

from ouroboros.orchestrator.copilot_acp_runtime import CopilotAcpRuntime
from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
from ouroboros.orchestrator.runtime_message_projection import (
    project_runtime_message,
    should_emit_runtime_progress,
)
from ouroboros.persistence.event_store import EventStore, sqlite_database_url


async def run(args: argparse.Namespace) -> None:
    directory = Path(args.output_dir).resolve()
    try:
        directory.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        raise SystemExit("Choose a fresh --output-dir to retain previous evidence") from None
    log_path = directory / "messages.ndjson"
    adapter = await asyncio.to_thread(
        CopilotAcpRuntime,
        cli_path=args.cli,
        cwd=Path(args.workspace),
        model=args.model,
        permission_mode="acceptEdits",
        fallback_to_cli=False,
    )
    started = time.monotonic()
    first_tool = None
    last_result = None
    counts: Counter[str] = Counter()
    message_count = 0
    store = EventStore(sqlite_database_url(directory / "events.db"))
    try:
        await store.initialize()
        session_id = f"acp-smoke-{uuid4().hex}"

        async def append(event) -> bool:
            await store.append(event)
            return True

        emitter = ExecutionEventEmitter(store, safe_emit_event=append)
        with log_path.open("x", encoding="utf-8") as log:
            log_path.chmod(0o600)
            async for message in adapter.execute_task(args.prompt, tools=args.tools.split(",")):
                elapsed = round(time.monotonic() - started, 3)
                message_count += 1
                projected = project_runtime_message(message)
                event_type = message.data.get("runtime_event_type", message.type)
                counts[event_type] += 1
                log.write(
                    json.dumps(
                        {
                            "elapsed": elapsed,
                            "type": message.type,
                            "content": message.content,
                            "tool": message.tool_name,
                            "data": message.data,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                log.flush()
                if should_emit_runtime_progress(message, message_count, projected=projected):
                    await store.append(
                        emitter.build_session_progress_event(
                            session_id, message, projected=projected
                        )
                    )
                if projected.is_tool_call and first_tool is None:
                    first_tool = elapsed
                if message.is_final:
                    last_result = message
                preview = message.content.replace("\n", " ")[:220]
                print(f"{elapsed:8.3f}s  {event_type:26} {preview}", flush=True)
        replay = await store.replay("session", session_id)
        summary = {
            "session_id": session_id,
            "elapsed": round(time.monotonic() - started, 3),
            "first_tool_seconds": first_tool,
            "events": dict(counts),
            "persisted_events": len(replay),
            "success": last_result is not None and not last_result.is_error,
        }
        (directory / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(summary, indent=2))
        if not summary["success"] or first_tool is None:
            raise SystemExit("Smoke test did not demonstrate successful tool streaming")
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cli")
    parser.add_argument("--model")
    parser.add_argument("--output-dir", default=".acp-artifacts/runtime-smoke")
    parser.add_argument("--tools", default="Read,Glob,Grep")
    parser.add_argument(
        "--prompt",
        default=(
            "Read-only diagnostic: list source files, grep for TODO, read a matching source file, "
            "then read README.md separately and summarize. Describe progress between tool calls. "
            "Do not edit files, use the network, inspect credentials, or read outside this workspace."
        ),
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
