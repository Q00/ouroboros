"""Brownfield-priming tests for SeedGenerator (PR-C)."""

from __future__ import annotations

from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.bigbang.seed_generator import SeedGenerator


def _generator() -> SeedGenerator:
    with tempfile.TemporaryDirectory() as tmp:
        return SeedGenerator(llm_adapter=AsyncMock(), output_dir=Path(tmp) / "seeds")


def _state_with_codebase() -> InterviewState:
    state = InterviewState(
        interview_id="bf_001",
        initial_context="Extend the existing service",
        is_brownfield=True,
        codebase_context="Python FastAPI service with SQLAlchemy models.",
        codebase_paths=[{"path": "/repo/api", "role": "primary"}],
    )
    state.rounds.append(InterviewRound(round_number=1, question="Q?", user_response="A"))
    return state


class TestInterviewContext:
    def test_context_includes_codebase_context_and_paths(self) -> None:
        gen = _generator()
        context = gen._build_interview_context(_state_with_codebase())
        assert "Codebase Context:" in context
        assert "FastAPI service" in context
        assert "Codebase Paths:" in context
        assert "/repo/api (primary)" in context

    def test_greenfield_context_omits_codebase_sections(self) -> None:
        gen = _generator()
        state = InterviewState(interview_id="gf_001", initial_context="Build fresh CLI")
        context = gen._build_interview_context(state)
        assert "Codebase Context:" not in context
        assert "Codebase Paths:" not in context


class TestExtractionTemplate:
    def test_brownfield_prompt_requests_context_keys(self) -> None:
        gen = _generator()
        prompt = gen._build_extraction_user_prompt("ctx", is_brownfield=True)
        assert "PROJECT_TYPE: brownfield" in prompt
        assert "CONTEXT_REFERENCES:" in prompt
        assert "EXISTING_PATTERNS:" in prompt
        assert "EXISTING_DEPENDENCIES:" in prompt
        assert "PROJECT_TYPE: greenfield" not in prompt

    def test_greenfield_prompt_unchanged(self) -> None:
        gen = _generator()
        prompt = gen._build_extraction_user_prompt("ctx", is_brownfield=False)
        assert prompt.rstrip().endswith("PROJECT_TYPE: greenfield")
        assert "CONTEXT_REFERENCES:" not in prompt

    def test_retry_prompt_brownfield_requests_context_keys(self) -> None:
        gen = _generator()
        prompt = gen._build_retry_prompt("ctx", "bad", "err", is_brownfield=True)
        assert "PROJECT_TYPE: brownfield" in prompt
        assert "EXISTING_DEPENDENCIES:" in prompt


class TestBrownfieldParsingRoundTrip:
    def test_brownfield_requirements_populate_context(self) -> None:
        gen = _generator()
        requirements = gen._parse_extraction_response(
            "GOAL: Extend service\n"
            "ONTOLOGY_NAME: Svc\n"
            "ONTOLOGY_DESCRIPTION: A service\n"
            "PROJECT_TYPE: brownfield\n"
            "CONTEXT_REFERENCES: /repo/api:primary:API layer\n"
            'EXISTING_PATTERNS: ["repository pattern", "dependency injection"]\n'
            'EXISTING_DEPENDENCIES: ["fastapi", "sqlalchemy"]'
        )
        seed = gen._build_seed(requirements, metadata=_metadata())
        bf = seed.brownfield_context
        assert bf.project_type == "brownfield"
        assert bf.context_references[0].path == "/repo/api"
        assert bf.context_references[0].role == "primary"
        assert "repository pattern" in bf.existing_patterns
        assert "fastapi" in bf.existing_dependencies


class TestBrownfieldListExtractionContract:
    """EXISTING_PATTERNS/EXISTING_DEPENDENCIES follow the #1714 JSON-array contract (#1729)."""

    _BASE = (
        "GOAL: Extend service\n"
        "ONTOLOGY_NAME: Svc\n"
        "ONTOLOGY_DESCRIPTION: A service\n"
        "PROJECT_TYPE: brownfield\n"
        "CONTEXT_REFERENCES: /repo/api:primary:API layer\n"
    )

    def test_strict_rejects_pipe_list_existing_patterns(self) -> None:
        gen = _generator()
        with pytest.raises(ValueError, match="EXISTING_PATTERNS"):
            gen._parse_extraction_response(
                self._BASE
                + "EXISTING_PATTERNS: repository pattern | dependency injection\n"
                + 'EXISTING_DEPENDENCIES: ["fastapi"]'
            )

    def test_strict_rejects_pipe_list_existing_dependencies(self) -> None:
        gen = _generator()
        with pytest.raises(ValueError, match="EXISTING_DEPENDENCIES"):
            gen._parse_extraction_response(
                self._BASE
                + 'EXISTING_PATTERNS: ["repository pattern"]\n'
                + "EXISTING_DEPENDENCIES: fastapi | sqlalchemy"
            )

    def test_json_arrays_preserve_literal_pipes_end_to_end(self) -> None:
        gen = _generator()
        requirements = gen._parse_extraction_response(
            self._BASE
            + 'EXISTING_PATTERNS: ["Use Result.ok() | Result.err() unions", "Repository pattern"]\n'
            + 'EXISTING_DEPENDENCIES: ["typer (CLI | completion extras)", "structlog"]'
        )
        seed = gen._build_seed(requirements, metadata=_metadata())
        bf = seed.brownfield_context
        assert "Use Result.ok() | Result.err() unions" in bf.existing_patterns
        assert "Repository pattern" in bf.existing_patterns
        assert "typer (CLI | completion extras)" in bf.existing_dependencies
        assert "structlog" in bf.existing_dependencies

    def test_build_seed_keeps_legacy_pipe_lists_for_stored_data(self) -> None:
        gen = _generator()
        requirements = {
            "goal": "Extend service",
            "ontology_name": "Svc",
            "ontology_description": "A service",
            "project_type": "brownfield",
            "context_references": "/repo/api:primary:API layer",
            "existing_patterns": "repository pattern | dependency injection",
            "existing_dependencies": "fastapi | sqlalchemy",
        }
        seed = gen._build_seed(requirements, metadata=_metadata())
        bf = seed.brownfield_context
        assert bf.existing_patterns == ("repository pattern", "dependency injection")
        assert bf.existing_dependencies == ("fastapi", "sqlalchemy")


def _metadata():
    from ouroboros.core.seed import SeedMetadata

    return SeedMetadata(ambiguity_score=0.1)
