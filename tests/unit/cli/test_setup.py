"""Unit tests for the setup CLI command.

Tests the scan → select → set_default flow in the setup command,
including helper functions for display and selection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.cli.commands.setup import (
    _display_repos_table,
    _list_repos,
    _prompt_repo_selection,
    _scan_and_register_repos,
    _set_default_repo,
)

# ── Helper function tests ────────────────────────────────────────


class TestDisplayReposTable:
    """Tests for _display_repos_table rendering."""

    def test_renders_without_error(self, capsys) -> None:
        """Table renders without raising for typical repo data."""
        repos = [
            {"path": "/home/user/proj", "name": "proj", "desc": "A project", "is_default": True},
            {"path": "/home/user/other", "name": "other", "desc": "", "is_default": False},
        ]
        # Should not raise
        _display_repos_table(repos)

    def test_renders_empty_list(self) -> None:
        """Empty list renders without error."""
        _display_repos_table([])

    def test_renders_without_default_column(self) -> None:
        """Can hide the default column."""
        repos = [{"path": "/p", "name": "n", "desc": "d", "is_default": False}]
        _display_repos_table(repos, show_default=False)


class TestPromptRepoSelection:
    """Tests for _prompt_repo_selection interactive input."""

    def test_valid_number_selection(self) -> None:
        """Selecting a valid number returns 0-based index."""
        repos = [
            {"path": "/a", "name": "a"},
            {"path": "/b", "name": "b"},
            {"path": "/c", "name": "c"},
        ]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="2"):
            result = _prompt_repo_selection(repos)
        assert result == 1  # 0-based

    def test_skip_returns_none(self) -> None:
        """Typing 'skip' returns None."""
        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="skip"):
            result = _prompt_repo_selection(repos)
        assert result is None

    def test_invalid_input_returns_none(self) -> None:
        """Invalid input (non-number) returns None."""
        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="abc"):
            result = _prompt_repo_selection(repos)
        assert result is None

    def test_out_of_range_returns_none(self) -> None:
        """Number out of range returns None."""
        repos = [{"path": "/a", "name": "a"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="5"):
            result = _prompt_repo_selection(repos)
        assert result is None

    def test_first_repo_selection(self) -> None:
        """Selecting 1 returns index 0."""
        repos = [{"path": "/a", "name": "a"}, {"path": "/b", "name": "b"}]
        with patch("ouroboros.cli.commands.setup.Prompt.ask", return_value="1"):
            result = _prompt_repo_selection(repos)
        assert result == 0


# ── Async core logic tests ───────────────────────────────────────


class TestScanAndRegisterRepos:
    """Tests for _scan_and_register_repos async function."""

    @pytest.mark.asyncio
    async def test_returns_repo_dicts(self) -> None:
        """Returns list of dicts from scan_and_register."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repos = [
            BrownfieldRepo(path="/home/user/proj", name="proj", desc="A project", is_default=True),
            BrownfieldRepo(path="/home/user/lib", name="lib", desc="", is_default=False),
        ]

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
        ):
            result = await _scan_and_register_repos()

        assert len(result) == 2
        assert result[0]["name"] == "proj"
        assert result[0]["is_default"] is True
        assert result[1]["name"] == "lib"
        assert result[1]["desc"] == ""

    @pytest.mark.asyncio
    async def test_empty_scan(self) -> None:
        """Returns empty list when no repos found."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await _scan_and_register_repos()

        assert result == []

    @pytest.mark.asyncio
    async def test_store_closed_on_success(self) -> None:
        """Store is closed even after successful operation."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            await _scan_and_register_repos()

        mock_store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_store_closed_on_error(self) -> None:
        """Store is closed even when scan raises."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                side_effect=RuntimeError("scan failed"),
            ),
        ):
            with pytest.raises(RuntimeError, match="scan failed"):
                await _scan_and_register_repos()

        mock_store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_does_not_call_clear_all_before_scan(self) -> None:
        """Setup delegates clearing to scan_and_register — no separate clear_all."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_scan,
        ):
            await _scan_and_register_repos()

        # clear_all should NOT be called — scan_and_register handles it internally
        mock_store.clear_all.assert_not_awaited()
        mock_scan.assert_awaited_once()


