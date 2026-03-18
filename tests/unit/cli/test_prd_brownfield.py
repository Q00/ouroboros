"""Unit tests for brownfield auto-detection and inline registration in prd CLI.

Tests verify that _check_brownfield() and _collect_additional_repos() use the
brownfield schema utilities from ouroboros.bigbang.brownfield for persistence
to ~/.ouroboros/brownfield.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.bigbang.brownfield import BrownfieldEntry
from ouroboros.cli.commands.prd import _check_brownfield, _collect_additional_repos


@pytest.fixture()
def brownfield_dir(tmp_path: Path) -> Path:
    """Create a directory that looks like a brownfield project."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    return tmp_path


@pytest.fixture()
def empty_dir(tmp_path: Path) -> Path:
    """Create an empty directory (greenfield)."""
    return tmp_path


class TestCheckBrownfield:
    """Tests for _check_brownfield() user confirmation flow."""

    def test_no_detection_in_empty_dir(self, empty_dir: Path) -> None:
        """Returns empty list when cwd has no recognised config files."""
        with patch(
            "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
            return_value=[],
        ):
            result = _check_brownfield(empty_dir)
        assert result == []

    def test_detects_brownfield_user_confirms(
        self, brownfield_dir: Path,
    ) -> None:
        """Registers repo when user confirms brownfield detection."""
        resolved = str(brownfield_dir.resolve())
        registered_entry = BrownfieldEntry(path=resolved, name="my-project", desc="A test project")

        with (
            patch(
                "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
                return_value=[],
            ),
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
                return_value=[registered_entry],
            ) as mock_register,
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
        ):
            mock_confirm.ask.return_value = True
            mock_prompt.ask.side_effect = ["my-project", "A test project"]

            result = _check_brownfield(brownfield_dir)

        assert len(result) == 1
        assert result[0]["name"] == "my-project"
        assert result[0]["desc"] == "A test project"
        assert result[0]["path"] == resolved
        mock_register.assert_called_once_with(
            path=resolved, name="my-project", desc="A test project",
        )

    def test_detects_brownfield_user_declines(
        self, brownfield_dir: Path,
    ) -> None:
        """Returns existing repos unchanged when user declines registration."""
        with (
            patch(
                "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
                return_value=[],
            ),
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
            ) as mock_register,
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
        ):
            mock_confirm.ask.return_value = False

            result = _check_brownfield(brownfield_dir)

        assert result == []
        mock_register.assert_not_called()

    def test_already_registered_skips_prompt(
        self, brownfield_dir: Path,
    ) -> None:
        """Skips confirmation when cwd is already registered."""
        resolved = str(brownfield_dir.resolve())
        existing = [{"path": resolved, "name": "demo", "desc": ""}]

        with (
            patch(
                "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
                return_value=existing,
            ),
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
        ):
            result = _check_brownfield(brownfield_dir)

        assert result == existing
        mock_confirm.ask.assert_not_called()

    def test_returns_existing_repos_on_greenfield(
        self, empty_dir: Path,
    ) -> None:
        """Returns existing repos even when cwd is not brownfield."""
        existing = [{"path": "/other/repo", "name": "other", "desc": ""}]

        with patch(
            "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
            return_value=existing,
        ):
            result = _check_brownfield(empty_dir)

        assert result == existing

    def test_default_name_uses_dirname(
        self, brownfield_dir: Path,
    ) -> None:
        """Default project name prompt uses the directory basename."""
        resolved = str(brownfield_dir.resolve())

        with (
            patch(
                "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
                return_value=[],
            ),
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
                return_value=[BrownfieldEntry(path=resolved, name=brownfield_dir.name, desc="")],
            ),
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
        ):
            mock_confirm.ask.return_value = True
            mock_prompt.ask.side_effect = [brownfield_dir.name, ""]

            _check_brownfield(brownfield_dir)

        # Verify name prompt was called with dirname as default
        name_call = mock_prompt.ask.call_args_list[0]
        assert name_call.kwargs.get("default") == brownfield_dir.name

    def test_nonexistent_path_returns_existing(self, tmp_path: Path) -> None:
        """Returns existing repos for a nonexistent path (no crash)."""
        nonexistent = tmp_path / "does_not_exist"

        with patch(
            "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
            return_value=[],
        ):
            result = _check_brownfield(nonexistent)

        assert result == []


