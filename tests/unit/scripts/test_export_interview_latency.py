"""Tests for the privacy-safe interview latency exporter."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "export_interview_latency.py"


def _create_event_store(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )
    return connection


def _insert_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    interview_id: str,
    event_type: str,
    payload: dict[str, object],
    timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO events (
            id, aggregate_type, aggregate_id, event_type, payload, timestamp
        ) VALUES (?, 'interview', ?, ?, ?, ?)
        """,
        (event_id, interview_id, event_type, json.dumps(payload), timestamp),
    )


def _run_exporter(
    db_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def _run_exporter_default(home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_exporter_allowlists_fields_and_hashes_interview_id(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    raw_interview_id = "interview_0123456789abcdef"
    secret_prompt = "Build the private acquisition workflow"
    secret_answer = "Use /Users/alice/secret-plan.md and token sk-secret"
    with _create_event_store(db_path) as connection:
        _insert_event(
            connection,
            event_id="event-1",
            interview_id=raw_interview_id,
            event_type="interview.response.emitted",
            timestamp="2026-07-17T18:00:00+00:00",
            payload={
                "response_kind": "answer",
                "round_number": 4,
                "payload_chars": 120,
                "transcript_chars": 900,
                "ambiguity_prefix_present": False,
                "is_length_guard": False,
                "timings_ms": {
                    "total": 3000.0,
                    "ambiguity_scoring": 750.0,
                    "question_generation": 1500.0,
                    "advisory_build": 250.0,
                },
                "prompt": secret_prompt,
                "response_preview": secret_answer,
                "error": secret_answer,
                "environment": {"HOME": "/Users/alice"},
            },
        )

    before = db_path.read_bytes()
    result = _run_exporter(db_path, "--interview-id", raw_interview_id)
    after = db_path.read_bytes()

    assert result.returncode == 0, result.stderr
    assert before == after, "export must leave the EventStore unchanged"
    record = json.loads(result.stdout)
    expected_hash = hashlib.sha256(f"ouroboros-interview:{raw_interview_id}".encode()).hexdigest()
    assert record == {
        "interview_id_sha256": expected_hash,
        "timestamp": "2026-07-17T18:00:00+00:00",
        "event_type": "interview.response.emitted",
        "metadata": {
            "response_kind": "answer",
            "round_number": 4,
            "payload_chars": 120,
            "transcript_chars": 900,
            "ambiguity_prefix_present": False,
            "is_length_guard": False,
        },
        "timings_ms": {
            "total": 3000.0,
            "ambiguity_scoring": 750.0,
            "question_generation": 1500.0,
            "advisory_build": 250.0,
        },
    }
    assert raw_interview_id not in result.stdout
    assert secret_prompt not in result.stdout
    assert secret_answer not in result.stdout
    assert "/Users/alice" not in result.stdout


def test_exporter_includes_terminal_events_but_excludes_failure_text(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    raw_error = "provider failed with token sk-secret at /Users/alice/project"
    unsafe_error_type = "RuntimeError at /Users/alice/private-config.json"
    timing_payload = {
        "total": 2000.0,
        "ambiguity_scoring": None,
        "question_generation": 500.0,
        "advisory_build": None,
    }
    with _create_event_store(db_path) as connection:
        _insert_event(
            connection,
            event_id="event-1",
            interview_id="interview_failure",
            event_type="interview.failed",
            timestamp="2026-07-17T18:01:00+00:00",
            payload={
                "phase": "question_generation",
                "error": raw_error,
                "timings_ms": timing_payload,
            },
        )
        _insert_event(
            connection,
            event_id="event-2",
            interview_id="interview_handoff",
            event_type="interview.question_generation.parent_handoff",
            timestamp="2026-07-17T18:02:00+00:00",
            payload={
                "phase": "next_question_generation",
                "reason_code": "question_generation_envelope_violation",
                "provider_error_type": "ToolUseBlockViolation",
                "timings_ms": timing_payload,
            },
        )
        _insert_event(
            connection,
            event_id="event-3",
            interview_id="interview_handoff_unsafe",
            event_type="interview.question_generation.parent_handoff",
            timestamp="2026-07-17T18:02:30+00:00",
            payload={
                "phase": "next_question_generation",
                "reason_code": "question_generation_envelope_violation",
                "provider_error_type": unsafe_error_type,
                "timings_ms": timing_payload,
            },
        )
        _insert_event(
            connection,
            event_id="event-4",
            interview_id="interview_legacy",
            event_type="interview.completed",
            timestamp="2026-07-17T18:03:00+00:00",
            payload={
                "total_rounds": 3,
                "timings_ms": {
                    "total": None,
                    "ambiguity_scoring": None,
                    "question_generation": None,
                    "advisory_build": None,
                },
            },
        )

    result = _run_exporter(db_path)

    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["event_type"] for record in records] == [
        "interview.failed",
        "interview.question_generation.parent_handoff",
        "interview.question_generation.parent_handoff",
    ]
    assert records[0]["metadata"] == {"phase": "question_generation"}
    assert records[1]["metadata"] == {
        "phase": "next_question_generation",
        "reason_code": "question_generation_envelope_violation",
        "provider_error_type": "ToolUseBlockViolation",
    }
    assert records[2]["metadata"] == {
        "phase": "next_question_generation",
        "reason_code": "question_generation_envelope_violation",
    }
    assert raw_error not in result.stdout
    assert unsafe_error_type not in result.stdout
    assert "sk-secret" not in result.stdout
    assert "/Users/alice" not in result.stdout


def test_exporter_fails_cleanly_when_database_is_missing(tmp_path: Path) -> None:
    result = _run_exporter(tmp_path / "missing.db")

    assert result.returncode == 2


def test_explicit_database_path_expansion_failure_is_redacted() -> None:
    private_user = "ouroboros_user_that_does_not_exist_1817"

    result = _run_exporter(Path(f"~{private_user}/db.sqlite"))
    output = result.stdout + result.stderr

    assert result.returncode == 2
    assert "invalid EventStore configuration" in result.stderr
    assert private_user not in output
    assert "Traceback" not in output


def test_exporter_uses_configured_default_database(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".ouroboros"
    db_path = config_dir / "data" / "events.db"
    db_path.parent.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("persistence:\n  database_path: data/events.db\n")
    with _create_event_store(db_path):
        pass

    result = _run_exporter_default(home)

    assert result.returncode == 0
    assert "no timed interview events found" in result.stderr
    assert "database not found" not in result.stderr


def test_explicit_database_bypasses_invalid_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".ouroboros"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("persistence: []\n")
    env = os.environ.copy()
    env["HOME"] = str(home)

    result = _run_exporter(tmp_path / "missing.db", env=env)

    assert result.returncode == 2
    assert "database not found" in result.stderr
    assert "invalid EventStore configuration" not in result.stderr
    assert result.stdout == ""
    assert str(tmp_path) not in result.stderr


def test_invalid_config_error_redacts_secret_and_home_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_dir = home / ".ouroboros"
    config_dir.mkdir(parents=True)
    secret = "sk-super-secret"
    (config_dir / "config.yaml").write_text(
        f"persistence:\n  database_path: [\napi_key: {secret}: exposed\n"
    )

    result = _run_exporter_default(home)
    output = result.stdout + result.stderr

    assert result.returncode == 2
    assert "invalid EventStore configuration" in result.stderr
    assert secret not in output
    assert str(home) not in output
    assert "Traceback" not in output
