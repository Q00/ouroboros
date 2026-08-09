"""Regression: a detached job worker must not drop its terminal telemetry event.

JobManager queues exactly one `workflow_outcome` event when a durable job
reaches a terminal state (via JobTelemetryBoundary, src/ouroboros/mcp/
telemetry_boundary.py). That is the only event PostHog's published counting
rule reads (TELEMETRY.md). Because the telemetry worker is a daemon thread
that never flushes automatically, a detached worker process that returns
from run_worker() and exits without an explicit flush would silently drop
that event -- undercounting every detached run. See
src/ouroboros/mcp/detached_worker.py's `_flush_telemetry_before_exit()` and
`main()`.

Two independent things are proven here:

1. TestFlushWiring -- main() actually calls _flush_telemetry_before_exit()
   on every run_worker exit path (success, and exception), in-process with
   run_worker mocked out (fast, no subprocess).
2. TestFlushDelivery -- _flush_telemetry_before_exit() actually delivers a
   queued workflow_outcome event before the process exits, proven end to end
   in a real subprocess talking to a local HTTP sink standing in for
   PostHog.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from typing import Any

import pytest

from ouroboros.mcp import detached_worker


class TestFlushWiring:
    """main() must call _flush_telemetry_before_exit() on every exit path."""

    def test_flush_called_after_successful_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_run_worker(_request_path: Path) -> int:
            return 0

        flush_calls: list[bool] = []
        monkeypatch.setattr(detached_worker, "run_worker", fake_run_worker)
        monkeypatch.setattr(
            detached_worker, "_flush_telemetry_before_exit", lambda: flush_calls.append(True)
        )

        exit_code = detached_worker.main(["dummy-request.json"])

        assert exit_code == 0
        assert flush_calls == [True]

    def test_flush_called_even_when_run_worker_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_run_worker(_request_path: Path) -> int:
            raise RuntimeError("boom")

        flush_calls: list[bool] = []
        monkeypatch.setattr(detached_worker, "run_worker", fake_run_worker)
        monkeypatch.setattr(
            detached_worker, "_flush_telemetry_before_exit", lambda: flush_calls.append(True)
        )

        with pytest.raises(RuntimeError, match="boom"):
            detached_worker.main(["dummy-request.json"])

        assert flush_calls == [True]

    def test_no_flush_on_argv_validation_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bad argv returns before any work is queued -- nothing to flush."""
        flush_calls: list[bool] = []
        monkeypatch.setattr(
            detached_worker, "_flush_telemetry_before_exit", lambda: flush_calls.append(True)
        )

        exit_code = detached_worker.main([])

        assert exit_code == 2
        assert flush_calls == []


def _make_recording_handler(sink: queue.Queue[bytes]) -> type[http.server.BaseHTTPRequestHandler]:
    """A minimal PostHog-batch-endpoint stand-in: records POST bodies, replies 200."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler method name
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            sink.put(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # keep test output quiet

    return Handler


class TestFlushDelivery:
    """_flush_telemetry_before_exit() must deliver before the process exits."""

    def test_queued_workflow_outcome_survives_to_a_real_sink(self, tmp_path: Path) -> None:
        sink: queue.Queue[bytes] = queue.Queue()
        server = http.server.HTTPServer(("127.0.0.1", 0), _make_recording_handler(sink))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            home = tmp_path / "home"
            home.mkdir()
            repo_root = Path(__file__).resolve().parents[3]

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHONPATH"] = str(repo_root / "src")
            env["OUROBOROS_POSTHOG_API_KEY"] = "phc_test"
            env["OUROBOROS_POSTHOG_HOST"] = f"http://127.0.0.1:{port}"
            for key in ("DO_NOT_TRACK", "OUROBOROS_TELEMETRY"):
                env.pop(key, None)

            script = (
                "from ouroboros import telemetry as usage_telemetry\n"
                "from ouroboros.mcp import detached_worker\n"
                "usage_telemetry.capture_job_outcome(\n"
                "    'job-x', 'evaluate', terminal_status='completed',\n"
                "    result_meta={'final_approved': True},\n"
                ")\n"
                # This is the exact call main()'s finally makes -- proving it
                # delivers is what proves the fix, independent of whether
                # main() is wired correctly (that's TestFlushWiring's job).
                "detached_worker._flush_telemetry_before_exit()\n"
            )

            subprocess.run(
                [sys.executable, "-c", script],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )

            # By the time subprocess.run() returns, the child's flush() has
            # already waited for _post()'s urlopen() to complete a full
            # request/response round trip -- so the sink already has the
            # body; no polling wait needed.
            body = sink.get(timeout=1.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        batch = json.loads(body)["batch"]
        outcomes = [e for e in batch if e["event"] == "workflow_outcome"]
        assert len(outcomes) == 1, f"expected exactly one workflow_outcome, got {batch}"
        props: dict[str, Any] = outcomes[0]["properties"]
        assert props["verified"] is True
        assert props["terminal_status"] == "completed"
        assert props["command"] == "evaluate"
