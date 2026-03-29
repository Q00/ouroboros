"""Tests for PMInterviewHandler — start/brownfield (AC 2), diff computation (AC 8), completion (AC 12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
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
    from tests.unit.mcp.tools.conftest import make_pm_engine_mock

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
            "brownfield_repos": [{"path": "/repo", "name": "repo"}],
        }

        _restore_engine_meta(engine, meta)

        assert engine.deferred_items == ["d1", "d2"]
        assert engine.decide_later_items == ["dl1"]
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
        engine = make_pm_engine_mock(deferred_items=["existing_deferred"], decide_later_items=[])

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

        result = await handler.handle(
            {
                "session_id": "sess-1",
                "answer": "We want user authentication",
            }
        )

        assert result.is_ok
        meta = result.value.meta

        assert meta["new_deferred"] == ["new_tech_q"]
        assert meta["new_decide_later"] == ["new_premature_q"]
        assert meta["deferred_count"] == 2
        assert meta["decide_later_count"] == 1

    @pytest.mark.asyncio
    async def test_handle_answer_no_new_items(self, tmp_path: Path) -> None:
        """When no items are deferred/decide-later, diff lists are empty."""
        engine = make_pm_engine_mock(deferred_items=["d1"], decide_later_items=["dl1"])

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

        result = await handler.handle(
            {
                "session_id": "sess-2",
                "answer": "The target is developers",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["new_deferred"] == []
        assert meta["new_decide_later"] == []
        assert meta["deferred_count"] == 1
        assert meta["decide_later_count"] == 1

    @pytest.mark.asyncio
    async def test_handle_answer_includes_interview_complete_false(self, tmp_path: Path) -> None:
        """Non-complete response meta includes interview_complete=False."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

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

        result = await handler.handle(
            {
                "session_id": "sess-3",
                "answer": "Some answer",
            }
        )

        assert result.is_ok
        assert result.value.meta["interview_complete"] is False

    @pytest.mark.asyncio
    async def test_handle_answer_decide_later_skips_and_records(self, tmp_path: Path) -> None:
        """When answer='[decide_later]', skip_as_decide_later is called instead of record_response."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])
        # Set up classification so the guard accepts the sentinel
        engine.classifications = [_make_classification(ClassifierOutputType.DECIDE_LATER)]

        question = "What caching strategy should we use?"
        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question=question, user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))

        # skip_as_decide_later should be called (not record_response)
        async def mock_skip(s: Any, q: str) -> Result:
            engine.decide_later_items.append(q)
            return Result.ok(s)

        engine.skip_as_decide_later = AsyncMock(side_effect=mock_skip)
        engine.record_response = AsyncMock(return_value=Result.ok(state))

        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-dl", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "session_id": "sess-dl",
                "answer": "[decide_later]",
            }
        )

        assert result.is_ok
        # skip_as_decide_later was called
        engine.skip_as_decide_later.assert_called_once_with(state, question)
        # record_response was NOT called (skip_as_decide_later handles it internally)
        engine.record_response.assert_not_called()
        # The question was recorded in decide_later_items
        assert question in engine.decide_later_items

    @pytest.mark.asyncio
    async def test_handle_answer_decide_later_with_whitespace(self, tmp_path: Path) -> None:
        """Whitespace-padded '[decide_later]' is still recognized."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])
        # Set up classification so the guard accepts the sentinel
        engine.classifications = [_make_classification(ClassifierOutputType.DECIDE_LATER)]

        question = "What rate limiting approach?"
        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question=question, user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.skip_as_decide_later = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-dl2", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "session_id": "sess-dl2",
                "answer": "  [decide_later]  ",
            }
        )

        assert result.is_ok
        engine.skip_as_decide_later.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_answer_deferred_skips_and_records(self, tmp_path: Path) -> None:
        """When answer='[deferred]', skip_as_deferred is called instead of record_response."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])
        # Set up classification so the guard accepts the sentinel
        engine.classifications = [_make_classification(ClassifierOutputType.DEFERRED)]

        question = "Should we use gRPC or REST for inter-service communication?"
        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question=question, user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))

        async def mock_skip(s: Any, q: str) -> Result:
            engine.deferred_items.append(q)
            return Result.ok(s)

        engine.skip_as_deferred = AsyncMock(side_effect=mock_skip)
        engine.record_response = AsyncMock(return_value=Result.ok(state))

        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-def", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "session_id": "sess-def",
                "answer": "[deferred]",
            }
        )

        assert result.is_ok
        engine.skip_as_deferred.assert_called_once_with(state, question)
        engine.record_response.assert_not_called()
        assert question in engine.deferred_items

    @pytest.mark.asyncio
    async def test_decide_later_sentinel_for_passthrough_treated_as_normal_answer(
        self, tmp_path: Path
    ) -> None:
        """[decide_later] sent for a passthrough question is recorded as a normal answer.

        Guards against clients blindly sending sentinels regardless of question type.
        When the last classification is not 'decide_later', the sentinel must not
        trigger skip_as_decide_later — it should fall through to record_response.
        """
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])
        # Last classification is PASSTHROUGH, not DECIDE_LATER
        engine.classifications = [_make_classification(ClassifierOutputType.PASSTHROUGH)]

        question = "What is the primary goal of the product?"
        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question=question, user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.skip_as_decide_later = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-passthrough-guard", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "session_id": "sess-passthrough-guard",
                "answer": "[decide_later]",
            }
        )

        assert result.is_ok
        # Must NOT call skip_as_decide_later — the sentinel is treated as a normal answer
        engine.skip_as_decide_later.assert_not_called()
        # Must record the literal string as a normal response
        engine.record_response.assert_called_once_with(state, "[decide_later]", question)

    @pytest.mark.asyncio
    async def test_decide_later_skip_populates_per_round_diff(self, tmp_path: Path) -> None:
        """Skipping via [decide_later] populates new_decide_later in response metadata.

        Verifies that the per-round diff snapshot is taken BEFORE the skip call
        so that items added inside skip_as_decide_later() appear in the diff.
        """
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])
        # Set up classification so the guard accepts the sentinel
        engine.classifications = [_make_classification(ClassifierOutputType.DECIDE_LATER)]

        question = "What scaling approach suits future traffic?"
        state = _make_state(
            rounds=[
                InterviewRound(round_number=1, question=question, user_response=None),
            ],
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))

        async def mock_skip(s: Any, q: str) -> Result:
            engine.decide_later_items.append(q)
            return Result.ok(s)

        engine.skip_as_decide_later = AsyncMock(side_effect=mock_skip)
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-diff-skip", engine, cwd="/tmp/proj", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "session_id": "sess-diff-skip",
                "answer": "[decide_later]",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        # The skipped question must appear in new_decide_later (per-round diff)
        assert question in meta["new_decide_later"], (
            f"Expected '{question}' in new_decide_later, got {meta['new_decide_later']}"
        )
        assert meta["decide_later_this_round"] == meta["new_decide_later"]
        assert meta["decide_later_count"] == 1


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


class TestHandlerCompletionIntegration:
    """Test that handle() returns interview_complete when completion triggers."""

    @pytest.mark.asyncio
    async def test_handle_answer_completion_via_ambiguity(self, tmp_path: Path) -> None:
        """When ambiguity is resolved, handle() returns completion response."""
        engine = make_pm_engine_mock(deferred_items=["d1"], decide_later_items=["dl1"])
        engine.format_decide_later_summary = MagicMock(
            return_value="Items to decide later:\n  1. dl1"
        )

        # 4 answered rounds + 1 unanswered
        rounds = _make_answered_rounds(4)
        rounds.append(InterviewRound(round_number=5, question="Current Q?", user_response=None))
        state = _make_state(rounds=rounds)
        state.is_brownfield = False

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.complete_interview = AsyncMock(return_value=Result.ok(state))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        _save_pm_meta("sess-complete", engine, cwd="/tmp/proj", data_dir=tmp_path)

        # Mock auto-generate on completion
        seed_mock = MagicMock()
        seed_mock.product_name = "Test Product"
        seed_mock.deferred_items = ["d1"]
        seed_mock.decide_later_items = ["dl1"]
        engine.generate_pm_seed = AsyncMock(return_value=Result.ok(seed_mock))
        engine.save_pm_seed = MagicMock(return_value=tmp_path / "seed.json")

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        completion_meta = {
            "interview_complete": True,
            "completion_reason": "ambiguity_resolved",
            "rounds_completed": 5,
            "ambiguity_score": 0.18,
        }

        with (
            patch(
                "ouroboros.mcp.tools.pm_handler.maybe_complete_pm_interview",
                new_callable=AsyncMock,
                return_value=Result.ok((state, completion_meta)),
            ) as mock_complete,
            patch(
                "ouroboros.mcp.tools.pm_handler.save_pm_document",
                return_value=tmp_path / "pm.md",
            ),
        ):
            result = await handler.handle(
                {
                    "session_id": "sess-complete",
                    "answer": "Final answer",
                }
            )

        assert result.is_ok
        meta = result.value.meta
        assert meta["interview_complete"] is True
        assert meta["completion_reason"] == "ambiguity_resolved"
        assert meta["ambiguity_score"] == 0.18
        assert meta["session_id"] == "sess-complete"

        # Verify the shared completion helper was used
        mock_complete.assert_awaited_once_with(state, engine)

        # Verify response text includes completion and PM document info
        text = result.value.content[0].text
        assert "Interview complete" in text
        assert "PM document" in text

    @pytest.mark.asyncio
    async def test_no_done_signal_processing(self, tmp_path: Path) -> None:
        """User typing 'done' is treated as a normal answer, not a completion signal."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

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
        result = await handler.handle(
            {
                "session_id": "sess-done",
                "answer": "done",
            }
        )

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


