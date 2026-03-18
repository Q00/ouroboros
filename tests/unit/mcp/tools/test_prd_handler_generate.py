"""Tests for PRDInterviewHandler action:generate (AC 4).

Verifies that _handle_generate:
- Loads InterviewState and prd_meta
- Restores engine via restore_meta() (not _restore_engine_meta)
- Runs generate_prd_seed
- Saves PRD seed to ~/.ouroboros/seeds/ and prd.md to {cwd}/.ouroboros/
- Returns meta with session_id, prd_path, seed_path
- Is idempotent (same result on retry)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.prd_interview import PRDInterviewEngine
from ouroboros.bigbang.prd_seed import PRDSeed, UserStory
from ouroboros.core.types import Result
from ouroboros.mcp.tools.prd_handler import (
    PRDInterviewHandler,
    _save_prd_meta,
)

# ── Helpers ──────────────────────────────────────────────────────


def _make_seed(
    prd_id: str = "prd_seed_test123",
    product_name: str = "Test Product",
    interview_id: str = "test-session-gen",
) -> PRDSeed:
    """Create a minimal PRDSeed for testing."""
    return PRDSeed(
        prd_id=prd_id,
        product_name=product_name,
        goal="Build a great product",
        user_stories=(
            UserStory(persona="User", action="do stuff", benefit="save time"),
        ),
        constraints=("Timeline: 3 months",),
        success_criteria=("100 users",),
        deferred_items=("DB choice",),
        decide_later_items=("Auth provider",),
        assumptions=("Users have internet",),
        interview_id=interview_id,
    )


def _make_state(
    interview_id: str = "test-session-gen",
    rounds: list[InterviewRound] | None = None,
) -> InterviewState:
    """Create a minimal InterviewState for testing."""
    state = MagicMock(spec=InterviewState)
    state.interview_id = interview_id
    state.initial_context = "Build a task manager"
    state.rounds = rounds or [
        InterviewRound(round_number=1, question="Q1?", user_response="A1"),
        InterviewRound(round_number=2, question="Q2?", user_response="A2"),
    ]
    state.is_complete = True
    state.is_brownfield = False
    return state


def _make_engine_for_generate(
    state: InterviewState,
    seed: PRDSeed,
    seed_path: Path | None = None,
    prd_path: Path = Path("/fake/cwd/.ouroboros/prd.md"),
) -> PRDInterviewEngine:
    """Create a mock PRDInterviewEngine for generate tests."""
    if seed_path is None:
        seed_path = Path.home() / ".ouroboros" / "seeds" / "prd_seed_test123.yaml"
    engine = MagicMock(spec=PRDInterviewEngine)
    engine.deferred_items = []
    engine.decide_later_items = []
    engine.codebase_context = ""
    engine._reframe_map = {}

    engine.load_state = AsyncMock(return_value=Result.ok(state))
    engine.generate_prd_seed = AsyncMock(return_value=Result.ok(seed))
    engine.save_prd_seed = MagicMock(return_value=seed_path)
    engine.save_prd_document = MagicMock(return_value=prd_path)
    engine.restore_meta = MagicMock()

    return engine


# ── Tests ────────────────────────────────────────────────────────


class TestHandleGenerate:
    """Tests for PRDInterviewHandler._handle_generate."""

    @pytest.mark.asyncio
    async def test_generate_returns_session_id_in_meta(self, tmp_path: Path) -> None:
        """Generate returns session_id in response meta."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        meta = result.value.meta
        assert meta["session_id"] == "test-session-gen"

    @pytest.mark.asyncio
    async def test_generate_returns_prd_path_in_meta(self, tmp_path: Path) -> None:
        """Generate returns prd_path (not doc_path) in response meta."""
        seed = _make_seed()
        state = _make_state()
        prd_path = tmp_path / ".ouroboros" / "prd.md"
        engine = _make_engine_for_generate(state, seed, prd_path=prd_path)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        meta = result.value.meta
        assert "prd_path" in meta
        assert meta["prd_path"] == str(prd_path)
        # Should NOT have doc_path key
        assert "doc_path" not in meta

    @pytest.mark.asyncio
    async def test_generate_returns_seed_path_in_meta(self, tmp_path: Path) -> None:
        """Generate returns seed_path in response meta."""
        seed = _make_seed()
        state = _make_state()
        seed_path = Path.home() / ".ouroboros" / "seeds" / "prd_seed_test123.yaml"
        engine = _make_engine_for_generate(state, seed, seed_path=seed_path)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        meta = result.value.meta
        assert meta["seed_path"] == str(seed_path)

    @pytest.mark.asyncio
    async def test_generate_meta_has_exactly_three_keys(self, tmp_path: Path) -> None:
        """Generate meta contains exactly session_id, prd_path, seed_path."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        meta = result.value.meta
        assert set(meta.keys()) == {"session_id", "prd_path", "seed_path"}

    @pytest.mark.asyncio
    async def test_generate_loads_interview_state(self, tmp_path: Path) -> None:
        """Generate loads InterviewState via engine.load_state."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        engine.load_state.assert_awaited_once_with("test-session-gen")

    @pytest.mark.asyncio
    async def test_generate_restores_meta_via_engine_method(self, tmp_path: Path) -> None:
        """Generate restores PRD meta via engine.restore_meta(), not _restore_engine_meta."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        # Save some meta so it gets loaded
        meta_data = {
            "deferred_items": ["DB choice"],
            "decide_later_items": ["Auth provider"],
            "codebase_context": "some context",
            "pending_reframe": None,
            "cwd": str(tmp_path),
        }
        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        # Manually save meta
        _save_prd_meta.__wrapped__ if hasattr(_save_prd_meta, '__wrapped__') else None
        meta_path = tmp_path / "prd_meta_test-session-gen.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        import json
        meta_path.write_text(json.dumps(meta_data), encoding="utf-8")

        await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        # Verify engine.restore_meta was called with the loaded meta
        engine.restore_meta.assert_called_once_with(meta_data)

    @pytest.mark.asyncio
    async def test_generate_skips_restore_when_no_meta(self, tmp_path: Path) -> None:
        """Generate works without prd_meta file (no restore_meta call)."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        engine.restore_meta.assert_not_called()

    @pytest.mark.asyncio
    async def test_generate_calls_generate_prd_seed(self, tmp_path: Path) -> None:
        """Generate calls engine.generate_prd_seed with loaded state."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        engine.generate_prd_seed.assert_awaited_once_with(state)

    @pytest.mark.asyncio
    async def test_generate_saves_seed_to_seeds_dir(self, tmp_path: Path) -> None:
        """Generate saves seed via engine.save_prd_seed."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        engine.save_prd_seed.assert_called_once_with(seed)

    @pytest.mark.asyncio
    async def test_generate_saves_prd_to_cwd_ouroboros(self, tmp_path: Path) -> None:
        """Generate saves prd.md to {cwd}/.ouroboros/."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        cwd = str(tmp_path / "my_project")
        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": cwd,
        })

        expected_dir = Path(cwd) / ".ouroboros"
        engine.save_prd_document.assert_called_once_with(seed, output_dir=expected_dir)

    @pytest.mark.asyncio
    async def test_generate_content_includes_product_name(self, tmp_path: Path) -> None:
        """Generate response content includes product name."""
        seed = _make_seed(product_name="My App")
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        text = result.value.content[0].text
        assert "My App" in text

    @pytest.mark.asyncio
    async def test_generate_error_on_load_state_failure(self, tmp_path: Path) -> None:
        """Generate returns error when load_state fails."""
        from ouroboros.core.errors import ValidationError

        engine = MagicMock(spec=PRDInterviewEngine)
        engine.load_state = AsyncMock(
            return_value=Result.err(ValidationError("Not found", field="session_id"))
        )

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "nonexistent",
            "cwd": str(tmp_path),
        })

        assert result.is_err

    @pytest.mark.asyncio
    async def test_generate_error_on_seed_generation_failure(self, tmp_path: Path) -> None:
        """Generate returns error when generate_prd_seed fails."""
        from ouroboros.core.errors import ProviderError

        state = _make_state()
        engine = MagicMock(spec=PRDInterviewEngine)
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.generate_prd_seed = AsyncMock(
            return_value=Result.err(ProviderError("LLM failed"))
        )
        engine.restore_meta = MagicMock()

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result.is_err

    @pytest.mark.asyncio
    async def test_generate_is_not_error(self, tmp_path: Path) -> None:
        """Generate result has is_error=False on success."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result.is_ok
        assert result.value.is_error is False

    @pytest.mark.asyncio
    async def test_generate_idempotent_same_session(self, tmp_path: Path) -> None:
        """Generate is idempotent — calling twice with same session_id yields same meta keys."""
        seed = _make_seed()
        state = _make_state()
        seed_path = Path.home() / ".ouroboros" / "seeds" / "prd_seed_test123.yaml"
        prd_path = tmp_path / ".ouroboros" / "prd.md"
        engine = _make_engine_for_generate(state, seed, seed_path=seed_path, prd_path=prd_path)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)

        result1 = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })
        result2 = await handler.handle({
            "action": "generate",
            "session_id": "test-session-gen",
            "cwd": str(tmp_path),
        })

        assert result1.is_ok
        assert result2.is_ok
        assert result1.value.meta == result2.value.meta

    @pytest.mark.asyncio
    async def test_generate_requires_session_id(self, tmp_path: Path) -> None:
        """Generate with action='generate' but no session_id returns error."""
        engine = MagicMock(spec=PRDInterviewEngine)

        handler = PRDInterviewHandler(prd_engine=engine, data_dir=tmp_path)
        result = await handler.handle({
            "action": "generate",
        })

        # Without session_id, falls through to error
        assert result.is_err
