"""Unit tests for anonymous usage telemetry (src/ouroboros/telemetry.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import random
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

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

        telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 1})
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

        telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 1})
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
            telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 1})
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
            telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 1})
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
            telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 1})
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
            telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 1})
            telemetry.flush(timeout=2.0)
            assert sent == []
            assert telemetry.distinct_id() == ""
        finally:
            os.chmod(state_dir, 0o700)

        telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 2})
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        persisted_id = sent[0]["distinct_id"]
        assert telemetry._UUID_PATTERN.fullmatch(persisted_id)
        persisted = json.loads((state_dir / "telemetry.json").read_text(encoding="utf-8"))
        assert persisted["distinct_id"] == persisted_id


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

    def test_hostile_tool_name_is_replaced_not_forwarded(self, sent: list[dict[str, Any]]) -> None:
        """A caller-controlled name that merely starts with "ouroboros_" is
        not automatically a real registered tool -- e.g. a path smuggled
        past a boundary bug upstream. capture_tool_call must never let it
        become the `tool`/`command` property value."""
        hostile = "ouroboros_/home/alice/private-project"
        telemetry.capture_tool_call(hostile, ok=False, error_type="KeyError")
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        event = sent[0]
        assert event["properties"]["tool"] == "ouroboros_unknown_tool"
        assert event["properties"]["command"] == "unknown_tool"
        # Assert on the full serialized event, not just the two fields
        # above -- proves the hostile string can't leak through any other
        # property (e.g. error_type) either.
        assert "alice" not in repr(event)
        assert "private-project" not in repr(event)
        assert hostile not in repr(event)

    def test_hostile_tool_name_variants_are_all_replaced(self, sent: list[dict[str, Any]]) -> None:
        """Uppercase, dots, and other non-snake-case punctuation are just as
        invalid as a path -- the charset gate is exact, not a blocklist of
        specific dangerous characters."""
        for hostile in ("ouroboros_Evil.Tool", "ouroboros_evil tool", "ouroboros_EVIL_TOOL!"):
            telemetry.capture_tool_call(hostile, ok=True)
        telemetry.flush(timeout=2.0)

        assert len(sent) == 3
        for event in sent:
            assert event["properties"]["tool"] == "ouroboros_unknown_tool"
            assert event["properties"]["command"] == "unknown_tool"

    def test_canonical_tool_name_passes_through_unchanged(self, sent: list[dict[str, Any]]) -> None:
        """The backstop must not false-positive on real tool names -- a
        canonical, registered name still gets its funnel mapping."""
        telemetry.capture_tool_call("ouroboros_interview", ok=True)
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        assert sent[0]["properties"]["tool"] == "ouroboros_interview"
        assert sent[0]["properties"]["command"] == "interview"
        assert sent[0]["properties"]["is_funnel"] is True

    def test_overlong_tool_name_is_replaced(self, sent: list[dict[str, Any]]) -> None:
        """A name that is otherwise charset-valid but exceeds the length
        bound (no real tool name is anywhere close to 64 chars) is still
        not a real tool -- must not pass through as free text."""
        overlong = "ouroboros_" + ("a" * 65)
        telemetry.capture_tool_call(overlong, ok=True)
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        assert sent[0]["properties"]["tool"] == "ouroboros_unknown_tool"
        assert sent[0]["properties"]["command"] == "unknown_tool"
        assert overlong not in repr(sent[0])

    def test_registered_extension_tool_name_is_replaced_not_forwarded(
        self, sent: list[dict[str, Any]]
    ) -> None:
        """A name that is charset-valid AND shaped exactly like a real tool
        -- but isn't one this project actually ships -- is a REGISTERED
        extension/custom tool (register_tool() accepts arbitrary names), not
        a lookup failure. It must still never carry an identifying name
        through `tool`/`command`."""
        hostile = "ouroboros_acme_private_project"
        telemetry.capture_tool_call(hostile, ok=True)
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        event = sent[0]
        assert event["properties"]["tool"] == "ouroboros_extension_tool"
        assert event["properties"]["command"] == "extension_tool"
        assert "acme" not in repr(event)
        assert "private_project" not in repr(event)
        assert hostile not in repr(event)

    def test_unknown_and_extension_literals_stay_distinct(self, sent: list[dict[str, Any]]) -> None:
        """ouroboros_unknown_tool (charset-invalid -- a lookup failure) and
        ouroboros_extension_tool (charset-valid but not audited -- a real
        registered non-Ouroboros tool) must not collapse into each other:
        the literal ouroboros_unknown_tool itself is charset-valid and not
        in the canonical set, so the extension-tool check must special-case
        it rather than re-mapping it a second time."""
        telemetry.capture_tool_call(telemetry._UNKNOWN_TOOL_NAME, ok=True)
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        assert sent[0]["properties"]["tool"] == "ouroboros_unknown_tool"
        assert sent[0]["properties"]["command"] == "unknown_tool"

    def test_canonical_tool_names_are_subsets_of_the_audited_set(self) -> None:
        """The funnel/polling/submission maps must never name a tool that
        isn't in the audited canonical set -- if they drift apart, a real
        tool could silently start getting extension-tool-mangled, or a
        typo'd/renamed entry in one of the smaller sets would go unnoticed."""
        assert set(telemetry._TOOL_FUNNEL) <= telemetry._CANONICAL_TOOL_NAMES
        assert telemetry._POLLING_TOOLS <= telemetry._CANONICAL_TOOL_NAMES
        assert telemetry._ASYNC_SUBMISSION_TOOLS <= telemetry._CANONICAL_TOOL_NAMES

    def test_polling_tools_sampled(self, sent: list[dict[str, Any]]) -> None:
        # Sampling is a per-call random draw (see telemetry._poll_rng), not a
        # deterministic 1-in-50 counter, so the batch needs a seed that is
        # known (precomputed offline) to produce exactly one hit in 50 draws.
        telemetry._poll_rng.seed(1)
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
        telemetry._poll_rng.seed(1)  # see test_polling_tools_sampled
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

    def test_extension_job_type_is_replaced_not_forwarded(self, sent: list[dict[str, Any]]) -> None:
        """job_type is caller-controlled the same way an MCP tool name is:
        JobManager.start_job() accepts an arbitrary string, so an unaudited
        job type must never carry an identifying name through `command`."""
        hostile = "acme_private_project"
        telemetry.capture_job_outcome(
            "job-1", hostile, terminal_status="completed", result_meta={"final_approved": True}
        )
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        event = sent[0]
        assert event["properties"]["command"] == "extension_job"
        assert hostile not in repr(event)

    def test_extension_job_type_never_earns_verified(self, sent: list[dict[str, Any]]) -> None:
        """An extension job must not earn verified=true just because it
        isn't literally "evaluate" -- `verified` compares the RAW job_type,
        never the folded `command`, and folding must not change that."""
        telemetry.capture_job_outcome(
            "job-1",
            "acme_private_project",
            terminal_status="completed",
            result_meta={"final_approved": True},
        )
        telemetry.flush(timeout=2.0)

        assert sent[0]["properties"]["verified"] is False
        assert sent[0]["properties"]["command"] == "extension_job"

    @pytest.mark.parametrize(
        ("job_type", "expected_command"),
        (
            ("execute_seed", "run"),
            ("evolve_step", "evolve"),
            ("auto", "auto"),
            ("evaluate", "evaluate"),
            ("ralph", "ralph"),
        ),
    )
    def test_shipped_job_types_keep_their_funnel_commands(
        self,
        sent: list[dict[str, Any]],
        job_type: str,
        expected_command: str,
    ) -> None:
        telemetry.capture_job_outcome("job-1", job_type, terminal_status="completed")
        telemetry.flush(timeout=2.0)
        assert sent[0]["properties"]["command"] == expected_command

    @pytest.mark.parametrize("job_type", ("detached_probe", "detached_nested_probe"))
    def test_internal_shipped_job_types_pass_through_unfolded(
        self,
        sent: list[dict[str, Any]],
        job_type: str,
    ) -> None:
        """detached_worker.py's process-lifetime probes are shipped and
        non-identifying -- audited into the canonical set so they aren't
        mislabeled as a foreign extension_job."""
        telemetry.capture_job_outcome("job-1", job_type, terminal_status="completed")
        telemetry.flush(timeout=2.0)
        assert sent[0]["properties"]["command"] == job_type

    def test_job_funnel_keys_are_subset_of_canonical_job_types(self) -> None:
        assert set(telemetry._JOB_FUNNEL) <= telemetry._CANONICAL_JOB_TYPES

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
            "command_run",
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
        telemetry.capture("command_run", {"command": "run"})
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        assert "prompt" not in props
        assert props["runtime_backend"] == "claude"

    def test_oversized_string_property_dropped(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture(
            "command_run",
            {"command": "run", "error_type": "E" * (telemetry._MAX_PROPERTY_STR_LEN + 1)},
        )
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        assert props["command"] == "run"
        assert "error_type" not in props

    def test_non_scalar_property_dropped(self, sent: list[dict[str, Any]]) -> None:
        telemetry.capture(
            "command_run",
            {"command": "run", "tool": {"nested": "value"}, "sample_rate": [1, 2]},
        )
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        assert props["command"] == "run"
        assert "tool" not in props
        assert "sample_rate" not in props


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
    telemetry.capture_cli_command("run")
    telemetry.flush(timeout=2.0)
    assert len(sent) == 1
    assert sent[0]["properties"]["ci"] is True


class TestExactPropertySets:
    """Regression: the declared TELEMETRY.md table and the executable
    per-variant allowlist must match key-for-key, not just "subset of a
    generic base+context union". A prior round's union-based allowlist over-
    granted every event every base+context key regardless of what that
    event's row in TELEMETRY.md actually declared (e.g. mcp_serve_started
    picked up python_version and all 4 backend fields; the two command_run
    variants were indistinguishable).
    """

    def test_command_run_mcp_completion_exact_keys(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _no_ambient_frontdoor_or_ci(monkeypatch)
        telemetry.set_context(
            runtime_backend="claude",
            execute_runtime_backend="claude",
            interview_llm_backend="anthropic",
            evaluate_llm_backend="anthropic",
        )
        telemetry.capture_tool_call(
            "ouroboros_evaluate", ok=True, duration_ms=12.3, error_type="TimeoutError"
        )
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        keys = set(props)

        assert keys <= telemetry._COMMAND_RUN_MCP_KEYS
        assert keys == {
            "command",
            "tool",
            "source",
            "is_funnel",
            "phase",
            "ok",
            "duration_ms",
            "error_type",
            "runtime_backend",
            "execute_runtime_backend",
            "interview_llm_backend",
            "evaluate_llm_backend",
            "app_version",
            "os",
            "python_version",
        }
        assert "accepted" not in props
        assert "sample_rate" not in props

    def test_command_run_mcp_submission_exact_keys(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _no_ambient_frontdoor_or_ci(monkeypatch)
        telemetry.capture_tool_call("ouroboros_start_execute_seed", ok=True)
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        keys = set(props)

        assert keys <= telemetry._COMMAND_RUN_MCP_KEYS
        assert keys == {
            "command",
            "tool",
            "source",
            "is_funnel",
            "phase",
            "accepted",
            "app_version",
            "os",
            "python_version",
        }
        assert "ok" not in props  # submission receipts have accepted, never ok
        assert props["accepted"] is True
        assert props["phase"] == "submission"

    def test_command_run_cli_exact_keys_even_with_context_set(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _no_ambient_frontdoor_or_ci(monkeypatch)
        # Global context carries backend keys regardless of source -- the cli
        # variant must strip them even though they're genuinely set, proving
        # this is per-variant enforcement and not just "nobody happened to
        # set context in this test".
        telemetry.set_context(
            runtime_backend="claude",
            execute_runtime_backend="claude",
            interview_llm_backend="anthropic",
            evaluate_llm_backend="anthropic",
        )
        telemetry.capture_cli_command("run")
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        keys = set(props)

        assert keys <= telemetry._COMMAND_RUN_CLI_KEYS
        assert keys == {"command", "source", "is_funnel", "app_version", "os", "python_version"}
        for undeclared in (
            "tool",
            "phase",
            "ok",
            "accepted",
            "duration_ms",
            "error_type",
            "sample_rate",
            "runtime_backend",
            "execute_runtime_backend",
            "interview_llm_backend",
            "evaluate_llm_backend",
        ):
            assert undeclared not in props, f"cli command_run leaked {undeclared!r}"

    def test_workflow_outcome_exact_keys(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _no_ambient_frontdoor_or_ci(monkeypatch)
        telemetry.set_context(
            runtime_backend="claude",
            execute_runtime_backend="claude",
            interview_llm_backend="anthropic",
            evaluate_llm_backend="anthropic",
        )
        telemetry.capture_job_outcome(
            "job-x", "evaluate", terminal_status="completed", result_meta={"final_approved": True}
        )
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        keys = set(props)

        assert keys <= telemetry._WORKFLOW_OUTCOME_KEYS
        assert keys == {
            "command",
            "phase",
            "terminal_status",
            "ok",
            "verified",
            "final_approved",
            "$insert_id",
            "runtime_backend",
            "execute_runtime_backend",
            "interview_llm_backend",
            "evaluate_llm_backend",
            "app_version",
            "os",
            "python_version",
        }

    def test_mcp_serve_started_exact_keys_excludes_python_version_and_backends(
        self, monkeypatch: pytest.MonkeyPatch, sent: list[dict[str, Any]]
    ) -> None:
        _no_ambient_frontdoor_or_ci(monkeypatch)
        # Set context anyway -- mcp_serve_started must drop it regardless.
        telemetry.set_context(
            runtime_backend="claude",
            execute_runtime_backend="claude",
            interview_llm_backend="anthropic",
            evaluate_llm_backend="anthropic",
        )
        telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 12})
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        props = sent[0]["properties"]
        keys = set(props)

        assert keys <= telemetry._MCP_SERVE_STARTED_KEYS
        assert keys == {"transport", "tool_count", "app_version", "os"}
        assert "python_version" not in props
        for backend_key in telemetry._CONTEXT_ALLOWLIST:
            assert backend_key not in props


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

    def test_dynamic_plugin_command_is_replaced_not_forwarded(
        self, sent: list[dict[str, Any]]
    ) -> None:
        """subcommand is caller-controlled the same way an MCP tool name or
        a job_type is: _PluginAwareGroup resolves any name not in the
        static command table as a dynamically-installed plugin command, so
        an unaudited value must never carry an identifying name through
        `command`."""
        hostile = "acme-private-project"
        telemetry.capture_cli_command(hostile)
        telemetry.flush(timeout=2.0)

        assert len(sent) == 1
        event = sent[0]
        assert event["properties"]["command"] == "extension_command"
        assert event["properties"]["is_funnel"] is False
        assert hostile not in repr(event)

    def test_canonical_cli_command_passes_through_unchanged(
        self, sent: list[dict[str, Any]]
    ) -> None:
        telemetry.capture_cli_command("run")
        telemetry.flush(timeout=2.0)
        assert sent[0]["properties"]["command"] == "run"

    def test_cli_funnel_and_is_funnel_tuple_are_subsets_of_canonical_set(self) -> None:
        assert set(telemetry._CLI_FUNNEL) <= telemetry._CANONICAL_CLI_COMMANDS
        is_funnel_names = {"auto", "init", "interview", "seed", "run", "qa", "pm"}
        assert is_funnel_names <= telemetry._CANONICAL_CLI_COMMANDS


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

        telemetry.capture("mcp_serve_started", {"transport": "stdio", "tool_count": 1})
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


class TestUnbiasedPollSampling:
    """Regression: polling-tool sampling must stay 1/50 per call, not per
    process (telemetry._poll_rng, replacing a per-process call counter).

    A per-process counter always samples call #1, so a short-lived MCP
    process that polls once or twice before exiting would be captured at
    close to 1:1 while still reporting `sample_rate=50` -- an up-to-50x
    over-count concentrated in exactly the processes least representative of
    steady-state usage. An independent per-call random draw keeps the
    probability 1/50 regardless of how long a given process lives.
    """

    def test_first_call_is_not_structurally_captured(self, sent: list[dict[str, Any]]) -> None:
        # Precomputed offline: random.Random(1).random() == 0.1344 >= 1/50,
        # so the very first polling draw with this seed is a miss. A
        # per-process counter would have captured it regardless (call #1
        # always sampled) -- this is exactly the bias being fixed.
        telemetry._poll_rng.seed(1)
        telemetry.capture_tool_call("ouroboros_job_status", ok=True)
        telemetry.flush(timeout=2.0)
        assert sent == []

    def test_seeded_draw_below_threshold_is_captured(self, sent: list[dict[str, Any]]) -> None:
        # Precomputed offline: random.Random(31).random() == 0.0123 < 1/50.
        telemetry._poll_rng.seed(31)
        telemetry.capture_tool_call("ouroboros_job_status", ok=True)
        telemetry.flush(timeout=2.0)
        assert len(sent) == 1
        assert sent[0]["properties"]["sample_rate"] == telemetry._POLL_SAMPLE_RATE

    def test_multi_process_sampling_matches_precomputed_expectation(self, tmp_path: Path) -> None:
        """Fresh processes must NOT all capture their first polling call.

        Ten fresh processes, each seeding telemetry._poll_rng with a
        different precomputed seed and making exactly ONE polling capture
        (their "call #1"). The old per-process-counter bug would have
        captured every single one of these (5/5 CAPTURED). The expected
        outcome per seed is precomputed offline from the same
        random.Random(seed).random() draw the production code makes, so
        this is a zero-flake proof that fresh-process sampling is unbiased.
        """
        # Precomputed offline (random.Random(seed).random() vs. 1/50):
        # captured: 31, 139, 165, 206, 281 -- skipped: 1, 2, 3, 4, 5
        seeds = [1, 2, 3, 4, 5, 31, 139, 165, 206, 281]
        threshold = 1.0 / telemetry._POLL_SAMPLE_RATE
        expected = [
            "CAPTURED" if random.Random(seed).random() < threshold else "SKIPPED" for seed in seeds
        ]
        assert "CAPTURED" in expected and "SKIPPED" in expected, (
            "seed selection must cover both outcomes or this test can't "
            "distinguish the fix from the old always-captures-call-1 bug"
        )

        script_path = tmp_path / "sampling_worker.py"
        script_path.write_text(
            "import sys\n"
            "from ouroboros import telemetry\n"
            "\n"
            "telemetry._poll_rng.seed(int(sys.argv[1]))\n"
            "captured = []\n"
            "telemetry._post = lambda batch: captured.extend(batch)\n"
            "telemetry.capture_tool_call('ouroboros_job_status', ok=True)\n"
            "telemetry.flush(timeout=2.0)\n"
            "print('CAPTURED' if captured else 'SKIPPED')\n",
            encoding="utf-8",
        )

        repo_root = Path(__file__).resolve().parents[2]
        observed = []
        for seed in seeds:
            home = tmp_path / f"home_{seed}"
            home.mkdir()
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PYTHONPATH"] = str(repo_root / "src")
            env["OUROBOROS_POSTHOG_API_KEY"] = "phc_test"
            for key in ("DO_NOT_TRACK", "OUROBOROS_TELEMETRY", "OUROBOROS_POSTHOG_HOST"):
                env.pop(key, None)
            completed = subprocess.run(
                [sys.executable, str(script_path), str(seed)],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            observed.append(completed.stdout.strip())

        assert observed == expected, f"seeds={seeds} expected={expected} observed={observed}"
