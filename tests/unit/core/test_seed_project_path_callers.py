"""Caller-level tests for ``resolve_seed_project_path`` rejection handling.

The helper now distinguishes "no path encoded" from "every path rejected".
These tests pin caller behaviour so a rejected seed never silently runs in
the fallback directory — the security event must surface.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

# ---------------------------------------------------------------------------
# evolution_handlers._resolve_verification_working_dir
# ---------------------------------------------------------------------------


class TestEvolutionHandlerCaller:
    @staticmethod
    def _seed(project_dir: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            metadata=SimpleNamespace(project_dir=project_dir, working_directory=None),
            brownfield_context=None,
        )

    def test_rejected_seed_falls_back_with_audit_log(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.evolution_handlers import _resolve_verification_working_dir

        seed = self._seed(str(tmp_path.parent / "outside"))
        with capture_logs() as cap_logs:
            result = _resolve_verification_working_dir(
                project_dir=None,
                seed=seed,
                stable_base=tmp_path,
            )
        assert result == tmp_path
        events = [e.get("event") for e in cap_logs]
        assert "evolution_handlers.seed_project_path_rejected" in events

    def test_empty_seed_falls_back_without_audit_log(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.evolution_handlers import _resolve_verification_working_dir

        seed = self._seed(None)
        with capture_logs() as cap_logs:
            result = _resolve_verification_working_dir(
                project_dir=None,
                seed=seed,
                stable_base=tmp_path,
            )
        assert result == tmp_path
        events = [e.get("event") for e in cap_logs]
        assert "evolution_handlers.seed_project_path_rejected" not in events

    def test_contained_seed_uses_resolved_path(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.evolution_handlers import _resolve_verification_working_dir

        inside = tmp_path / "project"
        inside.mkdir()
        seed = self._seed(str(inside))
        result = _resolve_verification_working_dir(
            project_dir=None,
            seed=seed,
            stable_base=tmp_path,
        )
        assert result == inside.resolve()


# ---------------------------------------------------------------------------
# execution_handlers.ExecuteSeedHandler._resolve_verification_working_dir
# ---------------------------------------------------------------------------


class TestExecutionHandlerCaller:
    @staticmethod
    def _seed(project_dir: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            metadata=SimpleNamespace(project_dir=project_dir, working_directory=None),
            brownfield_context=None,
        )

    def test_rejected_seed_falls_back_with_audit_log(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler

        seed = self._seed(str(tmp_path.parent / "outside_project"))
        with capture_logs() as cap_logs:
            result = ExecuteSeedHandler._resolve_verification_working_dir(
                seed=seed,
                dispatch_cwd=tmp_path,
                raw_cwd=None,
                delegated_parent_cwd=None,
            )
        assert result == tmp_path
        events = [e.get("event") for e in cap_logs]
        assert "execution_handlers.seed_project_path_rejected" in events

    def test_empty_seed_falls_back_without_audit_log(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler

        seed = self._seed(None)
        with capture_logs() as cap_logs:
            result = ExecuteSeedHandler._resolve_verification_working_dir(
                seed=seed,
                dispatch_cwd=tmp_path,
                raw_cwd=None,
                delegated_parent_cwd=None,
            )
        assert result == tmp_path
        events = [e.get("event") for e in cap_logs]
        assert "execution_handlers.seed_project_path_rejected" not in events


# ---------------------------------------------------------------------------
# cli/commands/run.py:_resolve_cli_project_dir
# ---------------------------------------------------------------------------


class TestCliRunCaller:
    @staticmethod
    def _seed(project_dir: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            metadata=SimpleNamespace(project_dir=project_dir, working_directory=None),
            brownfield_context=None,
        )

    def test_rejected_seed_aborts_cli_with_typer_exit(self, tmp_path: Path) -> None:
        import typer

        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        seed = self._seed(str(tmp_path.parent / "outside_repo"))

        with patch("ouroboros.cli.commands.run.print_error") as mock_print:
            with pytest.raises(typer.Exit) as exc_info:
                _resolve_cli_project_dir(seed, seed_file)

        assert exc_info.value.exit_code == 1
        assert mock_print.call_count == 1
        assert "escapes" in mock_print.call_args[0][0]

    def test_empty_seed_falls_back_to_seed_file_dir(self, tmp_path: Path) -> None:
        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        seed = self._seed(None)

        result = _resolve_cli_project_dir(seed, seed_file)
        assert result == tmp_path.resolve()

    def test_contained_seed_uses_resolved_path(self, tmp_path: Path) -> None:
        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        inside = tmp_path / "project"
        inside.mkdir()
        seed = self._seed(str(inside))

        result = _resolve_cli_project_dir(seed, seed_file)
        assert result == inside.resolve()
