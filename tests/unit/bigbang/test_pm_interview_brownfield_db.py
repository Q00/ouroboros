"""Tests for PM interview using DB default repo as brownfield context.

Verifies the end-to-end flow where the PM interview loads the default
brownfield repo from the database (via ``get_default_brownfield_context``)
and uses it to provide codebase context during the interview.

Key scenarios:
- Default repo from DB is used as brownfield context in start_interview
- No default repo → greenfield (no codebase context)
- Brownfield context appears in initial_context passed to inner engine
- explore_codebases uses DB-loaded repos when no explicit repos given
- PMSeed generation includes brownfield repos from DB
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.brownfield import get_default_brownfield_context
from ouroboros.bigbang.interview import InterviewStatus
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.core.types import Result
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _mock_completion(content: str = "What problem does this solve?") -> CompletionResponse:
    """Create a mock completion response."""
    return CompletionResponse(
        content=content,
        model="claude-opus-4-6",
        usage=UsageInfo(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        finish_reason="stop",
    )


def _make_adapter() -> MagicMock:
    """Create a mock LLM adapter."""
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.ok(_mock_completion()))
    return adapter


def _make_engine(
    adapter: MagicMock | None = None, tmp_path: Path | None = None
) -> PMInterviewEngine:
    """Create a PMInterviewEngine with mocked dependencies."""
    if adapter is None:
        adapter = _make_adapter()
    state_dir = tmp_path or Path("/tmp/test_pm_interview")
    return PMInterviewEngine.create(
        llm_adapter=adapter,
        state_dir=state_dir,
    )


class TestGetDefaultBrownfieldContext:
    """Test get_default_brownfield_context delegates to BrownfieldStore."""

    @pytest.mark.asyncio
    async def test_returns_default_repos_when_exist(self) -> None:
        """Returns list of default BrownfieldRepo instances."""
        expected = [
            BrownfieldRepo(
                path="/home/user/my-project",
                name="my-project",
                desc="A web application",
                is_default=True,
            ),
        ]
        store = AsyncMock(spec=BrownfieldStore)
        store.get_defaults = AsyncMock(return_value=expected)

        result = await get_default_brownfield_context(store)

        assert len(result) == 1
        assert result[0].path == "/home/user/my-project"
        assert result[0].name == "my-project"
        assert result[0].desc == "A web application"
        assert result[0].is_default is True
        store.get_defaults.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_default(self) -> None:
        """Returns empty list when no default repo is set."""
        store = AsyncMock(spec=BrownfieldStore)
        store.get_defaults = AsyncMock(return_value=[])

        result = await get_default_brownfield_context(store)

        assert result == []
        store.get_defaults.assert_called_once()


class TestPMInterviewWithDBBrownfield:
    """Test PM interview start_interview using DB default repo as brownfield context."""

    @pytest.mark.asyncio
    async def test_start_with_db_default_repo_marks_brownfield_without_a_summary(
        self, tmp_path: Path
    ) -> None:
        """The roster decides brownfield; no summary is embedded in the context.

        The embedded ``Existing Codebase Context`` section is gone with the
        engine-side scan (RFC Q00/ouroboros#1937). What survives is the fact the
        registration always carried — this is brownfield, and here are the paths.
        """
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.start_interview(
            initial_context="Add user notifications",
            brownfield_repos=[
                {"path": "/home/user/my-project", "name": "my-project", "role": "primary"}
            ],
        )

        assert result.is_ok
        state = result.value
        ctx = state.initial_context

        assert "Existing Codebase Context" not in ctx
        assert "Add user notifications" in ctx
        assert state.is_brownfield is True
        assert state.codebase_paths == [{"path": "/home/user/my-project", "role": "primary"}]

    @pytest.mark.asyncio
    async def test_start_without_brownfield_repos_is_greenfield(self, tmp_path: Path) -> None:
        """When no brownfield_repos are passed, interview is greenfield."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.start_interview(
            initial_context="Build a brand new app from scratch",
        )

        assert result.is_ok
        ctx = result.value.initial_context
        assert "Existing Codebase Context" not in ctx
        assert "Build a brand new app from scratch" in ctx

    @pytest.mark.asyncio
    async def test_start_with_empty_brownfield_repos_is_greenfield(self, tmp_path: Path) -> None:
        """When brownfield_repos is an empty list, interview is greenfield."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        result = await engine.start_interview(
            initial_context="Build something new",
            brownfield_repos=[],
        )

        assert result.is_ok
        ctx = result.value.initial_context
        assert "Existing Codebase Context" not in ctx

    @pytest.mark.asyncio
    async def test_start_redirects_repos_to_snapshot_worktrees(self, tmp_path: Path) -> None:
        """Snapshot redirection survives the engine no longer reading code.

        The engine still decides *where* the repositories are read from — the
        snapshot machinery is engine-side, and the roster handed to the lanes is
        built from these records — so a stale local checkout still cannot reach
        the PRD. What changed is only who does the reading.
        """
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        def _fake_refresh(repos: list[dict[str, str]]) -> list[dict[str, str]]:
            return [{**r, "path": f"/snap{r['path']}", "source_path": r["path"]} for r in repos]

        with patch(
            "ouroboros.bigbang.pm_interview.refresh_pm_snapshot_worktrees",
            side_effect=_fake_refresh,
        ) as mock_refresh:
            result = await engine.ask_opening_and_start(
                user_response="Enhance existing project",
                brownfield_repos=[{"path": "/home/user/project", "name": "project"}],
            )

        assert result.is_ok
        mock_refresh.assert_called_once()
        selected = engine._selected_brownfield_repos
        assert selected[0]["path"] == "/snap/home/user/project"
        assert selected[0]["source_path"] == "/home/user/project"
        # State persists the durable checkout, not the snapshot.
        assert result.value.codebase_paths == [{"path": "/home/user/project", "role": "primary"}]

    @pytest.mark.asyncio
    async def test_opening_and_start_with_db_brownfield_repos(self, tmp_path: Path) -> None:
        """ask_opening_and_start forwards brownfield_repos without reading them."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        with patch("ouroboros.bigbang.explore.CodebaseExplorer") as MockExplorer:
            result = await engine.ask_opening_and_start(
                user_response="Enhance existing project",
                brownfield_repos=[
                    {"path": "/home/user/project", "name": "project", "desc": "Main backend"}
                ],
            )

        assert result.is_ok
        MockExplorer.assert_not_called()
        assert result.value.is_brownfield is True