class TestHandleStartBrownfield:
    """Test brownfield detection in _handle_start — uses DB default repo."""

    @pytest.mark.asyncio
    async def test_start_directly_starts_interview_with_defaults(self, tmp_path: Path) -> None:
        """Start auto-loads default repos from DB and starts interview directly."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="direct-start-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = []
            result = await handler.handle(
                {
                    "initial_context": "Build a task manager",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        meta = result.value.meta
        assert meta["status"] == "interview_started"
        assert meta["response_param"] == "answer"
        assert "session_id" in meta

        # Engine should have been called — interview started directly
        engine.ask_opening_and_start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_auto_loads_default_repos_as_brownfield(self, tmp_path: Path) -> None:
        """Start auto-loads is_default=true repos from DB as brownfield context."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        mock_repos = [
            BrownfieldRepo(
                path="/home/user/my-repo",
                name="my-repo",
                desc="A cool project",
                is_default=True,
            ),
        ]

        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="bf-auto-sess", is_brownfield=True)
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = mock_repos

            result = await handler.handle(
                {
                    "initial_context": "Build a task manager",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        meta = result.value.meta
        assert meta["status"] == "interview_started"

        # Verify brownfield_repos was passed to engine
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is not None
        assert len(brownfield_repos) == 1
        assert brownfield_repos[0]["path"] == "/home/user/my-repo"
        assert brownfield_repos[0]["name"] == "my-repo"
        assert brownfield_repos[0]["role"] == "main"

    @pytest.mark.asyncio
    async def test_start_no_default_repos_starts_greenfield(self, tmp_path: Path) -> None:
        """When no default repos in DB, starts interview as greenfield."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="gf-auto-sess", is_brownfield=False)
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = []  # No defaults

            result = await handler.handle(
                {
                    "initial_context": "Build something",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        # Verify brownfield_repos was None (greenfield)
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is None

    @pytest.mark.asyncio
    async def test_start_step2_with_selected_repos(self, tmp_path: Path) -> None:
        """Step 2: When selected_repos provided, start interview with those repos."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="brownfield-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        # Mock DB lookup to return the repo
        db_repo = BrownfieldRepo(path="/home/user/my-repo", name="my-repo")
        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = [db_repo]
            result = await handler.handle(
                {
                    "initial_context": "Build a task manager",
                    "selected_repos": ["/home/user/my-repo"],
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok

        # Verify brownfield_repos was passed to ask_opening_and_start
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is not None
        assert len(brownfield_repos) == 1
        assert brownfield_repos[0]["path"] == "/home/user/my-repo"
        assert brownfield_repos[0]["name"] == "my-repo"
        assert brownfield_repos[0]["role"] == "main"

    @pytest.mark.asyncio
    async def test_start_greenfield_when_no_selected_repos_and_no_defaults(
        self, tmp_path: Path
    ) -> None:
        """Start with no selected_repos and no DB defaults starts greenfield."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="greenfield-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = []
            result = await handler.handle(
                {
                    "initial_context": "Build a new app",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        meta = result.value.meta
        assert meta["status"] == "interview_started"

        # Verify brownfield_repos was None (greenfield)
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is None

    @pytest.mark.asyncio
    async def test_start_graceful_on_db_error_falls_back_to_greenfield(
        self, tmp_path: Path
    ) -> None:
        """When DB default repo query returns empty, starts as greenfield."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="fallback-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("What features?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = []  # DB error / no defaults
            result = await handler.handle(
                {
                    "initial_context": "Build something",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok

        # Should gracefully fall back to greenfield (no brownfield repos)
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is None

    @pytest.mark.asyncio
    async def test_start_returns_session_id_and_question(self, tmp_path: Path) -> None:
        """Start returns the session_id in meta and first question in content."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="new-sess-42")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Who are the target users?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "initial_context": "Build a task manager",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        assert result.value.meta["session_id"] == "new-sess-42"
        assert "Who are the target users?" in result.value.content[0].text
        assert "new-sess-42" in result.value.content[0].text

    @pytest.mark.asyncio
    async def test_start_saves_pm_meta(self, tmp_path: Path) -> None:
        """Start persists pm_meta with cwd for later restoration."""
        engine = make_pm_engine_mock(deferred_items=["deferred_q"], decide_later_items=[])
        engine.codebase_context = "some context"

        state = _make_state(interview_id="meta-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        await handler.handle(
            {
                "initial_context": "Build something",
                "selected_repos": [],
                "cwd": "/my/project",
            }
        )

        # Verify pm_meta was saved
        meta = _load_pm_meta("meta-sess", data_dir=tmp_path)
        assert meta is not None
        assert meta["cwd"] == "/my/project"
        assert meta["deferred_items"] == ["deferred_q"]
        assert meta["codebase_context"] == "some context"

    @pytest.mark.asyncio
    async def test_start_includes_diff_in_meta(self, tmp_path: Path) -> None:
        """Start response meta includes deferred/decide-later diff."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="diff-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))

        # Simulate ask_next_question adding a deferred item
        async def mock_ask(s: Any) -> Result:
            engine.deferred_items.append("tech_q_deferred")
            return Result.ok("What is the product goal?")

        engine.ask_next_question = AsyncMock(side_effect=mock_ask)
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "initial_context": "Build an app",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["new_deferred"] == ["tech_q_deferred"]
        assert meta["new_decide_later"] == []
        assert meta["deferred_count"] == 1

    @pytest.mark.asyncio
    async def test_start_engine_error_returns_err(self, tmp_path: Path) -> None:
        """When engine.ask_opening_and_start fails, handle returns error."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        engine.ask_opening_and_start = AsyncMock(
            return_value=Result.err(MagicMock(__str__=lambda _s: "Validation failed"))
        )

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "initial_context": "Build something",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_err

    @pytest.mark.asyncio
    async def test_start_question_error_returns_err(self, tmp_path: Path) -> None:
        """When engine.ask_next_question fails, handle returns error."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state()
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(
            return_value=Result.err(MagicMock(__str__=lambda _s: "LLM error"))
        )

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "initial_context": "Build something",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_err

    @pytest.mark.asyncio
    async def test_start_cwd_defaults_to_getcwd(self, tmp_path: Path) -> None:
        """When cwd is not provided, defaults to os.getcwd()."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="default-cwd-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Q?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch("ouroboros.mcp.tools.pm_handler.os.getcwd", return_value="/fallback/cwd"):
            await handler.handle({"initial_context": "Build it", "selected_repos": []})

        # cwd saved in meta should be the fallback
        meta = _load_pm_meta("default-cwd-sess", data_dir=tmp_path)
        assert meta is not None
        assert meta["cwd"] == "/fallback/cwd"

    @pytest.mark.asyncio
    async def test_start_records_unanswered_round(self, tmp_path: Path) -> None:
        """Start appends an unanswered round to state before saving."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="round-sess")
        state.rounds = []  # Start with no rounds
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("First question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        await handler.handle(
            {
                "initial_context": "Build something",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

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
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])
        engine.codebase_context = ""
        engine._reframe_map = {"PM-friendly Q": "Original tech Q"}

        state = _make_state(interview_id="reframe-sess")
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("PM-friendly Q"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result = await handler.handle(
            {
                "initial_context": "Build something",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["pending_reframe"] is not None
        assert meta["pending_reframe"]["reframed"] == "PM-friendly Q"
        assert meta["pending_reframe"]["original"] == "Original tech Q"

    @pytest.mark.asyncio
    async def test_start_meta_contains_question_field(self, tmp_path: Path) -> None:
        """Response meta includes the generated question as a separate field."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="q-meta-sess")
        state.is_brownfield = False
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(
            return_value=Result.ok("What is your target audience?")
        )
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "initial_context": "Build an app",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        assert result.value.meta["question"] == "What is your target audience?"

    @pytest.mark.asyncio
    async def test_start_meta_contains_is_brownfield_true(self, tmp_path: Path) -> None:
        """Response meta includes is_brownfield=True for brownfield projects."""
        (tmp_path / ".git").mkdir()

        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="bf-meta-sess")
        state.is_brownfield = True
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Q?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "initial_context": "Add auth",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        assert result.value.meta["is_brownfield"] is True

    @pytest.mark.asyncio
    async def test_start_meta_contains_is_brownfield_false(self, tmp_path: Path) -> None:
        """Response meta includes is_brownfield=False for greenfield projects."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="gf-meta-sess")
        state.is_brownfield = False
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Q?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "initial_context": "Build something new",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        assert result.value.meta["is_brownfield"] is False

    @pytest.mark.asyncio
    async def test_start_meta_has_all_required_fields(self, tmp_path: Path) -> None:
        """Response meta contains session_id, question, is_brownfield, and diff fields."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

        state = _make_state(interview_id="full-meta-sess")
        state.is_brownfield = False
        engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("First question?"))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "s.json"))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "initial_context": "Build an API",
                "selected_repos": [],
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        meta = result.value.meta
        # Core fields from Sub-AC 2
        assert "session_id" in meta
        assert "question" in meta
        assert "is_brownfield" in meta
        # Step 2 response meta: status, input_type, response_param (AC 7)
        assert meta["status"] == "interview_started"
        assert meta["input_type"] == "freeText"
        assert meta["response_param"] == "answer"
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

        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])
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

        result = await handler.handle(
            {
                "session_id": "resume-ac3",
                "answer": "User auth and dashboards",
            }
        )

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

        engine = make_pm_engine_mock(deferred_items=["old_deferred"], decide_later_items=[])
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

        result = await handler.handle(
            {
                "session_id": "resume-deferred",
                "answer": "We need REST APIs",
            }
        )

        assert result.is_ok
        meta = result.value.meta

        assert meta["deferred_this_round"] == ["new_tech_q"]
        assert meta["decide_later_this_round"] == ["premature_q"]
        assert meta["classification"] == "deferred"

    @pytest.mark.asyncio
    async def test_resume_meta_classification_none_when_no_classifications(
        self, tmp_path: Path
    ) -> None:
        """When engine has no classifications, classification is None."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])

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

        result = await handler.handle(
            {
                "session_id": "resume-noclassify",
                "answer": "Something",
            }
        )

        assert result.is_ok
        assert result.value.meta["classification"] is None

    @pytest.mark.asyncio
    async def test_resume_completion_meta_has_required_fields(self, tmp_path: Path) -> None:
        """When interview completes during resume, meta has AC 3 fields."""
        from ouroboros.bigbang.question_classifier import (
            ClassificationResult,
            QuestionCategory,
        )

        engine = make_pm_engine_mock(deferred_items=["d1"], decide_later_items=["dl1"])
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
            rounds=_make_answered_rounds(5),
        )

        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.complete_interview = AsyncMock(return_value=Result.ok(state))
        engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
        completion_meta = {
            "interview_complete": True,
            "completion_reason": "ambiguity_resolved",
            "rounds_completed": 5,
            "ambiguity_score": 0.15,
        }
        # Mock auto-generate on completion
        seed_mock = MagicMock()
        seed_mock.product_name = "Test Product"
        seed_mock.deferred_items = ["d1"]
        seed_mock.decide_later_items = ["dl1"]
        engine.generate_pm_seed = AsyncMock(return_value=Result.ok(seed_mock))
        engine.save_pm_seed = MagicMock(return_value=tmp_path / "seed.json")

        _save_pm_meta("resume-complete", engine, cwd="/tmp", data_dir=tmp_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with (
            patch(
                "ouroboros.mcp.tools.pm_handler.maybe_complete_pm_interview",
                new_callable=AsyncMock,
                return_value=Result.ok((state, completion_meta)),
            ),
            patch(
                "ouroboros.mcp.tools.pm_handler.save_pm_document",
                return_value=tmp_path / "pm.md",
            ),
        ):
            result = await handler.handle(
                {
                    "session_id": "resume-complete",
                    "answer": "Final answer",
                }
            )

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
        # Auto-generated PM document
        assert "pm_path" in meta
        assert "seed_path" in meta

    @pytest.mark.asyncio
    async def test_resume_loads_state_and_meta(self, tmp_path: Path) -> None:
        """Resume loads InterviewState via engine and restores pm_meta."""
        engine = make_pm_engine_mock(deferred_items=[], decide_later_items=[])
        engine.codebase_context = "existing context"

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

        result = await handler.handle(
            {
                "session_id": "resume-load",
                "answer": "My answer",
            }
        )

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
        """Calling with only initial_context (no action) triggers start directly."""
        state = _make_state(interview_id="auto-start")
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("What features?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = []
            # No "action" key in arguments at all
            result = await handler.handle({"initial_context": "Build a CRM"})

        assert result.is_ok
        # Start goes directly to interview_started (no project type step)
        assert result.value.meta["status"] == "interview_started"
        # Engine should have been started
        engine.ask_opening_and_start.assert_called_once()

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
        result = await handler.handle(
            {
                "session_id": "auto-resume",
                "answer": "My answer",
            }
        )

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


# ── select_repos routing tests (AC 4) ─────────────────────────


class TestSelectReposRouting:
    """Integration tests for _handle_select_repos via handle()."""

    @pytest.fixture
    def engine(self):
        """Create a mock engine."""
        eng = _make_engine_stub()
        eng.ask_opening_and_start = AsyncMock()
        eng.ask_next_question = AsyncMock()
        eng.save_state = AsyncMock()
        return eng

    def _make_handler(self, engine, tmp_path):
        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        return handler

    @pytest.mark.asyncio
    async def test_select_repos_with_initial_context_backward_compat(self, engine, tmp_path):
        """selected_repos + initial_context → 1-step start (backward compat)."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        state = _make_state(interview_id="s1")
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("What is your target user?")

        handler = self._make_handler(engine, tmp_path)

        db_repos = [
            BrownfieldRepo(path="/repo/a", name="a"),
            BrownfieldRepo(path="/repo/b", name="b"),
        ]
        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = db_repos
            result = await handler.handle(
                {
                    "selected_repos": ["/repo/a", "/repo/b"],
                    "initial_context": "Build a todo app",
                }
            )

        assert result.is_ok
        # Should have called ask_opening_and_start with brownfield_repos
        call_kwargs = engine.ask_opening_and_start.call_args
        repos = call_kwargs.kwargs.get("brownfield_repos") or call_kwargs[1].get("brownfield_repos")
        assert repos is not None
        assert len(repos) == 2
        assert all(r["role"] == "main" for r in repos)

    @pytest.mark.asyncio
    async def test_select_repos_with_session_id_step2(self, engine, tmp_path):
        """selected_repos + session_id → 2-step start step 2 (loads pm_meta)."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        # Save pm_meta from step 1
        _save_pm_meta(
            "interview_20260319_120000",
            engine=None,
            cwd="/tmp",
            data_dir=tmp_path,
            status="awaiting_repo_selection",
            extra={"initial_context": "Build a todo app"},
        )

        state = _make_state(interview_id="interview_20260319_120000")
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Who are the users?")

        handler = self._make_handler(engine, tmp_path)

        db_repo = BrownfieldRepo(path="/repo/a", name="a")
        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = [db_repo]
            result = await handler.handle(
                {
                    "selected_repos": ["/repo/a"],
                    "session_id": "interview_20260319_120000",
                }
            )

        assert result.is_ok
        # Should have recovered initial_context from pm_meta
        call_args = engine.ask_opening_and_start.call_args
        repos = call_args.kwargs.get("brownfield_repos") or call_args[1].get("brownfield_repos")
        assert repos is not None
        assert len(repos) == 1
        assert repos[0]["role"] == "main"
        assert repos[0]["name"] == "a"
        # desc absent when BrownfieldRepo has no desc
        assert "desc" not in repos[0]

    @pytest.mark.asyncio
    async def test_select_repos_no_session_no_context_error(self, engine, tmp_path):
        """selected_repos without session_id or initial_context → error."""
        handler = self._make_handler(engine, tmp_path)
        result = await handler.handle(
            {
                "selected_repos": ["/repo/a"],
            }
        )

        assert result.is_err
        assert "session_id" in str(result.error)

    @pytest.mark.asyncio
    async def test_select_repos_missing_pm_meta_error(self, engine, tmp_path):
        """selected_repos + session_id but no pm_meta on disk → error."""
        handler = self._make_handler(engine, tmp_path)
        result = await handler.handle(
            {
                "selected_repos": ["/repo/a"],
                "session_id": "nonexistent_session",
            }
        )

        assert result.is_err
        assert "No pm_meta" in str(result.error)

    @pytest.mark.asyncio
    async def test_select_repos_pm_meta_no_initial_context_error(self, engine, tmp_path):
        """pm_meta exists but has no initial_context → error."""
        _save_pm_meta(
            "interview_20260319_130000",
            engine=None,
            cwd="/tmp",
            data_dir=tmp_path,
            status="awaiting_repo_selection",
            extra={},  # No initial_context saved
        )

        handler = self._make_handler(engine, tmp_path)
        result = await handler.handle(
            {
                "selected_repos": ["/repo/a"],
                "session_id": "interview_20260319_130000",
            }
        )

        assert result.is_err
        assert "initial_context" in str(result.error)

    @pytest.mark.asyncio
    async def test_select_repos_idempotent_returns_first_question(self, engine, tmp_path):
        """Duplicate select_repos on already-started session returns first question (AC 9)."""
        session_id = "interview_20260319_140000"

        # Simulate a session that already completed step 2 (status=interview_started)
        _save_pm_meta(
            session_id,
            engine=None,
            cwd="/tmp",
            data_dir=tmp_path,
            status="interview_started",
        )

        # Set up engine.load_state to return a state with rounds
        state = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What is the target platform?",
                    user_response=None,
                ),
            ],
            is_brownfield=True,
        )
        engine.load_state = AsyncMock(return_value=Result.ok(state))

        handler = self._make_handler(engine, tmp_path)
        result = await handler.handle(
            {
                "selected_repos": ["/repo/a"],
                "session_id": session_id,
            }
        )

        assert result.is_ok
        tool_result = result.value
        meta = tool_result.meta

        # Should return the first question idempotently
        assert meta["session_id"] == session_id
        assert meta["status"] == "interview_started"
        assert meta["question"] == "What is the target platform?"
        assert meta["is_brownfield"] is True
        assert meta["idempotent"] is True

        # Content should include the first question
        assert "What is the target platform?" in tool_result.content[0].text
        assert session_id in tool_result.content[0].text

        # Engine should NOT have been asked to start a new interview
        engine.ask_opening_and_start.assert_not_called()
        engine.ask_next_question.assert_not_called()

    @pytest.mark.asyncio
    async def test_select_repos_idempotent_load_state_failure(self, engine, tmp_path):
        """Idempotent select_repos when state load fails returns error (AC 9)."""
        session_id = "interview_20260319_150000"

        _save_pm_meta(
            session_id,
            engine=None,
            cwd="/tmp",
            data_dir=tmp_path,
            status="interview_started",
        )

        # Engine can't load state (e.g., file corrupted)
        engine.load_state = AsyncMock(return_value=Result.err(Exception("state file corrupted")))

        handler = self._make_handler(engine, tmp_path)
        result = await handler.handle(
            {
                "selected_repos": ["/repo/a"],
                "session_id": session_id,
            }
        )

        assert result.is_err
        assert "marked as started" in str(result.error)


class TestResolveReposFromDB:
    """AC 5: DB lookup for path→BrownfieldRepo conversion in step 2."""

    @pytest.fixture
    def engine(self):
        eng = _make_engine_stub()
        eng.ask_opening_and_start = AsyncMock()
        eng.ask_next_question = AsyncMock()
        eng.save_state = AsyncMock()
        return eng

    def _make_handler(self, engine, tmp_path):
        return PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_resolve_repos_filters_missing_paths(self, engine, tmp_path):
        """Paths not in DB are silently ignored."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)
        db_repos = [
            BrownfieldRepo(path="/repo/a", name="alpha"),
            BrownfieldRepo(path="/repo/c", name="charlie"),
        ]
        with patch.object(handler, "_query_all_repos", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = db_repos
            resolved = await handler._resolve_repos_from_db(["/repo/a", "/repo/b", "/repo/c"])

        assert len(resolved) == 2
        assert resolved[0].path == "/repo/a"
        assert resolved[0].name == "alpha"
        assert resolved[1].path == "/repo/c"
        assert resolved[1].name == "charlie"

    @pytest.mark.asyncio
    async def test_resolve_repos_empty_when_all_missing(self, engine, tmp_path):
        """All paths missing from DB → empty list."""
        handler = self._make_handler(engine, tmp_path)
        with patch.object(handler, "_query_all_repos", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = []
            resolved = await handler._resolve_repos_from_db(["/gone/a", "/gone/b"])

        assert resolved == []

    @pytest.mark.asyncio
    async def test_resolve_repos_preserves_order(self, engine, tmp_path):
        """Resolved repos should preserve the order of input paths."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)
        db_repos = [
            BrownfieldRepo(path="/repo/b", name="bravo"),
            BrownfieldRepo(path="/repo/a", name="alpha"),
        ]
        with patch.object(handler, "_query_all_repos", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = db_repos
            resolved = await handler._resolve_repos_from_db(["/repo/a", "/repo/b"])

        assert [r.path for r in resolved] == ["/repo/a", "/repo/b"]

    @pytest.mark.asyncio
    async def test_resolve_repos_uses_db_name_not_basename(self, engine, tmp_path):
        """Resolved repo name comes from DB, not from Path(p).name."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)
        db_repos = [
            BrownfieldRepo(path="/long/path/to/repo", name="custom-name"),
        ]
        with patch.object(handler, "_query_all_repos", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = db_repos
            resolved = await handler._resolve_repos_from_db(["/long/path/to/repo"])

        assert resolved[0].name == "custom-name"

    @pytest.mark.asyncio
    async def test_step2_all_repos_missing_returns_error(self, engine, tmp_path):
        """Step 2: if all selected_repos are missing from DB, return explicit error."""
        handler = self._make_handler(engine, tmp_path)

        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = []  # All missing
            result = await handler.handle(
                {
                    "selected_repos": ["/gone/a"],
                    "initial_context": "Build something",
                }
            )

        assert result.is_err
        assert "could be resolved" in str(result.error)

    @pytest.mark.asyncio
    async def test_step2_partial_repos_missing_uses_found_only(self, engine, tmp_path):
        """Step 2: only repos found in DB are used; missing ones ignored."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        state = _make_state(interview_id="partial-sess")
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Tell me more")

        handler = self._make_handler(engine, tmp_path)

        db_repo = BrownfieldRepo(path="/repo/a", name="alpha", desc="A project")
        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = [db_repo]
            result = await handler.handle(
                {
                    "selected_repos": ["/repo/a", "/repo/gone"],
                    "initial_context": "Build something",
                }
            )

        assert result.is_ok
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is not None
        assert len(brownfield_repos) == 1
        assert brownfield_repos[0]["path"] == "/repo/a"
        assert brownfield_repos[0]["name"] == "alpha"
        assert brownfield_repos[0]["role"] == "main"
        assert brownfield_repos[0]["desc"] == "A project"


class TestRestartRecovery:
    """AC 10: MCP server restart recovery for awaiting_repo_selection sessions."""

    def _make_handler(self, engine, tmp_path):
        return PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_resume_interview_started_session_works(self, tmp_path: Path):
        """Resume on an interview_started session processes answer normally."""
        session_id = "interview_20260319_140000"

        _save_pm_meta(
            session_id,
            engine=None,
            cwd="/project",
            data_dir=tmp_path,
            status="interview_started",
        )

        state = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(round_number=1, question="Q1", user_response=None),
            ],
        )
        engine = _make_engine_stub()
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock()

        handler = self._make_handler(engine, tmp_path)

        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "My answer",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["session_id"] == session_id
        engine.load_state.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_resume_session_continues_interview(self, tmp_path: Path):
        """Resume on active session continues the interview loop."""
        session_id = "interview_20260319_140000"

        _save_pm_meta(
            session_id,
            engine=None,
            cwd="/project",
            data_dir=tmp_path,
            status="interview_started",
        )

        state = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(round_number=1, question="Q1", user_response=None),
            ],
        )
        engine = _make_engine_stub()
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Follow-up question?"))
        engine.save_state = AsyncMock()

        handler = self._make_handler(engine, tmp_path)

        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "Some answer",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["question"] == "Follow-up question?"
        assert meta["is_complete"] is False

    @pytest.mark.asyncio
    async def test_resume_active_session_not_intercepted(self, tmp_path: Path):
        """Resume on an active session bypasses recovery and goes to normal _handle_answer."""
        session_id = "interview_20260319_140000"

        # Save pm_meta with active status (not awaiting)
        _save_pm_meta(
            session_id,
            engine=None,
            cwd="/project",
            data_dir=tmp_path,
            status="active",
        )

        # Set up engine for normal resume
        state = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(round_number=1, question="Q1", user_response=None),
            ],
        )
        engine = _make_engine_stub()
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock()
        engine.record_response = AsyncMock(return_value=Result.ok(state))

        handler = self._make_handler(engine, tmp_path)
        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "My answer",
            }
        )

        assert result.is_ok
        # Normal resume path was taken (engine.load_state was called)
        engine.load_state.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_resume_no_pm_meta_not_intercepted(self, tmp_path: Path):
        """Resume with no pm_meta file → normal resume path (no recovery)."""
        session_id = "interview_20260319_140000"

        engine = _make_engine_stub()
        state = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(round_number=1, question="Q1", user_response=None),
            ],
        )
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))
        engine.save_state = AsyncMock()
        engine.record_response = AsyncMock(return_value=Result.ok(state))

        handler = self._make_handler(engine, tmp_path)
        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "My answer",
            }
        )

        assert result.is_ok
        engine.load_state.assert_called_once_with(session_id)

    @pytest.mark.asyncio
    async def test_resume_missing_state_returns_error(self, tmp_path: Path):
        """Resume when engine can't load state returns error."""
        session_id = "interview_20260319_140000"

        engine = _make_engine_stub()
        engine.load_state = AsyncMock(return_value=Result.err(Exception("state not found")))

        handler = self._make_handler(engine, tmp_path)

        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "My answer",
            }
        )

        assert result.is_err

    @pytest.mark.asyncio
    async def test_resume_preserves_session_id(self, tmp_path: Path):
        """Resume reuses the original session_id."""
        session_id = "interview_20260319_140000"

        _save_pm_meta(
            session_id,
            engine=None,
            cwd="/project",
            data_dir=tmp_path,
            status="interview_started",
        )

        state = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(round_number=1, question="Q1", user_response=None),
            ],
        )
        engine = _make_engine_stub()
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(return_value=Result.ok(state))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next?"))
        engine.save_state = AsyncMock()

        handler = self._make_handler(engine, tmp_path)

        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "My answer",
            }
        )

        assert result.is_ok
        assert result.value.meta["session_id"] == session_id