class TestCollectAdditionalRepos:
    """Tests for _collect_additional_repos() inline registration flow."""

    def test_user_declines_adding_repos(self) -> None:
        """Returns repos unchanged when user says no to adding more."""
        existing = [{"path": "/a", "name": "a", "desc": ""}]

        with patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm:
            mock_confirm.ask.return_value = False
            result = _collect_additional_repos(existing)

        assert result == existing

    def test_user_adds_one_repo(self, tmp_path: Path) -> None:
        """Registers a single additional repo from user input."""
        repo_dir = tmp_path / "my-lib"
        repo_dir.mkdir()
        resolved = str(repo_dir.resolve())
        entry = BrownfieldEntry(path=resolved, name="my-lib", desc="A library")

        with (
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
                return_value=[entry],
            ) as mock_register,
        ):
            # First ask: yes to add. Second ask (after adding): no more.
            mock_confirm.ask.side_effect = [True, False]
            mock_prompt.ask.side_effect = [str(repo_dir), "my-lib", "A library"]

            result = _collect_additional_repos([])

        assert len(result) == 1
        assert result[0]["name"] == "my-lib"
        mock_register.assert_called_once_with(
            path=resolved, name="my-lib", desc="A library",
        )

    def test_user_adds_multiple_repos(self, tmp_path: Path) -> None:
        """User adds two repos in sequence."""
        repo_a = tmp_path / "repo-a"
        repo_a.mkdir()
        repo_b = tmp_path / "repo-b"
        repo_b.mkdir()
        resolved_a = str(repo_a.resolve())
        resolved_b = str(repo_b.resolve())

        entry_a = BrownfieldEntry(path=resolved_a, name="repo-a", desc="")
        entry_b = BrownfieldEntry(path=resolved_b, name="repo-b", desc="Second")

        with (
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
                side_effect=[[entry_a], [entry_a, entry_b]],
            ) as mock_register,
        ):
            # Yes add, yes add more, no more after second
            mock_confirm.ask.side_effect = [True, True, False]
            mock_prompt.ask.side_effect = [
                str(repo_a), "repo-a", "",       # first repo
                str(repo_b), "repo-b", "Second",  # second repo
            ]

            result = _collect_additional_repos([])

        assert len(result) == 2
        assert mock_register.call_count == 2

    def test_skips_nonexistent_path(self, tmp_path: Path) -> None:
        """Warns and skips when user provides a nonexistent path."""
        bad_path = tmp_path / "nope"

        with (
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
            ) as mock_register,
        ):
            # Yes to initial, give bad path, then no more
            mock_confirm.ask.side_effect = [True, False]
            mock_prompt.ask.side_effect = [str(bad_path)]

            result = _collect_additional_repos([])

        assert result == []
        mock_register.assert_not_called()

    def test_skips_empty_path(self) -> None:
        """Warns and skips when user provides an empty path."""
        with (
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
            ) as mock_register,
        ):
            mock_confirm.ask.side_effect = [True, False]
            mock_prompt.ask.side_effect = [""]

            result = _collect_additional_repos([])

        assert result == []
        mock_register.assert_not_called()

    def test_skips_duplicate_path(self, tmp_path: Path) -> None:
        """Skips registration when path is already in the list."""
        repo_dir = tmp_path / "existing"
        repo_dir.mkdir()
        resolved = str(repo_dir.resolve())
        existing = [{"path": resolved, "name": "existing", "desc": ""}]

        with (
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
            ) as mock_register,
        ):
            mock_confirm.ask.side_effect = [True, False]
            mock_prompt.ask.side_effect = [str(repo_dir)]

            result = _collect_additional_repos(existing)

        assert result == existing
        mock_register.assert_not_called()

    def test_default_name_uses_dirname(self, tmp_path: Path) -> None:
        """Default project name for manually added repos is the dir basename."""
        repo_dir = tmp_path / "cool-project"
        repo_dir.mkdir()
        resolved = str(repo_dir.resolve())
        entry = BrownfieldEntry(path=resolved, name="cool-project", desc="")

        with (
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
                return_value=[entry],
            ),
        ):
            mock_confirm.ask.side_effect = [True, False]
            mock_prompt.ask.side_effect = [str(repo_dir), "cool-project", ""]

            _collect_additional_repos([])

        # The name prompt (second call) should have dirname as default
        name_call = mock_prompt.ask.call_args_list[1]
        assert name_call.kwargs.get("default") == "cool-project"

    def test_tilde_expansion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Expands ~ in user-provided paths."""
        repo_dir = tmp_path / "home-repo"
        repo_dir.mkdir()
        resolved = str(repo_dir.resolve())
        entry = BrownfieldEntry(path=resolved, name="home-repo", desc="")

        monkeypatch.setenv("HOME", str(tmp_path))

        with (
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
            patch(
                "ouroboros.bigbang.brownfield.register_brownfield_repo",
                return_value=[entry],
            ) as mock_register,
        ):
            mock_confirm.ask.side_effect = [True, False]
            mock_prompt.ask.side_effect = ["~/home-repo", "home-repo", ""]

            result = _collect_additional_repos([])

        assert len(result) == 1
        mock_register.assert_called_once_with(
            path=resolved, name="home-repo", desc="",
        )


class TestBrownfieldPersistenceIntegration:
    """Integration tests verifying brownfield entries are persisted to disk
    via the brownfield schema utilities (BrownfieldEntry, validate_entries).

    These tests use real file I/O (tmp_path) instead of mocking the
    persistence layer, to ensure the full flow writes valid brownfield.json.
    """

    def test_check_brownfield_persists_to_file(
        self, brownfield_dir: Path, tmp_path: Path,
    ) -> None:
        """_check_brownfield persists the new entry to brownfield.json on disk."""
        bf_path = tmp_path / "bf" / "brownfield.json"
        resolved = str(brownfield_dir.resolve())

        with (
            patch("ouroboros.bigbang.brownfield.BROWNFIELD_PATH", bf_path),
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
        ):
            mock_confirm.ask.return_value = True
            mock_prompt.ask.side_effect = ["my-app", "Main application"]

            result = _check_brownfield(brownfield_dir)

        # Verify return value
        assert len(result) == 1
        assert result[0]["path"] == resolved
        assert result[0]["name"] == "my-app"
        assert result[0]["desc"] == "Main application"

        # Verify file was created with valid schema
        assert bf_path.exists()
        data = json.loads(bf_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["path"] == resolved
        assert data[0]["name"] == "my-app"
        assert data[0]["desc"] == "Main application"

    def test_collect_additional_repos_persists_to_file(
        self, tmp_path: Path,
    ) -> None:
        """_collect_additional_repos persists each entry to brownfield.json."""
        bf_path = tmp_path / "bf" / "brownfield.json"
        repo_dir = tmp_path / "extra-repo"
        repo_dir.mkdir()
        resolved = str(repo_dir.resolve())

        with (
            patch("ouroboros.bigbang.brownfield.BROWNFIELD_PATH", bf_path),
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
        ):
            mock_confirm.ask.side_effect = [True, False]
            mock_prompt.ask.side_effect = [str(repo_dir), "extra-repo", "Extra"]

            result = _collect_additional_repos([])

        assert len(result) == 1
        assert result[0]["name"] == "extra-repo"

        # Verify persisted to disk
        assert bf_path.exists()
        data = json.loads(bf_path.read_text())
        assert len(data) == 1
        assert data[0]["path"] == resolved
        assert data[0]["name"] == "extra-repo"
        assert data[0]["desc"] == "Extra"

    def test_persistence_validates_schema_on_load(
        self, brownfield_dir: Path, tmp_path: Path,
    ) -> None:
        """Previously persisted entries are validated through BrownfieldEntry
        schema on load — invalid entries are skipped."""
        bf_path = tmp_path / "bf" / "brownfield.json"
        bf_path.parent.mkdir(parents=True)

        # Pre-seed with one valid and one invalid entry (empty path)
        bf_path.write_text(json.dumps([
            {"path": "/valid/repo", "name": "valid", "desc": "ok"},
            {"path": "", "name": "invalid"},  # empty path — will be skipped
        ]))

        with (
            patch("ouroboros.bigbang.brownfield.BROWNFIELD_PATH", bf_path),
            patch("ouroboros.cli.commands.prd.Confirm") as mock_confirm,
            patch("ouroboros.cli.commands.prd.Prompt") as mock_prompt,
        ):
            mock_confirm.ask.return_value = True
            mock_prompt.ask.side_effect = ["new-app", "A new app"]

            result = _check_brownfield(brownfield_dir)

        # The invalid entry was skipped; valid + new entry remain
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert "valid" in names
        assert "new-app" in names
        # "invalid" was skipped by schema validation
        assert "invalid" not in names

    def test_deduplication_on_persist(
        self, brownfield_dir: Path, tmp_path: Path,
    ) -> None:
        """Re-registering the same path updates the entry (no duplicates)."""
        bf_path = tmp_path / "bf" / "brownfield.json"
        bf_path.parent.mkdir(parents=True)
        resolved = str(brownfield_dir.resolve())

        # Pre-seed with the same path under old name
        bf_path.write_text(json.dumps([
            {"path": resolved, "name": "old-name", "desc": "old desc"},
        ]))

        # Use register_brownfield_repo directly to verify dedup
        from ouroboros.bigbang.brownfield import register_brownfield_repo

        with patch("ouroboros.bigbang.brownfield.BROWNFIELD_PATH", bf_path):
            entries = register_brownfield_repo(
                path=resolved, name="new-name", desc="new desc",
            )

        assert len(entries) == 1
        assert entries[0].name == "new-name"
        assert entries[0].desc == "new desc"

        # Verify on disk — no duplicates
        data = json.loads(bf_path.read_text())
        assert len(data) == 1
        assert data[0]["name"] == "new-name"

    def test_multiple_repos_persisted_across_calls(
        self, tmp_path: Path,
    ) -> None:
        """Multiple registrations accumulate in brownfield.json."""
        bf_path = tmp_path / "bf" / "brownfield.json"

        from ouroboros.bigbang.brownfield import register_brownfield_repo

        with patch("ouroboros.bigbang.brownfield.BROWNFIELD_PATH", bf_path):
            register_brownfield_repo(path="/a", name="repo-a", desc="First")
            register_brownfield_repo(path="/b", name="repo-b", desc="Second")
            entries = register_brownfield_repo(path="/c", name="repo-c", desc="Third")

        assert len(entries) == 3

        # Verify all are on disk
        data = json.loads(bf_path.read_text())
        assert len(data) == 3
        paths = {d["path"] for d in data}
        assert paths == {"/a", "/b", "/c"}
