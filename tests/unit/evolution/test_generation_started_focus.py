"""lineage.generation.started must tell observers WHAT the generation redoes.

The ``ac_focus`` block (active/frozen AC indices + active descriptions +
selection reason) lets hosts report "Gen N reworks AC 2, 5; 3 frozen" instead
of a bare generation number. Descriptions only — verify_command and
output_assertion never appear (answer-sheet hiding).
"""

import pytest

from ouroboros.core.seed import AcceptanceCriterionSpec, OntologySchema, Seed, SeedMetadata
from ouroboros.events.lineage import lineage_generation_started
from ouroboros.evolution import loop_support
from ouroboros.evolution.focus import EvolutionFocus
from ouroboros.persistence.event_store import EventStore


def _make_seed() -> Seed:
    ontology = OntologySchema(name="test", description="test ontology", fields=[])
    return Seed(
        goal="test goal",
        ontology_schema=ontology,
        metadata=SeedMetadata(seed_id="seed-focus-1"),
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="CSV export writes summary.csv",
                verify_command="test -f summary.csv",
                output_assertion="SECRET-TOKEN",
            ),
            "CLI exits 0 on --help",
            AcceptanceCriterionSpec(description="README documents install"),
        ),
    )


def test_event_builder_includes_ac_focus_block() -> None:
    event = lineage_generation_started(
        "lin-1",
        7,
        "wondering",
        seed_id="seed-focus-1",
        active_ac_indices=[0, 1],
        frozen_ac_indices=[2],
        active_ac_descriptions=["CSV export writes summary.csv", "CLI exits 0 on --help"],
        focus_reason="prior evaluation failed 2 of 3 ACs",
    )
    focus_block = event.data["ac_focus"]
    assert focus_block["active_ac_indices"] == [0, 1]
    assert focus_block["frozen_ac_indices"] == [2]
    assert focus_block["active_ac_descriptions"] == [
        "CSV export writes summary.csv",
        "CLI exits 0 on --help",
    ]
    assert focus_block["reason"] == "prior evaluation failed 2 of 3 ACs"


def test_event_builder_omits_ac_focus_when_absent() -> None:
    event = lineage_generation_started("lin-1", 1, "executing", seed_id="seed-focus-1")
    assert "ac_focus" not in event.data


@pytest.mark.asyncio
async def test_emit_generation_started_once_projects_focus(tmp_path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'focus.db'}")
    await store.initialize()
    try:
        seed = _make_seed()
        focus = EvolutionFocus(
            active_ac_indices=(0, 1),
            frozen_ac_indices=(2,),
            reason="prior evaluation failed 2 of 3 ACs",
        )
        await loop_support.emit_generation_started_once(
            store,
            lineage_id="lin-focus",
            generation_number=7,
            phase="wondering",
            seed=seed,
            focus=focus,
        )
        events = await store.replay_lineage("lin-focus")
        started = [e for e in events if e.type == "lineage.generation.started"]
        assert len(started) == 1
        focus_block = started[0].data["ac_focus"]
        assert focus_block["active_ac_indices"] == [0, 1]
        assert focus_block["frozen_ac_indices"] == [2]
        # Spec ACs project their description; bare-string ACs project as-is.
        assert focus_block["active_ac_descriptions"] == [
            "CSV export writes summary.csv",
            "CLI exits 0 on --help",
        ]
        # Answer-sheet hiding: the focus block never leaks the checklist.
        assert "SECRET-TOKEN" not in str(focus_block)
        assert "test -f summary.csv" not in str(focus_block)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_emit_generation_started_once_without_focus_keeps_legacy_shape(tmp_path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'nofocus.db'}")
    await store.initialize()
    try:
        await loop_support.emit_generation_started_once(
            store,
            lineage_id="lin-legacy",
            generation_number=1,
            phase="executing",
            seed=_make_seed(),
        )
        events = await store.replay_lineage("lin-legacy")
        started = [e for e in events if e.type == "lineage.generation.started"]
        assert len(started) == 1
        assert "ac_focus" not in started[0].data
    finally:
        await store.close()
