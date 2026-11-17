"""Unit tests for anonymous usage telemetry (src/ouroboros/telemetry.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import os
from pathlib import Path
import platform
import queue
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

import pytest

from ouroboros import telemetry, telemetry_rollup
from ouroboros.config.loader import get_telemetry_enabled, telemetry_opt_out_in_env


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


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, int]]:
    """Counts handed to the local usage rollup, one dict per call."""
    calls: list[dict[str, int]] = []
    monkeypatch.setattr(telemetry, "_record_usage", lambda counts: calls.append(dict(counts)))
    return calls


_OUTCOME: dict[str, Any] = {"command": "run", "terminal_status": "completed", "verified": False}


def _capture_outcome() -> None:
    """Capture one workflow_outcome, the only event capture() may send."""
    telemetry.capture("workflow_outcome", dict(_OUTCOME))


def _rollup_path() -> Path:
    return Path.home() / ".ouroboros" / telemetry_rollup.ROLLUP_FILENAME


def _on_day(monkeypatch: pytest.MonkeyPatch, day: str) -> None:
    monkeypatch.setattr(telemetry, "_utc_day", lambda: day)


def _next_process_on(monkeypatch: pytest.MonkeyPatch, day: str) -> None:
    """Settle this "process", then start a fresh one (empty caches) on ``day``."""
    telemetry.flush(timeout=2.0)
    telemetry._reset_for_tests()
    _on_day(monkeypatch, day)


def _sentinel_wait_snippet(sentinel: Path) -> str:
    """Python source: busy-wait on a sentinel file before proceeding.

    Used by cross-process regression tests to synchronize subprocess starts
    so their calls to the function under test land as close together as
    possible, tightening the race window instead of relying on interpreter
    startup jitter alone.
    """
    return (
        "import time\n"
        "from pathlib import Path\n"
        f"sentinel = Path({str(sentinel)!r})\n"
        "deadline = time.monotonic() + 5.0\n"
        "while not sentinel.exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        raise SystemExit('sentinel never appeared')\n"
        "    time.sleep(0.002)\n"
    )


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
            "OUROBOROS_FIRST_COMMAND_SURFACE",
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
        _capture_outcome()
        telemetry.flush(timeout=1.0)
        assert events == []


class TestTelemetryOptOutInEnv:
    """The env half of the telemetry opt-out contract, persisted by setup."""

    def test_do_not_track_truthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DO_NOT_TRACK", "1")
        monkeypatch.delenv("OUROBOROS_TELEMETRY", raising=False)
        assert telemetry_opt_out_in_env() is True

    def test_ouroboros_telemetry_falsy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DO_NOT_TRACK", raising=False)
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "off")
        assert telemetry_opt_out_in_env() is True

    def test_ouroboros_telemetry_1_is_not_an_opt_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DO_NOT_TRACK", raising=False)
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "1")
        assert telemetry_opt_out_in_env() is False

    def test_unset_env_is_not_an_opt_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DO_NOT_TRACK", raising=False)
        monkeypatch.delenv("OUROBOROS_TELEMETRY", raising=False)
        assert telemetry_opt_out_in_env() is False


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


# Independent transcription of the installer's shell UUID regex (agreed
# cross-runtime semantics), NOT telemetry._UUID_PATTERN -- reusing the
# production pattern object would make the cross-runtime tests circular.
_SHELL_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class TestDistinctIdValidation:
    """Regression: distinct_id must be a validated, canonicalized UUID.

    _load_state() previously accepted any non-empty string as a valid
    identity and forwarded it to the transport unsanitized -- a probe with
    {"distinct_id": "person@example.com"} sent that email unchanged. Every
    read path now validates against the canonical hyphenated UUID shape
    (see telemetry._UUID_PATTERN) and canonicalizes case, with a malformed-
    JSON salvage path that keeps a Python-side repair convergent with the
    installer's shell-side repair of the same file.
    """

    def test_valid_json_non_uuid_distinct_id_never_transmitted(
        self, tmp_path: Path, sent: list[dict[str, Any]]
    ) -> None:
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        state_path.write_text(
            json.dumps({"distinct_id": "person@example.com", "notice_shown": True}),
            encoding="utf-8",
        )

        _capture_outcome()
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        transmitted = sent[0]["distinct_id"]
        assert transmitted != "person@example.com"
        assert telemetry._UUID_PATTERN.fullmatch(transmitted)

        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        assert repaired["distinct_id"] == transmitted

    def test_nested_distinct_id_is_invalid_and_never_salvaged(
        self, tmp_path: Path, sent: list[dict[str, Any]]
    ) -> None:
        """A UUID that only exists nested under another key is not a valid
        top-level identity -- _canonical_distinct_id only ever looks at
        raw.get("distinct_id"), so this document has no distinct_id at all
        as far as the identity contract is concerned. It's also valid JSON,
        so _build_repair_candidate's malformed-JSON salvage regex (which
        would happily find the nested UUID by text search alone, with no
        notion of nesting) must never run against it -- only genuinely
        unparseable JSON is eligible for salvage. Python mints a fresh id
        and repairs the file to top-level shape."""
        nested_uuid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        state_path.write_text(
            json.dumps({"wrapper": {"distinct_id": nested_uuid}, "notice_shown": True}),
            encoding="utf-8",
        )

        _capture_outcome()
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        transmitted = sent[0]["distinct_id"]
        assert telemetry._UUID_PATTERN.fullmatch(transmitted)
        assert transmitted != nested_uuid

        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        assert repaired["distinct_id"] == transmitted
        assert "wrapper" not in repaired

    def test_duplicate_top_level_keys_json_loads_keeps_last_no_repair(self, tmp_path: Path) -> None:
        """json.loads (like most JSON parsers) silently keeps the LAST
        occurrence of a duplicate key, so raw text with two distinct
        top-level "distinct_id" values parses to a single, already-valid
        id -- the second one. Nothing about that looks invalid once parsed,
        so no repair is triggered and the file is left byte-identical."""
        uuid_a = "11111111-1111-4111-8111-111111111111"
        uuid_b = "22222222-2222-4222-8222-222222222222"
        raw_text = (
            f'{{"distinct_id": "{uuid_a}", "distinct_id": "{uuid_b}", "notice_shown": false}}'
        )
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        state_path.write_text(raw_text, encoding="utf-8")

        result = telemetry.distinct_id()

        assert result == uuid_b
        assert state_path.read_text(encoding="utf-8") == raw_text  # untouched, no repair mint

    def test_malformed_json_with_salvageable_uuid_is_recovered(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        state_path.write_text(
            '{"distinct_id": "3F2504E0-4F89-11D3-9A0C-0305E82C3301", garbage',
            encoding="utf-8",
        )

        result = telemetry.distinct_id()

        assert result == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        assert repaired["distinct_id"] == "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    def test_malformed_json_with_no_salvageable_uuid_mints_fresh(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        state_path.write_text('{"distinct_id": "not-a-uuid-at-all", garbage', encoding="utf-8")

        result = telemetry.distinct_id()

        assert telemetry._UUID_PATTERN.fullmatch(result)
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        assert repaired["distinct_id"] == result

    def test_valid_json_uppercase_uuid_canonicalized_and_persisted(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        upper = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
        state_path.write_text(
            json.dumps({"distinct_id": upper, "created_at": "x", "notice_shown": False}),
            encoding="utf-8",
        )

        result = telemetry.distinct_id()

        assert result == upper.lower()
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["distinct_id"] == upper.lower()

    def test_installer_shaped_state_is_adopted_unchanged(self, tmp_path: Path) -> None:
        """Exact single-line shape scripts/install.sh's _telemetry_distinct_id
        writes via `printf '{"distinct_id": "%s", "created_at": "%s",
        "notice_shown": false}\\n'` -- Python must adopt it as-is."""
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        installer_id = str(uuid.uuid4())
        state_path.write_text(
            f'{{"distinct_id": "{installer_id}", "created_at": "2026-01-01T00:00:00Z", '
            '"notice_shown": false}\n',
            encoding="utf-8",
        )

        result = telemetry.distinct_id()

        assert result == installer_id

    def test_python_repaired_state_matches_installer_regex(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        state_path.write_text("not json at all {{{", encoding="utf-8")

        result = telemetry.distinct_id()

        assert _SHELL_UUID_PATTERN.fullmatch(result), (
            f"Python-repaired id {result!r} would not be accepted by the installer's shell regex"
        )
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert _SHELL_UUID_PATTERN.fullmatch(persisted["distinct_id"])


def _require_non_root() -> None:
    """chmod-based read-only tests are meaningless as root (root ignores
    directory permission bits and can write anyway)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores directory permission bits")


