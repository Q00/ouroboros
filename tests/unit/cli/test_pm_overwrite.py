"""Tests for AC 15: Existing pm_seed triggers overwrite confirmation on re-run."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.types import Result


@pytest.fixture()
def seeds_dir(tmp_path: Path) -> Path:
    """Create a temporary seeds directory."""
    d = tmp_path / ".ouroboros" / "seeds"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def _patch_home(tmp_path: Path):
    """Patch Path.home() to use tmp_path."""
    with patch.object(Path, "home", return_value=tmp_path):
        yield


class TestCheckExistingPrdSeeds:
    """Tests for _check_existing_pm_seeds."""

    def test_no_seeds_dir_returns_true(self, tmp_path: Path):
        """When ~/.ouroboros/seeds/ doesn't exist, should return True (proceed)."""
        from ouroboros.cli.commands.pm import _check_existing_pm_seeds

        with patch.object(Path, "home", return_value=tmp_path):
            assert _check_existing_pm_seeds() is True

    def test_empty_seeds_dir_returns_true(self, seeds_dir: Path, _patch_home):
        """When seeds dir exists but has no pm_seed files, return True."""
        from ouroboros.cli.commands.pm import _check_existing_pm_seeds

        assert _check_existing_pm_seeds() is True

    def test_non_pm_seeds_ignored(self, seeds_dir: Path, _patch_home):
        """Non-pm_seed files should not trigger the prompt."""
        from ouroboros.cli.commands.pm import _check_existing_pm_seeds

        (seeds_dir / "regular_seed_abc123.json").write_text('{"test": true}')
        (seeds_dir / "other_file.json").write_text('{"test": true}')
        assert _check_existing_pm_seeds() is True

    def test_existing_seed_user_confirms_overwrite(self, seeds_dir: Path, _patch_home):
        """When existing seeds found and user confirms, return True."""
        from ouroboros.cli.commands.pm import _check_existing_pm_seeds

        (seeds_dir / "pm_seed_abc123def456.json").write_text("pm_id: test")

        with patch("ouroboros.cli.commands.pm.Confirm") as mock_confirm:
            mock_confirm.ask.return_value = True
            result = _check_existing_pm_seeds()

        assert result is True
        mock_confirm.ask.assert_called_once()

    def test_existing_seed_user_declines_overwrite(self, seeds_dir: Path, _patch_home):
        """When existing seeds found and user declines, return False."""
        from ouroboros.cli.commands.pm import _check_existing_pm_seeds

        (seeds_dir / "pm_seed_abc123def456.json").write_text("pm_id: test")

        with patch("ouroboros.cli.commands.pm.Confirm") as mock_confirm:
            mock_confirm.ask.return_value = False
            result = _check_existing_pm_seeds()

        assert result is False

    def test_multiple_existing_seeds_shown(self, seeds_dir: Path, _patch_home):
        """When multiple seeds exist, all should be listed."""
        from ouroboros.cli.commands.pm import _check_existing_pm_seeds

        (seeds_dir / "pm_seed_aaa111.json").write_text("pm_id: a")
        (seeds_dir / "pm_seed_bbb222.json").write_text("pm_id: b")
        (seeds_dir / "pm_seed_ccc333.json").write_text("pm_id: c")

        with (
            patch("ouroboros.cli.commands.pm.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.pm.console") as mock_console,
        ):
            mock_confirm.ask.return_value = True
            _check_existing_pm_seeds()

        # Verify each seed file name was printed
        printed = " ".join(str(call) for call in mock_console.print.call_args_list)
        assert "pm_seed_aaa111.json" in printed
        assert "pm_seed_bbb222.json" in printed
        assert "pm_seed_ccc333.json" in printed

    def test_confirm_prompt_defaults_to_no(self, seeds_dir: Path, _patch_home):
        """The overwrite confirmation should default to No (safe default)."""
        from ouroboros.cli.commands.pm import _check_existing_pm_seeds

        (seeds_dir / "pm_seed_test123.json").write_text("pm_id: test")

        with patch("ouroboros.cli.commands.pm.Confirm") as mock_confirm:
            mock_confirm.ask.return_value = False
            _check_existing_pm_seeds()

        # Verify default=False was passed
        _, kwargs = mock_confirm.ask.call_args
        assert kwargs.get("default") is False

    def test_resume_skips_overwrite_check(self, seeds_dir: Path, _patch_home):
        """When resuming a session, the overwrite check should be skipped.

        This tests the integration logic — _check_existing_pm_seeds is
        only called when resume_id is None.
        """
        # Create existing seed
        (seeds_dir / "pm_seed_existing.json").write_text("pm_id: existing")

        # Read the source to verify the guard condition
        import inspect

        from ouroboros.cli.commands.pm import _run_pm_interview

        source = inspect.getsource(_run_pm_interview)
        assert "if not resume_id:" in source
        assert "_check_existing_pm_seeds" in source


