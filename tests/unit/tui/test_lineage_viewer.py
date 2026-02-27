"""Unit tests for TUI lineage viewer components.

Tests for:
- EventStore.get_all_lineages()
- LineageTreeWidget
- GenerationDetailPanel reactive behavior
- LineageSelectorScreen
- LineageDetailScreen
- TUI message types (LineageSelected, GenerationSelected)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.core.seed import OntologyField, OntologySchema
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.tui.events import GenerationSelected, LineageSelected
from ouroboros.tui.screens.lineage_detail import GenerationDetailPanel, LineageDetailScreen
from ouroboros.tui.screens.lineage_selector import LineageSelectorScreen
from ouroboros.tui.widgets.lineage_tree import (
    GenerationNodeSelected,
    LineageTreeWidget,
)


# =============================================================================
# Test Fixtures
# =============================================================================


def make_ontology(
    name: str = "TestOntology",
    fields: tuple[OntologyField, ...] | None = None,
) -> OntologySchema:
    """Create a test ontology schema."""
    if fields is None:
        fields = (
            OntologyField(name="id", field_type="string", description="Unique identifier"),
            OntologyField(name="name", field_type="string", description="Display name"),
        )
    return OntologySchema(name=name, description=f"Test {name}", fields=fields)


def make_eval_summary(
    approved: bool = True,
    score: float = 0.85,
    stage: int = 3,
) -> EvaluationSummary:
    """Create a test evaluation summary."""
    return EvaluationSummary(
        final_approved=approved,
        highest_stage_passed=stage,
        score=score,
    )


def make_generation(
    gen_num: int = 1,
    ontology: OntologySchema | None = None,
    phase: GenerationPhase = GenerationPhase.COMPLETED,
    eval_summary: EvaluationSummary | None = None,
    wonder_questions: tuple[str, ...] = (),
) -> GenerationRecord:
    """Create a test generation record."""
    return GenerationRecord(
        generation_number=gen_num,
        seed_id=f"seed_{gen_num}",
        ontology_snapshot=ontology or make_ontology(),
        evaluation_summary=eval_summary,
        wonder_questions=wonder_questions,
        phase=phase,
    )


def make_lineage(
    lineage_id: str = "lin_test123",
    goal: str = "Build a task manager",
    generations: tuple[GenerationRecord, ...] | None = None,
    status: LineageStatus = LineageStatus.ACTIVE,
) -> OntologyLineage:
    """Create a test lineage."""
    if generations is None:
        generations = (make_generation(1), make_generation(2))
    return OntologyLineage(
        lineage_id=lineage_id,
        goal=goal,
        generations=generations,
        status=status,
    )


# =============================================================================
# EventStore.get_all_lineages() Tests
# =============================================================================


class TestGetAllLineages:
    """Tests for EventStore.get_all_lineages()."""

    @pytest.fixture
    async def store(self) -> EventStore:
        """Create an in-memory event store."""
        s = EventStore("sqlite+aiosqlite:///:memory:")
        await s.initialize()
        return s

    @pytest.mark.asyncio
    async def test_get_all_lineages_empty(self, store: EventStore) -> None:
        """Returns empty list when no lineages exist."""
        result = await store.get_all_lineages()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_lineages_returns_creation_events(self, store: EventStore) -> None:
        """Returns lineage.created events ordered by timestamp desc."""
        # Insert two lineage creation events
        event1 = BaseEvent(
            aggregate_type="lineage",
            aggregate_id="lin_001",
            type="lineage.created",
            data={"goal": "Goal A"},
        )
        event2 = BaseEvent(
            aggregate_type="lineage",
            aggregate_id="lin_002",
            type="lineage.created",
            data={"goal": "Goal B"},
        )
        await store.append(event1)
        await store.append(event2)

        result = await store.get_all_lineages()
        assert len(result) == 2
        # Most recent first (DESC order)
        assert result[0].aggregate_id == "lin_002"
        assert result[1].aggregate_id == "lin_001"

    @pytest.mark.asyncio
    async def test_get_all_lineages_excludes_other_events(self, store: EventStore) -> None:
        """Only returns lineage.created events, not other lineage events."""
        creation = BaseEvent(
            aggregate_type="lineage",
            aggregate_id="lin_001",
            type="lineage.created",
            data={"goal": "Test"},
        )
        gen_completed = BaseEvent(
            aggregate_type="lineage",
            aggregate_id="lin_001",
            type="lineage.generation.completed",
            data={"generation_number": 1, "seed_id": "s1"},
        )
        await store.append(creation)
        await store.append(gen_completed)

        result = await store.get_all_lineages()
        assert len(result) == 1
        assert result[0].type == "lineage.created"

    @pytest.mark.asyncio
    async def test_get_all_lineages_not_initialized(self) -> None:
        """Raises PersistenceError when store is not initialized."""
        from ouroboros.core.errors import PersistenceError

        store = EventStore("sqlite+aiosqlite:///:memory:")
        with pytest.raises(PersistenceError, match="not initialized"):
            await store.get_all_lineages()


# =============================================================================
# TUI Message Tests
# =============================================================================


class TestLineageMessages:
    """Tests for LineageSelected and GenerationSelected messages."""

    def test_lineage_selected_message(self) -> None:
        msg = LineageSelected(lineage_id="lin_abc")
        assert msg.lineage_id == "lin_abc"

    def test_generation_selected_message(self) -> None:
        msg = GenerationSelected(lineage_id="lin_abc", generation_number=3)
        assert msg.lineage_id == "lin_abc"
        assert msg.generation_number == 3


# =============================================================================
# LineageTreeWidget Tests
# =============================================================================


class TestLineageTreeWidget:
    """Tests for LineageTreeWidget construction and reactives."""

    def test_create_widget_without_lineage(self) -> None:
        """Widget can be created with no lineage data."""
        widget = LineageTreeWidget()
        assert widget.lineage is None

    def test_create_widget_with_lineage(self) -> None:
        """Widget accepts lineage data at construction."""
        lineage = make_lineage()
        widget = LineageTreeWidget(lineage=lineage)
        assert widget.lineage is not None
        assert widget.lineage.goal == "Build a task manager"

    def test_gen_node_map_initialized(self) -> None:
        """Node map is initialized empty."""
        widget = LineageTreeWidget()
        assert widget._gen_node_map == {}

    def test_generation_node_selected_message(self) -> None:
        """GenerationNodeSelected message carries generation number."""
        msg = GenerationNodeSelected(generation_number=3)
        assert msg.generation_number == 3


# =============================================================================
# GenerationDetailPanel Tests
# =============================================================================


class TestGenerationDetailPanel:
    """Tests for GenerationDetailPanel reactive behavior."""

    def test_create_panel_empty(self) -> None:
        """Panel can be created with no selection."""
        panel = GenerationDetailPanel()
        assert panel.selected_generation is None
        assert panel.previous_generation is None

    def test_selected_generation_reactive(self) -> None:
        """Setting selected_generation triggers recompose."""
        panel = GenerationDetailPanel()
        gen = make_generation(1)
        panel.selected_generation = gen
        assert panel.selected_generation == gen

    def test_previous_generation_reactive(self) -> None:
        """Setting previous_generation works."""
        panel = GenerationDetailPanel()
        gen = make_generation(1)
        panel.previous_generation = gen
        assert panel.previous_generation == gen


# =============================================================================
# LineageSelectorScreen Tests
# =============================================================================


class TestLineageSelectorScreen:
    """Tests for LineageSelectorScreen message types."""

    def test_lineage_selected_message(self) -> None:
        """LineageSelected message carries lineage_id and lineage object."""
        lineage = make_lineage()
        msg = LineageSelectorScreen.LineageSelected("lin_test123", lineage)
        assert msg.lineage_id == "lin_test123"
        assert msg.lineage is lineage


# =============================================================================
# LineageDetailScreen Tests
# =============================================================================


class TestLineageDetailScreen:
    """Tests for LineageDetailScreen construction and methods."""

    def test_create_screen(self) -> None:
        """Screen can be created with a lineage."""
        lineage = make_lineage()
        screen = LineageDetailScreen(lineage)
        assert screen._lineage is lineage
        assert screen._selected_gen_num is None

    def test_find_generation(self) -> None:
        """_find_generation locates a generation by number."""
        gen1 = make_generation(1)
        gen2 = make_generation(2)
        lineage = make_lineage(generations=(gen1, gen2))
        screen = LineageDetailScreen(lineage)

        assert screen._find_generation(1) is gen1
        assert screen._find_generation(2) is gen2
        assert screen._find_generation(3) is None

    def test_find_generation_empty(self) -> None:
        """_find_generation returns None for empty lineage."""
        lineage = make_lineage(generations=())
        screen = LineageDetailScreen(lineage)
        assert screen._find_generation(1) is None


# =============================================================================
# OntologyDelta Integration Tests (used by tree/detail)
# =============================================================================


class TestOntologyDeltaInViewer:
    """Tests verifying OntologyDelta usage patterns in the viewer."""

    def test_delta_added_fields(self) -> None:
        """Detects added fields between generations."""
        old = make_ontology(fields=(
            OntologyField(name="id", field_type="string", description="ID"),
        ))
        new = make_ontology(fields=(
            OntologyField(name="id", field_type="string", description="ID"),
            OntologyField(name="title", field_type="string", description="Title"),
        ))
        delta = OntologyDelta.compute(old, new)
        assert len(delta.added_fields) == 1
        assert delta.added_fields[0].name == "title"
        assert len(delta.removed_fields) == 0

    def test_delta_removed_fields(self) -> None:
        """Detects removed fields between generations."""
        old = make_ontology(fields=(
            OntologyField(name="id", field_type="string", description="ID"),
            OntologyField(name="obsolete", field_type="string", description="Old field"),
        ))
        new = make_ontology(fields=(
            OntologyField(name="id", field_type="string", description="ID"),
        ))
        delta = OntologyDelta.compute(old, new)
        assert len(delta.removed_fields) == 1
        assert "obsolete" in delta.removed_fields

    def test_delta_similarity_identical(self) -> None:
        """Identical schemas have similarity 1.0."""
        onto = make_ontology()
        delta = OntologyDelta.compute(onto, onto)
        assert delta.similarity == 1.0
        assert len(delta.added_fields) == 0
        assert len(delta.removed_fields) == 0

    def test_delta_modified_fields(self) -> None:
        """Detects type changes in common fields."""
        old = make_ontology(fields=(
            OntologyField(name="count", field_type="string", description="Count"),
        ))
        new = make_ontology(fields=(
            OntologyField(name="count", field_type="number", description="Count"),
        ))
        delta = OntologyDelta.compute(old, new)
        assert len(delta.modified_fields) == 1
        assert delta.modified_fields[0].field_name == "count"
        assert delta.modified_fields[0].old_type == "string"
        assert delta.modified_fields[0].new_type == "number"


# =============================================================================
# ConfirmRewindScreen Tests
# =============================================================================


class TestConfirmRewindScreen:
    """Tests for ConfirmRewindScreen construction."""

    def test_create_confirm_screen(self) -> None:
        """ConfirmRewindScreen stores parameters correctly."""
        from ouroboros.tui.screens.confirm_rewind import ConfirmRewindScreen

        screen = ConfirmRewindScreen("lin_abc", from_generation=5, to_generation=3)
        assert screen._lineage_id == "lin_abc"
        assert screen._from_generation == 5
        assert screen._to_generation == 3

    def test_confirm_bindings_exist(self) -> None:
        """ConfirmRewindScreen has y, n, escape bindings."""
        from ouroboros.tui.screens.confirm_rewind import ConfirmRewindScreen

        screen = ConfirmRewindScreen("lin_abc", from_generation=5, to_generation=3)
        binding_keys = {b.key for b in screen.BINDINGS}
        assert "y" in binding_keys
        assert "n" in binding_keys
        assert "escape" in binding_keys


# =============================================================================
# LineageDetailScreen Rewind Tests
# =============================================================================


class TestLineageDetailScreenRewind:
    """Tests for rewind functionality in LineageDetailScreen."""

    def test_rewind_binding_exists(self) -> None:
        """LineageDetailScreen has 'r' binding for rewind."""
        lineage = make_lineage()
        screen = LineageDetailScreen(lineage)
        binding_keys = {b.key for b in screen.BINDINGS}
        assert "r" in binding_keys

    def test_event_store_parameter(self) -> None:
        """LineageDetailScreen accepts optional event_store."""
        lineage = make_lineage()
        screen = LineageDetailScreen(lineage, event_store=None)
        assert screen._event_store is None

    def test_event_store_stored(self) -> None:
        """LineageDetailScreen stores event_store when provided."""
        lineage = make_lineage()
        # Use a mock-like object -- just needs to not be None
        mock_store = object()
        screen = LineageDetailScreen(lineage, event_store=mock_store)
        assert screen._event_store is mock_store
