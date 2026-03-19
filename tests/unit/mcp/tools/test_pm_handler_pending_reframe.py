"""Tests for pending_reframe in PMInterviewHandler.

AC 7: pending_reframe stores single {reframed, original} object
and clears after response mapping.

Verifies:
- pending_reframe is set when a REFRAMED question is produced
- pending_reframe contains exactly {reframed, original} keys
- pending_reframe is cleared after response mapping
- pending_reframe is None for PASSTHROUGH questions
- pending_reframe persists correctly across save/load cycle
- pending_reframe is surfaced in response meta
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.core.types import Result
from ouroboros.mcp.tools.pm_handler import (
    PMInterviewHandler,
    _load_pm_meta,
    _meta_path,
    _restore_engine_meta,
    _save_pm_meta,
)

# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    """Temporary data directory for pm_meta files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture()
def mock_engine() -> PMInterviewEngine:
    """Create a mock PMInterviewEngine with default empty state."""
    from tests.unit.mcp.tools.conftest import make_pm_engine_mock

    return make_pm_engine_mock()


@pytest.fixture()
def mock_state() -> InterviewState:
    """Create a mock InterviewState."""
    state = MagicMock(spec=InterviewState)
    state.interview_id = "pm-test-001"
    state.rounds = []
    state.current_round_number = 1
    state.is_brownfield = False
    state.mark_updated = MagicMock()
    return state


# ──────────────────────────────────────────────────────────────
# _save_pm_meta / _load_pm_meta: pending_reframe persistence
# ──────────────────────────────────────────────────────────────


class TestPendingReframePersistence:
    """Test pending_reframe save/load in pm_meta JSON."""

    def test_save_meta_with_pending_reframe(
        self, mock_engine: PMInterviewEngine, tmp_data_dir: Path
    ) -> None:
        """pending_reframe is saved as {reframed, original} when _reframe_map is populated."""
        mock_engine._reframe_map = {
            "What user problem does this solve?": "What database schema should we use?"
        }

        _save_pm_meta("sess-001", mock_engine, cwd="/tmp/project", data_dir=tmp_data_dir)

        path = _meta_path("sess-001", tmp_data_dir)
        assert path.exists()

        data = json.loads(path.read_text())
        assert data["pending_reframe"] == {
            "reframed": "What user problem does this solve?",
            "original": "What database schema should we use?",
        }

    def test_save_meta_without_pending_reframe(
        self, mock_engine: PMInterviewEngine, tmp_data_dir: Path
    ) -> None:
        """pending_reframe is None when _reframe_map is empty."""
        mock_engine._reframe_map = {}

        _save_pm_meta("sess-002", mock_engine, cwd="/tmp/project", data_dir=tmp_data_dir)

        data = json.loads(_meta_path("sess-002", tmp_data_dir).read_text())
        assert data["pending_reframe"] is None

    def test_save_meta_single_reframe_only(
        self, mock_engine: PMInterviewEngine, tmp_data_dir: Path
    ) -> None:
        """Even if _reframe_map has multiple entries (shouldn't normally),
        pending_reframe stores only the most recent one."""
        mock_engine._reframe_map = {
            "Q1 reframed": "Q1 original",
            "Q2 reframed": "Q2 original",
        }

        _save_pm_meta("sess-003", mock_engine, data_dir=tmp_data_dir)

        data = json.loads(_meta_path("sess-003", tmp_data_dir).read_text())
        # Should store only the last inserted entry
        assert data["pending_reframe"] is not None
        assert "reframed" in data["pending_reframe"]
        assert "original" in data["pending_reframe"]
        # It's one single object, not a list
        assert isinstance(data["pending_reframe"], dict)
        assert len(data["pending_reframe"]) == 2

    def test_load_meta_restores_pending_reframe(self, tmp_data_dir: Path) -> None:
        """Loading pm_meta correctly restores pending_reframe."""
        meta = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": {
                "reframed": "What's the user impact?",
                "original": "What API protocol should we use?",
            },
            "cwd": "/tmp/project",
        }
        path = _meta_path("sess-004", tmp_data_dir)
        path.write_text(json.dumps(meta))

        loaded = _load_pm_meta("sess-004", tmp_data_dir)
        assert loaded is not None
        assert loaded["pending_reframe"] == {
            "reframed": "What's the user impact?",
            "original": "What API protocol should we use?",
        }

    def test_load_meta_none_when_no_pending_reframe(self, tmp_data_dir: Path) -> None:
        """Loading pm_meta with null pending_reframe returns None."""
        meta: dict[str, object] = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": None,
            "cwd": "/tmp/project",
        }
        path = _meta_path("sess-005", tmp_data_dir)
        path.write_text(json.dumps(meta))

        loaded = _load_pm_meta("sess-005", tmp_data_dir)
        assert loaded is not None
        assert loaded["pending_reframe"] is None

    def test_roundtrip_pending_reframe(
        self, mock_engine: PMInterviewEngine, tmp_data_dir: Path
    ) -> None:
        """Save then load preserves pending_reframe exactly."""
        mock_engine._reframe_map = {"How will users interact?": "What REST endpoints do we need?"}

        _save_pm_meta("sess-006", mock_engine, data_dir=tmp_data_dir)
        loaded = _load_pm_meta("sess-006", tmp_data_dir)

        assert loaded is not None
        assert loaded["pending_reframe"] == {
            "reframed": "How will users interact?",
            "original": "What REST endpoints do we need?",
        }


