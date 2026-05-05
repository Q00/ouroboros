"""Tests for ``ExecuteSeedHandler._resolve_seed_content`` — seed-path
containment policy shared by the synchronous and background handlers.

Prior to refactor each handler reimplemented the same containment check;
these tests pin the contract to the consolidated helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler


async def _resolve(
    arguments: dict[str, Any],
    *,
    cwd: Path,
    tool_name: str = "ouroboros_execute_seed",
) -> Any:
    return await ExecuteSeedHandler._resolve_seed_content(
        arguments=arguments,
        resolved_cwd=cwd,
        tool_name=tool_name,
    )


class TestResolveSeedContent:
    async def test_inline_seed_content_short_circuits(self, tmp_path: Path) -> None:
        result = await _resolve(
            {"seed_content": "goal: ship\nacceptance_criteria: []\n"},
            cwd=tmp_path,
        )
        assert result.is_ok
        assert "goal: ship" in result.value

    async def test_missing_inputs_returns_error(self, tmp_path: Path) -> None:
        result = await _resolve({}, cwd=tmp_path)
        assert result.is_err
        assert "seed_content or seed_path is required" in str(result.error)

    async def test_relative_path_inside_cwd_is_read(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "my_seed.yaml"
        seed_file.write_text("goal: relative\n", encoding="utf-8")

        result = await _resolve({"seed_path": "my_seed.yaml"}, cwd=tmp_path)
        assert result.is_ok
        assert result.value == "goal: relative\n"

    async def test_absolute_path_inside_cwd_is_read(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "absolute_seed.yaml"
        seed_file.write_text("goal: absolute\n", encoding="utf-8")

        result = await _resolve({"seed_path": str(seed_file)}, cwd=tmp_path)
        assert result.is_ok
        assert result.value == "goal: absolute\n"

    async def test_absolute_path_outside_allowed_dirs_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force ``~/.ouroboros/seeds`` to a location that does not contain the candidate.
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        outside = tmp_path.parent / "outside_seed.yaml"
        outside.write_text("goal: escape\n", encoding="utf-8")

        try:
            result = await _resolve({"seed_path": str(outside)}, cwd=tmp_path)
            assert result.is_err
            assert "Seed path escapes allowed directories" in str(result.error)
        finally:
            outside.unlink(missing_ok=True)

    async def test_non_existent_path_falls_through_to_inline_yaml(self, tmp_path: Path) -> None:
        # ``seed_path`` doubles as inline YAML when no file exists at the location;
        # the candidate must still be inside the cwd so containment is satisfied.
        inline = "goal: inline\nacceptance_criteria: []"
        result = await _resolve({"seed_path": inline}, cwd=tmp_path)
        assert result.is_ok
        assert result.value == inline

    async def test_path_in_dedicated_seeds_dir_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_home = tmp_path / "fake_home"
        seeds_dir = fake_home / ".ouroboros" / "seeds"
        seeds_dir.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))

        seed_file = seeds_dir / "shared.yaml"
        seed_file.write_text("goal: shared\n", encoding="utf-8")

        # cwd is unrelated; allowed because ``~/.ouroboros/seeds`` contains it.
        result = await _resolve({"seed_path": str(seed_file)}, cwd=tmp_path)
        assert result.is_ok
        assert result.value == "goal: shared\n"

    async def test_tool_name_propagates_to_error(self, tmp_path: Path) -> None:
        result = await _resolve(
            {},
            cwd=tmp_path,
            tool_name="ouroboros_start_execute_seed",
        )
        assert result.is_err
        assert getattr(result.error, "tool_name", None) == "ouroboros_start_execute_seed"

    async def test_inline_seed_content_takes_priority_over_seed_path(self, tmp_path: Path) -> None:
        seed_file = tmp_path / "ignored.yaml"
        seed_file.write_text("goal: from_file\n", encoding="utf-8")
        result = await _resolve(
            {"seed_content": "goal: inline\n", "seed_path": str(seed_file)},
            cwd=tmp_path,
        )
        assert result.is_ok
        assert result.value == "goal: inline\n"