# ── 2-step start orchestration flow tests ────────────────────────


class TestTwoStepStartOrchestrationFlow:
    """End-to-end tests for the 2-step start pattern where
    ouroboros_pm_interview orchestrates brownfield repo selection.

    These tests simulate the full flow:
      Step 1: initial_context → repo list (awaiting_repo_selection)
      Step 2: selected_repos + session_id → interview started
      Step 3+: answer + session_id → normal interview flow
    """

    @pytest.fixture
    def engine(self):
        """Create a mock engine reusable across the flow."""
        eng = _make_engine_stub()
        eng.ask_opening_and_start = AsyncMock()
        eng.ask_next_question = AsyncMock()
        eng.record_response = AsyncMock()
        eng.load_state = AsyncMock()
        eng.save_state = AsyncMock()
        eng.complete_interview = AsyncMock()
        return eng

    def _make_handler(self, engine, tmp_path):
        return PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_simple_flow_start_answer_next(self, engine, tmp_path):
        """Simple flow: start → first question → answer → next question."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)

        default_repos = [
            BrownfieldRepo(
                path="/repos/frontend", name="frontend", desc="React app", is_default=True
            ),
        ]

        # ── Step 1: initial_context → interview starts directly ──
        state = _make_state(interview_id="simple-flow-sess", is_brownfield=True)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("What notifications do you need?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = default_repos
            step1_result = await handler.handle(
                {
                    "initial_context": "Add a notification system",
                    "cwd": str(tmp_path),
                }
            )

        assert step1_result.is_ok
        step1_meta = step1_result.value.meta
        assert step1_meta["status"] == "interview_started"
        assert step1_meta["question"] == "What notifications do you need?"
        session_id = step1_meta["session_id"]

        # Engine should have been started directly
        engine.ask_opening_and_start.assert_called_once()

        # ── Step 2: user answers first question ───────────────────
        state2 = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(
                    round_number=1, question="What notifications do you need?", user_response=None
                ),
            ],
            is_brownfield=True,
        )
        engine.load_state.return_value = Result.ok(state2)
        engine.record_response.return_value = Result.ok(state2)
        engine.ask_next_question.return_value = Result.ok("Who should receive notifications?")

        # Save pm_meta as engine would after start
        _save_pm_meta(
            session_id, engine, cwd=str(tmp_path), data_dir=tmp_path, status="interview_started"
        )

        step2_result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "Push notifications and emails",
            }
        )

        assert step2_result.is_ok
        step2_meta = step2_result.value.meta
        assert step2_meta["session_id"] == session_id
        assert step2_meta["question"] == "Who should receive notifications?"
        assert step2_meta["is_complete"] is False

    @pytest.mark.asyncio
    async def test_2step_all_repos_selected_role_main(self, engine, tmp_path):
        """Step 2 with multiple selected repos assigns role=main to all."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)
        session_id = "interview_20260319_100000"

        # Step 1 pm_meta
        _save_pm_meta(
            session_id,
            engine=None,
            cwd=str(tmp_path),
            data_dir=tmp_path,
            status="awaiting_repo_selection",
            extra={"initial_context": "Build a dashboard"},
        )

        state = _make_state(interview_id=session_id, is_brownfield=True)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("What data to display?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        db_repos = [
            BrownfieldRepo(path="/a", name="alpha", desc="Service A"),
            BrownfieldRepo(path="/b", name="bravo"),
            BrownfieldRepo(path="/c", name="charlie", desc="Service C"),
        ]

        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = db_repos
            result = await handler.handle(
                {
                    "selected_repos": ["/a", "/b", "/c"],
                    "session_id": session_id,
                }
            )

        assert result.is_ok
        call_kwargs = engine.ask_opening_and_start.call_args
        bf_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert len(bf_repos) == 3
        assert all(r["role"] == "main" for r in bf_repos)
        # desc present when available, absent when not
        assert bf_repos[0]["desc"] == "Service A"
        assert "desc" not in bf_repos[1]
        assert bf_repos[2]["desc"] == "Service C"

    @pytest.mark.asyncio
    async def test_2step_user_deselects_all_repos_greenfield(self, engine, tmp_path):
        """Step 2 with empty selected_repos → greenfield (no brownfield context)."""
        handler = self._make_handler(engine, tmp_path)
        session_id = "interview_20260319_110000"

        _save_pm_meta(
            session_id,
            engine=None,
            cwd=str(tmp_path),
            data_dir=tmp_path,
            status="awaiting_repo_selection",
            extra={"initial_context": "Build from scratch"},
        )

        state = _make_state(interview_id=session_id, is_brownfield=False)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Describe the project")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = []  # Empty: user deselected all (or all missing)
            result = await handler.handle(
                {
                    "selected_repos": [],
                    "session_id": session_id,
                }
            )

        assert result.is_ok
        # Should start as greenfield
        call_kwargs = engine.ask_opening_and_start.call_args
        assert call_kwargs.kwargs.get("brownfield_repos") is None

    @pytest.mark.asyncio
    async def test_greenfield_start_no_defaults(self, engine, tmp_path):
        """Start with no default repos starts greenfield interview directly."""
        handler = self._make_handler(engine, tmp_path)

        state = _make_state(interview_id="auto-gf-sess", is_brownfield=False)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("What do you want to build?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = []
            result = await handler.handle(
                {
                    "initial_context": "Brand new project",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        meta = result.value.meta
        assert meta["status"] == "interview_started"
        assert meta["input_type"] == "freeText"
        assert meta["is_brownfield"] is False
        # Engine was started with greenfield
        engine.ask_opening_and_start.assert_called_once()
        call_kwargs = engine.ask_opening_and_start.call_args
        assert call_kwargs.kwargs.get("brownfield_repos") is None

    @pytest.mark.asyncio
    async def test_1step_backward_compat_with_selected_repos_and_context(self, engine, tmp_path):
        """selected_repos + initial_context in one call → 1-step start (backward compat)."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)

        state = _make_state(interview_id="compat-sess", is_brownfield=True)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Tell me about the feature")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        db_repo = BrownfieldRepo(path="/repo/main", name="main-app", desc="Main application")
        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = [db_repo]
            result = await handler.handle(
                {
                    "initial_context": "Add auth module",
                    "selected_repos": ["/repo/main"],
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        meta = result.value.meta
        # Should go straight to interview_started (no awaiting step)
        assert meta["status"] == "interview_started"
        assert meta["session_id"] == "compat-sess"
        # No pm_meta with awaiting status should exist
        # Engine was started with brownfield repos
        call_kwargs = engine.ask_opening_and_start.call_args
        bf_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert bf_repos is not None
        assert bf_repos[0]["role"] == "main"

    @pytest.mark.asyncio
    async def test_start_pm_meta_persists_after_interview_start(self, engine, tmp_path):
        """Start saves pm_meta with interview_started status."""
        handler = self._make_handler(engine, tmp_path)

        state = _make_state(interview_id="persist-sess", is_brownfield=False)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Which services?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = []
            result = await handler.handle(
                {
                    "initial_context": "Migrate to microservices",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        session_id = result.value.meta["session_id"]

        # Verify pm_meta on disk has interview_started status
        import json

        meta_file = tmp_path / f"pm_meta_{session_id}.json"
        assert meta_file.exists()
        saved_meta = json.loads(meta_file.read_text())
        assert saved_meta["status"] == "interview_started"

    @pytest.mark.asyncio
    async def test_step2_session_id_reuse(self, engine, tmp_path):
        """Step 2 returns the same session_id as step 1."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)
        session_id = "interview_20260319_160000"

        _save_pm_meta(
            session_id,
            engine=None,
            cwd=str(tmp_path),
            data_dir=tmp_path,
            status="awaiting_repo_selection",
            extra={"initial_context": "Build something"},
        )

        state = _make_state(interview_id=session_id)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("First question?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = [BrownfieldRepo(path="/r", name="r")]
            result = await handler.handle(
                {
                    "selected_repos": ["/r"],
                    "session_id": session_id,
                }
            )

        assert result.is_ok
        # Session ID must match step 1's session ID
        assert result.value.meta["session_id"] == session_id

    @pytest.mark.asyncio
    async def test_start_uses_default_repos_from_db(self, engine, tmp_path):
        """Start auto-loads default repos from DB and passes them to engine."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)

        default_repos = [
            BrownfieldRepo(path="/repos/default-repo", name="default-repo", is_default=True),
            BrownfieldRepo(path="/repos/third", name="third", is_default=True),
        ]

        state = _make_state(interview_id="defaults-sess", is_brownfield=True)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Some feature question?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        with patch.object(handler, "_query_default_repos", new_callable=AsyncMock) as mock_defaults:
            mock_defaults.return_value = default_repos
            result = await handler.handle(
                {
                    "initial_context": "Some feature",
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        meta = result.value.meta
        assert meta["status"] == "interview_started"

        # Verify both default repos were passed to engine
        call_kwargs = engine.ask_opening_and_start.call_args
        brownfield_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert brownfield_repos is not None
        assert len(brownfield_repos) == 2
        assert brownfield_repos[0]["name"] == "default-repo"
        assert brownfield_repos[1]["name"] == "third"

    @pytest.mark.asyncio
    async def test_restart_recovery_resumes_active_interview(self, engine, tmp_path):
        """After server restart, resume on interview_started session works normally."""
        handler = self._make_handler(engine, tmp_path)

        session_id = "interview_20260319_170000"
        _save_pm_meta(
            session_id,
            engine=None,
            cwd=str(tmp_path),
            data_dir=tmp_path,
            status="interview_started",
        )

        state = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(
                    round_number=1, question="What search features?", user_response=None
                ),
            ],
            is_brownfield=True,
        )
        engine.load_state.return_value = Result.ok(state)
        engine.record_response.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("Who uses the search?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "Full-text search and filters",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["session_id"] == session_id
        assert meta["question"] == "Who uses the search?"
        assert meta["is_complete"] is False

    @pytest.mark.asyncio
    async def test_step2_desc_optional_in_brownfield_repos(self, engine, tmp_path):
        """Step 2 omits desc key when BrownfieldRepo.desc is None."""
        from ouroboros.persistence.brownfield import BrownfieldRepo

        handler = self._make_handler(engine, tmp_path)

        state = _make_state(interview_id="no-desc-sess", is_brownfield=True)
        engine.ask_opening_and_start.return_value = Result.ok(state)
        engine.ask_next_question.return_value = Result.ok("First question?")
        engine.save_state.return_value = Result.ok(tmp_path / "state.json")

        no_desc_repo = BrownfieldRepo(path="/repo/plain", name="plain")
        with_desc_repo = BrownfieldRepo(path="/repo/documented", name="documented", desc="Has docs")

        with patch.object(
            handler, "_resolve_repos_from_db", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = [no_desc_repo, with_desc_repo]
            result = await handler.handle(
                {
                    "initial_context": "Test project",
                    "selected_repos": ["/repo/plain", "/repo/documented"],
                    "cwd": str(tmp_path),
                }
            )

        assert result.is_ok
        call_kwargs = engine.ask_opening_and_start.call_args
        bf_repos = call_kwargs.kwargs.get("brownfield_repos")
        assert len(bf_repos) == 2
        # First repo: no desc key
        assert "desc" not in bf_repos[0]
        assert bf_repos[0]["name"] == "plain"
        # Second repo: desc present
        assert bf_repos[1]["desc"] == "Has docs"
        assert bf_repos[1]["name"] == "documented"

    @pytest.mark.asyncio
    async def test_step2_duplicate_call_idempotent(self, engine, tmp_path):
        """Calling step 2 twice on the same session returns first question idempotently."""
        handler = self._make_handler(engine, tmp_path)
        session_id = "interview_20260319_180000"

        # First call to step 2 already started the interview
        _save_pm_meta(
            session_id,
            engine=None,
            cwd=str(tmp_path),
            data_dir=tmp_path,
            status="interview_started",
        )

        state = _make_state(
            interview_id=session_id,
            rounds=[
                InterviewRound(round_number=1, question="What is the scope?", user_response=None),
            ],
            is_brownfield=True,
        )
        engine.load_state = AsyncMock(return_value=Result.ok(state))

        # Duplicate step 2 call
        result = await handler.handle(
            {
                "selected_repos": ["/repo/a"],
                "session_id": session_id,
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["idempotent"] is True
        assert meta["session_id"] == session_id
        assert meta["question"] == "What is the scope?"
        # Engine should NOT have been restarted
        engine.ask_opening_and_start.assert_not_called()