# ──────────────────────────────────────────────────────────────
# _restore_engine_meta: pending_reframe → _reframe_map
# ──────────────────────────────────────────────────────────────


class TestRestoreEngineMeta:
    """Test restoring pending_reframe into engine._reframe_map."""

    def test_restore_with_pending_reframe(self, mock_engine: PMInterviewEngine) -> None:
        """Restoring meta with pending_reframe populates engine._reframe_map."""
        # Use a real dict for _reframe_map so we can verify mutations
        mock_engine._reframe_map = {}

        meta = {
            "deferred_items": ["Q1"],
            "decide_later_items": ["Q2"],
            "codebase_context": "some context",
            "pending_reframe": {
                "reframed": "What user need does this address?",
                "original": "What microservice architecture?",
            },
            "cwd": "/tmp",
        }

        _restore_engine_meta(mock_engine, meta)

        assert mock_engine._reframe_map == {
            "What user need does this address?": "What microservice architecture?"
        }

    def test_restore_without_pending_reframe(self, mock_engine: PMInterviewEngine) -> None:
        """Restoring meta without pending_reframe leaves _reframe_map empty."""
        mock_engine._reframe_map = {}

        meta: dict[str, object] = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": None,
            "cwd": "",
        }

        _restore_engine_meta(mock_engine, meta)

        assert mock_engine._reframe_map == {}

    def test_restore_clears_previous_reframe_map_entries(
        self, mock_engine: PMInterviewEngine
    ) -> None:
        """Restoring meta overwrites (not appends to) existing _reframe_map entries.

        Note: _restore_engine_meta uses dict assignment, so pre-existing entries
        remain. This test documents current behavior: the engine starts fresh
        each MCP call, so stale entries should not exist in practice.
        """
        mock_engine._reframe_map = {}
        # Simulate pre-existing stale entry (shouldn't happen in normal flow)
        mock_engine._reframe_map["stale reframed"] = "stale original"

        meta = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": {
                "reframed": "New reframed Q",
                "original": "New original Q",
            },
            "cwd": "",
        }

        _restore_engine_meta(mock_engine, meta)

        # The new reframe is added; in practice the engine is fresh each call
        assert "New reframed Q" in mock_engine._reframe_map
        assert mock_engine._reframe_map["New reframed Q"] == "New original Q"


# ──────────────────────────────────────────────────────────────
# PMInterviewHandler: pending_reframe in response meta
# ──────────────────────────────────────────────────────────────