class TestEngineDoesNotExploreDBRepos:
    """The engine reads no repository, whatever the roster says.

    RFC Q00/ouroboros#1937 moved repository reading to the advisory lanes in the
    host session. ``explore_codebases`` survives as a no-op for out-of-engine
    callers, and the empty string it returns is now the correct answer rather
    than a failure signal.
    """

    @pytest.mark.asyncio
    async def test_explore_is_a_no_op_and_reads_nothing(self, tmp_path: Path) -> None:
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        db_repos = [
            {"path": "/home/user/repo-a", "name": "repo-a", "desc": "Repo A"},
            {"path": "/home/user/repo-b", "name": "repo-b", "desc": "Repo B"},
        ]

        with patch.object(PMInterviewEngine, "load_brownfield_repos", return_value=db_repos):
            with patch("ouroboros.bigbang.explore.CodebaseExplorer") as MockExplorer:
                assert await engine.explore_codebases() == ""
                assert await engine.explore_codebases(db_repos) == ""

        MockExplorer.assert_not_called()
        assert engine.codebase_context == ""
        assert engine.classifier.codebase_context == ""

    @pytest.mark.asyncio
    async def test_explore_returns_empty_when_db_has_no_repos(self, tmp_path: Path) -> None:
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        with patch.object(PMInterviewEngine, "load_brownfield_repos", return_value=[]):
            assert await engine.explore_codebases() == ""
            assert engine._explored is True


