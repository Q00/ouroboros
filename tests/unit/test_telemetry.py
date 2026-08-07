"""Unit tests for anonymous usage telemetry (src/ouroboros/telemetry.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from ouroboros import telemetry


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
        ("job_type", "terminal_status", "meta", "verified"),
        (
            ("execute_seed", "completed", {}, False),
            ("evaluate", "completed", {"final_approved": True}, True),
            ("evaluate", "completed", {"final_approved": False}, False),
            ("execute_seed", "failed", {}, False),
        ),
    )
    def test_durable_job_outcome_distinguishes_verified_success(
        self,
        sent: list[dict[str, Any]],
        job_type: str,
        terminal_status: str,
        meta: dict[str, Any],
        verified: bool,
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