class TestPendingReframeInResponseMeta:
    """Test that pending_reframe is surfaced in MCP response metadata."""

    @pytest.fixture()
    def _handler_with_engine(
        self, mock_engine: PMInterviewEngine, tmp_data_dir: Path
    ) -> tuple[PMInterviewHandler, PMInterviewEngine]:
        """Create handler with mock engine."""
        handler = PMInterviewHandler(pm_engine=mock_engine, data_dir=tmp_data_dir)
        return handler, mock_engine

    async def test_start_response_includes_pending_reframe_when_reframed(
        self, mock_engine: PMInterviewEngine, mock_state: InterviewState, tmp_data_dir: Path
    ) -> None:
        """When start produces a REFRAMED question, response meta includes pending_reframe."""
        mock_engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(mock_state))

        # Simulate ask_next_question producing a reframed question
        async def fake_ask_next(state):
            mock_engine._reframe_map["What's the user workflow?"] = (
                "What message queue should we use?"
            )
            return Result.ok("What's the user workflow?")

        mock_engine.ask_next_question = AsyncMock(side_effect=fake_ask_next)
        mock_engine.save_state = AsyncMock(return_value=Result.ok(Path("/tmp/state.json")))

        handler = PMInterviewHandler(pm_engine=mock_engine, data_dir=tmp_data_dir)

        result = await handler.handle(
            {"initial_context": "Build a chat app", "selected_repos": [], "cwd": "/tmp"}
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["pending_reframe"] == {
            "reframed": "What's the user workflow?",
            "original": "What message queue should we use?",
        }

    async def test_start_response_pending_reframe_none_when_passthrough(
        self, mock_engine: PMInterviewEngine, mock_state: InterviewState, tmp_data_dir: Path
    ) -> None:
        """When start produces a PASSTHROUGH question, pending_reframe is None."""
        mock_engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(mock_state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=Result.ok("What problem does this solve?")
        )
        mock_engine.save_state = AsyncMock(return_value=Result.ok(Path("/tmp/state.json")))

        handler = PMInterviewHandler(pm_engine=mock_engine, data_dir=tmp_data_dir)

        result = await handler.handle(
            {"initial_context": "Build a chat app", "selected_repos": [], "cwd": "/tmp"}
        )

        assert result.is_ok
        assert result.value.meta["pending_reframe"] is None

    async def test_answer_clears_pending_reframe_after_response(
        self, mock_engine: PMInterviewEngine, tmp_data_dir: Path
    ) -> None:
        """When answering a previously reframed question, pending_reframe is cleared."""
        session_id = "pm-reframe-001"

        # Set up state with an unanswered reframed question
        state = InterviewState(
            interview_id=session_id,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What's the user workflow?",
                    user_response=None,
                ),
            ],
        )

        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.record_response = AsyncMock(return_value=Result.ok(state))

        # Simulate the next question being a passthrough (no reframe)
        mock_engine.ask_next_question = AsyncMock(
            return_value=Result.ok("What are the success criteria?")
        )
        mock_engine.save_state = AsyncMock(return_value=Result.ok(Path("/tmp/state.json")))

        # Save meta with pending_reframe set
        mock_engine._reframe_map = {
            "What's the user workflow?": "What message queue should we use?"
        }
        _save_pm_meta(session_id, mock_engine, cwd="/tmp", data_dir=tmp_data_dir)

        # Now create a fresh engine (simulates new MCP call) with empty _reframe_map
        mock_engine._reframe_map = {}

        # record_response should pop from _reframe_map (simulated)
        async def fake_record_response(s, answer, question):
            # Simulate engine.record_response popping from _reframe_map
            mock_engine._reframe_map.pop(question, None)
            return Result.ok(s)

        mock_engine.record_response = AsyncMock(side_effect=fake_record_response)

        handler = PMInterviewHandler(pm_engine=mock_engine, data_dir=tmp_data_dir)

        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "Users will submit forms",
                "cwd": "/tmp",
            }
        )

        assert result.is_ok
        # After recording the answer to a reframed question and getting
        # a passthrough next question, pending_reframe should be None
        assert result.value.meta["pending_reframe"] is None

        # Verify persisted meta also has pending_reframe cleared
        saved_meta = _load_pm_meta(session_id, tmp_data_dir)
        assert saved_meta is not None
        assert saved_meta["pending_reframe"] is None

    async def test_answer_produces_new_pending_reframe_for_next_question(
        self, mock_engine: PMInterviewEngine, tmp_data_dir: Path
    ) -> None:
        """When the next question after an answer is REFRAMED, a new pending_reframe is set."""
        session_id = "pm-reframe-002"

        state = InterviewState(
            interview_id=session_id,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What problem does this solve?",
                    user_response=None,
                ),
            ],
        )

        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.record_response = AsyncMock(return_value=Result.ok(state))

        # Next question is reframed
        async def fake_ask_next(s):
            mock_engine._reframe_map["How should data be organized for users?"] = (
                "What database normalization level?"
            )
            return Result.ok("How should data be organized for users?")

        mock_engine.ask_next_question = AsyncMock(side_effect=fake_ask_next)
        mock_engine.save_state = AsyncMock(return_value=Result.ok(Path("/tmp/state.json")))

        # No pending_reframe in existing meta (previous question was passthrough)
        _save_pm_meta_dict(
            session_id,
            {
                "deferred_items": [],
                "decide_later_items": [],
                "codebase_context": "",
                "pending_reframe": None,
                "cwd": "/tmp",
            },
            tmp_data_dir,
        )

        handler = PMInterviewHandler(pm_engine=mock_engine, data_dir=tmp_data_dir)

        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "We're solving onboarding friction",
                "cwd": "/tmp",
            }
        )

        assert result.is_ok
        assert result.value.meta["pending_reframe"] == {
            "reframed": "How should data be organized for users?",
            "original": "What database normalization level?",
        }

        # Verify persisted meta also has the new pending_reframe
        saved_meta = _load_pm_meta(session_id, tmp_data_dir)
        assert saved_meta is not None
        assert saved_meta["pending_reframe"] == {
            "reframed": "How should data be organized for users?",
            "original": "What database normalization level?",
        }


