"""Unit tests for ReflectEngine._parse_response.

Tests JSON parsing, mutation extraction, and fallback behavior without LLM calls.
"""

from __future__ import annotations

import pytest

from ouroboros.core.lineage import MutationAction
from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.evolution.reflect import OntologyMutation, ReflectEngine, ReflectOutput


def _make_engine() -> ReflectEngine:
    """Create ReflectEngine with a dummy adapter (not used in these tests)."""

    class DummyAdapter:
        async def complete(self, messages, config):
            raise NotImplementedError

    return ReflectEngine(llm_adapter=DummyAdapter())  # type: ignore[arg-type]


def _make_seed(**overrides) -> Seed:
    defaults = {
        "goal": "Build a task manager",
        "constraints": ("Must use Python",),
        "acceptance_criteria": ("Tasks can be created",),
        "ontology_schema": OntologySchema(
            name="TaskManager",
            description="A task management system",
            fields=(
                OntologyField(name="task", field_type="entity", description="A work item"),
            ),
        ),
        "metadata": SeedMetadata(),
    }
    defaults.update(overrides)
    return Seed(**defaults)


class TestParseResponse:
    """Tests for ReflectEngine._parse_response."""

    def test_valid_full_response(self) -> None:
        engine = _make_engine()
        seed = _make_seed()
        result = engine._parse_response(
            '{"refined_goal": "Build a prioritized task manager", '
            '"refined_constraints": ["Must use Python", "Must support priorities"], '
            '"refined_acs": ["Tasks can be created", "Tasks can be prioritized"], '
            '"ontology_mutations": ['
            '  {"action": "add", "field_name": "priority", "field_type": "enum", '
            '   "description": "Task priority level", "reason": "Missing from ontology"}'
            '], '
            '"reasoning": "Priority was identified as a gap"}',
            seed,
        )
        assert result is not None
        assert result.refined_goal == "Build a prioritized task manager"
        assert len(result.refined_constraints) == 2
        assert len(result.refined_acs) == 2
        assert len(result.ontology_mutations) == 1
        assert result.ontology_mutations[0].action == MutationAction.ADD
        assert result.ontology_mutations[0].field_name == "priority"

    def test_json_with_markdown_fences(self) -> None:
        engine = _make_engine()
        seed = _make_seed()
        result = engine._parse_response(
            '```json\n{"refined_goal": "g", "refined_constraints": [], '
            '"refined_acs": [], "ontology_mutations": [], "reasoning": "r"}\n```',
            seed,
        )
        assert result is not None
        assert result.refined_goal == "g"

    def test_invalid_json_returns_none(self) -> None:
        engine = _make_engine()
        seed = _make_seed()
        result = engine._parse_response("not json", seed)
        assert result is None

    def test_missing_fields_use_seed_defaults(self) -> None:
        engine = _make_engine()
        seed = _make_seed()
        result = engine._parse_response("{}", seed)
        assert result is not None
        assert result.refined_goal == seed.goal
        assert result.refined_constraints == seed.constraints
        assert result.refined_acs == seed.acceptance_criteria

    def test_invalid_mutation_action_defaults_to_modify(self) -> None:
        engine = _make_engine()
        seed = _make_seed()
        result = engine._parse_response(
            '{"ontology_mutations": [{"action": "invalid_action", "field_name": "x"}]}',
            seed,
        )
        assert result is not None
        assert result.ontology_mutations[0].action == MutationAction.MODIFY

    def test_mutation_missing_field_name_defaults(self) -> None:
        engine = _make_engine()
        seed = _make_seed()
        result = engine._parse_response(
            '{"ontology_mutations": [{"action": "add"}]}',
            seed,
        )
        assert result is not None
        assert result.ontology_mutations[0].field_name == "unknown"

    def test_multiple_mutations(self) -> None:
        engine = _make_engine()
        seed = _make_seed()
        result = engine._parse_response(
            '{"ontology_mutations": ['
            '  {"action": "add", "field_name": "a", "reason": "r1"},'
            '  {"action": "modify", "field_name": "task", "reason": "r2"},'
            '  {"action": "remove", "field_name": "old", "reason": "r3"}'
            "]}",
            seed,
        )
        assert result is not None
        assert len(result.ontology_mutations) == 3
        actions = [m.action for m in result.ontology_mutations]
        assert actions == [MutationAction.ADD, MutationAction.MODIFY, MutationAction.REMOVE]

    def test_empty_mutations_list(self) -> None:
        engine = _make_engine()
        seed = _make_seed()
        result = engine._parse_response(
            '{"refined_goal": "g", "ontology_mutations": []}',
            seed,
        )
        assert result is not None
        assert result.ontology_mutations == ()
