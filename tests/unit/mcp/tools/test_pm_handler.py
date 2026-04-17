"""Tests for PMInterviewHandler — start/brownfield (AC 2), diff computation (AC 8), completion (AC 12)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.question_classifier import (
    ClassificationResult,
    ClassifierOutputType,
    QuestionCategory,
)
from ouroboros.core.types import Result
from ouroboros.mcp.tools.pm_handler import (
    _DATA_DIR,
    PMInterviewHandler,
    _check_completion,
    _compute_deferred_diff,
    _detect_action,
    _load_pm_meta,
    _meta_path,
    _restore_engine_meta,
    _save_pm_meta,
)
from tests.unit.mcp.tools.conftest import make_pm_engine_mock

# ── Helpers ──────────────────────────────────────────────────────


def _make_classification(output_type: ClassifierOutputType) -> ClassificationResult:
    """Create a minimal ClassificationResult stub for a given output type."""
    category_map = {
        ClassifierOutputType.PASSTHROUGH: QuestionCategory.PLANNING,
        ClassifierOutputType.REFRAMED: QuestionCategory.DEVELOPMENT,
        ClassifierOutputType.DEFERRED: QuestionCategory.DEVELOPMENT,
        ClassifierOutputType.DECIDE_LATER: QuestionCategory.DECIDE_LATER,
    }
    return ClassificationResult(
        original_question="stub question",
        category=category_map[output_type],
        reframed_question="stub question",
        reasoning="stub",
        defer_to_dev=(output_type == ClassifierOutputType.DEFERRED),
        decide_later=(output_type == ClassifierOutputType.DECIDE_LATER),
    )


def _make_engine_stub(
    deferred: list[str] | None = None,
    decide_later: list[str] | None = None,
) -> PMInterviewEngine:
    """Create a PMInterviewEngine stub with controllable lists."""

    return make_pm_engine_mock(
        deferred_items=list(deferred or []),
        decide_later_items=list(decide_later or []),
    )


def _make_state(
    interview_id: str = "test-session-1",
    rounds: list[InterviewRound] | None = None,
    is_brownfield: bool = False,
) -> InterviewState:
    """Create a minimal InterviewState for testing."""
    state = MagicMock(spec=InterviewState)
    state.interview_id = interview_id
    state.initial_context = "Build a task manager"
    state.rounds = list(rounds or [])
    state.current_round_number = len(state.rounds) + 1
    state.is_complete = False
    state.is_brownfield = is_brownfield
    state.ambiguity_score = None
    state.mark_updated = MagicMock()
    state.clear_stored_ambiguity = MagicMock()
    return state


# ── Unit tests for _compute_deferred_diff ────────────────────────


class TestComputeDeferredDiff:
    """Test the core diff computation function."""

    def test_no_new_items(self) -> None:
        """Diff is empty when no items were added."""
        engine = _make_engine_stub(deferred=["old1"], decide_later=["old_dl1"])
        diff = _compute_deferred_diff(engine, deferred_len_before=1, decide_later_len_before=1)

        assert diff["new_deferred"] == []
        assert diff["new_decide_later"] == []
        assert diff["deferred_count"] == 0
        assert diff["decide_later_count"] == 2  # combined: 1 deferred + 1 decide_later

    def test_one_new_deferred(self) -> None:
        """Diff captures a single newly deferred item."""
        engine = _make_engine_stub(deferred=["old1", "new_deferred_q"])
        diff = _compute_deferred_diff(engine, deferred_len_before=1, decide_later_len_before=0)

        assert diff["new_deferred"] == ["new_deferred_q"]
        assert diff["new_decide_later"] == []
        assert diff["deferred_count"] == 0

    def test_one_new_decide_later(self) -> None:
        """Diff captures a single newly decide-later item."""
        engine = _make_engine_stub(decide_later=["old_dl", "new_dl_q"])
        diff = _compute_deferred_diff(engine, deferred_len_before=0, decide_later_len_before=1)

        assert diff["new_deferred"] == []
        assert diff["new_decide_later"] == ["new_dl_q"]
        assert diff["decide_later_count"] == 2

    def test_multiple_new_items_both_lists(self) -> None:
        """Diff captures multiple new items in both lists.

        This happens when ask_next_question recursively defers/decide-laters
        several questions before finding a PASSTHROUGH or REFRAMED one.
        """
        engine = _make_engine_stub(
            deferred=["old_d", "new_d1", "new_d2"],
            decide_later=["old_dl", "new_dl1", "new_dl2", "new_dl3"],
        )
        diff = _compute_deferred_diff(engine, deferred_len_before=1, decide_later_len_before=1)

        assert diff["new_deferred"] == ["new_d1", "new_d2"]
        assert diff["new_decide_later"] == ["new_dl1", "new_dl2", "new_dl3"]
        assert diff["deferred_count"] == 0
        assert diff["decide_later_count"] == 7  # combined: 3 deferred + 4 decide_later

    def test_empty_lists_with_zero_before(self) -> None:
        """Handles empty lists with zero snapshot gracefully."""
        engine = _make_engine_stub()
        diff = _compute_deferred_diff(engine, deferred_len_before=0, decide_later_len_before=0)

        assert diff["new_deferred"] == []
        assert diff["new_decide_later"] == []
        assert diff["deferred_count"] == 0
        assert diff["decide_later_count"] == 0

    def test_all_items_are_new(self) -> None:
        """When snapshot was 0, all items are new."""
        engine = _make_engine_stub(
            deferred=["d1", "d2"],
            decide_later=["dl1"],
        )
        diff = _compute_deferred_diff(engine, deferred_len_before=0, decide_later_len_before=0)

        assert diff["new_deferred"] == ["d1", "d2"]
        assert diff["new_decide_later"] == ["dl1"]


# ── Unit tests for meta persistence ─────────────────────────────


class TestPrdMetaPersistence:
    """Test save/load/restore of pm_meta JSON files."""

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        """Meta data survives save/load roundtrip."""
        engine = _make_engine_stub(
            deferred=["q1", "q2"],
            decide_later=["dl1"],
        )
        engine.codebase_context = "some context"

        _save_pm_meta("sess-1", engine, cwd="/tmp/proj", data_dir=tmp_path)
        meta = _load_pm_meta("sess-1", data_dir=tmp_path)

        assert meta is not None
        assert meta["deferred_items"] == []  # Deprecated: merged into decide_later_items
        assert meta["decide_later_items"] == ["dl1", "q1", "q2"]
        assert meta["codebase_context"] == "some context"
        assert meta["cwd"] == "/tmp/proj"

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        """Loading nonexistent meta returns None."""
        assert _load_pm_meta("nonexistent", data_dir=tmp_path) is None

    def test_restore_engine_meta(self) -> None:
        """Engine state is restored from meta dict."""
        engine = _make_engine_stub()
        meta = {
            "deferred_items": ["d1", "d2"],
            "decide_later_items": ["dl1"],
            "codebase_context": "ctx",
            "pending_reframe": {"reframed": "simple q", "original": "technical q"},
            "cwd": "/proj",
            "brownfield_repos": [{"path": "/repo", "name": "repo"}],
        }

        _restore_engine_meta(engine, meta)

        # Legacy deferred_items are merged into decide_later_items on restore
        assert engine.deferred_items == []
        assert engine.decide_later_items == ["dl1", "d1", "d2"]
        assert engine.codebase_context == "ctx"
        assert engine._reframe_map["simple q"] == "technical q"
        assert engine._selected_brownfield_repos == [{"path": "/repo", "name": "repo"}]

    def test_restore_without_pending_reframe(self) -> None:
        """Restore works when pending_reframe is None."""
        engine = _make_engine_stub()
        meta: dict[str, object] = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": None,
            "cwd": "",
        }
        _restore_engine_meta(engine, meta)
        assert engine._reframe_map == {}

    def test_save_captures_pending_reframe(self, tmp_path: Path) -> None:
        """Save captures the most recent reframe mapping."""
        engine = _make_engine_stub()
        engine._reframe_map = {"pm question": "tech question"}

        _save_pm_meta("sess-2", engine, data_dir=tmp_path)
        meta = _load_pm_meta("sess-2", data_dir=tmp_path)

        assert meta is not None
        assert meta["pending_reframe"] == {
            "reframed": "pm question",
            "original": "tech question",
        }


# ── AC 6: pm_meta alongside interview state in ~/.ouroboros/data/ ─


class TestPrdMetaFileLocation:
    """Verify pm_meta_{session_id}.json is persisted in ~/.ouroboros/data/
    alongside interview state files (AC 6)."""

    def test_default_data_dir_is_ouroboros_data(self) -> None:
        """_DATA_DIR points to ~/.ouroboros/data/."""
        expected = Path.home() / ".ouroboros" / "data"
        assert expected == _DATA_DIR

    def test_meta_path_uses_session_id_in_filename(self) -> None:
        """_meta_path produces pm_meta_{session_id}.json naming."""
        path = _meta_path("my-session-42")
        assert path.name == "pm_meta_my-session-42.json"

    def test_meta_path_default_dir_matches_data_dir(self) -> None:
        """Default _meta_path parent is _DATA_DIR (same as interview state)."""
        path = _meta_path("sess-1")
        assert path.parent == _DATA_DIR

    def test_meta_path_custom_dir(self, tmp_path: Path) -> None:
        """_meta_path respects custom data_dir override."""
        path = _meta_path("sess-x", data_dir=tmp_path)
        assert path.parent == tmp_path
        assert path.name == "pm_meta_sess-x.json"

    def test_meta_file_alongside_interview_state(self, tmp_path: Path) -> None:
        """pm_meta and interview state live in the same directory.

        This is the core AC 6 requirement: both files share the data_dir
        so they can be discovered together.
        """
        # Simulate interview state file
        interview_state_path = tmp_path / "interview_sess-co.json"
        interview_state_path.write_text('{"interview_id": "sess-co"}')

        # Save pm_meta in the same directory
        engine = _make_engine_stub(deferred=["q1"], decide_later=["dl1"])
        engine.codebase_context = "brownfield context"
        _save_pm_meta("sess-co", engine, cwd="/proj", data_dir=tmp_path)

        # Both files exist in the same directory
        meta_path = _meta_path("sess-co", tmp_path)
        assert meta_path.exists()
        assert meta_path.parent == interview_state_path.parent

    def test_save_creates_directory_if_missing(self, tmp_path: Path) -> None:
        """_save_pm_meta creates parent directories as needed."""
        nested_dir = tmp_path / "nested" / "deep"
        assert not nested_dir.exists()

        engine = _make_engine_stub()
        _save_pm_meta("sess-nested", engine, data_dir=nested_dir)

        assert nested_dir.exists()
        meta = _load_pm_meta("sess-nested", data_dir=nested_dir)
        assert meta is not None

    def test_meta_file_has_expected_fields(self, tmp_path: Path) -> None:
        """pm_meta JSON contains all required fields."""
        engine = _make_engine_stub(deferred=["d1"], decide_later=["dl1"])
        engine.codebase_context = "ctx"
        engine._reframe_map = {"simple": "complex"}

        _save_pm_meta("sess-fields", engine, cwd="/w", data_dir=tmp_path)
        meta = _load_pm_meta("sess-fields", data_dir=tmp_path)

        assert meta is not None
        expected_keys = {
            "deferred_items",
            "decide_later_items",
            "codebase_context",
            "pending_reframe",
            "cwd",
            "brownfield_repos",
            "classifications",
            "initial_context",
        }
        assert set(meta.keys()) == expected_keys

    def test_meta_persists_across_multiple_saves(self, tmp_path: Path) -> None:
        """Subsequent saves overwrite the same file correctly."""
        engine = _make_engine_stub(deferred=["q1"])
        _save_pm_meta("sess-over", engine, cwd="/v1", data_dir=tmp_path)

        # Update engine state and re-save
        engine.deferred_items = ["q1", "q2", "q3"]
        engine.codebase_context = "updated context"
        _save_pm_meta("sess-over", engine, cwd="/v2", data_dir=tmp_path)

        meta = _load_pm_meta("sess-over", data_dir=tmp_path)
        assert meta is not None
        assert meta["deferred_items"] == []  # Deprecated: merged into decide_later_items
        assert "q1" in meta["decide_later_items"]
        assert "q2" in meta["decide_later_items"]
        assert "q3" in meta["decide_later_items"]
        assert meta["codebase_context"] == "updated context"
        assert meta["cwd"] == "/v2"

    def test_load_corrupted_file_returns_none(self, tmp_path: Path) -> None:
        """Loading corrupted JSON returns None gracefully."""
        path = _meta_path("sess-bad", tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json{{{", encoding="utf-8")

        assert _load_pm_meta("sess-bad", data_dir=tmp_path) is None


# ── Integration test: diff in handler.handle ─────────────────────


class TestPMHandlerDefinition:
    """Test handler definition properties (not dependent on handle() flow)."""

    def test_definition_name(self) -> None:
        """Handler has correct tool name."""
        handler = PMInterviewHandler()
        assert handler.definition.name == "ouroboros_pm_interview"

    def test_definition_has_flat_optional_params(self) -> None:
        """All parameters are optional (flat params pattern)."""
        handler = PMInterviewHandler()
        defn = handler.definition
        for param in defn.parameters:
            assert param.required is False, f"{param.name} should be optional"


# ── Unit tests for _check_completion (AC 12) ─────────────────────


def _make_answered_rounds(n: int) -> list[InterviewRound]:
    """Create n answered rounds for testing."""
    return [
        InterviewRound(
            round_number=i + 1,
            question=f"Question {i + 1}?",
            user_response=f"Answer {i + 1}",
        )
        for i in range(n)
    ]


class TestCheckCompletion:
    """Test that interview completion is determined solely by engine, not user 'done' signal."""

    @pytest.mark.asyncio
    async def test_returns_none_before_min_rounds(self) -> None:
        """No completion check before MIN_ROUNDS_BEFORE_EARLY_EXIT rounds."""
        state = _make_state(rounds=_make_answered_rounds(2))
        engine = _make_engine_stub()

        result = await _check_completion(state, engine)
        assert result is None

    @pytest.mark.asyncio
    async def test_ambiguity_resolved_triggers_completion(self) -> None:
        """Low ambiguity score (≤0.2) triggers completion after min rounds."""
        from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown

        state = _make_state(rounds=_make_answered_rounds(5))
        state.is_brownfield = False
        engine = _make_engine_stub()
        engine.llm_adapter = MagicMock()
        engine.model = "test-model"

        # Mock the AmbiguityScorer to return a low score
        mock_score = AmbiguityScore(
            overall_score=0.15,
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal", clarity_score=0.9, weight=0.4, justification="Clear"
                ),
                constraint_clarity=ComponentScore(
                    name="Constraints", clarity_score=0.85, weight=0.3, justification="Clear"
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success", clarity_score=0.85, weight=0.3, justification="Clear"
                ),
            ),
        )

        with patch("ouroboros.bigbang.pm_interview.AmbiguityScorer") as mock_scorer_cls:
            mock_scorer = MagicMock()
            mock_scorer.score = AsyncMock(return_value=Result.ok(mock_score))
            mock_scorer_cls.return_value = mock_scorer

            result = await _check_completion(state, engine)

        assert result is not None
        assert result["interview_complete"] is True
        assert result["completion_reason"] == "ambiguity_resolved"
        assert result["ambiguity_score"] == 0.15
        assert result["rounds_completed"] == 5

    @pytest.mark.asyncio
    async def test_high_ambiguity_continues_interview(self) -> None:
        """High ambiguity score (>0.2) does NOT trigger completion."""
        from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown

        state = _make_state(rounds=_make_answered_rounds(5))
        state.is_brownfield = False
        engine = _make_engine_stub()
        engine.llm_adapter = MagicMock()
        engine.model = "test-model"

        mock_score = AmbiguityScore(
            overall_score=0.45,
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal", clarity_score=0.6, weight=0.4, justification="Vague"
                ),
                constraint_clarity=ComponentScore(
                    name="Constraints", clarity_score=0.5, weight=0.3, justification="Unclear"
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success", clarity_score=0.55, weight=0.3, justification="Vague"
                ),
            ),
        )

        with patch("ouroboros.bigbang.pm_interview.AmbiguityScorer") as mock_scorer_cls:
            mock_scorer = MagicMock()
            mock_scorer.score = AsyncMock(return_value=Result.ok(mock_score))
            mock_scorer_cls.return_value = mock_scorer

            result = await _check_completion(state, engine)

        assert result is None

    @pytest.mark.asyncio
    async def test_scoring_failure_continues_interview(self) -> None:
        """If ambiguity scoring fails, the interview continues (no blocking)."""
        from ouroboros.core.errors import ProviderError

        state = _make_state(rounds=_make_answered_rounds(5))
        state.is_brownfield = False
        engine = _make_engine_stub()
        engine.llm_adapter = MagicMock()
        engine.model = "test-model"

        with patch("ouroboros.bigbang.pm_interview.AmbiguityScorer") as mock_scorer_cls:
            mock_scorer = MagicMock()
            mock_scorer.score = AsyncMock(return_value=Result.err(ProviderError("LLM down")))
            mock_scorer_cls.return_value = mock_scorer

            result = await _check_completion(state, engine)

        assert result is None

    @pytest.mark.asyncio
    async def test_unanswered_rounds_not_counted(self) -> None:
        """Only answered rounds count toward completion thresholds."""
        rounds = _make_answered_rounds(2)
        # Add an unanswered round (pending question)
        rounds.append(InterviewRound(round_number=3, question="Pending?", user_response=None))
        state = _make_state(rounds=rounds)
        engine = _make_engine_stub()

        # 2 answered rounds < MIN_ROUNDS_BEFORE_EARLY_EXIT (3)
        result = await _check_completion(state, engine)
        assert result is None

    @pytest.mark.asyncio
    async def test_decide_later_items_passed_as_additional_context(self) -> None:
        """Decide-later items are passed to the scorer as additional context."""
        from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown

        state = _make_state(rounds=_make_answered_rounds(4))
        state.is_brownfield = False
        engine = _make_engine_stub(decide_later=["Should we use Redis?", "What API format?"])
        engine.llm_adapter = MagicMock()
        engine.model = "test-model"

        mock_score = AmbiguityScore(
            overall_score=0.35,
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal", clarity_score=0.7, weight=0.4, justification="OK"
                ),
                constraint_clarity=ComponentScore(
                    name="Constraints", clarity_score=0.6, weight=0.3, justification="OK"
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success", clarity_score=0.65, weight=0.3, justification="OK"
                ),
            ),
        )

        with patch("ouroboros.bigbang.pm_interview.AmbiguityScorer") as mock_scorer_cls:
            mock_scorer = MagicMock()
            mock_scorer.score = AsyncMock(return_value=Result.ok(mock_score))
            mock_scorer_cls.return_value = mock_scorer

            await _check_completion(state, engine)

            # Verify scorer was called with additional_context containing decide-later items
            call_kwargs = mock_scorer.score.call_args
            additional_ctx = call_kwargs.kwargs.get("additional_context", "")
            assert "Should we use Redis?" in additional_ctx
            assert "What API format?" in additional_ctx


# ── Tests for _handle_start: engine creation & brownfield detection (AC 2) ──


class TestGetEngine:
    """Test PMInterviewEngine creation via _get_engine."""

    def test_returns_injected_engine(self) -> None:
        """When pm_engine is injected, _get_engine returns it directly."""
        engine = _make_engine_stub()
        handler = PMInterviewHandler(pm_engine=engine)
        assert handler._get_engine() is engine

    def test_creates_engine_when_none_injected(self) -> None:
        """When no engine is injected, creates one via create_llm_adapter factory."""
        with (
            patch("ouroboros.mcp.tools.pm_handler.create_llm_adapter") as mock_create,
            patch("ouroboros.mcp.tools.pm_handler.get_clarification_model") as mock_model,
            patch("ouroboros.mcp.tools.pm_handler.PMInterviewEngine") as mock_engine_cls,
        ):
            mock_adapter = MagicMock()
            mock_create.return_value = mock_adapter
            mock_model.return_value = "claude-opus-4-6"
            mock_engine_cls.create.return_value = MagicMock()

            handler = PMInterviewHandler()
            handler._get_engine()

            mock_create.assert_called_once_with(
                backend=None,
                max_turns=1,
                use_case="interview",
                allowed_tools=[],
            )
            mock_engine_cls.create.assert_called_once()
            call_kwargs = mock_engine_cls.create.call_args
            assert call_kwargs.kwargs["llm_adapter"] is mock_adapter
            assert call_kwargs.kwargs["model"] == "claude-opus-4-6"

    def test_uses_custom_data_dir(self, tmp_path: Path) -> None:
        """When data_dir is set, passes it to PMInterviewEngine.create."""
        with (
            patch("ouroboros.mcp.tools.pm_handler.create_llm_adapter"),
            patch("ouroboros.mcp.tools.pm_handler.get_clarification_model", return_value="m"),
            patch("ouroboros.mcp.tools.pm_handler.PMInterviewEngine") as mock_engine_cls,
        ):
            mock_engine_cls.create.return_value = MagicMock()

            handler = PMInterviewHandler(data_dir=tmp_path)
            handler._get_engine()

            call_kwargs = mock_engine_cls.create.call_args
            assert call_kwargs.kwargs["state_dir"] == tmp_path


# ── Action auto-detection tests (AC 13) ───────────────────────


class TestDetectAction:
    """Test _detect_action auto-detects action from parameter presence."""

    def test_explicit_action_returned_as_is(self):
        """Explicit action param takes precedence over auto-detection."""
        assert _detect_action({"action": "generate", "session_id": "s1"}) == "generate"

    def test_explicit_action_start(self):
        """Explicit action='start' is returned even with session_id."""
        assert _detect_action({"action": "start", "session_id": "s1"}) == "start"

    def test_initial_context_auto_detects_start(self):
        """initial_context without action → 'start'."""
        assert _detect_action({"initial_context": "Build a todo app"}) == "start"

    def test_initial_context_with_cwd_auto_detects_start(self):
        """initial_context + cwd without action → 'start'."""
        assert _detect_action({"initial_context": "Build X", "cwd": "/tmp"}) == "start"

    def test_session_id_auto_detects_resume(self):
        """session_id alone without action → 'resume'."""
        assert _detect_action({"session_id": "abc-123"}) == "resume"

    def test_session_id_with_answer_auto_detects_resume(self):
        """session_id + answer without action → 'resume'."""
        assert _detect_action({"session_id": "abc-123", "answer": "Yes"}) == "resume"

    def test_session_id_with_answer_and_cwd_auto_detects_resume(self):
        """session_id + answer + cwd without action → 'resume'."""
        assert (
            _detect_action(
                {
                    "session_id": "abc-123",
                    "answer": "Yes",
                    "cwd": "/projects/myapp",
                }
            )
            == "resume"
        )

    def test_no_params_returns_unknown(self):
        """Empty arguments → 'unknown'."""
        assert _detect_action({}) == "unknown"

    def test_only_cwd_returns_unknown(self):
        """Only cwd provided → 'unknown' (not enough to detect action)."""
        assert _detect_action({"cwd": "/tmp"}) == "unknown"

    def test_only_answer_returns_unknown(self):
        """Only answer without session_id → 'unknown'."""
        assert _detect_action({"answer": "some answer"}) == "unknown"

    def test_initial_context_takes_priority_over_session_id(self):
        """When both initial_context and session_id provided, start wins."""
        result = _detect_action(
            {
                "initial_context": "Build X",
                "session_id": "s1",
            }
        )
        assert result == "start"

    def test_empty_initial_context_falls_through(self):
        """Empty string initial_context is falsy → falls through."""
        assert _detect_action({"initial_context": "", "session_id": "s1"}) == "resume"

    def test_none_action_treated_as_omitted(self):
        """action=None is treated same as omitted."""
        assert _detect_action({"action": None, "initial_context": "X"}) == "start"

    # ── selected_repos → select_repos (AC 4) ──────────────────

    def test_selected_repos_auto_detects_select_repos(self):
        """selected_repos present → 'select_repos' (2-step start step 2)."""
        assert _detect_action({"selected_repos": ["/path/a"]}) == "select_repos"

    def test_selected_repos_empty_list_detects_select_repos(self):
        """Even an empty list triggers select_repos (user deselected all)."""
        assert _detect_action({"selected_repos": []}) == "select_repos"

    def test_selected_repos_with_initial_context_is_1step_start(self):
        """selected_repos + initial_context → 'start' (1-step backward compat, AC 8)."""
        result = _detect_action(
            {
                "selected_repos": ["/path/a"],
                "initial_context": "Build X",
            }
        )
        assert result == "start"

    def test_selected_repos_takes_priority_over_session_id(self):
        """selected_repos takes priority over session_id."""
        result = _detect_action(
            {
                "selected_repos": ["/path/a"],
                "session_id": "s1",
            }
        )
        assert result == "select_repos"

    def test_selected_repos_with_all_params_1step(self):
        """selected_repos + initial_context + session_id → 'start' (AC 8, initial_context wins)."""
        result = _detect_action(
            {
                "selected_repos": ["/repo"],
                "initial_context": "Build X",
                "session_id": "s1",
            }
        )
        assert result == "start"

    def test_explicit_action_overrides_selected_repos(self):
        """Explicit action still takes precedence even with selected_repos."""
        result = _detect_action(
            {
                "action": "resume",
                "selected_repos": ["/path/a"],
                "session_id": "s1",
            }
        )
        assert result == "resume"

    def test_selected_repos_none_does_not_trigger(self):
        """selected_repos=None (absent) should NOT trigger select_repos."""
        assert _detect_action({"selected_repos": None, "initial_context": "X"}) == "start"

    def test_selected_repos_without_initial_context_is_step2(self):
        """selected_repos without initial_context → 'select_repos' (2-step step 2)."""
        assert _detect_action({"selected_repos": ["/a"], "session_id": "s1"}) == "select_repos"

    def test_empty_selected_repos_with_initial_context_is_1step(self):
        """Even empty selected_repos + initial_context → 'start' (AC 8)."""
        result = _detect_action(
            {
                "selected_repos": [],
                "initial_context": "Build X",
            }
        )
        assert result == "start"