class TestPmCliHandoff:
    """Regression tests for PM CLI handoff into the dev interview."""

    @pytest.mark.asyncio
    async def test_continue_to_dev_resolves_pm_seed_before_running_interview(
        self, tmp_path: Path, _patch_home
    ) -> None:
        """Auto-continue uses resolved PM seed content, not the literal path string."""
        from ouroboros.cli.commands.pm import _run_pm_interview

        state = SimpleNamespace(
            is_complete=True,
            interview_id="pm_123",
            rounds=[SimpleNamespace(user_response="Build a task app")],
        )
        seed_path = tmp_path / "pm_seed_taskflow.json"
        pm_path = tmp_path / "pm.md"
        seed = MagicMock()

        engine = MagicMock()
        engine.get_opening_question.return_value = "What do you want to build?"
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.generate_pm_seed = AsyncMock(return_value=Result.ok(seed))
        engine.save_pm_seed.return_value = seed_path
        engine.format_decide_later_summary.return_value = ""
        engine._reframe_map = {}
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = None
        engine._selected_brownfield_repos = []
        engine.classifications = []

        with (
            patch("ouroboros.cli.commands.pm.create_llm_adapter", return_value=object()),
            patch("ouroboros.bigbang.pm_interview.PMInterviewEngine.create", return_value=engine),
            patch("ouroboros.cli.commands.pm._check_existing_pm_seeds", return_value=True),
            patch("ouroboros.cli.commands.pm._load_brownfield_from_db", return_value=[]),
            patch("ouroboros.cli.commands.pm._select_repos", return_value=[]),
            patch(
                "ouroboros.cli.commands.pm.multiline_prompt_async",
                new=AsyncMock(return_value="Build a task app"),
            ),
            patch("ouroboros.cli.commands.pm.Confirm.ask", return_value=True),
            patch("ouroboros.bigbang.pm_document.save_pm_document", return_value=pm_path),
            patch(
                "ouroboros.core.initial_context.resolve_initial_context_input",
                return_value=Result.ok("resolved PM seed context"),
            ) as mock_resolve_initial_context,
            patch("ouroboros.cli.commands.init._run_interview", new_callable=AsyncMock) as mock_run,
            patch("ouroboros.cli.commands.pm.print_error"),
            patch("ouroboros.cli.commands.pm.print_info") as mock_info,
            patch("ouroboros.cli.commands.pm.print_success"),
        ):
            await _run_pm_interview(
                resume_id=None,
                model="anthropic/claude-sonnet-4-20250514",
                backend="codex",
                debug=False,
                output_dir=str(tmp_path),
            )

        mock_resolve_initial_context.assert_called_once_with(str(seed_path), cwd=Path.cwd())
        mock_run.assert_awaited_once_with(
            "resolved PM seed context",
            resume_id=None,
            state_dir=None,
            use_orchestrator=False,
            debug=False,
            workflow_runtime_backend=None,
            llm_backend="codex",
        )
        assert any(
            call.args and call.args[0] == f"Next: ouroboros init start {seed_path}"
            for call in mock_info.call_args_list
        )