class TestIdentityFailClosed:
    """Regression: identity repair must fail closed, never emit under a
    process-local, unpersisted candidate.

    _repair_state()/_publish_new_state() previously fell back to returning
    their process-local candidate when every write and every reread failed,
    so _load_state() cached it and capture() transmitted events under an id
    that never actually landed on disk. A probe against a read-only
    ~/.ouroboros produced two different UUIDs in two fresh processes as a
    result. Now: an event may only be emitted under an id that was either
    already valid on disk, or freshly persisted and reread back as valid --
    otherwise capture() drops silently, exactly like every other telemetry
    failure mode.
    """

    def test_readonly_dir_with_invalid_file_drops_in_two_fresh_processes(
        self, tmp_path: Path
    ) -> None:
        """The reviewer's exact probe, inverted: read-only ~/.ouroboros with
        an invalid file used to mint a different UUID per fresh process and
        transmit it. Now neither process transmits anything, and
        distinct_id() is empty in both."""
        _require_non_root()
        home = tmp_path / "home"
        state_dir = home / ".ouroboros"
        state_dir.mkdir(parents=True)
        (state_dir / "telemetry.json").write_text(
            json.dumps({"distinct_id": "not-a-valid-uuid"}), encoding="utf-8"
        )
        os.chmod(state_dir, 0o500)
        try:
            repo_root = Path(__file__).resolve().parents[2]
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHONPATH"] = str(repo_root / "src")
            env["OUROBOROS_POSTHOG_API_KEY"] = "phc_test"
            for key in ("DO_NOT_TRACK", "OUROBOROS_TELEMETRY", "OUROBOROS_POSTHOG_HOST"):
                env.pop(key, None)

            script = (
                "from ouroboros import telemetry\n"
                "captured = []\n"
                "telemetry._post = lambda batch: captured.extend(batch)\n"
                "telemetry.capture('mcp_serve_started', "
                "{'transport': 'stdio', 'tool_count': 1})\n"
                "telemetry.flush(timeout=2.0)\n"
                "print(telemetry.distinct_id() or '<empty>')\n"
                "print('TRANSMITTED' if captured else 'NOTHING')\n"
            )

            outputs = []
            for _ in range(2):
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=True,
                )
                outputs.append(completed.stdout.strip().splitlines())
        finally:
            os.chmod(state_dir, 0o700)

        for lines in outputs:
            assert lines == ["<empty>", "NOTHING"], f"unexpected output: {lines}"

    def test_readonly_dir_with_valid_file_still_emits(
        self, tmp_path: Path, sent: list[dict[str, Any]]
    ) -> None:
        """Adoption of an already-valid identity needs no write -- a
        read-only directory must not block reading a file that's fine."""
        _require_non_root()
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        valid_id = str(uuid.uuid4())
        (state_dir / "telemetry.json").write_text(
            json.dumps({"distinct_id": valid_id, "notice_shown": False}), encoding="utf-8"
        )
        os.chmod(state_dir, 0o500)
        try:
            _capture_outcome()
            telemetry.flush(timeout=2.0)
        finally:
            os.chmod(state_dir, 0o700)

        assert len(sent) == 1
        assert sent[0]["distinct_id"] == valid_id

    def test_readonly_dir_with_uppercase_file_still_emits_lowercased(
        self, tmp_path: Path, sent: list[dict[str, Any]]
    ) -> None:
        """Case canonicalization is in-memory-sufficient: even though the
        best-effort rewrite to lowercase fails (read-only dir), the
        underlying UUID was already durable, so the event still ships under
        the canonicalized id."""
        _require_non_root()
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        upper = "3F2504E0-4F89-11D3-9A0C-0305E82C3301"
        (state_dir / "telemetry.json").write_text(
            json.dumps({"distinct_id": upper, "notice_shown": False}), encoding="utf-8"
        )
        os.chmod(state_dir, 0o500)
        try:
            _capture_outcome()
            telemetry.flush(timeout=2.0)
        finally:
            os.chmod(state_dir, 0o700)

        assert len(sent) == 1
        assert sent[0]["distinct_id"] == upper.lower()

    def test_readonly_dir_with_absent_file_drops(
        self, tmp_path: Path, sent: list[dict[str, Any]]
    ) -> None:
        """No file at all, in a parent directory that can't be written to
        -- nothing can ever be persisted, so nothing should be emitted."""
        _require_non_root()
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        os.chmod(state_dir, 0o500)
        try:
            _capture_outcome()
            telemetry.flush(timeout=2.0)
            result = telemetry.distinct_id()
        finally:
            os.chmod(state_dir, 0o700)

        assert sent == []
        assert result == ""

    def test_failure_is_not_cached_recovers_after_dir_becomes_writable(
        self, tmp_path: Path, sent: list[dict[str, Any]]
    ) -> None:
        """A failed attempt must not poison _state_cache: the same process,
        with no restart, must recover once the filesystem does."""
        _require_non_root()
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        (state_dir / "telemetry.json").write_text(
            json.dumps({"distinct_id": "not-a-valid-uuid"}), encoding="utf-8"
        )
        os.chmod(state_dir, 0o500)
        try:
            _capture_outcome()
            telemetry.flush(timeout=2.0)
            assert sent == []
            assert telemetry.distinct_id() == ""
        finally:
            os.chmod(state_dir, 0o700)

        _capture_outcome()
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        persisted_id = sent[0]["distinct_id"]
        assert telemetry._UUID_PATTERN.fullmatch(persisted_id)
        persisted = json.loads((state_dir / "telemetry.json").read_text(encoding="utf-8"))
        assert persisted["distinct_id"] == persisted_id


