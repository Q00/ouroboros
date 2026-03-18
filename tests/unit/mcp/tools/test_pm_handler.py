"""Tests for PMInterviewHandler — start/brownfield (AC 2), diff computation (AC 8), completion (AC 12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.core.types import Result
from ouroboros.mcp.tools.pm_handler import (
    _DATA_DIR,
    MAX_PM_INTERVIEW_ROUNDS,
    PMInterviewHandler,
    _check_completion,
    _compute_deferred_diff,
    _detect_action,
    _load_pm_meta,
    _meta_path,
    _restore_engine_meta,
    _save_pm_meta,
)

# ── Helpers ──────────────────────────────────────────────────────


def _make_engine_stub(
    deferred: list[str] | None = None,
    decide_later: list[str] | None = None,
) -> PMInterviewEngine:
    """Create a PMInterviewEngine stub with controllable lists."""
    engine = MagicMock(spec=PMInterviewEngine)
    engine.deferred_items = list(deferred or [])
    engine.decide_later_items = list(decide_later or [])
    engine.codebase_context = ""
    engine._reframe_map = {}
    engine.classifications = []
    return engine


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
        assert diff["deferred_count"] == 1
        assert diff["decide_later_count"] == 1

    def test_one_new_deferred(self) -> None:
        """Diff captures a single newly deferred item."""
        engine = _make_engine_stub(deferred=["old1", "new_deferred_q"])
        diff = _compute_deferred_diff(engine, deferred_len_before=1, decide_later_len_before=0)

        assert diff["new_deferred"] == ["new_deferred_q"]
        assert diff["new_decide_later"] == []
        assert diff["deferred_count"] == 2

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
        assert diff["deferred_count"] == 3
        assert diff["decide_later_count"] == 4

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
        assert meta["deferred_items"] == ["q1", "q2"]
        assert meta["decide_later_items"] == ["dl1"]
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
        }

        _restore_engine_meta(engine, meta)

        assert engine.deferred_items == ["d1", "d2"]
        assert engine.decide_later_items == ["dl1"]
        assert engine.codebase_context == "ctx"
        assert engine._reframe_map["simple q"] == "technical q"

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

    def test_meta_file_has_exactly_five_fields(self, tmp_path: Path) -> None:
        """pm_meta JSON contains exactly the 5 required fields."""
        engine = _make_engine_stub(deferred=["d1"], decide_later=["dl1"])
        engine.codebase_context = "ctx"
        engine._reframe_map = {"simple": "complex"}

        _save_pm_meta("sess-fields", engine, cwd="/w", data_dir=tmp_path)
        meta = _load_pm_meta("sess-fields", data_dir=tmp_path)

        assert meta is not None
        expected_keys = {"deferred_items", "decide_later_items", "codebase_context", "pending_reframe", "cwd"}
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
        assert meta["deferred_items"] == ["q1", "q2", "q3"]
        assert meta["codebase_context"] == "updated context"
        assert meta["cwd"] == "/v2"

    def test_load_corrupted_file_returns_none(self, tmp_path: Path) -> None:
        """Loading corrupted JSON returns None gracefully."""
        path = _meta_path("sess-bad", tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json{{{", encoding="utf-8")

        assert _load_pm_meta("sess-bad", data_dir=tmp_path) is None


# ── Integration test: diff in handler.handle ─────────────────────


class TestPMHandlerDiffIntegration:
    """Test that handle() includes correct diff in response meta."""

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

    @pytest.mark.asyncio
    async def test_handle_answer_includes_diff_in_meta(self, tmp_path: Path) -> None:
        """When answering, the response meta includes new_deferred/new_decide_later."""
        # Set up engine mock that simulates classification adding items
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = ["existing_deferred"]
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = []

        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question="What features?", user_response=None),
            ],
        )

        # load_state returns the state
        engine.load_state = AsyncMock(return_value=Result.ok(state))

        # record_response returns updated state
        engine.record_response = AsyncMock(return_value=Result.ok(state))

        # ask_next_question simulates adding one deferred + one decide_later
        async def mock_ask_next(s: Any) -> Result:
            engine.deferred_items.append("new_tech_q")
            engine.decide_later_items.append("new_premature_q")
            return Result.ok("What is the target audience?")

        engine.ask_next_question = AsyncMock(side_effect=mock_ask_next)
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        # Save initial meta so restore works
        _save_pm_meta(
            "sess-1",
            engine,
            cwd="/tmp/proj",
            data_dir=tmp_path,
        )
        # Reset lists to simulate pre-restore state (restore will set them back)
        engine.deferred_items = ["existing_deferred"]
        engine.decide_later_items = []

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "session_id": "sess-1",
            "answer": "We want user authentication",
        })

        assert result.is_ok
        meta = result.value.meta

        assert meta["new_deferred"] == ["new_tech_q"]
        assert meta["new_decide_later"] == ["new_premature_q"]
        assert meta["deferred_count"] == 2
        assert meta["decide_later_count"] == 1

    @pytest.mark.asyncio
    async def test_handle_answer_no_new_items(self, tmp_path: Path) -> None:
        """When no items are deferred/decide-later, diff lists are empty."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = ["d1"]
        engine.decide_later_items = ["dl1"]
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = []

        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question="What features?", user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))

        # ask_next_question does NOT add any items
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-2", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "session_id": "sess-2",
            "answer": "The target is developers",
        })

        assert result.is_ok
        meta = result.value.meta
        assert meta["new_deferred"] == []
        assert meta["new_decide_later"] == []
        assert meta["deferred_count"] == 1
        assert meta["decide_later_count"] == 1

    @pytest.mark.asyncio
    async def test_handle_answer_includes_interview_complete_false(self, tmp_path: Path) -> None:
        """Non-complete response meta includes interview_complete=False."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = []

        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question="What features?", user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-3", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "session_id": "sess-3",
            "answer": "Some answer",
        })

        assert result.is_ok
        assert result.value.meta["interview_complete"] is False


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
    async def test_max_rounds_triggers_completion(self) -> None:
        """Hitting MAX_PM_INTERVIEW_ROUNDS triggers forced completion."""
        state = _make_state(rounds=_make_answered_rounds(MAX_PM_INTERVIEW_ROUNDS))
        engine = _make_engine_stub()

        result = await _check_completion(state, engine)

        assert result is not None
        assert result["interview_complete"] is True
        assert result["completion_reason"] == "max_rounds"
        assert result["rounds_completed"] == MAX_PM_INTERVIEW_ROUNDS

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

        with patch(
            "ouroboros.mcp.tools.pm_handler.AmbiguityScorer"
        ) as mock_scorer_cls:
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

        with patch(
            "ouroboros.mcp.tools.pm_handler.AmbiguityScorer"
        ) as mock_scorer_cls:
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

        with patch(
            "ouroboros.mcp.tools.pm_handler.AmbiguityScorer"
        ) as mock_scorer_cls:
            mock_scorer = MagicMock()
            mock_scorer.score = AsyncMock(
                return_value=Result.err(ProviderError("LLM down"))
            )
            mock_scorer_cls.return_value = mock_scorer

            result = await _check_completion(state, engine)

        assert result is None

    @pytest.mark.asyncio
    async def test_unanswered_rounds_not_counted(self) -> None:
        """Only answered rounds count toward completion thresholds."""
        rounds = _make_answered_rounds(2)
        # Add an unanswered round (pending question)
        rounds.append(
            InterviewRound(round_number=3, question="Pending?", user_response=None)
        )
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

        with patch(
            "ouroboros.mcp.tools.pm_handler.AmbiguityScorer"
        ) as mock_scorer_cls:
            mock_scorer = MagicMock()
            mock_scorer.score = AsyncMock(return_value=Result.ok(mock_score))
            mock_scorer_cls.return_value = mock_scorer

            await _check_completion(state, engine)

            # Verify scorer was called with additional_context containing decide-later items
            call_kwargs = mock_scorer.score.call_args
            additional_ctx = call_kwargs.kwargs.get("additional_context", "")
            assert "Should we use Redis?" in additional_ctx
            assert "What API format?" in additional_ctx


class TestHandlerCompletionIntegration:
    """Test that handle() returns interview_complete when completion triggers."""

    @pytest.mark.asyncio
    async def test_handle_answer_completion_via_ambiguity(self, tmp_path: Path) -> None:
        """When ambiguity is resolved, handle() returns completion response."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = ["d1"]
        engine.decide_later_items = ["dl1"]
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = []
        engine.format_decide_later_summary = MagicMock(
            return_value="Items to decide later:\n  1. dl1"
        )

        # 4 answered rounds + 1 unanswered
        rounds = _make_answered_rounds(4)
        rounds.append(
            InterviewRound(round_number=5, question="Current Q?", user_response=None)
        )
        state = _make_state(rounds=rounds)
        state.is_brownfield = False

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.complete_interview = AsyncMock(return_value=Result.ok(state))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-complete", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        # Mock _check_completion to return completion
        completion_meta = {
            "interview_complete": True,
            "completion_reason": "ambiguity_resolved",
            "rounds_completed": 5,
            "ambiguity_score": 0.18,
        }

        with patch(
            "ouroboros.mcp.tools.pm_handler._check_completion",
            new_callable=AsyncMock,
            return_value=completion_meta,
        ):
            result = await handler.handle({
                "session_id": "sess-complete",
                "answer": "Final answer",
            })

        assert result.is_ok
        meta = result.value.meta
        assert meta["interview_complete"] is True
        assert meta["completion_reason"] == "ambiguity_resolved"
        assert meta["ambiguity_score"] == 0.18
        assert meta["session_id"] == "sess-complete"

        # Verify complete_interview was called
        engine.complete_interview.assert_called_once()

        # Verify response text includes generate instructions
        text = result.value.content[0].text
        assert "Interview complete" in text
        assert "generate" in text

    @pytest.mark.asyncio
    async def test_no_done_signal_processing(self, tmp_path: Path) -> None:
        """User typing 'done' is treated as a normal answer, not a completion signal."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = []

        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question="What features?", user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-done", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        # User types "done" — should NOT trigger completion
        result = await handler.handle({
            "session_id": "sess-done",
            "answer": "done",
        })

        assert result.is_ok
        # "done" is passed as a regular answer — interview continues
        meta = result.value.meta
        assert meta["interview_complete"] is False
        # record_response was called with "done" as the answer
        engine.record_response.assert_called_once()
        call_args = engine.record_response.call_args
        assert call_args[0][1] == "done"  # answer arg


# ── Tests for _handle_start: engine creation & brownfield detection (AC 2) ──


class TestGetEngine:
    """Test PMInterviewEngine creation via _get_engine."""

    def test_returns_injected_engine(self) -> None:
        """When pm_engine is injected, _get_engine returns it directly."""
        engine = _make_engine_stub()
        handler = PMInterviewHandler(pm_engine=engine)
        assert handler._get_engine() is engine

    def test_creates_engine_when_none_injected(self) -> None:
        """When no engine is injected, creates one with ClaudeAgentAdapter."""
        with patch(
            "ouroboros.mcp.tools.pm_handler.ClaudeAgentAdapter"
        ) as mock_adapter_cls, patch(
            "ouroboros.mcp.tools.pm_handler.PMInterviewEngine"
        ) as mock_engine_cls:
            mock_adapter = MagicMock()
            mock_adapter_cls.return_value = mock_adapter
            mock_engine_cls.create.return_value = MagicMock()

            handler = PMInterviewHandler()
            handler._get_engine()

            mock_adapter_cls.assert_called_once_with(permission_mode="bypassPermissions")
            mock_engine_cls.create.assert_called_once()
            call_kwargs = mock_engine_cls.create.call_args
            assert call_kwargs.kwargs["llm_adapter"] is mock_adapter

    def test_uses_custom_data_dir(self, tmp_path: Path) -> None:
        """When data_dir is set, passes it to PMInterviewEngine.create."""
        with patch(
            "ouroboros.mcp.tools.pm_handler.ClaudeAgentAdapter"
        ), patch(
            "ouroboros.mcp.tools.pm_handler.PMInterviewEngine"
        ) as mock_engine_cls:
            mock_engine_cls.create.return_value = MagicMock()

            handler = PMInterviewHandler(data_dir=tmp_path)
            handler._get_engine()

            call_kwargs = mock_engine_cls.create.call_args
            assert call_kwargs.kwargs["state_dir"] == tmp_path


class TestHandleStartBrownfield:
    """Test brownfield detection in _handle_start — uses DB default repo."""

    @pytest.mark.asyncio
    async def test_start_detects_brownfield_from_db_default(self, tmp_path: Path) -> None:
        """When DB has a default repo, passes brownfield_repos to engine."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_default = BrownfieldRepo(
            path="/home/user/my-repo",
            name="my-repo",
            desc="A cool project",
            is_default=True,
        )

        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="brownfield-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch(
            "ouroboros.mcp.tools.pm_handler.BrownfieldStore"
        ) as mock_store_cls, patch(
            "ouroboros.mcp.tools.pm_handler.get_default_brownfield_context",
            new_callable=AsyncMock,
            return_value=mock_default,
        ):
            mock_store = AsyncMock()
            mock_store_cls.return_value = mock_store

            result = await handler.handle({
                "initial_context": "Build a task manager",
                "cwd": str(tmp_path),
            })

        assert result.is_ok

        # Verify brownfield_repos was passed to ask_opening_and_start
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is not None
        assert len(brownfield_repos) == 1
        assert brownfield_repos[0]["path"] == "/home/user/my-repo"
        assert brownfield_repos[0]["name"] == "my-repo"
        assert brownfield_repos[0]["role"] == "primary"

    @pytest.mark.asyncio
    async def test_start_no_brownfield_when_no_db_default(self, tmp_path: Path) -> None:
        """When DB has no default repo, brownfield_repos is None (greenfield)."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="greenfield-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch(
            "ouroboros.mcp.tools.pm_handler.BrownfieldStore"
        ) as mock_store_cls, patch(
            "ouroboros.mcp.tools.pm_handler.get_default_brownfield_context",
            new_callable=AsyncMock,
            return_value=None,
        ):
            mock_store = AsyncMock()
            mock_store_cls.return_value = mock_store

            result = await handler.handle({
                "initial_context": "Build a new app",
                "cwd": str(tmp_path),
            })

        assert result.is_ok

        # Verify brownfield_repos was None (greenfield)
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is None

    @pytest.mark.asyncio
    async def test_start_brownfield_graceful_on_db_error(self, tmp_path: Path) -> None:
        """When DB access fails, brownfield_repos falls back to None."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="fallback-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch(
            "ouroboros.mcp.tools.pm_handler.BrownfieldStore"
        ) as mock_store_cls:
            mock_store = AsyncMock()
            mock_store.initialize.side_effect = RuntimeError("DB unavailable")
            mock_store_cls.return_value = mock_store

            result = await handler.handle({
                "initial_context": "Build something",
                "cwd": str(tmp_path),
            })

        assert result.is_ok

        # Should gracefully fall back to no brownfield
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is None

    @pytest.mark.asyncio
    async def test_start_returns_session_id_and_question(self, tmp_path: Path) -> None:
        """Start returns the session_id in meta and first question in content."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="new-sess-42")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(
            return_value=Result.ok("Who are the target users?")
        )
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "initial_context": "Build a task manager",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        assert result.value.meta["session_id"] == "new-sess-42"
        assert "Who are the target users?" in result.value.content[0].text
        assert "new-sess-42" in result.value.content[0].text

    @pytest.mark.asyncio
    async def test_start_saves_pm_meta(self, tmp_path: Path) -> None:
        """Start persists pm_meta with cwd for later restoration."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = ["deferred_q"]
        engine.decide_later_items = []
        engine.codebase_context = "some context"
        engine._reframe_map = {}

        state = _make_state(interview_id="meta-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        await handler.handle({
            "initial_context": "Build something",
            "cwd": "/my/project",
        })

        # Verify pm_meta was saved
        meta = _load_pm_meta("meta-sess", data_dir=tmp_path)
        assert meta is not None
        assert meta["cwd"] == "/my/project"
        assert meta["deferred_items"] == ["deferred_q"]
        assert meta["codebase_context"] == "some context"

    @pytest.mark.asyncio
    async def test_start_includes_diff_in_meta(self, tmp_path: Path) -> None:
        """Start response meta includes deferred/decide-later diff."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="diff-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))

        # Simulate ask_next_question adding a deferred item
        async def mock_ask(s: Any) -> Result:
            engine.deferred_items.append("tech_q_deferred")
            return Result.ok("What is the product goal?")

        engine.ask_next_question = AsyncMock(side_effect=mock_ask)
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "initial_context": "Build an app",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        meta = result.value.meta
        assert meta["new_deferred"] == ["tech_q_deferred"]
        assert meta["new_decide_later"] == []
        assert meta["deferred_count"] == 1

    @pytest.mark.asyncio
    async def test_start_engine_error_returns_err(self, tmp_path: Path) -> None:
        """When engine.ask_opening_and_start fails, handle returns error."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        engine.ask_opening_and_start = AsyncMock(
            return_value=Result.err(
                MagicMock(__str__=lambda _s: "Validation failed")
            )
        )

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "initial_context": "Build something",
            "cwd": str(tmp_path),
        })

        assert result.is_err

    @pytest.mark.asyncio
    async def test_start_question_error_returns_err(self, tmp_path: Path) -> None:
        """When engine.ask_next_question fails, handle returns error."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state()
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(
            return_value=Result.err(MagicMock(__str__=lambda _s: "LLM error"))
        )

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "initial_context": "Build something",
            "cwd": str(tmp_path),
        })

        assert result.is_err

    @pytest.mark.asyncio
    async def test_start_cwd_defaults_to_getcwd(self, tmp_path: Path) -> None:
        """When cwd is not provided, defaults to os.getcwd()."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="default-cwd-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Q?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch("ouroboros.mcp.tools.pm_handler.os.getcwd", return_value="/fallback/cwd"):
            await handler.handle({"initial_context": "Build it"})

        # cwd saved in meta should be the fallback
        meta = _load_pm_meta("default-cwd-sess", data_dir=tmp_path)
        assert meta is not None
        assert meta["cwd"] == "/fallback/cwd"

    @pytest.mark.asyncio
    async def test_start_records_unanswered_round(self, tmp_path: Path) -> None:
        """Start appends an unanswered round to state before saving."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="round-sess")
        state.rounds = []  # Start with no rounds
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("First question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        await handler.handle({
            "initial_context": "Build something",
            "cwd": str(tmp_path),
        })

        # Verify an unanswered round was added
        assert len(state.rounds) == 1
        assert state.rounds[0].question == "First question?"
        assert state.rounds[0].user_response is None
        assert state.rounds[0].round_number == 1

        # Verify mark_updated was called
        state.mark_updated.assert_called_once()

        # Verify save_state was called
        engine.save_state.assert_called_once_with(state)

    @pytest.mark.asyncio
    async def test_start_includes_pending_reframe_in_meta(self, tmp_path: Path) -> None:
        """Start response includes pending_reframe when engine has reframe."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {"PM-friendly Q": "Original tech Q"}

        state = _make_state(interview_id="reframe-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("PM-friendly Q"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "initial_context": "Build something",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        meta = result.value.meta
        assert meta["pending_reframe"] is not None
        assert meta["pending_reframe"]["reframed"] == "PM-friendly Q"
        assert meta["pending_reframe"]["original"] == "Original tech Q"

    @pytest.mark.asyncio
    async def test_start_meta_contains_question_field(self, tmp_path: Path) -> None:
        """Response meta includes the generated question as a separate field."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="q-meta-sess")
        state.is_brownfield = False
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(
            return_value=Result.ok("What is your target audience?")
        )
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "initial_context": "Build an app",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        assert result.value.meta["question"] == "What is your target audience?"

    @pytest.mark.asyncio
    async def test_start_meta_contains_is_brownfield_true(self, tmp_path: Path) -> None:
        """Response meta includes is_brownfield=True for brownfield projects."""
        (tmp_path / ".git").mkdir()

        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="bf-meta-sess")
        state.is_brownfield = True
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Q?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "initial_context": "Add auth",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        assert result.value.meta["is_brownfield"] is True

    @pytest.mark.asyncio
    async def test_start_meta_contains_is_brownfield_false(self, tmp_path: Path) -> None:
        """Response meta includes is_brownfield=False for greenfield projects."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="gf-meta-sess")
        state.is_brownfield = False
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Q?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "initial_context": "Build something new",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        assert result.value.meta["is_brownfield"] is False

    @pytest.mark.asyncio
    async def test_start_meta_has_all_required_fields(self, tmp_path: Path) -> None:
        """Response meta contains session_id, question, is_brownfield, and diff fields."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}

        state = _make_state(interview_id="full-meta-sess")
        state.is_brownfield = False
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("First question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "initial_context": "Build an API",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        meta = result.value.meta
        # Core fields from Sub-AC 2
        assert "session_id" in meta
        assert "question" in meta
        assert "is_brownfield" in meta
        # Diff fields
        assert "new_deferred" in meta
        assert "new_decide_later" in meta
        assert "deferred_count" in meta
        assert "decide_later_count" in meta
        # Reframe field
        assert "pending_reframe" in meta


# ── AC 3: action:resume meta fields ──────────────────────────────


class TestResumeMetaFields:
    """Test that action:resume returns the AC 3 required meta fields:
    session_id, question, deferred_this_round, decide_later_this_round,
    is_complete, classification.
    """

    @pytest.mark.asyncio
    async def test_resume_meta_has_all_required_fields(self, tmp_path: Path) -> None:
        """Resume response meta includes all AC 3 required fields."""
        from ouroboros.bigbang.question_classifier import (
            ClassificationResult,
            QuestionCategory,
        )

        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = [
            ClassificationResult(
                original_question="What DB?",
                category=QuestionCategory.PLANNING,
                reframed_question="What DB?",
                reasoning="planning question",
            )
        ]

        state = _make_state(
            interview_id="resume-ac3",
            rounds=[
                InterviewRound(round_number=1, question="What features?", user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Who is the target user?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("resume-ac3", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "session_id": "resume-ac3",
            "answer": "User auth and dashboards",
        })

        assert result.is_ok
        meta = result.value.meta

        # AC 3 required fields
        assert meta["session_id"] == "resume-ac3"
        assert meta["question"] == "Who is the target user?"
        assert meta["is_complete"] is False
        assert meta["classification"] == "passthrough"
        assert meta["deferred_this_round"] == []
        assert meta["decide_later_this_round"] == []

    @pytest.mark.asyncio
    async def test_resume_meta_deferred_this_round(self, tmp_path: Path) -> None:
        """Resume correctly reports deferred_this_round items."""
        from ouroboros.bigbang.question_classifier import (
            ClassificationResult,
            QuestionCategory,
        )

        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = ["old_deferred"]
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = [
            ClassificationResult(
                original_question="What framework?",
                category=QuestionCategory.DEVELOPMENT,
                reframed_question="What framework?",
                reasoning="technical",
                defer_to_dev=True,
            )
        ]

        state = _make_state(
            interview_id="resume-deferred",
            rounds=[
                InterviewRound(round_number=1, question="Q1?", user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))

        async def mock_ask(s: Any) -> Result:
            engine.deferred_items.append("new_tech_q")
            engine.decide_later_items.append("premature_q")
            return Result.ok("How many users?")

        engine.ask_next_question = AsyncMock(side_effect=mock_ask)
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("resume-deferred", engine, cwd="/tmp/proj", data_dir=tmp_path)
        # Reset to simulate fresh restore
        engine.deferred_items = ["old_deferred"]
        engine.decide_later_items = []

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "session_id": "resume-deferred",
            "answer": "We need REST APIs",
        })

        assert result.is_ok
        meta = result.value.meta

        assert meta["deferred_this_round"] == ["new_tech_q"]
        assert meta["decide_later_this_round"] == ["premature_q"]
        assert meta["classification"] == "deferred"

    @pytest.mark.asyncio
    async def test_resume_meta_classification_none_when_no_classifications(self, tmp_path: Path) -> None:
        """When engine has no classifications, classification is None."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = []

        state = _make_state(
            interview_id="resume-noclassify",
            rounds=[
                InterviewRound(round_number=1, question="Q?", user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("resume-noclassify", engine, cwd="/tmp", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "session_id": "resume-noclassify",
            "answer": "Something",
        })

        assert result.is_ok
        assert result.value.meta["classification"] is None

    @pytest.mark.asyncio
    async def test_resume_completion_meta_has_required_fields(self, tmp_path: Path) -> None:
        """When interview completes during resume, meta has AC 3 fields."""
        from ouroboros.bigbang.question_classifier import (
            ClassificationResult,
            QuestionCategory,
        )

        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = ["d1"]
        engine.decide_later_items = ["dl1"]
        engine.codebase_context = ""
        engine._reframe_map = {}
        engine.classifications = [
            ClassificationResult(
                original_question="Passthrough q",
                category=QuestionCategory.PLANNING,
                reframed_question="Passthrough q",
                reasoning="ok",
            )
        ]
        engine.format_decide_later_summary = MagicMock(return_value="")

        state = _make_state(
            interview_id="resume-complete",
            rounds=_make_answered_rounds(MAX_PM_INTERVIEW_ROUNDS),
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.complete_interview = AsyncMock(return_value=Result.ok(state))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("resume-complete", engine, cwd="/tmp", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "session_id": "resume-complete",
            "answer": "Final answer",
        })

        assert result.is_ok
        meta = result.value.meta

        # AC 3 required fields on completion
        assert meta["session_id"] == "resume-complete"
        assert meta["question"] is None  # No next question when complete
        assert meta["is_complete"] is True
        assert meta["classification"] == "passthrough"
        assert meta["deferred_this_round"] == []
        assert meta["decide_later_this_round"] == []
        # Also has completion details
        assert meta["interview_complete"] is True

    @pytest.mark.asyncio
    async def test_resume_loads_state_and_meta(self, tmp_path: Path) -> None:
        """Resume loads InterviewState via engine and restores pm_meta."""
        engine = MagicMock(spec=PMInterviewEngine)
        engine.deferred_items = []
        engine.decide_later_items = []
        engine.codebase_context = "existing context"
        engine._reframe_map = {}
        engine.classifications = []

        state = _make_state(
            interview_id="resume-load",
            rounds=[
                InterviewRound(round_number=1, question="Q?", user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Follow-up?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        # Save meta with specific values
        _save_pm_meta("resume-load", engine, cwd="/project", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle({
            "session_id": "resume-load",
            "answer": "My answer",
        })

        assert result.is_ok
        # Verify engine.load_state was called with correct session_id
        engine.load_state.assert_called_once_with("resume-load")
        # Verify record_response was called
        engine.record_response.assert_called_once()
        # Verify ask_next_question was called
        engine.ask_next_question.assert_called_once()
        # Verify meta was saved after
        assert _load_pm_meta("resume-load", data_dir=tmp_path) is not None


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
        assert _detect_action({
            "session_id": "abc-123",
            "answer": "Yes",
            "cwd": "/projects/myapp",
        }) == "resume"

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
        result = _detect_action({
            "initial_context": "Build X",
            "session_id": "s1",
        })
        assert result == "start"

    def test_empty_initial_context_falls_through(self):
        """Empty string initial_context is falsy → falls through."""
        assert _detect_action({"initial_context": "", "session_id": "s1"}) == "resume"

    def test_none_action_treated_as_omitted(self):
        """action=None is treated same as omitted."""
        assert _detect_action({"action": None, "initial_context": "X"}) == "start"


class TestAutoDetectIntegration:
    """Integration tests: handle() auto-detects action from params (AC 13)."""

    @pytest.fixture
    def engine(self):
        """Create a mock engine for handler tests."""
        eng = _make_engine_stub()
        eng.ask_opening_and_start = AsyncMock()
        eng.ask_next_question = AsyncMock()
        eng.record_response = AsyncMock()
        eng.load_state = AsyncMock()
        eng.save_state = AsyncMock()
        eng.complete_interview = AsyncMock()
        return eng

    @pytest.mark.asyncio
    async def test_start_without_action_param(self, engine, tmp_path):
        """Calling with only initial_context (no action) triggers start."""
        state = _make_state(interview_id="auto-start")
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("First question?")

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        # No "action" key in arguments at all
        result = await handler.handle({"initial_context": "Build a CRM"})

        assert result.is_ok
        engine.ask_opening_and_start.assert_called_once()
        assert result.value.meta["session_id"] == "auto-start"

    @pytest.mark.asyncio
    async def test_resume_without_action_param(self, engine, tmp_path):
        """Calling with session_id + answer (no action) triggers resume."""
        state = _make_state(
            interview_id="auto-resume",
            rounds=[InterviewRound(round_number=1, question="Q1?", user_response=None)],
        )
        engine.load_state.return_value = Result.ok(state)
        engine.record_response.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Q2?")

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        # No "action" key — auto-detects "resume" from session_id + answer
        result = await handler.handle({
            "session_id": "auto-resume",
            "answer": "My answer",
        })

        assert result.is_ok
        engine.load_state.assert_called_once_with("auto-resume")
        engine.record_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_session_id_only_no_answer(self, engine, tmp_path):
        """session_id alone (no answer, no action) still auto-detects resume."""
        state = _make_state(
            interview_id="sid-only",
            rounds=[InterviewRound(round_number=1, question="Q1?", user_response=None)],
        )
        engine.load_state.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Q1 again?")

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle({"session_id": "sid-only"})

        assert result.is_ok
        engine.load_state.assert_called_once_with("sid-only")

    @pytest.mark.asyncio
    async def test_generate_requires_explicit_action(self, engine, tmp_path):
        """Generate is not auto-detected — requires explicit action='generate'."""
        state = _make_state(
            interview_id="gen-test",
            rounds=[InterviewRound(round_number=1, question="Q?", user_response=None)],
        )
        engine.load_state.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Next Q?")

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        # session_id only → auto-detects "resume", not "generate"
        result = await handler.handle({"session_id": "gen-test"})

        assert result.is_ok
        # Should have called load_state (resume path), not generate_pm_seed
        engine.load_state.assert_called_once()
        engine.generate_pm_seed = AsyncMock()
        engine.generate_pm_seed.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_params_returns_error(self, tmp_path):
        """Calling with no relevant params returns error."""
        handler = PMInterviewHandler(data_dir=tmp_path)
        result = await handler.handle({})

        assert result.is_err
        assert "Must provide" in str(result.error)