class TestListRepos:
    """Tests for _list_repos async function."""

    @pytest.mark.asyncio
    async def test_returns_all_repos(self) -> None:
        """Returns all registered repos as dicts."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repos = [
            BrownfieldRepo(path="/a", name="a", desc="desc-a", is_default=False),
        ]

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=mock_repos)

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            result = await _list_repos()

        assert len(result) == 1
        assert result[0]["path"] == "/a"
        assert result[0]["desc"] == "desc-a"


class TestSetDefaultRepo:
    """Tests for _set_default_repo async function."""

    @pytest.mark.asyncio
    async def test_set_default_success(self) -> None:
        """Returns True when set_default_repo succeeds."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repo = BrownfieldRepo(path="/a", name="a", is_default=True)

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.set_default_repo",
                new_callable=AsyncMock,
                return_value=mock_repo,
            ),
        ):
            result = await _set_default_repo("/a")

        assert result is True

    @pytest.mark.asyncio
    async def test_set_default_not_found(self) -> None:
        """Returns False when path is not registered."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.set_default_repo",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _set_default_repo("/nonexistent")

        assert result is False


# ── Full setup flow tests ─────────────────────────────────────────


class TestRunFullSetup:
    """Tests for _run_full_setup end-to-end flow with mocked I/O."""

    @pytest.mark.asyncio
    async def test_full_setup_with_repos_and_selection(self) -> None:
        """Full flow: scan finds repos → user selects → default is set."""
        from ouroboros.cli.commands.setup import _run_full_setup

        mock_repos_dicts = [
            {"path": "/a", "name": "alpha", "desc": "", "is_default": False},
            {"path": "/b", "name": "beta", "desc": "", "is_default": False},
        ]

        with (
            patch(
                "ouroboros.cli.commands.setup._scan_and_register_repos",
                new_callable=AsyncMock,
                return_value=mock_repos_dicts,
            ),
            patch(
                "ouroboros.cli.commands.setup._display_repos_table",
            ) as mock_display,
            patch(
                "ouroboros.cli.commands.setup._prompt_repo_selection",
                return_value=1,  # selects "beta"
            ),
            patch(
                "ouroboros.cli.commands.setup._set_default_repo",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_set_default,
            patch(
                "ouroboros.cli.commands.setup.console",
            ),
        ):
            await _run_full_setup()

        mock_display.assert_called_once_with(mock_repos_dicts)
        mock_set_default.assert_awaited_once_with("/b")

    @pytest.mark.asyncio
    async def test_full_setup_no_repos_found(self) -> None:
        """When scan finds no repos, setup exits early without selection."""
        from ouroboros.cli.commands.setup import _run_full_setup

        with (
            patch(
                "ouroboros.cli.commands.setup._scan_and_register_repos",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "ouroboros.cli.commands.setup._display_repos_table",
            ) as mock_display,
            patch(
                "ouroboros.cli.commands.setup._prompt_repo_selection",
            ) as mock_prompt,
            patch(
                "ouroboros.cli.commands.setup.console",
            ),
        ):
            await _run_full_setup()

        mock_display.assert_not_called()
        mock_prompt.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_setup_user_skips_selection(self) -> None:
        """When user skips selection, no default is set."""
        from ouroboros.cli.commands.setup import _run_full_setup

        mock_repos = [
            {"path": "/a", "name": "alpha", "desc": "", "is_default": False},
        ]

        with (
            patch(
                "ouroboros.cli.commands.setup._scan_and_register_repos",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
            patch(
                "ouroboros.cli.commands.setup._display_repos_table",
            ),
            patch(
                "ouroboros.cli.commands.setup._prompt_repo_selection",
                return_value=None,  # user skipped
            ),
            patch(
                "ouroboros.cli.commands.setup._set_default_repo",
                new_callable=AsyncMock,
            ) as mock_set_default,
            patch(
                "ouroboros.cli.commands.setup.console",
            ),
        ):
            await _run_full_setup()

        mock_set_default.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_full_setup_shows_existing_default(self) -> None:
        """When a default already exists, it is displayed before selection."""
        from ouroboros.cli.commands.setup import _run_full_setup

        mock_repos = [
            {"path": "/a", "name": "alpha", "desc": "desc-a", "is_default": True},
            {"path": "/b", "name": "beta", "desc": "", "is_default": False},
        ]

        with (
            patch(
                "ouroboros.cli.commands.setup._scan_and_register_repos",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
            patch(
                "ouroboros.cli.commands.setup._display_repos_table",
            ),
            patch(
                "ouroboros.cli.commands.setup._prompt_repo_selection",
                return_value=None,
            ),
            patch(
                "ouroboros.cli.commands.setup._set_default_repo",
                new_callable=AsyncMock,
            ),
            patch(
                "ouroboros.cli.commands.setup.console",
            ),
            patch(
                "ouroboros.cli.commands.setup.print_info",
            ) as mock_info,
        ):
            await _run_full_setup()

        # Should display info about the current default
        mock_info.assert_any_call("Current default: [cyan]alpha[/] (/a)")

    @pytest.mark.asyncio
    async def test_full_setup_set_default_failure(self) -> None:
        """When set_default fails, error is displayed."""
        from ouroboros.cli.commands.setup import _run_full_setup

        mock_repos = [
            {"path": "/a", "name": "alpha", "desc": "", "is_default": False},
        ]

        with (
            patch(
                "ouroboros.cli.commands.setup._scan_and_register_repos",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
            patch(
                "ouroboros.cli.commands.setup._display_repos_table",
            ),
            patch(
                "ouroboros.cli.commands.setup._prompt_repo_selection",
                return_value=0,
            ),
            patch(
                "ouroboros.cli.commands.setup._set_default_repo",
                new_callable=AsyncMock,
                return_value=False,  # failure
            ),
            patch(
                "ouroboros.cli.commands.setup.console",
            ),
            patch(
                "ouroboros.cli.commands.setup.print_error",
            ) as mock_error,
        ):
            await _run_full_setup()

        mock_error.assert_called_once_with("Failed to set default: /a")


# ── Scan-Register pipeline tests ──────────────────────────────────


class TestScanRegisterPipeline:
    """Tests verifying the scan → register pipeline in setup context.

    These tests verify that _scan_and_register_repos correctly orchestrates
    the BrownfieldStore lifecycle (initialize → clear_all → scan → close).
    """

    @pytest.mark.asyncio
    async def test_store_lifecycle_order(self) -> None:
        """Store operations happen in correct order: init → scan → close (no separate clear)."""
        call_order: list[str] = []

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock(side_effect=lambda: call_order.append("initialize"))
        mock_store.close = AsyncMock(side_effect=lambda: call_order.append("close"))

        async def fake_scan(store):
            call_order.append("scan_and_register")
            return []

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                side_effect=fake_scan,
            ),
        ):
            await _scan_and_register_repos()

        assert call_order == ["initialize", "scan_and_register", "close"]

    @pytest.mark.asyncio
    async def test_scan_passes_store_to_scan_and_register(self) -> None:
        """The store instance is passed to scan_and_register."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        captured_store = None

        async def capture_store(store):
            nonlocal captured_store
            captured_store = store
            return []

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                side_effect=capture_store,
            ),
        ):
            await _scan_and_register_repos()

        assert captured_store is mock_store

    @pytest.mark.asyncio
    async def test_converts_brownfield_repo_to_dict(self) -> None:
        """BrownfieldRepo objects are converted to plain dicts with all fields."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        mock_repos = [
            BrownfieldRepo(path="/home/user/proj", name="proj", desc="My project", is_default=True),
            BrownfieldRepo(path="/home/user/lib", name="lib", desc=None, is_default=False),
        ]

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
        ):
            result = await _scan_and_register_repos()

        assert len(result) == 2
        # Verify dict structure
        assert result[0] == {
            "path": "/home/user/proj",
            "name": "proj",
            "desc": "My project",
            "is_default": True,
        }
        # None desc should be converted to ""
        assert result[1]["desc"] == ""
        assert result[1]["is_default"] is False

    @pytest.mark.asyncio
    async def test_store_closed_even_on_scan_error(self) -> None:
        """Store is closed even if scan_and_register raises."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB locked"),
            ),
        ):
            with pytest.raises(RuntimeError, match="DB locked"):
                await _scan_and_register_repos()

        mock_store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_many_repos_all_returned(self) -> None:
        """Large number of scanned repos are all correctly returned."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        count = 50
        mock_repos = [
            BrownfieldRepo(
                path=f"/home/user/repo-{i}", name=f"repo-{i}", desc="", is_default=(i == 0)
            )
            for i in range(count)
        ]

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.clear_all = AsyncMock(return_value=0)

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.scan_and_register",
                new_callable=AsyncMock,
                return_value=mock_repos,
            ),
        ):
            result = await _scan_and_register_repos()

        assert len(result) == count
        assert result[0]["is_default"] is True
        assert all(r["is_default"] is False for r in result[1:])


# ── List repos extended tests ─────────────────────────────────────


class TestListReposExtended:
    """Extended tests for _list_repos async function."""

    @pytest.mark.asyncio
    async def test_list_converts_none_desc_to_empty(self) -> None:
        """None desc values are converted to empty strings."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(
            return_value=[
                BrownfieldRepo(path="/a", name="a", desc=None, is_default=False),
            ]
        )

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            result = await _list_repos()

        assert result[0]["desc"] == ""

    @pytest.mark.asyncio
    async def test_list_empty_db(self) -> None:
        """Returns empty list when no repos in DB."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=[])

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            result = await _list_repos()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_store_closed_after_query(self) -> None:
        """Store is always closed after listing."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()
        mock_store.list = AsyncMock(return_value=[])

        with patch(
            "ouroboros.cli.commands.setup.BrownfieldStore",
            return_value=mock_store,
        ):
            await _list_repos()

        mock_store.close.assert_awaited_once()


# ── Set default repo extended tests ───────────────────────────────


class TestSetDefaultRepoExtended:
    """Extended tests for _set_default_repo in setup context."""

    @pytest.mark.asyncio
    async def test_set_default_store_closed_on_success(self) -> None:
        """Store is closed after successful set_default."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.set_default_repo",
                new_callable=AsyncMock,
                return_value=BrownfieldRepo(path="/a", name="a", is_default=True),
            ),
        ):
            await _set_default_repo("/a")

        mock_store.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_set_default_store_closed_on_error(self) -> None:
        """Store is closed even when set_default_repo raises."""
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()
        mock_store.close = AsyncMock()

        with (
            patch(
                "ouroboros.cli.commands.setup.BrownfieldStore",
                return_value=mock_store,
            ),
            patch(
                "ouroboros.cli.commands.setup.set_default_repo",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB error"),
            ),
        ):
            with pytest.raises(RuntimeError, match="DB error"):
                await _set_default_repo("/a")

        mock_store.close.assert_awaited_once()
