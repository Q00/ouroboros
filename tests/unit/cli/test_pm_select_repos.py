"""Unit tests for multi-select brownfield repo UI in pm CLI.

Tests verify that _select_repos() and _parse_selection() correctly
handle multi-select interaction: auto-select for single repos,
numbered list display, comma/range parsing, and edge cases.
"""

from __future__ import annotations

from unittest.mock import patch

from ouroboros.cli.commands.pm import _parse_selection, _select_repos

# ── _parse_selection (pure function) ──────────────────────────────


class TestParseSelection:
    """Tests for _parse_selection() — converts user input to 0-based indices."""

    def test_all_keyword(self) -> None:
        assert _parse_selection("all", 5) == {0, 1, 2, 3, 4}

    def test_all_case_insensitive(self) -> None:
        assert _parse_selection("ALL", 3) == {0, 1, 2}

    def test_empty_string_means_all(self) -> None:
        assert _parse_selection("", 3) == {0, 1, 2}

    def test_whitespace_only_means_all(self) -> None:
        assert _parse_selection("   ", 4) == {0, 1, 2, 3}

    def test_single_number(self) -> None:
        assert _parse_selection("2", 5) == {1}

    def test_comma_separated(self) -> None:
        assert _parse_selection("1,3,5", 5) == {0, 2, 4}

    def test_comma_with_spaces(self) -> None:
        assert _parse_selection("1 , 3 , 5", 5) == {0, 2, 4}

    def test_range(self) -> None:
        assert _parse_selection("2-4", 5) == {1, 2, 3}

    def test_range_and_individual(self) -> None:
        assert _parse_selection("1,3-5", 6) == {0, 2, 3, 4}

    def test_out_of_range_high_ignored(self) -> None:
        assert _parse_selection("10", 3) == set()

    def test_out_of_range_zero_ignored(self) -> None:
        assert _parse_selection("0", 3) == set()

    def test_negative_ignored(self) -> None:
        assert _parse_selection("-1", 3) == set()

    def test_invalid_token_ignored(self) -> None:
        assert _parse_selection("abc", 3) == set()

    def test_mixed_valid_and_invalid(self) -> None:
        assert _parse_selection("1,abc,3", 5) == {0, 2}

    def test_range_clipped_to_bounds(self) -> None:
        # Range 2-10, but only 4 items exist → indices 1,2,3
        assert _parse_selection("2-10", 4) == {1, 2, 3}

    def test_empty_total(self) -> None:
        assert _parse_selection("all", 0) == set()

    def test_duplicate_numbers(self) -> None:
        assert _parse_selection("1,1,2,2", 3) == {0, 1}

    def test_reversed_range(self) -> None:
        # 4-2 → start > end, range(4, 3) is empty
        assert _parse_selection("4-2", 5) == set()

    def test_invalid_range_parts(self) -> None:
        assert _parse_selection("a-b", 5) == set()


# ── _select_repos (UI function) ──────────────────────────────────


class TestSelectRepos:
    """Tests for _select_repos() — multi-select UI for brownfield repos."""

    def test_empty_repos_returns_empty(self) -> None:
        """Returns empty list immediately when no repos are registered."""
        result = _select_repos([])
        assert result == []

    def test_single_repo_auto_selected(self) -> None:
        """Auto-selects the only registered repo without prompting."""
        repos = [{"path": "/a", "name": "alpha", "desc": ""}]
        result = _select_repos(repos)
        assert result == repos

    def test_single_repo_no_prompt(self) -> None:
        """Verify no Prompt.ask is called for a single repo."""
        repos = [{"path": "/a", "name": "alpha", "desc": ""}]
        with patch("ouroboros.cli.commands.pm.Prompt") as mock_prompt:
            _select_repos(repos)
        mock_prompt.ask.assert_not_called()

    def test_multiple_repos_select_all(self) -> None:
        """User types 'all' to select all repos."""
        repos = [
            {"path": "/a", "name": "alpha", "desc": ""},
            {"path": "/b", "name": "beta", "desc": "B project"},
        ]
        with patch("ouroboros.cli.commands.pm.Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "all"
            result = _select_repos(repos)
        assert result == repos

    def test_multiple_repos_select_subset(self) -> None:
        """User selects specific repos by number."""
        repos = [
            {"path": "/a", "name": "alpha", "desc": ""},
            {"path": "/b", "name": "beta", "desc": ""},
            {"path": "/c", "name": "gamma", "desc": ""},
        ]
        with patch("ouroboros.cli.commands.pm.Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "1,3"
            result = _select_repos(repos)
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "gamma"

    def test_multiple_repos_select_range(self) -> None:
        """User selects a range of repos."""
        repos = [
            {"path": "/a", "name": "alpha", "desc": ""},
            {"path": "/b", "name": "beta", "desc": ""},
            {"path": "/c", "name": "gamma", "desc": ""},
            {"path": "/d", "name": "delta", "desc": ""},
        ]
        with patch("ouroboros.cli.commands.pm.Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "2-3"
            result = _select_repos(repos)
        assert len(result) == 2
        assert result[0]["name"] == "beta"
        assert result[1]["name"] == "gamma"

    def test_default_all_on_empty_input(self) -> None:
        """Default prompt value of 'all' selects everything."""
        repos = [
            {"path": "/a", "name": "alpha", "desc": ""},
            {"path": "/b", "name": "beta", "desc": ""},
        ]
        with patch("ouroboros.cli.commands.pm.Prompt") as mock_prompt:
            # Simulate user pressing Enter (default="all")
            mock_prompt.ask.return_value = "all"
            result = _select_repos(repos)
        assert len(result) == 2

    def test_invalid_selection_falls_back_to_all(self) -> None:
        """Invalid selection returns all repos with a warning."""
        repos = [
            {"path": "/a", "name": "alpha", "desc": ""},
            {"path": "/b", "name": "beta", "desc": ""},
        ]
        with patch("ouroboros.cli.commands.pm.Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "xyz"
            result = _select_repos(repos)
        assert result == repos

    def test_selected_repos_preserve_order(self) -> None:
        """Selected repos maintain their original order (sorted indices)."""
        repos = [
            {"path": "/a", "name": "alpha", "desc": ""},
            {"path": "/b", "name": "beta", "desc": ""},
            {"path": "/c", "name": "gamma", "desc": ""},
        ]
        with patch("ouroboros.cli.commands.pm.Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "3,1"  # reversed order input
            result = _select_repos(repos)
        # Should be in original order: alpha, gamma
        assert result[0]["name"] == "alpha"
        assert result[1]["name"] == "gamma"

    def test_repos_with_desc_shown(self) -> None:
        """Repos with descriptions are displayed (no assertion on output,
        just verifies no crash with desc present)."""
        repos = [
            {"path": "/a", "name": "alpha", "desc": "Main API"},
            {"path": "/b", "name": "beta", "desc": "Frontend"},
        ]
        with patch("ouroboros.cli.commands.pm.Prompt") as mock_prompt:
            mock_prompt.ask.return_value = "1"
            result = _select_repos(repos)
        assert len(result) == 1
        assert result[0]["name"] == "alpha"