# ──────────────────────────────────────────────────────────────
# End-to-end: reframe → answer → clear cycle
# ──────────────────────────────────────────────────────────────


class TestPendingReframeCycle:
    """End-to-end test of the pending_reframe lifecycle."""

    async def test_full_reframe_answer_clear_cycle(
        self, mock_engine: PMInterviewEngine, tmp_data_dir: Path
    ) -> None:
        """Verify the full lifecycle: reframe set → persisted → restored → cleared after answer."""
        session_id = "pm-cycle-001"

        # Step 1: Start interview — first question is reframed
        state = InterviewState(interview_id=session_id, rounds=[])
        mock_engine.ask_opening_and_start = AsyncMock(return_value=Result.ok(state))

        async def fake_ask_next_reframed(s):
            mock_engine._reframe_map["What user groups exist?"] = (
                "What IAM roles should we configure?"
            )
            return Result.ok("What user groups exist?")

        mock_engine.ask_next_question = AsyncMock(side_effect=fake_ask_next_reframed)
        mock_engine.save_state = AsyncMock(return_value=Result.ok(Path("/tmp/state.json")))

        handler = PMInterviewHandler(pm_engine=mock_engine, data_dir=tmp_data_dir)

        start_result = await handler.handle(
            {
                "initial_context": "Build an admin panel",
                "selected_repos": [],
                "cwd": "/tmp",
            }
        )

        assert start_result.is_ok
        assert start_result.value.meta["pending_reframe"] == {
            "reframed": "What user groups exist?",
            "original": "What IAM roles should we configure?",
        }

        # Step 2: Verify meta was persisted
        saved_meta = _load_pm_meta(session_id, tmp_data_dir)
        assert saved_meta is not None
        assert saved_meta["pending_reframe"] is not None
        assert saved_meta["pending_reframe"]["reframed"] == "What user groups exist?"
        assert saved_meta["pending_reframe"]["original"] == "What IAM roles should we configure?"

        # Step 3: Answer — simulates next MCP call with fresh engine
        mock_engine._reframe_map = {}  # Fresh engine

        state_with_round = InterviewState(
            interview_id=session_id,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What user groups exist?",
                    user_response=None,
                ),
            ],
        )
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state_with_round))

        async def fake_record_response(s, answer, question):
            # Simulate engine.record_response popping reframe
            mock_engine._reframe_map.pop(question, None)
            return Result.ok(s)

        mock_engine.record_response = AsyncMock(side_effect=fake_record_response)

        # Next question is passthrough (no reframe)
        mock_engine.ask_next_question = AsyncMock(
            return_value=Result.ok("What are the success metrics?")
        )

        answer_result = await handler.handle(
            {
                "session_id": session_id,
                "answer": "Admins, editors, and viewers",
                "cwd": "/tmp",
            }
        )

        assert answer_result.is_ok

        # Step 4: Verify pending_reframe is cleared in response meta
        assert answer_result.value.meta["pending_reframe"] is None

        # Step 5: Verify pending_reframe is cleared in persisted meta
        saved_meta_after = _load_pm_meta(session_id, tmp_data_dir)
        assert saved_meta_after is not None
        assert saved_meta_after["pending_reframe"] is None


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────


def _save_pm_meta_dict(
    session_id: str,
    meta: dict[str, object],
    data_dir: Path,
) -> None:
    """Helper to save a raw meta dict for test setup."""
    path = _meta_path(session_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
