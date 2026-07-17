"""Tests for PM snapshot-worktree wiring on the plugin-dispatch path.

The engine-driven paths (MCP in-process, CLI) are covered by the
``start_interview`` hook tests in ``tests/unit/bigbang``. These tests pin
the plugin-dispatch contract: ``selected_repos`` forwarded to the child
session must be redirected to refreshed snapshot worktrees before they are
persisted in pm_meta or embedded in the subagent payload.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.tools.pm_handler import (
    PMInterviewHandler,
    _plugin_repo_paths,
    _refresh_plugin_repo_paths,
    _refresh_plugin_repo_records,
)


def _fake_refresh(repos: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{**r, "path": f"/snap{r['path']}", "source_path": r["path"]} for r in repos]


class TestRefreshPluginRepoPaths:
    def test_retains_scan_and_source_paths_for_persistence(self) -> None:
        with patch(
            "ouroboros.mcp.tools.pm_handler.refresh_pm_snapshot_worktrees",
            side_effect=_fake_refresh,
        ):
            result = _refresh_plugin_repo_records(["/repo/a"])
        assert result == [{"path": "/snap/repo/a", "source_path": "/repo/a"}]
        assert _plugin_repo_paths(result) == ["/snap/repo/a"]
        assert _plugin_repo_paths(result, durable=True) == ["/repo/a"]

    def test_swaps_string_paths_and_passes_through_others(self) -> None:
        with patch(
            "ouroboros.mcp.tools.pm_handler.refresh_pm_snapshot_worktrees",
            side_effect=_fake_refresh,
        ):
            result = _refresh_plugin_repo_paths(["/repo/a", 42, "/repo/b"])
        assert result == ["/snap/repo/a", 42, "/snap/repo/b"]


class TestPluginDispatchSnapshots:
    """selected_repos must be snapshot-redirected before plugin dispatch."""

    async def _dispatch(
        self, handler: PMInterviewHandler, args: dict[str, Any]
    ) -> tuple[Any, AsyncMock]:
        dispatch_mock = AsyncMock(return_value=Result.ok(None))
        with (
            patch(
                "ouroboros.mcp.tools.pm_handler.should_dispatch_via_plugin",
                return_value=True,
            ),
            patch(
                "ouroboros.mcp.tools.pm_handler.dispatch_plugin_terminal",
                new=dispatch_mock,
            ),
            patch(
                "ouroboros.mcp.tools.pm_handler.refresh_pm_snapshot_worktrees",
                side_effect=_fake_refresh,
            ),
        ):
            result = await handler.handle(args)
        return result, dispatch_mock

    @staticmethod
    def _load_single_meta(data_dir: Path) -> tuple[str, dict[str, Any]]:
        meta_files = list(data_dir.glob("pm_meta_*.json"))
        assert len(meta_files) == 1
        session_id = meta_files[0].stem.removeprefix("pm_meta_")
        return session_id, json.loads(meta_files[0].read_text())

    @pytest.mark.asyncio
    async def test_start_with_selected_repos_forwards_snapshot_paths(self, tmp_path: Path) -> None:
        handler = PMInterviewHandler(data_dir=tmp_path)
        result, dispatch_mock = await self._dispatch(
            handler,
            {
                "initial_context": "Build a review reminder feature",
                "selected_repos": ["/repo/a"],
                "cwd": str(tmp_path),
            },
        )

        assert result.is_ok
        _, meta = self._load_single_meta(tmp_path)
        assert meta["brownfield_repos"] == [{"path": "/snap/repo/a", "source_path": "/repo/a"}]
        payload = dispatch_mock.call_args.kwargs["payload"]
        assert payload.context["selected_repos"] == ["/snap/repo/a"]

    @pytest.mark.asyncio
    async def test_select_repos_step_persists_snapshot_paths(self, tmp_path: Path) -> None:
        handler = PMInterviewHandler(data_dir=tmp_path)

        # Step 1: start without repos to create the session + pm_meta.
        result, _ = await self._dispatch(
            handler,
            {
                "initial_context": "Build a review reminder feature",
                "cwd": str(tmp_path),
            },
        )
        assert result.is_ok
        session_id, _ = self._load_single_meta(tmp_path)

        # Step 2: select repos — persisted + forwarded paths must be snapshots.
        result, dispatch_mock = await self._dispatch(
            handler,
            {
                "action": "select_repos",
                "session_id": session_id,
                "selected_repos": ["/repo/a", "/repo/b"],
            },
        )

        assert result.is_ok
        _, meta = self._load_single_meta(tmp_path)
        assert meta["brownfield_repos"] == [
            {"path": "/snap/repo/a", "source_path": "/repo/a"},
            {"path": "/snap/repo/b", "source_path": "/repo/b"},
        ]
        payload = dispatch_mock.call_args.kwargs["payload"]
        assert payload.context["selected_repos"] == ["/snap/repo/a", "/snap/repo/b"]

    @pytest.mark.asyncio
    async def test_generate_restores_durable_source_paths(self, tmp_path: Path) -> None:
        handler = PMInterviewHandler(data_dir=tmp_path)
        result, _ = await self._dispatch(
            handler,
            {
                "initial_context": "Build a review reminder feature",
                "selected_repos": ["/repo/a", "/repo/b"],
                "cwd": str(tmp_path),
            },
        )
        assert result.is_ok
        session_id, _ = self._load_single_meta(tmp_path)

        result, _ = await self._dispatch(
            handler,
            {
                "action": "resume",
                "session_id": session_id,
                "answer": "Notify reviewers after 24 hours.",
                "last_question": "When should reminders be sent?",
            },
        )
        assert result.is_ok

        result, dispatch_mock = await self._dispatch(
            handler,
            {"action": "generate", "session_id": session_id},
        )

        assert result.is_ok
        payload = dispatch_mock.call_args.kwargs["payload"]
        assert payload.context["selected_repos"] == ["/repo/a", "/repo/b"]
        assert "/snap/repo/a" not in payload.prompt
        assert "- /repo/a" in payload.prompt

    @pytest.mark.asyncio
    async def test_cwd_derived_fallback_is_not_redirected(self, tmp_path: Path) -> None:
        """Without selected_repos, the cwd fallback must stay untouched (live WIP repo)."""
        handler = PMInterviewHandler(data_dir=tmp_path)
        refresh_mock = AsyncMock()
        with (
            patch(
                "ouroboros.mcp.tools.pm_handler.should_dispatch_via_plugin",
                return_value=True,
            ),
            patch(
                "ouroboros.mcp.tools.pm_handler.dispatch_plugin_terminal",
                new=AsyncMock(return_value=Result.ok(None)),
            ),
            patch(
                "ouroboros.mcp.tools.pm_handler.refresh_pm_snapshot_worktrees",
                new=refresh_mock,
            ),
        ):
            result = await handler.handle(
                {
                    "initial_context": "Build a review reminder feature",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        refresh_mock.assert_not_called()
