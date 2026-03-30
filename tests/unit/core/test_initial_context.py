"""Tests for resolving interview initial context from text or files."""

from pathlib import Path

import yaml

from ouroboros.core.initial_context import (
    load_pm_seed_as_context,
    resolve_initial_context_input,
)


class TestLoadPmSeedAsContext:
    """Tests for PMSeed handoff loading."""

    def test_loads_pm_seed_json_as_interview_context(self, tmp_path: Path) -> None:
        seed_path = tmp_path / "pm_seed_test123.json"
        seed_path.write_text(
            """{
  "pm_id": "pm_seed_test123",
  "product_name": "TaskFlow",
  "goal": "Help teams manage tasks",
  "constraints": ["Offline support"],
  "success_criteria": ["Tasks sync correctly"],
  "user_stories": [],
  "deferred_items": [],
  "decide_later_items": []
}
""",
            encoding="utf-8",
        )

        result = load_pm_seed_as_context(seed_path)

        assert result.is_ok
        parsed = yaml.safe_load(result.value)
        assert parsed["pm_id"] == "pm_seed_test123"
        assert parsed["product_name"] == "TaskFlow"


class TestResolveInitialContextInput:
    """Tests for interview context resolution."""

    def test_keeps_literal_text_context(self) -> None:
        result = resolve_initial_context_input("Build a task manager")

        assert result.is_ok
        assert result.value == "Build a task manager"

    def test_resolves_relative_pm_seed_path_against_cwd(self, tmp_path: Path) -> None:
        seed_path = tmp_path / "pm_seed_taskflow.yaml"
        seed_path.write_text(
            yaml.dump(
                {
                    "pm_id": "pm_seed_taskflow",
                    "product_name": "TaskFlow",
                    "goal": "Help teams manage tasks",
                    "constraints": ["Offline support"],
                    "success_criteria": ["Tasks sync correctly"],
                    "user_stories": [],
                    "deferred_items": [],
                    "decide_later_items": [],
                }
            ),
            encoding="utf-8",
        )

        result = resolve_initial_context_input(seed_path.name, cwd=tmp_path)

        assert result.is_ok
        parsed = yaml.safe_load(result.value)
        assert parsed["pm_id"] == "pm_seed_taskflow"
        assert parsed["goal"] == "Help teams manage tasks"

    def test_loads_markdown_context_files_verbatim(self, tmp_path: Path) -> None:
        pm_path = tmp_path / "pm.md"
        pm_path.write_text("# Product Requirements\n\nBuild a task manager.", encoding="utf-8")

        result = resolve_initial_context_input(str(pm_path))

        assert result.is_ok
        assert result.value == "# Product Requirements\n\nBuild a task manager."

    def test_invalid_pm_seed_artifact_fails_closed(self, tmp_path: Path) -> None:
        seed_path = tmp_path / "pm_seed_invalid.json"
        seed_path.write_text(
            """{
  "pm_id": "pm_seed_invalid",
  "product_name": "",
  "goal": ""
}
""",
            encoding="utf-8",
        )

        result = resolve_initial_context_input(str(seed_path))

        assert result.is_err
        assert "non-empty product_name" in str(result.error)

    def test_generic_json_context_loads_verbatim(self, tmp_path: Path) -> None:
        context_path = tmp_path / "context.json"
        raw_context = '{\n  "idea": "Build a task manager",\n  "notes": ["CLI", "Offline"]\n}'
        context_path.write_text(raw_context, encoding="utf-8")

        result = resolve_initial_context_input(str(context_path))

        assert result.is_ok
        assert result.value == raw_context

    def test_long_literal_text_skips_path_probe(self) -> None:
        long_context = "Build a task manager. " * 30

        result = resolve_initial_context_input(long_context)

        assert result.is_ok
        assert result.value == long_context
