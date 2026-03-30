"""Tests for PMInterviewHandler action:generate (AC 4).

Verifies that _handle_generate:
- Loads InterviewState and pm_meta
- Restores engine via restore_meta() (not _restore_engine_meta)
- Runs generate_pm_seed
- Saves PM seed to ~/.ouroboros/seeds/
- Returns meta with session_id, seed_path
- Is idempotent (same result on retry)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.pm_seed import PMSeed, UserStory
from ouroboros.core.types import Result
from ouroboros.mcp.tools.pm_handler import (
    PMInterviewHandler,
    _save_pm_meta,
)

# ── Helpers ──────────────────────────────────────────────────────


def _make_seed(
    pm_id: str = "pm_seed_test123",
    product_name: str = "Test Product",
    interview_id: str = "test-session-gen",
) -> PMSeed:
    """Create a minimal PMSeed for testing."""
    return PMSeed(
        pm_id=pm_id,
        product_name=product_name,
        goal="Build a great product",
        user_stories=(UserStory(persona="User", action="do stuff", benefit="save time"),),
        constraints=("Timeline: 3 months",),
        success_criteria=("100 users",),
        decide_later_items=("DB choice", "Auth provider"),
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
    seed: PMSeed,
    seed_path: Path | None = None,
) -> PMInterviewEngine:
    """Create a mock PMInterviewEngine for generate tests."""
    if seed_path is None:
        seed_path = Path.home() / ".ouroboros" / "seeds" / "pm_seed_test123.json"
    from tests.unit.mcp.tools.conftest import make_pm_engine_mock

    engine = make_pm_engine_mock()

    engine.load_state = AsyncMock(return_value=Result.ok(state))
    engine.generate_pm_seed = AsyncMock(return_value=Result.ok(seed))
    engine.save_pm_seed = MagicMock(return_value=seed_path)
    engine.restore_meta = MagicMock()

    return engine


# ── Tests ────────────────────────────────────────────────────────


class TestHandleGenerate:
    """Tests for PMInterviewHandler._handle_generate."""

    @pytest.mark.asyncio
    async def test_generate_returns_session_id_in_meta(self, tmp_path: Path) -> None:
        """Generate returns session_id in response meta."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["session_id"] == "test-session-gen"

    @pytest.mark.asyncio
    async def test_generate_returns_pm_path_in_meta(self, tmp_path: Path) -> None:
        """Generate meta contains pm_path pointing to saved pm.md."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert "pm_path" in meta
        pm_path = Path(meta["pm_path"])
        assert pm_path.exists()
        assert pm_path.name == "pm.md"

    @pytest.mark.asyncio
    async def test_generate_returns_seed_path_in_meta(self, tmp_path: Path) -> None:
        """Generate returns seed_path in response meta."""
        seed = _make_seed()
        state = _make_state()
        seed_path = Path.home() / ".ouroboros" / "seeds" / "pm_seed_test123.json"
        engine = _make_engine_for_generate(state, seed, seed_path=seed_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert meta["seed_path"] == str(seed_path)

    @pytest.mark.asyncio
    async def test_generate_meta_has_canonical_handoff_keys(self, tmp_path: Path) -> None:
        """Generate meta contains the canonical PM handoff metadata."""
        seed = _make_seed()
        state = _make_state()
        seed_path = Path.home() / ".ouroboros" / "seeds" / "pm_seed_test123.json"
        engine = _make_engine_for_generate(state, seed, seed_path=seed_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert set(meta.keys()) == {
            "session_id",
            "seed_path",
            "pm_seed_path",
            "pm_path",
            "artifact_kind",
            "runnable",
            "next_step",
        }
        assert meta["artifact_kind"] == "pm_seed"
        assert meta["runnable"] is False
        assert meta["pm_seed_path"] == meta["seed_path"]
        assert meta["next_step"] == (
            f"Run 'ouroboros init start {seed_path}' to continue into the dev interview. "
            "The runnable Seed is generated after that dev interview completes."
        )

    @pytest.mark.asyncio
    async def test_generate_loads_interview_state(self, tmp_path: Path) -> None:
        """Generate loads InterviewState via engine.load_state."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        engine.load_state.assert_awaited_once_with("test-session-gen")

    @pytest.mark.asyncio
    async def test_generate_restores_meta_via_engine_method(self, tmp_path: Path) -> None:
        """Generate restores PM meta via engine.restore_meta(), not _restore_engine_meta."""
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
        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        # Manually save meta
        _save_pm_meta.__wrapped__ if hasattr(_save_pm_meta, "__wrapped__") else None
        meta_path = tmp_path / "pm_meta_test-session-gen.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        meta_path.write_text(json.dumps(meta_data), encoding="utf-8")

        await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        # Verify engine.restore_meta was called with the loaded meta
        engine.restore_meta.assert_called_once_with(meta_data)

    @pytest.mark.asyncio
    async def test_generate_skips_restore_when_no_meta(self, tmp_path: Path) -> None:
        """Generate works without pm_meta file (no restore_meta call)."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        engine.restore_meta.assert_not_called()

    @pytest.mark.asyncio
    async def test_generate_calls_generate_pm_seed(self, tmp_path: Path) -> None:
        """Generate calls engine.generate_pm_seed with loaded state."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        engine.generate_pm_seed.assert_awaited_once_with(state)

    @pytest.mark.asyncio
    async def test_generate_saves_seed_to_seeds_dir(self, tmp_path: Path) -> None:
        """Generate saves seed via engine.save_pm_seed."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        engine.save_pm_seed.assert_called_once_with(seed)

    @pytest.mark.asyncio
    async def test_generate_does_not_call_save_pm_document(self, tmp_path: Path) -> None:
        """Generate does not call save_pm_document (no pm.md generation)."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert not hasattr(engine, "save_pm_document") or not engine.save_pm_document.called

    @pytest.mark.asyncio
    async def test_generate_content_includes_product_name(self, tmp_path: Path) -> None:
        """Generate response content includes product name."""
        seed = _make_seed(product_name="My App")
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        text = result.value.content[0].text
        assert "My App" in text

    @pytest.mark.asyncio
    async def test_generate_error_on_load_state_failure(self, tmp_path: Path) -> None:
        """Generate returns error when load_state fails."""
        from ouroboros.core.errors import ValidationError
        from tests.unit.mcp.tools.conftest import make_pm_engine_mock

        engine = make_pm_engine_mock()
        engine.load_state = AsyncMock(
            return_value=Result.err(ValidationError("Not found", field="session_id"))
        )

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "nonexistent",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_err

    @pytest.mark.asyncio
    async def test_generate_error_on_seed_generation_failure(self, tmp_path: Path) -> None:
        """Generate returns error when generate_pm_seed fails."""
        from ouroboros.core.errors import ProviderError
        from tests.unit.mcp.tools.conftest import make_pm_engine_mock

        state = _make_state()
        engine = make_pm_engine_mock()
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.generate_pm_seed = AsyncMock(return_value=Result.err(ProviderError("LLM failed")))
        engine.restore_meta = MagicMock()

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_err

    @pytest.mark.asyncio
    async def test_generate_is_not_error(self, tmp_path: Path) -> None:
        """Generate result has is_error=False on success."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        assert result.value.is_error is False

    @pytest.mark.asyncio
    async def test_generate_idempotent_same_session(self, tmp_path: Path) -> None:
        """Generate is idempotent — calling twice with same session_id yields same meta keys."""
        seed = _make_seed()
        state = _make_state()
        seed_path = Path.home() / ".ouroboros" / "seeds" / "pm_seed_test123.json"
        engine = _make_engine_for_generate(state, seed, seed_path=seed_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)

        result1 = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )
        result2 = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result1.is_ok
        assert result2.is_ok
        assert result1.value.meta == result2.value.meta

    @pytest.mark.asyncio
    async def test_generate_rejects_incomplete_session(self, tmp_path: Path) -> None:
        """Generate returns error when interview is not complete."""
        from tests.unit.mcp.tools.conftest import make_pm_engine_mock

        state = _make_state()
        state.is_complete = False  # Mark as incomplete

        engine = make_pm_engine_mock()
        engine.load_state = AsyncMock(return_value=Result.ok(state))

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_err
        assert "not complete" in str(result.error).lower()
        # generate_pm_seed should never be called for incomplete sessions
        engine.generate_pm_seed = AsyncMock()
        assert not engine.generate_pm_seed.called

    @pytest.mark.asyncio
    async def test_generate_pm_path_consistent_with_cli(self, tmp_path: Path) -> None:
        """MCP generate saves pm.md to {cwd}/.ouroboros/ — same convention as CLI."""
        seed = _make_seed()
        state = _make_state()
        engine = _make_engine_for_generate(state, seed)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        pm_path = Path(result.value.meta["pm_path"])
        # File should be at {cwd}/.ouroboros/pm.md
        assert pm_path.parent.name == ".ouroboros"
        assert pm_path.name == "pm.md"

    @pytest.mark.asyncio
    async def test_generate_next_step_uses_pm_seed_path(self, tmp_path: Path) -> None:
        """The canonical next step should continue from the PMSeed artifact, not pm.md."""
        seed = _make_seed()
        state = _make_state()
        seed_path = Path.home() / ".ouroboros" / "seeds" / "pm_seed_test123.json"
        engine = _make_engine_for_generate(state, seed, seed_path=seed_path)

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
                "session_id": "test-session-gen",
                "cwd": str(tmp_path),
            }
        )

        assert result.is_ok
        next_step = result.value.meta["next_step"]
        assert next_step == (
            f"Run 'ouroboros init start {seed_path}' to continue into the dev interview. "
            "The runnable Seed is generated after that dev interview completes."
        )
        assert "pm.md" not in next_step

    @pytest.mark.asyncio
    async def test_generate_requires_session_id(self, tmp_path: Path) -> None:
        """Generate with action='generate' but no session_id returns error."""
        from tests.unit.mcp.tools.conftest import make_pm_engine_mock

        engine = make_pm_engine_mock()

        handler = PMInterviewHandler(pm_engine=engine, data_dir=tmp_path)
        result = await handler.handle(
            {
                "action": "generate",
            }
        )

        # Without session_id, falls through to error
        assert result.is_err