class TestPMSeedIncludesDBBrownfieldRepos:
    """Test that PMSeed generation includes brownfield repos from DB."""

    @pytest.mark.asyncio
    async def test_pm_seed_includes_brownfield_repos(self, tmp_path: Path) -> None:
        """generate_pm_seed includes brownfield repos loaded from DB in the PMSeed."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        # Mock LLM response for extraction
        extraction_response = """{
            "product_name": "TaskFlow",
            "goal": "Manage tasks efficiently",
            "user_stories": [{"persona": "User", "action": "create tasks", "benefit": "stay organized"}],
            "constraints": ["must work offline"],
            "success_criteria": ["tasks can be created"],
            "deferred_items": [],
            "decide_later_items": [],
            "assumptions": ["users have internet"]
        }"""
        adapter.complete = AsyncMock(return_value=Result.ok(_mock_completion(extraction_response)))

        # Mock load_brownfield_repos to return DB repos
        db_repos = [
            {"path": "/home/user/existing-app", "name": "existing-app", "desc": "Main app"},
        ]

        from ouroboros.bigbang.interview import InterviewRound, InterviewState

        state = InterviewState(
            interview_id="test-seed-gen",
            initial_context="Build a task manager on top of existing-app",
            status=InterviewStatus.COMPLETED,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What problem does this solve?",
                    user_response="We need better task management",
                ),
            ],
        )

        engine._selected_brownfield_repos = db_repos
        result = await engine.generate_pm_seed(state)

        assert result.is_ok
        seed = result.value
        assert seed.product_name == "TaskFlow"
        # Brownfield repos should be included
        assert len(seed.brownfield_repos) == 1
        assert seed.brownfield_repos[0]["path"] == "/home/user/existing-app"
        assert seed.brownfield_repos[0]["name"] == "existing-app"

    @pytest.mark.asyncio
    async def test_pm_seed_records_source_path_over_snapshot_path(self, tmp_path: Path) -> None:
        """Seed must persist the durable source checkout, not the snapshot worktree."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        extraction_response = """{
            "product_name": "TaskFlow",
            "goal": "Manage tasks efficiently",
            "user_stories": [{"persona": "User", "action": "create tasks", "benefit": "stay organized"}],
            "constraints": [],
            "success_criteria": ["tasks can be created"],
            "deferred_items": [],
            "decide_later_items": [],
            "assumptions": []
        }"""
        adapter.complete = AsyncMock(return_value=Result.ok(_mock_completion(extraction_response)))

        from ouroboros.bigbang.interview import InterviewRound, InterviewState

        state = InterviewState(
            interview_id="test-seed-snapshot",
            initial_context="Build on top of existing-app",
            status=InterviewStatus.COMPLETED,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What problem does this solve?",
                    user_response="We need better task management",
                ),
            ],
        )

        engine._selected_brownfield_repos = [
            {
                "path": "/home/user/.ouroboros/pm-snapshots/existing-app-abc12345",
                "source_path": "/home/user/existing-app",
                "name": "existing-app",
            },
        ]
        result = await engine.generate_pm_seed(state)

        assert result.is_ok
        seed = result.value
        assert len(seed.brownfield_repos) == 1
        assert seed.brownfield_repos[0]["path"] == "/home/user/existing-app"
        assert "source_path" not in seed.brownfield_repos[0]


class TestBrownfieldContextInInterviewFlow:
    """Test the full interview flow with DB-backed brownfield context."""

    @pytest.mark.asyncio
    async def test_the_classifier_is_never_primed_with_a_repo_summary(self, tmp_path: Path) -> None:
        """The truncated whole-repo blob is gone.

        The ``code_context`` lane answers the same need per question, with
        evidence the PM can see rather than a summary only the classifier read.
        """
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        await engine.explore_codebases(
            [{"path": "/home/user/my-app", "name": "my-app", "desc": "Web app"}]
        )

        assert engine.codebase_context == ""
        assert engine.classifier.codebase_context == ""

    @pytest.mark.asyncio
    async def test_brownfield_context_included_in_extraction_prompt(self, tmp_path: Path) -> None:
        """Brownfield codebase context is included in extraction via initial_context."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        # Set codebase context as if explore_codebases was called
        engine.codebase_context = "Python project using Django and Celery"

        from ouroboros.bigbang.interview import InterviewRound, InterviewState

        # Brownfield context is now in initial_context (set during start_interview)
        state = InterviewState(
            interview_id="test-extraction",
            initial_context="Add new feature\n\n## Existing Codebase Context (BROWNFIELD)\nPython project using Django and Celery",
            status=InterviewStatus.COMPLETED,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What feature?",
                    user_response="A notification system",
                ),
            ],
        )

        prompt = engine._build_extraction_prompt(engine._build_interview_context(state))

        assert "Django and Celery" in prompt

    @pytest.mark.asyncio
    async def test_no_brownfield_context_omits_section_in_extraction(self, tmp_path: Path) -> None:
        """When no brownfield context, extraction prompt omits brownfield section."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        # No codebase context set (greenfield)
        assert engine.codebase_context == ""

        from ouroboros.bigbang.interview import InterviewRound, InterviewState

        state = InterviewState(
            interview_id="test-greenfield",
            initial_context="Build new app",
            status=InterviewStatus.COMPLETED,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What app?",
                    user_response="A chat app",
                ),
            ],
        )

        prompt = engine._build_extraction_prompt(engine._build_interview_context(state))

        assert "Brownfield codebase context:" not in prompt

    @pytest.mark.asyncio
    async def test_restore_meta_preserves_brownfield_context(self, tmp_path: Path) -> None:
        """restore_meta restores codebase_context and syncs it with classifier."""
        adapter = _make_adapter()
        engine = _make_engine(adapter, tmp_path)

        meta = {
            "deferred_items": ["tech q1"],
            "decide_later_items": ["dl q1"],
            "codebase_context": "Django project with REST API",
            "pending_reframe": None,
        }

        engine.restore_meta(meta)

        assert engine.codebase_context == "Django project with REST API"
        assert engine.classifier.codebase_context == "Django project with REST API"
        # Legacy deferred_items are merged into decide_later_items on restore
        assert engine.deferred_items == []
        assert engine.decide_later_items == ["dl q1", "tech q1"]