class TestCapture:
    def test_only_workflow_outcome_is_publicly_capturable(self, sent: list[dict[str, Any]]) -> None:
        for retired in (
            "command_run",
            "service_active",
            "mcp_serve_started",
            "subagent_dispatch",
            "ac_verify_failed",
            "runtime_drift",
            "acceptance_no_evidence",
            "acceptance_artifact_checks",
            "usage_daily",  # only the daily rollup may send this one
        ):
            telemetry.capture(retired, {"command": "run"})
        telemetry.flush(timeout=2.0)
        assert sent == []

    def test_durable_job_outcome_keeps_only_terminal_fields(
        self, sent: list[dict[str, Any]]
    ) -> None:
        telemetry.set_context(runtime_backend="codex")
        telemetry.capture_job_outcome(
            "job_private_id",
            "evaluate",
            terminal_status="completed",
            result_meta={"final_approved": True},
        )
        telemetry.flush(timeout=2.0)

        props = sent[0]["properties"]
        assert props["command"] == "evaluate"
        assert props["terminal_status"] == "completed"
        assert props["verified"] is True
        assert props["runtime_backend"] == "codex"
        assert "job_private_id" not in json.dumps(sent[0])

    def test_job_outcome_uuid_is_deterministic_per_job(self, sent: list[dict[str, Any]]) -> None:
        """PostHog dedupes on the event uuid (not $insert_id): a retried
        terminal append for the same job must carry the same uuid."""
        for job_id in ("job-a", "job-a", "job-b"):
            telemetry.capture_job_outcome(job_id, "auto", terminal_status="completed")
        telemetry.flush(timeout=2.0)

        uuids = [event["uuid"] for event in sent]
        assert uuids[0] == uuids[1] != uuids[2]
        assert all(uuid.UUID(value).version == 5 for value in uuids)
        assert "job-a" not in json.dumps(sent)

    def test_failed_outcome_keeps_closed_reason(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_job_outcome(
            "job-private-id",
            "execute_seed",
            terminal_status="failed",
            result_meta={
                "failure_reason_code": "validation",
                "error": "secret /Users/private/project",
            },
        )
        telemetry.flush(timeout=2.0)

        props = sent[0]["properties"]
        assert props["command"] == "run"
        assert props["terminal_status"] == "failed"
        assert props["failure_reason_code"] == "validation"
        assert "secret" not in json.dumps(sent[0])

    def test_failed_run_forwards_closed_failure_cause(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_job_outcome(
            "job-private-id",
            "execute_seed",
            terminal_status="failed",
            result_meta={
                "failure_reason_code": "validation",
                "failure_cause": "verify_workspace_mutated",
            },
        )
        telemetry.flush(timeout=2.0)

        props = sent[0]["properties"]
        assert props["failure_reason_code"] == "validation"
        assert props["failure_cause"] == "verify_workspace_mutated"

    def test_failed_run_forwards_launch_cause(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_job_outcome(
            "job-private-id",
            "execute_seed",
            terminal_status="failed",
            result_meta={
                "failure_reason_code": "config",
                "failure_cause": "launch_workspace_unavailable",
            },
        )
        telemetry.flush(timeout=2.0)

        props = sent[0]["properties"]
        assert props["failure_reason_code"] == "config"
        assert props["failure_cause"] == "launch_workspace_unavailable"

    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            ({"ac_passed": 3, "ac_total": 4}, {"ac_passed": 3, "ac_total": 4}),
            ({"ac_passed": 0, "ac_total": 0}, {"ac_passed": 0, "ac_total": 0}),
            ({"ac_passed": 5, "ac_total": 4}, {}),
            ({"ac_passed": -1, "ac_total": 4}, {}),
            ({"ac_passed": True, "ac_total": 1}, {}),
            ({"ac_passed": "3", "ac_total": 4}, {}),
            ({"ac_passed": 3}, {}),
            ({"ac_passed": 3, "ac_total": 10_001}, {}),
        ],
    )
    def test_run_ac_tally_forwards_only_a_consistent_int_pair(
        self, sent: list[dict[str, Any]], meta: dict[str, Any], expected: dict[str, int]
    ) -> None:
        telemetry.capture_job_outcome(
            "job-private-id", "execute_seed", terminal_status="failed", result_meta=meta
        )
        telemetry.flush(timeout=2.0)

        props = sent[0]["properties"]
        assert {k: props[k] for k in ("ac_passed", "ac_total") if k in props} == expected

    def test_ac_tally_is_a_run_only_dimension(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_job_outcome(
            "job-private-id",
            "evaluate",
            terminal_status="completed",
            result_meta={"ac_passed": 3, "ac_total": 4, "final_approved": True},
        )
        telemetry.flush(timeout=2.0)

        props = sent[0]["properties"]
        assert "ac_passed" not in props and "ac_total" not in props

    def test_unaudited_failure_cause_folds_to_unknown(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_job_outcome(
            "job-private-id",
            "execute_seed",
            terminal_status="failed",
            result_meta={"failure_cause": "verify_command: pytest /Users/private/project"},
        )
        telemetry.flush(timeout=2.0)

        props = sent[0]["properties"]
        assert props["failure_cause"] == "unknown"
        assert "private" not in json.dumps(sent[0])

    def test_successful_outcome_carries_no_failure_cause(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture_job_outcome(
            "job-private-id",
            "execute_seed",
            terminal_status="completed",
            result_meta={"failure_cause": "verify_exit_nonzero"},
        )
        telemetry.flush(timeout=2.0)

        assert "failure_cause" not in sent[0]["properties"]
        assert "failure_reason_code" not in sent[0]["properties"]

    def test_never_raises_when_post_fails(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        monkeypatch.setattr(telemetry, "_post", lambda _batch: (_ for _ in ()).throw(OSError()))
        telemetry.capture_tool_call("ouroboros_interview", ok=True)
        _capture_outcome()
        telemetry.flush(timeout=2.0)


class TestPropertyAllowlist:
    """Regression for enforcing the event/property whitelist at serialization.

    Auditing call sites for TELEMETRY.md compliance is not the same as
    enforcing it: capture() and set_context() must drop unlisted events,
    unlisted properties, and oversized/non-scalar values before anything
    reaches the transport, even if a caller passes them in directly.
    """

    def test_unknown_event_dropped_entirely(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture("not_whitelisted", {"prompt": "x"})
        telemetry.flush(timeout=2.0)
        assert sent == []

    def test_unlisted_properties_dropped_but_known_ones_kept(
        self, sent: list[dict[str, Any]]
    ) -> None:
        telemetry.capture(
            "workflow_outcome",
            {"command": "run", "prompt": "secret", "file_path": "/etc/passwd"},
        )
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        assert props["command"] == "run"
        assert "prompt" not in props
        assert "file_path" not in props

    def test_set_context_ignores_unlisted_keys(self, sent: list[dict[str, Any]]) -> None:
        telemetry.set_context(prompt="secret", runtime_backend="claude")
        telemetry.capture("workflow_outcome", {"command": "run"})
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        assert "prompt" not in props
        assert props["runtime_backend"] == "claude"

    def test_oversized_string_property_dropped(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture(
            "workflow_outcome",
            {"command": "run", "failure_cause": "E" * (telemetry._MAX_PROPERTY_STR_LEN + 1)},
        )
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        assert props["command"] == "run"
        assert "failure_cause" not in props

    def test_non_scalar_property_dropped(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture(
            "workflow_outcome",
            {"command": "run", "terminal_status": {"nested": "value"}, "ac_total": [1, 2]},
        )
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        assert props["command"] == "run"
        assert "terminal_status" not in props
        assert "ac_total" not in props


def _no_ambient_frontdoor_or_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip env vars that would otherwise make frontdoor/ci non-deterministic.

    CLAUDECODE=1 is set in this very environment (we're running inside
    Claude Code), so exact-key-set assertions need these cleared to get a
    deterministic properties dict rather than one that varies by CI/host.
    """
    for key in (
        "CLAUDECODE",
        "CODEX_THREAD_ID",
        "CODEX_SANDBOX_NETWORK_DISABLED",
        "CI",
        "GITHUB_ACTIONS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_genuine_process_ci_still_stamps_ci_true(
    monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
) -> None:
    """Denying CI/GITHUB_ACTIONS from the untrusted project .env (see
    tests/unit/config/test_loader_env.py) must not affect genuine CI
    detection: a real CI runner sets these in the actual process
    environment, which the loader never touches regardless of the
    denylist, so `ci=true` must still be stamped from there."""
    monkeypatch.setenv("CI", "true")
    _capture_outcome()
    telemetry.flush(timeout=2.0)
    assert len(sent) == 1
    assert sent[0]["properties"]["ci"] is True


class TestExactPropertySets:
    def test_workflow_outcome_exact_keys(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _no_ambient_frontdoor_or_ci(monkeypatch)
        telemetry.set_context(runtime_backend="claude")
        telemetry.capture_job_outcome(
            "job-x", "evaluate", terminal_status="completed", result_meta={"final_approved": True}
        )
        telemetry.flush(timeout=2.0)

        assert set(sent[0]["properties"]) == {
            "command",
            "terminal_status",
            "verified",
            "$insert_id",
            "runtime_backend",
            "app_version",
            "os",
        }

    def test_usage_daily_exact_keys(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _no_ambient_frontdoor_or_ci(monkeypatch)
        telemetry.set_context(runtime_backend="claude", execute_runtime_backend="ignored")
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_tool_call("ouroboros_interview", ok=True)
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_mcp_serve_started("stdio")
        telemetry.flush(timeout=2.0)

        (row,) = sent
        assert row["event"] == "usage_daily"
        assert row["properties"] == {
            "cmd_interview": 1,
            "cmd_run": 1,
            "surface_mcp": 1,
            "surface_cli": 1,
            "day": "2026-11-16",
            "runtime_backend": "claude",
            "app_version": telemetry.__version__,
            "os": platform.system().lower(),
        }
        assert set(row["properties"]) <= telemetry._USAGE_DAILY_KEYS


class TestCliCapture:
    def test_init_normalized_to_interview(self, recorded: list[dict[str, int]]) -> None:
        telemetry.capture_cli_command("init")
        assert recorded == [{"cmd_interview": 1, "surface_cli": 1}]

    def test_internal_commands_skipped(self, recorded: list[dict[str, int]]) -> None:
        telemetry.capture_cli_command("dispatch")
        telemetry.capture_cli_command("job")
        telemetry.capture_cli_command("mcp")
        telemetry.capture_cli_command(None)
        assert recorded == []

    def test_dynamic_plugin_command_is_replaced_not_forwarded(
        self, recorded: list[dict[str, int]]
    ) -> None:
        """subcommand is caller-controlled the same way an MCP tool name or
        a job_type is: _PluginAwareGroup resolves any name not in the
        static command table as a dynamically-installed plugin command, so
        an unaudited value must never become a usage key."""
        hostile = "acme-private-project"
        telemetry.capture_cli_command(hostile)
        assert recorded == [{"cmd_extension_command": 1, "surface_cli": 1}]

    def test_canonical_cli_command_passes_through_unchanged(
        self, recorded: list[dict[str, int]]
    ) -> None:
        telemetry.capture_cli_command("run")
        telemetry.capture_cli_command("workflow-ir")
        assert recorded == [
            {"cmd_run": 1, "surface_cli": 1},
            {"cmd_workflow_ir": 1, "surface_cli": 1},
        ]

    def test_gui_and_console_started_commands_count_as_console(
        self, monkeypatch: pytest.MonkeyPatch, recorded: list[dict[str, int]]
    ) -> None:
        telemetry.capture_cli_command("gui")
        monkeypatch.setenv(telemetry._SURFACE_ENV, "console")
        telemetry.capture_cli_command("auto")
        monkeypatch.setenv(telemetry._SURFACE_ENV, "anything-else")
        telemetry.capture_cli_command("auto")
        assert recorded == [
            {"cmd_gui": 1, "surface_console": 1},
            {"cmd_auto": 1, "surface_console": 1},
            {"cmd_auto": 1, "surface_cli": 1},
        ]

    def test_console_jobs_carry_the_surface_marker(self) -> None:
        from ouroboros.gui.jobs import _child_env

        assert _child_env()[telemetry._SURFACE_ENV] == "console"

    def test_every_counted_cli_command_has_a_usage_key(self) -> None:
        for name in telemetry._CANONICAL_CLI_COMMANDS - telemetry._CLI_SKIP:
            key = telemetry._usage_key(telemetry._CLI_FUNNEL.get(name, name))
            assert key in telemetry._USAGE_COUNT_KEYS
        # Every key fits in one rollup bucket, so none is ever capped away.
        assert len(telemetry._USAGE_COUNT_KEYS) <= telemetry_rollup.MAX_KEYS

    def test_cli_funnel_and_is_funnel_tuple_are_subsets_of_canonical_set(self) -> None:
        assert set(telemetry._CLI_FUNNEL) <= telemetry._CANONICAL_CLI_COMMANDS
        is_funnel_names = {"auto", "init", "interview", "seed", "run", "qa", "pm"}
        assert is_funnel_names <= telemetry._CANONICAL_CLI_COMMANDS


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

    def test_recovers_from_stale_crash_marker(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A process that creates the marker then dies must not silence the
        notice forever -- a later process should reclaim the stale marker
        and actually disclose."""
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        telemetry.distinct_id()  # create .ouroboros/ and telemetry.json (notice_shown=false)
        state_dir = tmp_path / ".ouroboros"
        marker = state_dir / "telemetry.notice"
        marker.write_text("", encoding="utf-8")
        stale = time.time() - telemetry._NOTICE_MARKER_STALE_SECONDS - 5
        os.utime(marker, (stale, stale))

        telemetry.show_first_run_notice()

        assert "anonymous" in capsys.readouterr().err.lower()
        state = json.loads((state_dir / "telemetry.json").read_text(encoding="utf-8"))
        assert state["notice_shown"] is True

    def test_fresh_marker_assumes_live_claimer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A marker younger than the stale threshold is assumed to belong to
        a concurrent process still mid-print -- do not duplicate."""
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        telemetry.distinct_id()
        state_dir = tmp_path / ".ouroboros"
        marker = state_dir / "telemetry.notice"
        marker.write_text("", encoding="utf-8")  # fresh mtime

        telemetry.show_first_run_notice()

        assert capsys.readouterr().err == ""
        state = json.loads((state_dir / "telemetry.json").read_text(encoding="utf-8"))
        assert state.get("notice_shown") is False

    def test_string_false_notice_shown_is_repaired_and_notice_prints(
        self,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        sent: list[dict[str, Any]],
    ) -> None:
        """A non-bool value must not be read as "already shown" via plain
        truthiness -- "false" is a non-empty, therefore truthy, STRING.
        It's structurally invalid, so it's coerced to False (never-shown):
        the notice actually prints, gets repaired to a real bool, and a
        subsequent capture still transmits under the same (already valid)
        identity."""
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        valid_id = str(uuid.uuid4())
        state_path.write_text(
            json.dumps({"distinct_id": valid_id, "notice_shown": "false"}), encoding="utf-8"
        )

        telemetry.show_first_run_notice()

        assert "anonymous" in capsys.readouterr().err.lower()
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        assert repaired["notice_shown"] is True
        assert repaired["distinct_id"] == valid_id

        _capture_outcome()
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        assert sent[0]["distinct_id"] == valid_id

    def test_string_true_notice_shown_still_prints_fail_toward_disclosure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A structurally invalid value that happens to spell "true" must
        still print: coercing toward "already shown" based on the string's
        content rather than its real type would be exactly the silent-
        suppression bug this validates against -- fail toward disclosure
        regardless of what the corrupted value looks like."""
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        valid_id = str(uuid.uuid4())
        state_path.write_text(
            json.dumps({"distinct_id": valid_id, "notice_shown": "true"}), encoding="utf-8"
        )

        telemetry.show_first_run_notice()

        assert "anonymous" in capsys.readouterr().err.lower()
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        assert repaired["notice_shown"] is True

    def test_nested_only_notice_shown_is_treated_as_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """notice_shown nested under another key is not visible at the top
        level -- same "no notion of nesting" principle as distinct_id."""
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        valid_id = str(uuid.uuid4())
        state_path.write_text(
            json.dumps({"distinct_id": valid_id, "wrapper": {"notice_shown": True}}),
            encoding="utf-8",
        )

        telemetry.show_first_run_notice()

        assert "anonymous" in capsys.readouterr().err.lower()
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        assert repaired["notice_shown"] is True
        assert repaired["distinct_id"] == valid_id

    def test_literal_true_notice_shown_is_not_reprinted(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """A genuinely valid, already-true notice_shown must not trigger a
        repair write -- only structurally invalid state should ever be
        rewritten."""
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        state_dir = tmp_path / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        valid_id = str(uuid.uuid4())
        original = json.dumps({"distinct_id": valid_id, "notice_shown": True})
        state_path.write_text(original, encoding="utf-8")

        telemetry.show_first_run_notice()

        assert capsys.readouterr().err == ""
        assert state_path.read_text(encoding="utf-8") == original  # untouched, no repair write


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
        # A local socket accepts a real telemetry request but never responds.
        # The child signals READY after imports, then starts telemetry and
        # waits on stdin. The parent also waits for actual request bytes before
        # signaling exit, so the measurement excludes startup and transport
        # setup while still catching a synchronous send that blocks the child.
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()[:2]
        stop = threading.Event()
        request_received = threading.Event()
        request_bytes = bytearray()
        server_errors: list[str] = []

        def accept_and_stall() -> None:
            server.settimeout(0.2)
            try:
                while not stop.is_set():
                    try:
                        conn, _addr = server.accept()
                    except TimeoutError:
                        continue
                    with conn:
                        conn.settimeout(0.2)
                        while not stop.is_set():
                            try:
                                data = conn.recv(65536)
                            except TimeoutError:
                                continue
                            if not data:
                                return
                            request_bytes.extend(data)
                            header_end = request_bytes.find(b"\r\n\r\n")
                            if header_end < 0:
                                continue
                            headers = bytes(request_bytes[:header_end]).split(b"\r\n")
                            request_line = headers[0]
                            content_length = next(
                                int(line.split(b":", 1)[1].strip())
                                for line in headers[1:]
                                if line.lower().startswith(b"content-length:")
                            )
                            body_start = header_end + 4
                            if (
                                request_line.startswith(b"POST /batch/")
                                and len(request_bytes) - body_start >= content_length
                            ):
                                request_received.set()
                                # Hold the connection open without sending a
                                # response. The child must still be able to exit.
                                stop.wait()
                                return
            except OSError as exc:
                if not stop.is_set():
                    server_errors.append(f"{type(exc).__name__}: {exc}")
            except (ValueError, StopIteration, IndexError) as exc:
                server_errors.append(f"invalid HTTP request: {type(exc).__name__}: {exc}")

        acceptor = threading.Thread(target=accept_and_stall, daemon=True)
        acceptor.start()
        child: subprocess.Popen[str] | None = None
        try:
            env = os.environ.copy()
            for key in ("DO_NOT_TRACK", "OUROBOROS_TELEMETRY", "OUROBOROS_POSTHOG_HOST"):
                env.pop(key, None)
            env["HOME"] = str(tmp_path)
            env["OUROBOROS_POSTHOG_API_KEY"] = "phc_test"
            env["OUROBOROS_POSTHOG_HOST"] = f"http://{host}:{port}"
            env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1"
            repo_root = Path(__file__).resolve().parents[2]
            env["PYTHONPATH"] = str(repo_root / "src")

            ready_file = tmp_path / "telemetry-child-ready"
            child_code = (
                "import sys\n"
                "from pathlib import Path\n"
                "from ouroboros import telemetry\n"
                "telemetry._HTTP_TIMEOUT_SECONDS = 30.0\n"
                "Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
                "telemetry.capture('workflow_outcome', {'command': 'run'})\n"
                "if sys.stdin.readline().strip() != 'exit':\n"
                "    raise SystemExit('parent exit signal was not received')\n"
                "assert telemetry._worker is not None and telemetry._worker.is_alive()\n"
                "assert telemetry._queue.unfinished_tasks > 0\n"
            )
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_code,
                    str(ready_file),
                ],
                env=env,
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            handshake_deadline = time.monotonic() + 10.0
            while time.monotonic() < handshake_deadline:
                if ready_file.exists() and request_received.is_set():
                    break
                if child.poll() is not None:
                    stderr = child.stderr.read() if child.stderr is not None else ""
                    raise AssertionError(
                        f"telemetry child exited before handshake ({child.returncode}): {stderr}"
                    )
                time.sleep(0.01)

            assert ready_file.exists(), "telemetry child did not signal startup completion"
            assert request_received.is_set(), (
                "local server did not receive a complete telemetry request"
            )
            assert time.monotonic() < handshake_deadline, (
                "telemetry handshake exceeded its 10s setup budget"
            )
            assert b"POST /batch/" in request_bytes, (
                "received bytes were not the PostHog batch request"
            )
            assert not server_errors, f"local stalled server failed: {server_errors}"

            assert child.stdin is not None
            start = time.monotonic()
            child.stdin.write("exit\n")
            child.stdin.flush()
            try:
                child.wait(timeout=1.2)
            except subprocess.TimeoutExpired:
                elapsed = time.monotonic() - start
                raise AssertionError(
                    f"child did not exit within 1.2s after request stalled ({elapsed:.2f}s)"
                ) from None
            elapsed = time.monotonic() - start
            stderr = child.stderr.read() if child.stderr is not None else ""
            assert child.returncode == 0, f"telemetry child exited {child.returncode}: {stderr}"
        finally:
            stop.set()
            if child is not None and child.poll() is None:
                if child.stdin is not None:
                    try:
                        child.stdin.write("exit\n")
                        child.stdin.flush()
                    except BrokenPipeError:
                        pass
                try:
                    child.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
            acceptor.join(timeout=2)
            server.close()
            if child is not None:
                for pipe in (child.stdin, child.stderr):
                    if pipe is not None and not pipe.closed:
                        pipe.close()
        assert not acceptor.is_alive(), "local stalled-server thread did not stop"
        assert elapsed < 1.2, f"process exit took {elapsed:.3f}s after exit signal"


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


class TestStateRepair:
    """Regression for repairing a corrupt/invalid telemetry.json.

    _publish_new_state's create-if-absent primitives (os.link / O_EXCL) both
    refuse when the target already exists, so neither can fix a file that is
    present but unparseable -- without _repair_state's lock-and-replace,
    every process would mint its own uuid on every call, forever, and no id
    would ever persist to disk.
    """

    def test_sequential_processes_converge_after_repair(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        state_dir = home / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        state_path.write_text("not-json{{{", encoding="utf-8")

        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(repo_root / "src")
        env["OUROBOROS_TELEMETRY"] = "0"
        for key in ("DO_NOT_TRACK", "OUROBOROS_POSTHOG_API_KEY", "OUROBOROS_POSTHOG_HOST"):
            env.pop(key, None)

        script = "from ouroboros import telemetry\nprint(telemetry.distinct_id())\n"

        first = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        second = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
        first_id = first.stdout.strip()
        second_id = second.stdout.strip()
        assert first_id, "first (repairing) process printed no id"
        assert first_id == second_id

        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["distinct_id"] == first_id

    def test_concurrent_processes_converge_after_repair(self, tmp_path: Path) -> None:
        n_procs = 8
        home = tmp_path / "home"
        state_dir = home / ".ouroboros"
        state_dir.mkdir(parents=True)
        state_path = state_dir / "telemetry.json"
        state_path.write_text("not-json{{{", encoding="utf-8")

        sentinel = tmp_path / "go"
        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(repo_root / "src")
        env["OUROBOROS_TELEMETRY"] = "0"
        for key in ("DO_NOT_TRACK", "OUROBOROS_POSTHOG_API_KEY", "OUROBOROS_POSTHOG_HOST"):
            env.pop(key, None)

        script = (
            "from ouroboros import telemetry\n"
            + _sentinel_wait_snippet(sentinel)
            + "print(telemetry.distinct_id())\n"
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

        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["distinct_id"] == outputs[0]


class TestNoticeRace:
    """Regression: concurrent first-use processes must print the notice once.

    Reading notice_shown=False out of telemetry.json is not enough to
    dedupe by itself -- every concurrent process can observe False before
    any of them has written the file. show_first_run_notice() must claim an
    exclusive marker so only the winner prints.
    """

    def test_concurrent_first_use_prints_notice_exactly_once(self, tmp_path: Path) -> None:
        n_procs = 8
        home = tmp_path / "home"
        home.mkdir()
        sentinel = tmp_path / "go"
        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["PYTHONPATH"] = str(repo_root / "src")
        env["OUROBOROS_POSTHOG_API_KEY"] = "phc_test"
        for key in ("DO_NOT_TRACK", "OUROBOROS_TELEMETRY", "OUROBOROS_POSTHOG_HOST"):
            env.pop(key, None)

        script = (
            "from ouroboros import telemetry\n"
            + _sentinel_wait_snippet(sentinel)
            + "telemetry.show_first_run_notice()\n"
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
            stderrs = []
            for proc in procs:
                out, err = proc.communicate(timeout=5)
                assert proc.returncode == 0, f"child failed: {err}"
                stderrs.append(err)
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()

        printed = [e for e in stderrs if "anonymous" in e.lower()]
        assert len(printed) == 1, (
            f"expected exactly one notice print, got {len(printed)}: {stderrs}"
        )


class TestVocabularies:
    """SSOT pairings for the closed labels folded into workflow_outcome."""

    def test_verify_causes_match_the_gate_ssot(self) -> None:
        from ouroboros.orchestrator.verify_gate_outcome import _VERIFY_GATE_CAUSES

        assert telemetry._AC_VERIFY_CAUSES == _VERIFY_GATE_CAUSES

    def test_drift_kinds_match_the_runtime_ssot(self) -> None:
        from ouroboros.orchestrator.runtime_drift import RUNTIME_DRIFT_KINDS

        assert telemetry._RUNTIME_DRIFT_KINDS == RUNTIME_DRIFT_KINDS

    def test_artifact_outcomes_match_the_product(self) -> None:
        from ouroboros.boundary.base_regression import ArtifactCheckOutcome

        assert {item.value for item in ArtifactCheckOutcome} == telemetry._ARTIFACT_CHECK_OUTCOMES


_NO_EVIDENCE_KW: dict[str, Any] = {
    "criterion_count": 4,
    "verification_coverage": "low",
    "surface": "cli_run",
    "check_package": "on",
    "check_package_status": "admitted",
    "runtime_backend": "codex",
}
_ARTIFACT_KW: dict[str, Any] = {
    "base_regression": "rejected",
    "worker_tests": "no_selected_files",
    "failed_criteria": 2,
    "criterion_count": 3,
    "repairs": 1,
    "surface": "cli_run",
    "runtime_backend": "codex",
}
_FOLDED_KEYS = frozenset(
    {
        "verify_rejections",
        "verify_top_cause",
        "runtime_drifts",
        "runtime_drift_kind",
        "no_evidence_criteria",
        "no_evidence_top_reason",
        "verification_coverage",
        "base_regression",
        "worker_tests",
        "artifact_failed_criteria",
    }
)


class TestOutcomeFolding:
    """Verify-gate, acceptance and drift observations ride on workflow_outcome."""

    def test_observations_outside_a_run_are_dropped(
        self, sent: list[dict[str, Any]], recorded: list[dict[str, int]]
    ) -> None:
        telemetry.capture_ac_verify_failed("exit_nonzero")
        telemetry.capture_runtime_drift("codex_config")
        telemetry.capture_acceptance_no_evidence(
            [("uncovered", "no_call_evidence")], **_NO_EVIDENCE_KW
        )
        telemetry.capture_acceptance_artifact_checks(**_ARTIFACT_KW)
        telemetry.flush(timeout=2.0)
        assert sent == []
        assert recorded == []

    def test_a_run_outcome_carries_its_folded_observations(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _no_ambient_frontdoor_or_ci(monkeypatch)
        with telemetry.outcome_scope(telemetry.new_outcome_tally()):
            for cause in ("exit_nonzero", "timeout", "exit_nonzero", "/private/seed.yaml: boom"):
                telemetry.capture_ac_verify_failed(cause)
            telemetry.capture_runtime_drift("/Users/private/codex was replaced")
            telemetry.capture_runtime_drift("cli_executable")
            telemetry.capture_runtime_drift("cli_executable")
            telemetry.capture_acceptance_no_evidence(
                [
                    ("uncovered", "no_verifier_verdict"),
                    ("no_binding", "transcript_unavailable"),
                    ("uncovered", None),
                ],
                **_NO_EVIDENCE_KW,
            )
            telemetry.capture_acceptance_artifact_checks(
                **{**_ARTIFACT_KW, "worker_tests": "/Users/private/tests/test_x.py"}
            )
            telemetry.capture_job_outcome(
                "job-1",
                "execute_seed",
                terminal_status="failed",
                result_meta={"failure_reason_code": "validation"},
            )
            telemetry.capture_job_outcome("job-2", "execute_seed", terminal_status="completed")
        telemetry.flush(timeout=2.0)

        first, second = sent
        assert {key: first["properties"][key] for key in _FOLDED_KEYS} == {
            "verify_rejections": 4,
            "verify_top_cause": "exit_nonzero",
            "runtime_drifts": 3,
            "runtime_drift_kind": "cli_executable",
            "no_evidence_criteria": 3,
            "no_evidence_top_reason": "uncovered",
            "verification_coverage": "low",
            "base_regression": "rejected",
            "worker_tests": "unknown",
            "artifact_failed_criteria": 2,
        }
        assert set(first["properties"]) <= telemetry._WORKFLOW_OUTCOME_KEYS
        # Drained: the next outcome in the same scope starts from nothing.
        assert _FOLDED_KEYS.isdisjoint(second["properties"])
        assert "private" not in json.dumps(sent)

    def test_ties_pick_the_alphabetically_first_label_and_counts_are_bounded(self) -> None:
        tally = telemetry.new_outcome_tally()
        with telemetry.outcome_scope(tally):
            telemetry.capture_ac_verify_failed("timeout")
            telemetry.capture_ac_verify_failed("exit_nonzero")
            telemetry.capture_acceptance_artifact_checks(
                **{**_ARTIFACT_KW, "failed_criteria": True}
            )
        props = tally.drain()
        assert props["verify_top_cause"] == "exit_nonzero"
        assert props["artifact_failed_criteria"] == 0

        for _ in range(10_001):
            tally.add_verify_rejection("timeout")
        tally.set_artifact_checks("passed", "passed", 10_001)
        props = tally.drain()
        assert props["verify_rejections"] == 10_000
        assert props["artifact_failed_criteria"] == 10_000

    def test_a_run_with_every_criterion_evidenced_adds_nothing(self) -> None:
        tally = telemetry.new_outcome_tally()
        with telemetry.outcome_scope(tally):
            telemetry.capture_acceptance_no_evidence([], **_NO_EVIDENCE_KW)
        assert tally.drain() == {}

    def test_tasks_and_threads_started_in_a_run_report_into_its_tally(self) -> None:
        async def child() -> None:
            telemetry.capture_ac_verify_failed("timeout")
            await asyncio.to_thread(telemetry.capture_runtime_drift, "codex_config")

        async def main() -> dict[str, Any]:
            tally = telemetry.new_outcome_tally()
            with telemetry.outcome_scope(tally):
                task = asyncio.create_task(child())
            # The scope has exited; the task still holds the run's binding.
            await task
            assert telemetry._OUTCOME_TALLY.get() is None
            return tally.drain()

        assert asyncio.run(main()) == {
            "verify_rejections": 1,
            "verify_top_cause": "timeout",
            "runtime_drifts": 1,
            "runtime_drift_kind": "codex_config",
        }

    def test_job_boundary_gives_each_job_its_own_tally(self, sent: list[dict[str, Any]]) -> None:
        from ouroboros.mcp.telemetry_boundary import JobTelemetryBoundary

        boundary = JobTelemetryBoundary()
        for job_id in ("outer", "inner", "untracked"):
            boundary.observe("mcp.job.created", job_id, {"job_type": "execute_seed"})

        async def inner_runner() -> None:
            telemetry.capture_ac_verify_failed("exit_nonzero")
            telemetry.capture_ac_verify_failed("exit_nonzero")

        async def outer_runner() -> None:
            telemetry.capture_ac_verify_failed("timeout")
            with boundary.runner_scope("inner"):
                inner = asyncio.create_task(inner_runner())
            await inner
            # Both terminals are observed inside the outer job's context: each
            # must drain only its own tally.
            boundary.observe("mcp.job.completed", "inner", {"status": "completed"})
            boundary.observe("mcp.job.completed", "untracked", {"status": "completed"})

        async def main() -> None:
            with boundary.runner_scope("outer"):
                outer = asyncio.create_task(outer_runner())
            await outer
            boundary.observe("mcp.job.failed", "outer", {"status": "failed"})

        asyncio.run(main())
        telemetry.flush(timeout=2.0)

        inner, untracked, outer = (event["properties"] for event in sent)
        assert (inner["verify_rejections"], inner["verify_top_cause"]) == (2, "exit_nonzero")
        assert _FOLDED_KEYS.isdisjoint(untracked)
        assert (outer["verify_rejections"], outer["verify_top_cause"]) == (1, "timeout")
        assert boundary._tallies == {}


def _exercise_every_capture_path() -> None:
    from ouroboros.mcp.telemetry_boundary import record_direct_evaluation_outcome

    for name in sorted(telemetry._CANONICAL_TOOL_NAMES | {"custom_tool"}):
        telemetry.capture_tool_call(name, ok=True, origin="session_context")
        telemetry.capture_tool_call(name, ok=False, error_type="TimeoutError")
        telemetry.capture_tool_call(name, ok=False, blocked=True)
        telemetry.capture_tool_call(name, ok=True, registered=False)
    for command in sorted(telemetry._CANONICAL_CLI_COMMANDS | {"acme-private"}):
        telemetry.capture_cli_command(command)
    telemetry.capture_cli_command(None)
    telemetry.capture_mcp_serve_started("stdio")
    telemetry.capture_subagent_dispatch({"phase": "emitted", "fanout_kind": "question_advisory"})
    telemetry.capture_subagent_dispatch({"phase": "submitted", "fanout_kind": "question_advisory"})
    telemetry.capture_ac_verify_failed("exit_nonzero")
    telemetry.capture_runtime_drift("codex_config")
    telemetry.capture_acceptance_no_evidence([("uncovered", "no_call_evidence")], **_NO_EVIDENCE_KW)
    telemetry.capture_acceptance_artifact_checks(**_ARTIFACT_KW)
    for job_type in sorted(telemetry._CANONICAL_JOB_TYPES | {"acme_private_job"}):
        telemetry.capture_job_outcome(f"job-{job_type}", job_type, terminal_status="completed")
    record_direct_evaluation_outcome(final_approved=True)
    for retired in (
        "command_run",
        "service_active",
        "mcp_serve_started",
        "subagent_dispatch",
        "ac_verify_failed",
        "runtime_drift",
        "acceptance_no_evidence",
        "acceptance_artifact_checks",
        "usage_daily",
        "install_started",
    ):
        telemetry.capture(retired, {"command": "run"})


class TestNetworkEventSet:
    """The complete set of event names that can reach the network."""

    def test_only_usage_daily_and_workflow_outcome_leave_the_process(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        assert {"usage_daily", "workflow_outcome"} == telemetry._NETWORK_EVENTS
        _on_day(monkeypatch, "2026-11-16")
        _exercise_every_capture_path()
        with telemetry.outcome_scope(telemetry.new_outcome_tally()):
            _exercise_every_capture_path()
        _next_process_on(monkeypatch, "2026-11-17")
        _exercise_every_capture_path()
        telemetry.flush(timeout=2.0)

        names = [event["event"] for event in sent]
        assert set(names) == telemetry._NETWORK_EVENTS
        assert names.count("usage_daily") == 1
        allowed = {
            "usage_daily": telemetry._USAGE_DAILY_KEYS,
            "workflow_outcome": telemetry._WORKFLOW_OUTCOME_KEYS,
        }
        for event in sent:
            assert set(event["properties"]) <= allowed[event["event"]]
        assert "acme" not in json.dumps(sent)

    def test_installers_send_only_the_install_funnel(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        install_sh = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
        pings = set(re.findall(r"^\s*_telemetry_ping (\w+)", install_sh, flags=re.MULTILINE))
        assert pings == {"install_started", "install_completed"}
        install_ps1 = (repo_root / "scripts" / "install.ps1").read_text(encoding="utf-8")
        assert "posthog" not in install_ps1.lower().replace("emits no installer events", "")

    def test_only_the_telemetry_module_holds_the_posthog_destination(self) -> None:
        package_root = Path(telemetry.__file__).resolve().parent
        holders = {
            path.relative_to(package_root).as_posix()
            for path in package_root.rglob("*.py")
            if "posthog.com" in path.read_text(encoding="utf-8")
        }
        assert holders == {"telemetry.py"}


# Every telemetry entry point that a process reaches when it does anything
# (CLI command, MCP tool call of any kind, MCP server start, fan-out, run
# observation, run outcome, console request via note_activity), called the
# way that adds no count wherever such a way exists.
_ACTIVITY_ENTRY_POINTS: dict[str, Callable[[], None]] = {
    "capture": lambda: telemetry.capture("workflow_outcome", dict(_OUTCOME)),
    "capture_ac_verify_failed": lambda: telemetry.capture_ac_verify_failed("unknown"),
    "capture_acceptance_artifact_checks": lambda: telemetry.capture_acceptance_artifact_checks(
        base_regression=None,
        worker_tests=None,
        failed_criteria=0,
        criterion_count=0,
        repairs=0,
        surface=None,
        runtime_backend=None,
    ),
    "capture_acceptance_no_evidence": lambda: telemetry.capture_acceptance_no_evidence(
        [],
        criterion_count=0,
        verification_coverage=None,
        surface=None,
        check_package=None,
        check_package_status=None,
        runtime_backend=None,
    ),
    "capture_cli_command": lambda: telemetry.capture_cli_command("mcp"),
    "capture_job_outcome": lambda: telemetry.capture_job_outcome(
        "job_1", "run", terminal_status="completed"
    ),
    "capture_mcp_serve_started": lambda: telemetry.capture_mcp_serve_started("stdio"),
    "capture_runtime_drift": lambda: telemetry.capture_runtime_drift("unknown"),
    "capture_subagent_dispatch": lambda: telemetry.capture_subagent_dispatch(
        {"phase": "submitted"}
    ),
    "capture_tool_call": lambda: telemetry.capture_tool_call("ouroboros_job_wait", ok=True),
    "note_activity": lambda: telemetry.note_activity(),
}


class TestUsageRollup:
    """Commands are counted locally and sent as one usage_daily row per day."""

    def test_counted_tools_record_command_and_surface(self, recorded: list[dict[str, int]]) -> None:
        telemetry.capture_tool_call("ouroboros_start_execute_seed", ok=True)
        telemetry.capture_tool_call("ouroboros_lateral_think", ok=True)
        telemetry.capture_tool_call("ouroboros_cancel_job", ok=True)
        assert recorded == [
            {"cmd_run": 1, "surface_mcp": 1},
            {"cmd_unstuck": 1, "surface_mcp": 1},
            {"cmd_cancel": 1, "surface_mcp": 1},
        ]

    @pytest.mark.parametrize("name", sorted(telemetry._UNCOUNTED_TOOLS))
    def test_polling_status_and_read_only_tools_are_not_usage(
        self, recorded: list[dict[str, int]], name: str
    ) -> None:
        for _ in range(50):  # a host pumping job_wait through a long run
            telemetry.capture_tool_call(name, ok=True)
        # A poll on a failed job returns is_error (a logical block), not an error.
        telemetry.capture_tool_call(name, ok=False, blocked=True)
        assert recorded == []

    def test_unregistered_and_extension_tools_are_not_usage(
        self, recorded: list[dict[str, int]]
    ) -> None:
        telemetry.capture_tool_call("custom_tool", ok=True)
        telemetry.capture_tool_call("ouroboros_interview", ok=True, registered=False)
        assert recorded == []

    def test_errors_count_for_any_tool_but_logical_blocks_do_not(
        self, recorded: list[dict[str, int]]
    ) -> None:
        telemetry.capture_tool_call("ouroboros_job_wait", ok=False, error_type="MCPToolError")
        telemetry.capture_tool_call("ouroboros_interview", ok=False, error_type="TimeoutError")
        telemetry.capture_tool_call("ouroboros_generate_seed", ok=False, blocked=True)
        assert recorded == [
            {"mcp_tool_errors": 1},
            {"cmd_interview": 1, "surface_mcp": 1, "mcp_tool_errors": 1},
            {"cmd_seed": 1, "surface_mcp": 1},
        ]

    def test_seed_origin_counts_only_the_interview_less_entrances(
        self, recorded: list[dict[str, int]]
    ) -> None:
        for origin in ("interview", "session_context", "session_context_gap", "/private/x", None):
            telemetry.capture_tool_call("ouroboros_generate_seed", ok=True, origin=origin)
        telemetry.capture_tool_call(
            "ouroboros_start_execute_seed", ok=True, origin="session_context"
        )
        assert recorded == [
            {"cmd_seed": 1, "surface_mcp": 1},
            {"cmd_seed": 1, "surface_mcp": 1, "seed_session_context": 1},
            {"cmd_seed": 1, "surface_mcp": 1, "seed_session_context_gap": 1},
            {"cmd_seed": 1, "surface_mcp": 1},
            {"cmd_seed": 1, "surface_mcp": 1},
            {"cmd_run": 1, "surface_mcp": 1},
        ]

    def test_every_shipped_tool_is_classified(self) -> None:
        counted = set(telemetry._USAGE_TOOLS)
        assert counted.isdisjoint(telemetry._UNCOUNTED_TOOLS)
        assert counted | telemetry._UNCOUNTED_TOOLS == telemetry._CANONICAL_TOOL_NAMES

    def test_mcp_starts_and_fanout_are_daily_counts(self, recorded: list[dict[str, int]]) -> None:
        telemetry.capture_mcp_serve_started("stdio")
        telemetry.capture_mcp_serve_started("/private/odd")
        telemetry.capture_subagent_dispatch({"phase": "emitted", "fanout_kind": "acme_private"})
        telemetry.capture_subagent_dispatch({"phase": "submitted", "fanout_kind": "x"})
        assert recorded == [
            {"mcp_serve_starts": 1},
            {"mcp_serve_starts": 1},
            {"subagent_dispatches": 1},
        ]

    def test_unknown_keys_and_bad_values_are_never_recorded(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry._record_usage({"cmd_acme_private": 1, "cmd_run": True, "cmd_seed": -1})
        assert not _rollup_path().exists()

    def test_nothing_is_recorded_or_sent_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        posted: list[dict[str, Any]] = []
        monkeypatch.setattr(telemetry, "_post", posted.extend)
        monkeypatch.setenv("OUROBOROS_POSTHOG_API_KEY", "phc_test")
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
        telemetry.capture_tool_call("ouroboros_interview", ok=True)
        telemetry.capture_cli_command("run")
        telemetry.flush(timeout=1.0)
        assert not _rollup_path().exists()
        assert posted == []

    def test_a_server_up_past_midnight_sends_the_day_on_a_status_poll(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        # One MCP server process: a counted call on day 1, then only
        # successful polls (counted as nothing) once the day is over.
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_tool_call("ouroboros_interview", ok=True)
        telemetry.flush(timeout=2.0)
        _on_day(monkeypatch, "2026-11-17")
        telemetry.capture_tool_call("ouroboros_job_wait", ok=True)
        telemetry.capture_tool_call("ouroboros_session_status", ok=True)
        telemetry.flush(timeout=2.0)

        (row,) = sent
        assert (row["event"], row["properties"]["day"]) == ("usage_daily", "2026-11-16")
        assert row["properties"]["cmd_interview"] == 1
        state = json.loads(_rollup_path().read_text(encoding="utf-8"))
        assert (state["days"], state["sending"], state["sent"]) == ({}, {}, ["2026-11-16"])

    def test_every_activity_entry_point_is_listed(self) -> None:
        public = {name for name in telemetry.__all__ if name.startswith("capture")}
        assert public | {"note_activity"} == set(_ACTIVITY_ENTRY_POINTS)

    @pytest.mark.parametrize("entry", sorted(_ACTIVITY_ENTRY_POINTS))
    def test_every_activity_entry_point_sends_a_finished_day_once(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]], entry: str
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        _ACTIVITY_ENTRY_POINTS[entry]()
        _ACTIVITY_ENTRY_POINTS[entry]()
        telemetry.flush(timeout=2.0)

        usage = [event for event in sent if event["event"] == "usage_daily"]
        assert [(e["properties"]["day"], e["properties"]["cmd_run"]) for e in usage] == [
            ("2026-11-16", 1)
        ]

    def test_activity_while_opted_out_sends_and_claims_nothing(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        monkeypatch.setenv("OUROBOROS_TELEMETRY", "0")
        for entry in _ACTIVITY_ENTRY_POINTS.values():
            entry()
        telemetry.flush(timeout=2.0)

        assert sent == []
        state = json.loads(_rollup_path().read_text(encoding="utf-8"))
        assert (list(state["days"]), state["sending"]) == (["2026-11-16"], {})

    def test_polls_check_the_rollup_once_a_day_and_post_nothing_when_none_is_due(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-17")
        claims: list[tuple[Any, ...]] = []
        enabled_checks: list[bool] = []
        real_claim, real_enabled = telemetry_rollup.claim_due, telemetry.is_enabled

        def counting_claim(*args: Any) -> list[dict[str, Any]]:
            claims.append(args)
            return real_claim(*args)

        def counting_enabled() -> bool:
            enabled_checks.append(True)
            return real_enabled()

        monkeypatch.setattr(telemetry_rollup, "claim_due", counting_claim)
        monkeypatch.setattr(telemetry, "is_enabled", counting_enabled)
        for _ in range(50):
            telemetry.capture_tool_call("ouroboros_job_wait", ok=True)
        telemetry.flush(timeout=2.0)

        # After the first poll of the day, a poll neither reads the
        # configuration nor touches the rollup file.
        assert (len(claims), len(enabled_checks)) == (1, 1)
        assert sent == []

    def test_a_day_is_sent_once_after_it_ends(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        for _ in range(3):
            telemetry.capture_tool_call("ouroboros_interview", ok=True)
        telemetry.flush(timeout=2.0)
        assert sent == []  # nothing per call, and nothing for today

        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.capture_cli_command("status")
        _capture_outcome()
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")  # a new process finds nothing left
        telemetry.flush(timeout=2.0)

        rows = [event for event in sent if event["event"] == "usage_daily"]
        (row,) = rows
        assert row["timestamp"] == "2026-11-16T12:00:00Z"
        assert row["properties"]["cmd_interview"] == 3
        assert row["properties"]["day"] == "2026-11-16"
        assert row["distinct_id"] == telemetry.distinct_id()
        assert row["uuid"] == telemetry_rollup.event_uuid(telemetry.distinct_id(), "2026-11-16")
        state = json.loads(_rollup_path().read_text(encoding="utf-8"))
        assert state["sending"] == {}
        assert list(state["days"]) == ["2026-11-17"]
        assert state["days"]["2026-11-17"]["counts"]["cmd_status"] == 3

    def test_a_failed_delivery_is_retried_under_the_same_uuid(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        attempts: list[dict[str, Any]] = []

        def offline(batch: list[dict[str, Any]]) -> None:
            attempts.extend(batch)
            raise OSError("offline")

        delivered = telemetry._post
        monkeypatch.setattr(telemetry, "_post", offline)
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)
        assert [event["event"] for event in attempts] == ["usage_daily"]

        monkeypatch.setattr(telemetry, "_post", delivered)
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)

        (row,) = sent
        assert row["uuid"] == attempts[0]["uuid"]
        assert row["properties"]["cmd_run"] == 1

    def test_a_queue_overflow_releases_the_day_for_retry(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        full: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        full.put_nowait({"event": "placeholder"})
        real_queue = telemetry._queue
        monkeypatch.setattr(telemetry, "_queue", full)
        telemetry.capture_cli_command("status")
        monkeypatch.setattr(telemetry, "_queue", real_queue)
        state = json.loads(_rollup_path().read_text(encoding="utf-8"))
        (released,) = state["sending"].values()
        assert released["lease_until"] == 0.0

        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)
        assert [event["properties"]["day"] for event in sent] == ["2026-11-16"]

    def test_skipped_days_send_only_days_with_activity(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-13")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)
        assert [event["properties"]["day"] for event in sent] == ["2026-11-13"]

    def test_a_clock_moved_back_keeps_the_later_day_until_it_is_over(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-18")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        _next_process_on(monkeypatch, "2026-11-18")
        telemetry.capture_cli_command("status")
        _next_process_on(monkeypatch, "2026-11-19")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)
        # 11-18 is not sent while the clock reads 11-17; the counts made on
        # 11-18 itself join it, and each day is sent once after it ends.
        rows = {event["properties"]["day"]: event for event in sent}
        assert sorted(event["properties"]["day"] for event in sent) == ["2026-11-17", "2026-11-18"]
        assert rows["2026-11-18"]["properties"]["cmd_run"] == 1
        assert rows["2026-11-18"]["properties"]["cmd_status"] == 1
        resolved = telemetry.distinct_id()
        assert [rows[day]["uuid"] for day in sorted(rows)] == [
            telemetry_rollup.event_uuid(resolved, day) for day in sorted(rows)
        ]

    def test_a_clock_40_days_behind_keeps_the_unsent_day(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-10-07")  # a clock 40 days behind
        telemetry.capture_cli_command("status")
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)

        rows = [event for event in sent if event["event"] == "usage_daily"]
        assert [(row["properties"]["day"], row["properties"]["cmd_run"]) for row in rows] == [
            ("2026-11-16", 1)
        ]

    def test_a_late_count_for_a_sent_day_never_makes_a_second_row(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")  # claims and sends 11-16
        _next_process_on(monkeypatch, "2026-11-16")  # a clock still on the old day
        telemetry.capture_cli_command("run")  # dropped: 11-16 is closed
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)

        (row,) = [event for event in sent if event["event"] == "usage_daily"]
        assert row["properties"]["day"] == "2026-11-16"
        assert row["properties"]["cmd_run"] == 1
        state = json.loads(_rollup_path().read_text(encoding="utf-8"))
        assert sorted(state["days"]) == ["2026-11-17"]
        assert state["sent"] == ["2026-11-16"]

    def test_a_day_rebuilt_after_the_file_was_lost_reuses_its_uuid(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)
        _rollup_path().unlink()  # loses the record that 11-16 was sent
        _next_process_on(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)

        rows = [event for event in sent if event["event"] == "usage_daily"]
        assert [row["properties"]["day"] for row in rows] == ["2026-11-16", "2026-11-16"]
        # Both sends carry the same uuid, so PostHog keeps one row for the day.
        expected = telemetry_rollup.event_uuid(telemetry.distinct_id(), "2026-11-16")
        assert [row["uuid"] for row in rows] == [expected, expected]

    def test_no_identity_keeps_the_day_for_a_later_process(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        telemetry.capture_cli_command("run")
        _next_process_on(monkeypatch, "2026-11-17")
        real_distinct_id = telemetry.distinct_id
        monkeypatch.setattr(telemetry, "distinct_id", lambda: "")
        telemetry.capture_cli_command("status")
        telemetry.flush(timeout=2.0)
        assert sent == []
        monkeypatch.setattr(telemetry, "distinct_id", real_distinct_id)
        telemetry.capture_cli_command("status")  # the same process tries again
        telemetry.flush(timeout=2.0)
        assert [event["properties"]["day"] for event in sent] == ["2026-11-16"]

    def test_concurrent_processes_send_a_day_exactly_once(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        _on_day(monkeypatch, "2026-11-16")
        for _ in range(5):
            telemetry.capture_tool_call("ouroboros_start_auto", ok=True)
        sentinel = tmp_path / "go"
        out_dir = tmp_path / "posted"
        out_dir.mkdir()
        child_code = (
            "import json, os, sys\n"
            + _sentinel_wait_snippet(sentinel)
            + "from ouroboros import telemetry\n"
            "telemetry._utc_day = lambda: '2026-11-17'\n"
            "out = Path(sys.argv[1])\n"
            "def post(batch):\n"
            "    with out.open('a', encoding='utf-8') as handle:\n"
            "        for event in batch:\n"
            "            handle.write(json.dumps(event) + '\\n')\n"
            "telemetry._post = post\n"
            "telemetry.capture_cli_command('status')\n"
            "telemetry.flush(timeout=5.0)\n"
        )
        env = os.environ.copy()
        for key in ("DO_NOT_TRACK", "OUROBOROS_TELEMETRY"):
            env.pop(key, None)
        env["HOME"] = str(tmp_path)
        env["OUROBOROS_POSTHOG_API_KEY"] = "phc_test"
        repo_root = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = str(repo_root / "src")
        children = [
            subprocess.Popen(
                [sys.executable, "-c", child_code, str(out_dir / f"{index}.jsonl")],
                env=env,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(6)
        ]
        sentinel.write_text("go", encoding="utf-8")
        for child in children:
            _, stderr = child.communicate(timeout=60)
            assert child.returncode == 0, stderr

        rows = [
            json.loads(line)
            for path in out_dir.glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        usage = [row for row in rows if row["event"] == "usage_daily"]
        assert len(usage) == 1
        assert usage[0]["properties"]["cmd_auto"] == 5
        state = json.loads(_rollup_path().read_text(encoding="utf-8"))
        assert state["sending"] == {}
        assert state["days"]["2026-11-17"]["counts"]["cmd_status"] == 6
