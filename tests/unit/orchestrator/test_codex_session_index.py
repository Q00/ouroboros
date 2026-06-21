"""Codex app session-index observability — deterministic (temp CODEX_HOME)."""

from __future__ import annotations

import json

from ouroboros.orchestrator.codex_session_index import (
    derive_session_label,
    register_codex_session,
)


class TestDeriveLabel:
    def test_prefixes_ooo(self) -> None:
        assert derive_session_label("Build a CLI tool").startswith("ooo: ")

    def test_skips_assignment_fences_and_headings(self) -> None:
        prompt = (
            "<assignment>\n"
            "## Task\n"
            "Execute the seed specification below.\n"
            "## Deliverable\n"
            "A working implementation.\n"
            "</assignment>"
        )
        label = derive_session_label(prompt)
        assert label == "ooo: Execute the seed specification below."

    def test_truncates_long_first_line(self) -> None:
        label = derive_session_label("x" * 200)
        assert label.startswith("ooo: ")
        assert len(label) <= len("ooo: ") + 56

    def test_empty_prompt_falls_back(self) -> None:
        assert derive_session_label("") == "ooo: worker"
        assert derive_session_label("\n<a>\n```\n") == "ooo: worker"


class TestRegister:
    def test_appends_schema_matched_entry(self, tmp_path) -> None:
        ok = register_codex_session("019ee-thread", "ooo: do the thing", codex_home=tmp_path)
        assert ok is True
        index = tmp_path / "session_index.jsonl"
        lines = index.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert sorted(entry.keys()) == ["id", "thread_name", "updated_at"]
        assert entry["id"] == "019ee-thread"
        assert entry["thread_name"] == "ooo: do the thing"
        assert entry["updated_at"].endswith("Z")

    def test_appends_without_clobbering(self, tmp_path) -> None:
        (tmp_path / "session_index.jsonl").write_text(
            json.dumps(
                {
                    "id": "pre",
                    "thread_name": "existing",
                    "updated_at": "2026-01-01T00:00:00.000000Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        register_codex_session("new", "ooo: new", codex_home=tmp_path)
        lines = (tmp_path / "session_index.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["id"] == "pre"  # pre-existing entry untouched
        assert json.loads(lines[1])["id"] == "new"

    def test_no_thread_id_is_noop(self, tmp_path) -> None:
        assert register_codex_session(None, "ooo: x", codex_home=tmp_path) is False
        assert register_codex_session("", "ooo: x", codex_home=tmp_path) is False
        assert not (tmp_path / "session_index.jsonl").exists()

    def test_missing_codex_home_is_noop_not_raise(self, tmp_path) -> None:
        missing = tmp_path / "does-not-exist"
        assert register_codex_session("id", "ooo: x", codex_home=missing) is False
