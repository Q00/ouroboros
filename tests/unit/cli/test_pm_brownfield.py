"""The direct ``ouroboros pm`` CLI and the brownfield roster.

Two things: the roster is loaded from the DB, and meeting one stops this
command. The second is the decision recorded in RFC #1937 — brownfield PM
grounding lives on the MCP path that carries the advisory lanes, and this
command does not fan out.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import typer

from ouroboros.bigbang.interview import InterviewState
from ouroboros.cli.commands.pm import (
    _load_brownfield_from_db,
    _refuse_brownfield_in_direct_cli,
    _run_pm_interview,
)
from ouroboros.core.types import Result


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


class TestBrownfieldIsNotAnEntranceHere:
    """The direct CLI refuses brownfield instead of grounding it (#1937, #1941).

    RFC #1937 moved repository reading out of the PM engine into the advisory
    lanes, and the lanes run in the session that fans out. This command does not
    fan out. Two repairs were available — re-ground the CLI, or remove the
    entrance — and removing it leaves one grounding mechanism instead of two
    that have to be kept in step forever.

    What the tests hold is a property, not a message: a PM interview whose Seed
    would claim ``is_brownfield=True`` on memory-only questioning cannot be
    started or continued from here. Revert either guard and the matching test
    reports the interview proceeding.
    """

    def test_a_registered_roster_stops_the_direct_cli(self) -> None:
        with pytest.raises(typer.Exit) as exc:
            _refuse_brownfield_in_direct_cli([{"path": "/repo/api", "name": "api"}])
        assert exc.value.exit_code == 1

    def test_no_roster_is_not_stopped(self) -> None:
        """Greenfield is untouched — over-blocking here would be the same defect."""
        assert _refuse_brownfield_in_direct_cli([]) is None

    @pytest.mark.asyncio
    async def test_a_brownfield_start_never_reaches_the_engine(self) -> None:
        engine = SimpleNamespace(
            ask_opening_and_start=AsyncMock(),
            get_opening_question=lambda: "What do you want to build?",
        )
        with (
            patch("ouroboros.cli.commands.pm.create_llm_adapter", return_value=object()),
            patch(
                "ouroboros.bigbang.pm_interview.PMInterviewEngine.create",
                return_value=engine,
            ),
            patch("ouroboros.cli.commands.pm._check_existing_pm_seeds", return_value=True),
            patch(
                "ouroboros.cli.commands.pm._load_brownfield_from_db",
                return_value=[{"path": "/repo/api", "name": "api"}],
            ),
            pytest.raises(typer.Exit) as exc,
        ):
            await _run_pm_interview(resume_id=None, model="m", backend=None, debug=False)

        assert exc.value.exit_code == 1
        engine.ask_opening_and_start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_greenfield_start_reaches_the_engine_with_no_repos(self) -> None:
        """The other direction, so the refusal cannot be satisfied by refusing all.

        The engine's start is made to fail so the interview loop is never
        entered; what is being checked is that it was called at all, and called
        without repositories.
        """
        engine = SimpleNamespace(
            ask_opening_and_start=AsyncMock(return_value=Result.err("stop here")),
            get_opening_question=lambda: "What do you want to build?",
        )
        with (
            patch("ouroboros.cli.commands.pm.create_llm_adapter", return_value=object()),
            patch(
                "ouroboros.bigbang.pm_interview.PMInterviewEngine.create",
                return_value=engine,
            ),
            patch("ouroboros.cli.commands.pm._check_existing_pm_seeds", return_value=True),
            patch("ouroboros.cli.commands.pm._load_brownfield_from_db", return_value=[]),
            patch(
                "ouroboros.cli.commands.pm.multiline_prompt_async",
                AsyncMock(return_value="a scheduling tool"),
            ),
            pytest.raises(typer.Exit),
        ):
            await _run_pm_interview(resume_id=None, model="m", backend=None, debug=False)

        engine.ask_opening_and_start.assert_awaited_once_with(user_response="a scheduling tool")

    @pytest.mark.asyncio
    async def test_a_brownfield_session_cannot_be_continued_here_either(self) -> None:
        """The resume edge. Without it the property is only half true: a session
        started through MCP would finish its questions ungrounded under a Seed
        that still claims the repositories.
        """
        state = InterviewState(interview_id="pm-bf", initial_context="ctx")
        state.is_brownfield = True
        engine = SimpleNamespace(load_state=AsyncMock(return_value=Result.ok(state)))
        with (
            patch("ouroboros.cli.commands.pm.create_llm_adapter", return_value=object()),
            patch(
                "ouroboros.bigbang.pm_interview.PMInterviewEngine.create",
                return_value=engine,
            ),
            pytest.raises(typer.Exit) as exc,
        ):
            await _run_pm_interview(resume_id="pm-bf", model="m", backend=None, debug=False)

        assert exc.value.exit_code == 1

    @pytest.mark.asyncio
    async def test_a_roster_held_only_in_pm_meta_is_refused_too(self, tmp_path) -> None:
        """The guard asks the repositories, not a flag kept beside them.

        A session that selected its roster through the passive-plugin path wrote
        it to ``pm_meta`` and left ``is_brownfield`` false, so reading the flag
        let the resume through — and the very next step restored those
        repositories and carried on without lanes.
        """
        state = InterviewState(interview_id="pm-plugin", initial_context="ctx")
        assert not state.is_brownfield and not state.codebase_paths
        data_dir = tmp_path / ".ouroboros" / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "pm_meta_pm-plugin.json").write_text(
            json.dumps({"brownfield_repos": [{"path": "/repo/api"}]}), encoding="utf-8"
        )
        engine = SimpleNamespace(load_state=AsyncMock(return_value=Result.ok(state)))
        with (
            patch("ouroboros.cli.commands.pm.Path.home", return_value=tmp_path),
            patch("ouroboros.cli.commands.pm.create_llm_adapter", return_value=object()),
            patch(
                "ouroboros.bigbang.pm_interview.PMInterviewEngine.create",
                return_value=engine,
            ),
            pytest.raises(typer.Exit) as exc,
        ):
            await _run_pm_interview(resume_id="pm-plugin", model="m", backend=None, debug=False)

        assert exc.value.exit_code == 1

    @pytest.mark.asyncio
    async def test_a_greenfield_session_still_resumes(self) -> None:
        """And the resume guard is not satisfied by refusing every resume."""
        from ouroboros.bigbang.interview import InterviewStatus

        state = InterviewState(interview_id="pm-gf", initial_context="ctx")
        state.status = InterviewStatus.COMPLETED
        engine = SimpleNamespace(
            load_state=AsyncMock(return_value=Result.ok(state)),
            restore_meta=lambda _m: None,
            _install_pm_steering=lambda: None,
            format_decide_later_summary=lambda: "",
        )
        with (
            patch("ouroboros.cli.commands.pm.create_llm_adapter", return_value=object()),
            patch(
                "ouroboros.bigbang.pm_interview.PMInterviewEngine.create",
                return_value=engine,
            ),
        ):
            await _run_pm_interview(resume_id="pm-gf", model="m", backend=None, debug=False)

        engine.load_state.assert_awaited_once_with("pm-gf")
