"""Unit tests for brownfield DB-based loading in pm CLI.

Tests verify that _load_brownfield_from_db() loads brownfield repos
from the database via load_brownfield_repos_as_dicts().
"""

from __future__ import annotations

from unittest.mock import patch

from ouroboros.cli.commands.pm import _load_brownfield_from_db


class TestLoadBrownfieldFromDb:
    """Tests for _load_brownfield_from_db() DB loading flow."""

    def test_returns_repos_from_db(self) -> None:
        """Returns repos loaded from the database."""
        expected = [
            {"path": "/repo/a", "name": "repo-a", "desc": "First repo"},
            {"path": "/repo/b", "name": "repo-b", "desc": "Second repo"},
        ]
        with patch(
            "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
            return_value=expected,
        ):
            result = _load_brownfield_from_db()

        assert result == expected
        assert len(result) == 2

    def test_returns_empty_when_no_repos(self) -> None:
        """Returns empty list when no repos are registered."""
        with patch(
            "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
            return_value=[],
        ):
            result = _load_brownfield_from_db()

        assert result == []

    def test_delegates_to_load_brownfield_repos_as_dicts(self) -> None:
        """Verifies delegation to the brownfield module function."""
        with patch(
            "ouroboros.bigbang.brownfield.load_brownfield_repos_as_dicts",
            return_value=[{"path": "/x", "name": "x", "desc": ""}],
        ) as mock_load:
            _load_brownfield_from_db()

        mock_load.assert_called_once()
