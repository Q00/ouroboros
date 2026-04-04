"""Unit tests for PMDocumentGenerator interface via pm.renderer module.

Validates that the PMDocumentGenerator class interface is properly defined
with a generate method accepting full Q&A history and PMSeed as inputs,
returning a PM markdown string.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.pm_seed import PMSeed, UserStory
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.pm.renderer import (
    PMDocumentGenerator,
    generate_pm_markdown,
    save_pm_document,
)
from ouroboros.providers.base import CompletionResponse, UsageInfo

# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────


def _make_seed(**overrides) -> PMSeed:
    """Create a PMSeed with sensible defaults for testing."""
    defaults = {
        "pm_id": "pm_seed_test_renderer",
        "product_name": "Widget Dashboard",
        "goal": "Build a dashboard for monitoring widgets in real-time.",
        "user_stories": (
            UserStory(
                persona="operations manager",
                action="view widget status",
                benefit="I can respond to failures quickly",
            ),
        ),
        "constraints": ("Must support 1000+ concurrent users",),
        "success_criteria": ("P95 latency < 200ms",),
        "decide_later_items": ("Offline mode", "Which charting library to use?"),
        "assumptions": ("Users have modern browsers",),
        "interview_id": "int_renderer_test",
    }
    defaults.update(overrides)
    return PMSeed(**defaults)


def _make_adapter(
    content: str = "# Widget Dashboard\n\n## Goal\n\nBuild a dashboard.",
) -> MagicMock:
    """Create a mock LLM adapter returning the given content."""
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=Result.ok(
            CompletionResponse(
                content=content,
                model="claude-opus-4-6",
                usage=UsageInfo(
                    prompt_tokens=400,
                    completion_tokens=200,
                    total_tokens=600,
                ),
            )
        )
    )
    return adapter


def _make_failing_adapter() -> MagicMock:
    """Create a mock adapter that returns an error."""
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.err(ProviderError("Service unavailable")))
    return adapter


def _sample_qa_pairs() -> list[tuple[str, str]]:
    """Return sample Q&A history for testing."""
    return [
        ("What problem does this solve?", "We need real-time visibility into widget health."),
        ("Who are the primary users?", "Operations managers and SREs."),
        ("What are your constraints?", "Must handle 1000+ concurrent users."),
        ("What does success look like?", "P95 latency under 200ms for all dashboard views."),
    ]


# ──────────────────────────────────────────────────────────────────
# Interface tests — PMDocumentGenerator class structure
# ──────────────────────────────────────────────────────────────────


class TestPMDocumentGeneratorInterface:
    """Verify the PMDocumentGenerator class has the expected interface."""

    def test_class_importable_from_pm_renderer(self):
        """PMDocumentGenerator is importable from ouroboros.pm.renderer."""
        from ouroboros.pm.renderer import PMDocumentGenerator as Gen

        assert Gen is PMDocumentGenerator

    def test_class_importable_from_pm_package(self):
        """PMDocumentGenerator is importable from ouroboros.pm."""
        from ouroboros.pm import PMDocumentGenerator as Gen

        assert Gen is PMDocumentGenerator

    def test_has_generate_method(self):
        """PMDocumentGenerator has an async generate method."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)

        assert hasattr(gen, "generate")
        assert callable(gen.generate)

    def test_has_save_method(self):
        """PMDocumentGenerator has a save method."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)

        assert hasattr(gen, "save")
        assert callable(gen.save)

    def test_has_generate_and_save_method(self):
        """PMDocumentGenerator has an async generate_and_save method."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)

        assert hasattr(gen, "generate_and_save")
        assert callable(gen.generate_and_save)

    def test_constructor_accepts_llm_adapter_and_model(self):
        """Constructor accepts llm_adapter and optional model."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter, model="test-model")

        assert gen.llm_adapter is adapter
        assert gen.model == "test-model"

    def test_default_model(self):
        """Default model is set when not provided."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)

        assert gen.model  # Has a default


# ──────────────────────────────────────────────────────────────────
# generate() — accepts Q&A history + PMSeed, returns markdown
# ──────────────────────────────────────────────────────────────────


