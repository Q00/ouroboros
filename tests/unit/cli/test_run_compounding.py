"""CLI tests for --compounding wiring on `ouroboros run workflow`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from typer.testing import CliRunner

from ouroboros.cli.commands.run import app as run_app

runner = CliRunner()


SEED = {
    "goal": "compounding test",
    "constraints": [],
    "acceptance_criteria": ["AC 1", "AC 2"],
    "ontology_schema": {"name": "X", "description": "x", "fields": []},
    "evaluation_principles": [],
    "exit_conditions": [],
    "metadata": {
        "seed_id": "seed-compound-test",
        "version": "1.0.0",
        "created_at": "2024-01-01T00:00:00Z",
        "ambiguity_score": 0.1,
    },
}


def _write_seed(tmp_path: Path) -> Path:
    path = tmp_path / "seed.yaml"
    path.write_text(yaml.safe_dump(SEED))
    return path


class TestCompoundingFlag:
    def test_compounding_and_sequential_are_mutually_exclusive(self, tmp_path: Path) -> None:
        seed_path = _write_seed(tmp_path)
        result = runner.invoke(
            run_app,
            ["workflow", str(seed_path), "--compounding", "--sequential"],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in (result.output or "").lower()

    def test_compounding_threads_mode_into_run_orchestrator(
        self, tmp_path: Path
    ) -> None:
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            result = runner.invoke(
                run_app,
                ["workflow", str(seed_path), "--compounding"],
            )
        assert result.exit_code == 0, result.output
        assert captured.get("mode") == "compounding"

    def test_default_run_has_no_mode_override(self, tmp_path: Path) -> None:
        seed_path = _write_seed(tmp_path)
        captured: dict = {}

        async def fake_run(*args, **kwargs):
            captured.update(kwargs)

        with patch(
            "ouroboros.cli.commands.run._run_orchestrator",
            new=AsyncMock(side_effect=fake_run),
        ):
            result = runner.invoke(run_app, ["workflow", str(seed_path)])
        assert result.exit_code == 0
        assert captured.get("mode") is None


