"""Unit tests for anonymous usage telemetry (src/ouroboros/telemetry.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from ouroboros import telemetry
from ouroboros.config.loader import get_telemetry_enabled


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("OUROBOROS_TELEMETRY", raising=False)
    monkeypatch.delenv("OUROBOROS_POSTHOG_API_KEY", raising=False)
    # Neutralize the shipped project key so no test can post to real PostHog;
    # enabled-path tests inject a fake key via OUROBOROS_POSTHOG_API_KEY.
    monkeypatch.setattr(telemetry, "_EMBEDDED_API_KEY", "")
    telemetry._reset_for_tests()
    yield
    telemetry.flush(timeout=2.0)
    telemetry._reset_for_tests()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Enable telemetry with a fake key and collect posted events."""
    events: list[dict[str, Any]] = []

    def fake_post(batch: list[dict[str, Any]]) -> None:
        events.extend(batch)

    monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
    monkeypatch.setattr(telemetry, "_post", fake_post)
    return events


class TestOptOut:
    def test_disabled_without_api_key(self) -> None:
        assert telemetry.is_enabled() is False

    def test_do_not_track_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        assert telemetry.is_enabled() is False

    def test_env_flag_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
        assert telemetry.is_enabled() is False

    def test_enabled_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        assert telemetry.is_enabled() is True

    @pytest.mark.parametrize(
        "config",
        (
            "telemetry: [\n",
            "telemetry:\n  enabled: false\nlogging:\n  level: invalid\n",
        ),
        ids=("malformed-yaml", "unrelated-validation-error"),
    )
    def test_existing_invalid_config_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        config: str,
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        config_path = tmp_path / ".ouroboros" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(config, encoding="utf-8")

        assert telemetry.is_enabled() is False

    def test_explicit_enable_cannot_override_persisted_opt_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "1")
        config_path = tmp_path / ".ouroboros" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("telemetry:\n  enabled: false\n", encoding="utf-8")

        assert get_telemetry_enabled() is False
        assert telemetry.is_enabled() is False

    def test_explicit_enable_cannot_override_malformed_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "1")
        config_path = tmp_path / ".ouroboros" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("telemetry: [\n", encoding="utf-8")

        assert get_telemetry_enabled() is False
        assert telemetry.is_enabled() is False

    def test_explicit_enable_with_absent_config_keeps_default_on(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "1")

        assert get_telemetry_enabled() is True
        assert telemetry.is_enabled() is True

    def test_dangling_config_symlink_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        config_path = tmp_path / ".ouroboros" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.symlink_to(config_path.parent / "missing-config.yaml")

        assert config_path.is_symlink()
        assert not config_path.exists()
        assert get_telemetry_enabled() is False
        assert telemetry.is_enabled() is False

    def test_project_env_cannot_override_privacy_or_destination(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text(
            "OUROBOROS_TELEMETRY=1\n"
            "OUROBOROS_POSTHOG_HOST=https://attacker.invalid\n"
            "OUROBOROS_POSTHOG_API_KEY=phc_attacker\n",
            encoding="utf-8",
        )
        config = tmp_path / "home" / ".ouroboros" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("telemetry:\n  enabled: false\n", encoding="utf-8")
        env = os.environ.copy()
        for key in (
            "DO_NOT_TRACK",
            "OUROBOROS_TELEMETRY",
            "OUROBOROS_POSTHOG_HOST",
            "OUROBOROS_POSTHOG_API_KEY",
        ):
            env.pop(key, None)
        env["HOME"] = str(tmp_path / "home")
        repo_root = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = str(repo_root / "src")

        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json; from ouroboros import telemetry; "
                    "print(json.dumps({'enabled': telemetry.is_enabled(), "
                    "'host': telemetry._host(), 'key': telemetry._api_key()}))"
                ),
            ],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        result = json.loads(probe.stdout)
        assert result["enabled"] is False
        assert result["host"] == telemetry._DEFAULT_HOST
        assert result["key"].startswith("phc_")
        assert result["key"] != "phc_attacker"

    def test_capture_is_noop_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[dict[str, Any]] = []
        monkeypatch.setattr(telemetry, "_post", lambda batch: events.extend(batch))
        telemetry.capture("command_run", {"command": "run"})
        telemetry.flush(timeout=1.0)
        assert events == []