class TestPMDocumentGeneratorGenerate:
    """Tests for the generate method accepting Q&A history and PMSeed."""

    @pytest.mark.asyncio
    async def test_generate_accepts_seed_and_qa_pairs(self):
        """generate() accepts PMSeed and Q&A pairs, returns markdown."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)
        seed = _make_seed()

        result = await gen.generate(seed, qa_pairs=_sample_qa_pairs())

        assert result.is_ok
        assert isinstance(result.value, str)
        assert len(result.value) > 0
        adapter.complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_returns_result_type(self):
        """generate() returns a Result[str, ProviderError]."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)
        seed = _make_seed()

        result = await gen.generate(seed, qa_pairs=_sample_qa_pairs())

        assert hasattr(result, "is_ok")
        assert hasattr(result, "is_err")
        assert hasattr(result, "value")

    @pytest.mark.asyncio
    async def test_generate_with_seed_only(self):
        """generate() works with seed alone (no Q&A history)."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)
        seed = _make_seed()

        result = await gen.generate(seed)

        assert result.is_ok
        assert isinstance(result.value, str)

    @pytest.mark.asyncio
    async def test_generate_with_interview_state(self):
        """generate() can extract Q&A from InterviewState."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)
        seed = _make_seed()

        mock_state = MagicMock()
        mock_round = MagicMock()
        mock_round.question = "What problem?"
        mock_round.user_response = "Widget monitoring."
        mock_state.rounds = [mock_round]

        result = await gen.generate(seed, interview_state=mock_state)

        assert result.is_ok

    @pytest.mark.asyncio
    async def test_generate_falls_back_to_template_on_llm_error(self):
        """generate() falls back to template when LLM fails."""
        adapter = _make_failing_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)
        seed = _make_seed()

        result = await gen.generate(seed, qa_pairs=_sample_qa_pairs())

        assert result.is_ok
        content = result.value
        # Template fallback should produce structured markdown
        assert "# Widget Dashboard" in content
        assert "## Goal" in content

    @pytest.mark.asyncio
    async def test_generate_prompt_includes_qa_history(self):
        """The LLM prompt includes the full Q&A history."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)
        seed = _make_seed()
        qa = _sample_qa_pairs()

        await gen.generate(seed, qa_pairs=qa)

        call_args = adapter.complete.call_args
        messages = call_args[0][0]
        user_msg = messages[1].content

        # Verify all Q&A pairs are in the prompt
        for question, answer in qa:
            assert question in user_msg
            assert answer in user_msg

    @pytest.mark.asyncio
    async def test_generate_prompt_includes_seed_data(self):
        """The LLM prompt includes PMSeed fields."""
        adapter = _make_adapter()
        gen = PMDocumentGenerator(llm_adapter=adapter)
        seed = _make_seed()

        await gen.generate(seed, qa_pairs=_sample_qa_pairs())

        call_args = adapter.complete.call_args
        user_msg = call_args[0][0][1].content

        assert "Widget Dashboard" in user_msg
        assert "Build a dashboard" in user_msg
        assert "operations manager" in user_msg
        assert "Offline mode" in user_msg
        assert "Which charting library" in user_msg


# ──────────────────────────────────────────────────────────────────
# Template-based generation
# ──────────────────────────────────────────────────────────────────


class TestTemplatePMGeneration:
    """Tests for the template-based generate_pm_markdown function."""

    def test_generate_pm_markdown_importable(self):
        """generate_pm_markdown is importable from pm.renderer."""
        from ouroboros.pm.renderer import generate_pm_markdown

        assert callable(generate_pm_markdown)

    def test_generates_markdown_from_seed(self):
        """Produces valid markdown with all populated sections."""
        seed = _make_seed()
        md = generate_pm_markdown(seed)

        assert "# Widget Dashboard" in md
        assert "## Goal" in md
        assert "Build a dashboard" in md
        assert "## User Stories" in md
        assert "operations manager" in md
        assert "## Constraints" in md
        assert "1000+ concurrent users" in md
        assert "## Success Criteria" in md
        assert "P95 latency" in md
        assert "## Decide Later" in md
        assert "Offline mode" in md
        assert "Which charting library" in md
        assert "## Deferred Items" not in md

    def test_generates_markdown_for_minimal_seed(self):
        """Handles a minimal seed with only defaults."""
        seed = PMSeed()
        md = generate_pm_markdown(seed)

        assert "## Goal" in md
        assert "Product Requirements Document" in md


# ──────────────────────────────────────────────────────────────────
# save_pm_document
# ──────────────────────────────────────────────────────────────────


class TestSavePMDocument:
    """Tests for save_pm_document function."""

    def test_save_pm_document_importable(self):
        """save_pm_document is importable from pm.renderer."""
        from ouroboros.pm.renderer import save_pm_document

        assert callable(save_pm_document)

    def test_saves_to_output_dir(self, tmp_path: Path):
        """Saves pm.md to specified output directory."""
        seed = _make_seed()
        path = save_pm_document(seed, output_dir=tmp_path)

        assert path.exists()
        assert path.name == "pm.md"
        content = path.read_text()
        assert "Widget Dashboard" in content
