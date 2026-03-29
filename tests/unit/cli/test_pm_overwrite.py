"""Tests for existing PM document overwrite confirmation on re-run."""

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture()
def ouroboros_dir(tmp_path: Path) -> Path:
    """Create a temporary .ouroboros directory."""
    d = tmp_path / ".ouroboros"
    d.mkdir(parents=True)
    return d


class TestCheckExistingPmDocument:
    """Tests for _check_existing_pm_document."""

    def test_no_pm_document_returns_true(self, tmp_path: Path):
        """When .ouroboros/pm.md doesn't exist, should return True (proceed)."""
        from ouroboros.cli.commands.pm import _check_existing_pm_document

        with patch("ouroboros.cli.commands.pm.Path.cwd", return_value=tmp_path):
            assert _check_existing_pm_document() is True

    def test_existing_pm_document_user_confirms_overwrite(self, ouroboros_dir: Path):
        """When existing pm.md found and user confirms, return True."""
        from ouroboros.cli.commands.pm import _check_existing_pm_document

        (ouroboros_dir / "pm.md").write_text("# Product Requirements")

        with (
            patch("ouroboros.cli.commands.pm.Path.cwd", return_value=ouroboros_dir.parent),
            patch("ouroboros.cli.commands.pm.Confirm") as mock_confirm,
        ):
            mock_confirm.ask.return_value = True
            result = _check_existing_pm_document()

        assert result is True
        mock_confirm.ask.assert_called_once()

    def test_existing_pm_document_user_declines_overwrite(self, ouroboros_dir: Path):
        """When existing pm.md found and user declines, return False."""
        from ouroboros.cli.commands.pm import _check_existing_pm_document

        (ouroboros_dir / "pm.md").write_text("# Product Requirements")

        with (
            patch("ouroboros.cli.commands.pm.Path.cwd", return_value=ouroboros_dir.parent),
            patch("ouroboros.cli.commands.pm.Confirm") as mock_confirm,
        ):
            mock_confirm.ask.return_value = False
            result = _check_existing_pm_document()

        assert result is False

    def test_confirm_prompt_defaults_to_no(self, ouroboros_dir: Path):
        """The overwrite confirmation should default to No (safe default)."""
        from ouroboros.cli.commands.pm import _check_existing_pm_document

        (ouroboros_dir / "pm.md").write_text("# Product Requirements")

        with (
            patch("ouroboros.cli.commands.pm.Path.cwd", return_value=ouroboros_dir.parent),
            patch("ouroboros.cli.commands.pm.Confirm") as mock_confirm,
        ):
            mock_confirm.ask.return_value = False
            _check_existing_pm_document()

        _, kwargs = mock_confirm.ask.call_args
        assert kwargs.get("default") is False

    def test_custom_output_dir_checks_correct_path(self, tmp_path: Path):
        """When --output is specified, check that directory instead of cwd."""
        from ouroboros.cli.commands.pm import _check_existing_pm_document

        custom_dir = tmp_path / "docs"
        custom_dir.mkdir()
        (custom_dir / "pm.md").write_text("# Existing PM")

        with patch("ouroboros.cli.commands.pm.Confirm") as mock_confirm:
            mock_confirm.ask.return_value = True
            result = _check_existing_pm_document(output_dir=str(custom_dir))

        assert result is True
        mock_confirm.ask.assert_called_once()

    def test_custom_output_dir_no_existing_doc(self, tmp_path: Path):
        """When --output dir has no pm.md, return True without prompting."""
        from ouroboros.cli.commands.pm import _check_existing_pm_document

        custom_dir = tmp_path / "docs"
        custom_dir.mkdir()

        result = _check_existing_pm_document(output_dir=str(custom_dir))

        assert result is True

    def test_custom_output_does_not_check_default_dir(self, ouroboros_dir: Path):
        """When --output points elsewhere, don't prompt for cwd/.ouroboros/pm.md."""
        from ouroboros.cli.commands.pm import _check_existing_pm_document

        # pm.md exists in default location
        (ouroboros_dir / "pm.md").write_text("# Existing PM")

        # But --output points to an empty dir
        other_dir = ouroboros_dir.parent / "other"
        other_dir.mkdir()

        result = _check_existing_pm_document(output_dir=str(other_dir))

        # Should NOT prompt — the target dir has no pm.md
        assert result is True

    def test_resume_skips_overwrite_check(self):
        """When resuming a session, the overwrite check should be skipped.

        This tests the integration logic — _check_existing_pm_document is
        only called when resume_id is None.
        """
        import inspect

        from ouroboros.cli.commands.pm import _run_pm_interview

        source = inspect.getsource(_run_pm_interview)
        assert "if not resume_id:" in source
        assert "_check_existing_pm_document" in source