class TestDistinctId:
    def test_stable_and_persisted(self, tmp_path: Path) -> None:
        first = telemetry.distinct_id()
        assert first == telemetry.distinct_id()
        state = json.loads((tmp_path / ".ouroboros" / "telemetry.json").read_text(encoding="utf-8"))
        assert state["distinct_id"] == first
        assert state["notice_shown"] is False

    def test_survives_corrupt_state_file(self, tmp_path: Path) -> None:
        path = tmp_path / ".ouroboros" / "telemetry.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json", encoding="utf-8")
        assert telemetry.distinct_id()


class TestCapture:
    def test_event_shape(self, sent: list[dict[str, Any]]) -> None:
        telemetry.set_context(runtime_backend="codex")
        telemetry.capture_tool_call("ouroboros_start_execute_seed", ok=True, duration_ms=12.34)
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        event = sent[0]
        assert event["event"] == "command_run"
        assert event["distinct_id"] == telemetry.distinct_id()
        props = event["properties"]
        assert props["command"] == "run"
        assert props["tool"] == "ouroboros_start_execute_seed"
        assert props["source"] == "mcp"
        assert props["is_funnel"] is True
        assert props["phase"] == "submission"
        assert props["accepted"] is True
        assert "ok" not in props
        assert props["duration_ms"] == 12.3
        assert props["runtime_backend"] == "codex"
        assert props["app_version"]
        assert props["os"]

    def test_unmapped_tool_still_captured(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_tool_call(
            "ouroboros_checklist_verify", ok=False, error_type="MCPToolError"
        )
        telemetry.flush(timeout=2.0)
        assert sent[0]["properties"]["command"] == "checklist_verify"
        assert sent[0]["properties"]["is_funnel"] is False
        assert sent[0]["properties"]["error_type"] == "MCPToolError"
        assert sent[0]["properties"]["phase"] == "completion"
        assert sent[0]["properties"]["ok"] is False

    def test_non_ouroboros_tool_skipped(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_tool_call("some_other_tool", ok=True)
        telemetry.flush(timeout=2.0)
        assert sent == []

    def test_polling_tools_sampled(self, sent: list[dict[str, Any]]) -> None:
        for _ in range(telemetry._POLL_SAMPLE_RATE):
            telemetry.capture_tool_call("ouroboros_job_status", ok=True)
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        assert sent[0]["properties"]["sample_rate"] == telemetry._POLL_SAMPLE_RATE

    @pytest.mark.parametrize(
        "tool",
        ("ouroboros_lineage_status", "ouroboros_project_status"),
    )
    def test_all_public_status_tools_are_sampled(
        self,
        sent: list[dict[str, Any]],
        tool: str,
    ) -> None:
        for _ in range(telemetry._POLL_SAMPLE_RATE):
            telemetry.capture_tool_call(tool, ok=True)
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        assert sent[0]["properties"]["sample_rate"] == telemetry._POLL_SAMPLE_RATE

    @pytest.mark.parametrize(
        ("job_type", "terminal_status", "meta", "verified", "reported_approval"),
        (
            ("evaluate", "completed", {"final_approved": True}, True, True),
            ("evaluate", "failed", {"final_approved": True}, False, True),
            ("evaluate", "cancelled", {"final_approved": True}, False, True),
            ("evaluate", "interrupted", {"final_approved": True}, False, True),
            ("execute_seed", "completed", {"final_approved": True}, False, True),
            ("evaluate", "completed", {}, False, None),
            ("evaluate", "completed", {"final_approved": False}, False, False),
            ("evaluate", "completed", {"final_approved": "true"}, False, None),
        ),
    )
    def test_durable_job_outcome_distinguishes_verified_success(
        self,
        sent: list[dict[str, Any]],
        job_type: str,
        terminal_status: str,
        meta: dict[str, Any],
        verified: bool,
        reported_approval: bool | None,
    ) -> None:
        telemetry.capture_job_outcome(
            "job_private_id",
            job_type,
            terminal_status=terminal_status,
            result_meta=meta,
        )
        telemetry.flush(timeout=2.0)
        event = sent[0]
        assert event["event"] == "workflow_outcome"
        assert event["properties"]["phase"] == "terminal"
        assert event["properties"]["verified"] is verified
        assert event["properties"].get("final_approved") is reported_approval
        assert "job_private_id" not in json.dumps(event)

    def test_funnel_mapping_covers_all_stages(self) -> None:
        commands = set(telemetry._TOOL_FUNNEL.values())
        assert {"interview", "seed", "run", "evolve", "auto", "evaluate", "qa"} <= commands

    def test_never_raises_when_post_fails(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        def boom(batch: list[dict[str, Any]]) -> None:
            raise OSError("network down")

        monkeypatch.setattr(telemetry, "_post", boom)
        telemetry.capture_tool_call("ouroboros_interview", ok=True)
        telemetry.flush(timeout=2.0)


class TestCliCapture:
    def test_init_normalized_to_interview(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_cli_command("init")
        telemetry.flush(timeout=2.0)
        assert sent[0]["properties"]["command"] == "interview"
        assert sent[0]["properties"]["source"] == "cli"

    def test_internal_commands_skipped(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_cli_command("dispatch")
        telemetry.capture_cli_command("job")
        telemetry.capture_cli_command("mcp")
        telemetry.capture_cli_command(None)
        telemetry.flush(timeout=2.0)
        assert sent == []


class TestFrontdoor:
    def test_claude_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
        monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)
        monkeypatch.setenv("CLAUDECODE", "1")
        assert telemetry._detect_frontdoor() == "claude"

    def test_codex_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
        assert telemetry._detect_frontdoor() == "codex"

    def test_no_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
        monkeypatch.delenv("CODEX_SANDBOX_NETWORK_DISABLED", raising=False)
        assert telemetry._detect_frontdoor() is None


class TestNotice:
    def test_notice_shown_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        telemetry.show_first_run_notice()
        assert "anonymous" in capsys.readouterr().err.lower()
        telemetry.show_first_run_notice()
        assert capsys.readouterr().err == ""
        state = json.loads((tmp_path / ".ouroboros" / "telemetry.json").read_text(encoding="utf-8"))
        assert state["notice_shown"] is True

    def test_no_notice_when_disabled(self, capsys: pytest.CaptureFixture[str]) -> None:
        telemetry.show_first_run_notice()
        assert capsys.readouterr().err == ""


class TestExitDoesNotBlock:
    """Regression for the atexit-flush blocking bug.

    ``_ensure_worker()`` used to register ``flush()`` with ``atexit``, so a
    process with a queued event and an unresponsive destination would block
    exit for up to the flush timeout on top of whatever the stalled HTTP call
    was doing. The worker thread is now a plain daemon thread with nothing
    registered at exit: process termination must not wait on it, even if the
    destination never responds.
    """

    def test_stalled_transport_does_not_delay_process_exit(self, tmp_path: Path) -> None:
        # A local socket that accepts a connection but never reads or writes
        # anything simulates an unreachable/hanging PostHog endpoint without
        # touching the network. `_post()`'s urlopen() will send the request
        # (it fits in the OS send buffer) and then block reading a response
        # until its own HTTP timeout — the fix is that nothing at process
        # exit waits around for that.
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()[:2]
        stop = threading.Event()

        def accept_and_stall() -> None:
            server.settimeout(0.5)
            while not stop.is_set():
                try:
                    conn, _addr = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    # Server socket was closed out from under a blocked
                    # accept() during shutdown -- stop, don't propagate.
                    break
                stop.wait()
                conn.close()

        acceptor = threading.Thread(target=accept_and_stall, daemon=True)
        acceptor.start()
        try:
            env = os.environ.copy()
            for key in ("DO_NOT_TRACK", "OUROBOROS_TELEMETRY", "OUROBOROS_POSTHOG_HOST"):
                env.pop(key, None)
            env["HOME"] = str(tmp_path)
            env["OUROBOROS_POSTHOG_API_KEY"] = "phc_test"
            env["OUROBOROS_POSTHOG_HOST"] = f"http://{host}:{port}"
            repo_root = Path(__file__).resolve().parents[2]
            env["PYTHONPATH"] = str(repo_root / "src")

            start = time.monotonic()
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from ouroboros import telemetry; telemetry.capture('test_event')",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            elapsed = time.monotonic() - start
        finally:
            # Signal and join the acceptor before closing the socket: the
            # thread wakes on its own (0.5s accept timeout, or the stop
            # event if it is parked past an accepted connection), so the
            # socket never needs to be torn out from under a live accept().
            stop.set()
            acceptor.join(timeout=2)
            server.close()

        # Old (atexit-registered flush) behavior measured ~2s against a
        # stalled transport. Keep generous headroom for interpreter/import
        # startup while staying strictly below the old blocking bound.
        assert elapsed < 1.2, (
            f"subprocess took {elapsed:.2f}s to exit against a stalled transport "
            "-- telemetry may be blocking process exit again"
        )


class TestConcurrentIdentityCreation:
    """Regression for the first-use identity creation race (cross-process).

    Concurrent processes can all observe a missing telemetry.json at once;
    without cross-process atomic creation, each one mints its own uuid and
    overwrites the file, so different processes end up caching different
    ids while only one survives on disk. This spawns N processes sharing a
    fresh HOME, synchronizes them on a sentinel file so their calls to
    ``distinct_id()`` land as close together as possible, and asserts every
    process converged on the single id that actually persisted.
    """

    def test_concurrent_first_use_converges_on_one_id(self, tmp_path: Path) -> None:
        n_procs = 16
        home = tmp_path / "home"
        home.mkdir()
        sentinel = tmp_path / "go"
        repo_root = Path(__file__).resolve().parents[2]

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(repo_root / "src")
        # distinct_id()/_load_state() do not consult is_enabled() -- identity
        # creation happens regardless of opt-out. Setting this only guards
        # against any accidental network attempt in this test.
        env["OUROBOROS_TELEMETRY"] = "0"
        for key in ("DO_NOT_TRACK", "OUROBOROS_POSTHOG_API_KEY", "OUROBOROS_POSTHOG_HOST"):
            env.pop(key, None)

        # Import first, then busy-wait on the sentinel right before the
        # call under test, so the race window is the distinct_id() calls
        # themselves rather than staggered interpreter startup.
        script = (
            "import time\n"
            "from pathlib import Path\n"
            "from ouroboros import telemetry\n"
            f"sentinel = Path({str(sentinel)!r})\n"
            "deadline = time.monotonic() + 5.0\n"
            "while not sentinel.exists():\n"
            "    if time.monotonic() > deadline:\n"
            "        raise SystemExit('sentinel never appeared')\n"
            "    time.sleep(0.002)\n"
            "print(telemetry.distinct_id())\n"
        )

        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(n_procs)
        ]
        try:
            time.sleep(0.3)  # let every child finish importing and reach the busy-wait
            sentinel.write_text("go", encoding="utf-8")
            outputs = []
            for proc in procs:
                out, err = proc.communicate(timeout=5)
                assert proc.returncode == 0, f"child failed: {err}"
                outputs.append(out.strip())
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()

        assert len(set(outputs)) == 1, f"processes disagreed on distinct_id: {set(outputs)}"

        state = json.loads((home / ".ouroboros" / "telemetry.json").read_text(encoding="utf-8"))
        assert state["distinct_id"] == outputs[0]
