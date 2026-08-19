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
        with patch("ouroboros.mcp.tools.execution_handlers.log.warning") as warning:
            result = ExecuteSeedHandler._resolve_verification_working_dir(
                seed=seed,
                dispatch_cwd=tmp_path,
                raw_cwd=None,
                delegated_parent_cwd=None,
            )
        assert result == tmp_path
        warning.assert_called_once()
        assert warning.call_args.args[0] == "execution_handlers.seed_project_path_rejected"

    def test_empty_seed_falls_back_without_audit_log(self, tmp_path: Path) -> None:
        from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler

        seed = self._seed(None)
        with patch("ouroboros.mcp.tools.execution_handlers.log.warning") as warning:
            result = ExecuteSeedHandler._resolve_verification_working_dir(
                seed=seed,
                dispatch_cwd=tmp_path,
                raw_cwd=None,
                delegated_parent_cwd=None,
            )
        assert result == tmp_path
        warning.assert_not_called()


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

    def test_fallback_dir_stands_in_for_the_seed_file_folder(self, tmp_path: Path) -> None:
        """A Seed that says nothing must not make its own folder the workspace.

        `init` writes Seeds to `~/.ouroboros/seeds`, so the file's folder is the
        Seed store, not a project.
        """
        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_store = tmp_path / "seeds"
        seed_store.mkdir()
        seed_file = seed_store / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        invocation = tmp_path / "project"
        invocation.mkdir()

        result = _resolve_cli_project_dir(self._seed(None), seed_file, fallback_dir=invocation)

        assert result == invocation.resolve()

    def test_relative_file_reference_keeps_the_invocation_directory_as_root(
        self, tmp_path: Path
    ) -> None:
        """The interview handoff must not push the cwd into a subdirectory.

        Regression for #2194: a brownfield seed carrying a primary
        ``context_references`` file (``app/widgets/kanban.js``) collapsed the
        runtime cwd to the file's parent (``app/widgets``), so every AC's
        ``expected_artifacts`` — written relative to the project root —
        resolved to ``app/widgets/app/widgets/...`` and failed verification.
        """
        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_store = tmp_path / "seeds"
        seed_store.mkdir()
        seed_file = seed_store / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        invocation = tmp_path / "project"
        widget = invocation / "app" / "widgets" / "kanban.js"
        widget.parent.mkdir(parents=True)
        widget.write_text("// widget\n", encoding="utf-8")

        refs = [SimpleNamespace(path="app/widgets/kanban.js", role="primary")]
        seed = SimpleNamespace(
            metadata=None,
            brownfield_context=SimpleNamespace(context_references=refs),
        )

        result = _resolve_cli_project_dir(seed, seed_file, fallback_dir=invocation)

        assert result == invocation.resolve()

    @pytest.mark.parametrize("target_kind", ["missing", "file"])
    def test_unusable_brownfield_target_does_not_fall_back_to_the_seed_store(
        self, tmp_path: Path, target_kind: str
    ) -> None:
        """An unusable `target_dir` must not send execution back to the store.

        `_resolve_brownfield_target_dir` drops stale and file-valued targets, and
        whatever fills that hole becomes the workspace. Before the fallback
        existed that was the Seed file's own folder.
        """
        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_store = tmp_path / "seeds"
        seed_store.mkdir()
        seed_file = seed_store / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        invocation = tmp_path / "project"
        invocation.mkdir()
        target = str(tmp_path / "gone") if target_kind == "missing" else str(seed_file)

        result = _resolve_cli_project_dir(
            self._seed(None),
            seed_file,
            seed_data={"brownfield_context": {"target_dir": target}},
            fallback_dir=invocation,
        )

        assert result == invocation.resolve()

    def test_usable_brownfield_target_outranks_the_fallback(self, tmp_path: Path) -> None:
        """A Seed that names a real target keeps it over the caller's directory."""
        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_store = tmp_path / "seeds"
        seed_store.mkdir()
        seed_file = seed_store / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        invocation = tmp_path / "project"
        invocation.mkdir()
        target = tmp_path / "brownfield-repo"
        target.mkdir()

        result = _resolve_cli_project_dir(
            self._seed(None),
            seed_file,
            seed_data={"brownfield_context": {"target_dir": str(target)}},
            fallback_dir=invocation,
        )

        assert result == target.resolve()

    def test_explicit_project_dir_outranks_the_fallback(self, tmp_path: Path) -> None:
        """`--project-dir` stays the last word."""
        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        explicit = tmp_path / "explicit"
        explicit.mkdir()

        result = _resolve_cli_project_dir(
            self._seed(None),
            seed_file,
            project_dir=explicit,
            fallback_dir=tmp_path / "ignored",
        )

        assert result == explicit.resolve()

    def test_contained_seed_uses_resolved_path(self, tmp_path: Path) -> None:
        from ouroboros.cli.commands.run import _resolve_cli_project_dir

        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text("goal: x\n", encoding="utf-8")
        inside = tmp_path / "project"
        inside.mkdir()
        seed = self._seed(str(inside))

        result = _resolve_cli_project_dir(seed, seed_file)
        assert result == inside.resolve()
