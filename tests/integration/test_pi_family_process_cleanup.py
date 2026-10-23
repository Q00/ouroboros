from __future__ import annotations

from contextlib import aclosing, suppress
import os
from pathlib import Path
import signal
import sys

import anyio
import pytest

from ouroboros.orchestrator.omp_runtime import OmpRuntime
from ouroboros.orchestrator.pi_runtime import PiRuntime
from ouroboros.orchestrator.runtime_factory import create_agent_runtime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.name != "posix", reason="POSIX signals"),
]


@pytest.mark.parametrize("backend", ["pi", "omp"])
@pytest.mark.parametrize("ending", ["cancel", "timeout", "close"])
async def test_factory_dispatch_reaps_sigterm_ignoring_child(
    backend: str, ending: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a real CLI fixture that ignores SIGTERM after emitting its PID.
    cli = tmp_path / "fixture-cli"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        "if '--help' in sys.argv:\n"
        "    print('--append-system-prompt --tools --no-tools')\n"
        "    sys.exit(0)\n"
        "if sys.argv[-1] == 'retry':\n"
        "    print(json.dumps({'type': 'agent_end'}), flush=True)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print(json.dumps({'type': 'message_update', 'delta': str(os.getpid())}), flush=True)\n"
        "signal.pause()\n"
    )
    cli.chmod(0o700)
    monkeypatch.setattr(PiRuntime, "_process_shutdown_timeout_seconds", 0.05)
    monkeypatch.setattr(OmpRuntime, "_process_shutdown_timeout_seconds", 0.05)
    runtime = create_agent_runtime(
        backend=backend, cli_path=cli, cwd=tmp_path, stdout_idle_timeout_seconds=0.05
    )
    pid: int | None = None
    try:
        # When the dispatch is cancelled, times out, or its consumer closes it.
        with anyio.fail_after(5):
            with anyio.CancelScope() as scope:
                async with aclosing(runtime.execute_task("work")) as messages:
                    ready = await anext(messages)
                    pid = int(ready.content)
                    match ending:
                        case "cancel":
                            scope.cancel()
                            await anext(messages)
                        case "close":
                            pass
                        case "timeout":
                            result = await anext(messages)
                            assert result.is_error
                            assert result.data["error_type"] == "TimeoutError"
                        case _:
                            pytest.fail(f"Unknown test ending: {ending}")
            assert scope.cancelled_caught == (ending == "cancel")

            # Then its PID and stderr reader are gone before the same instance retries.
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
            assert not any(
                "_collect_stream_lines" in repr(task.coro) for task in anyio.get_running_tasks()
            )
            result = [message async for message in runtime.execute_task("retry")]
            assert result[-1].is_final and not result[-1].is_error
    finally:
        if pid is not None:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
