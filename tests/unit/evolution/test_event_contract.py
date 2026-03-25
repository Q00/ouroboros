"""Contract test: lineage event factory types must match projector handling.

Ensures every event type defined in events/lineage.py is explicitly handled
(or explicitly excluded) by LineageProjector.project(). Prevents silent event
drops when new lineage events are added without updating the projector.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

# --- Paths ---
_SRC = Path(__file__).resolve().parents[3] / "src" / "ouroboros"
_LINEAGE_EVENTS = _SRC / "events" / "lineage.py"
_PROJECTOR = _SRC / "evolution" / "projector.py"


def _extract_event_types_from_factories(path: Path) -> set[str]:
    """Extract all event type strings from BaseEvent(type=...) calls."""
    source = path.read_text()
    tree = ast.parse(source)
    types: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "type":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                types.add(node.value.value)
    return types


def _extract_handled_types_from_projector(path: Path) -> set[str]:
    """Extract all event.type == '...' and event.type in (...) comparisons from projector."""
    source = path.read_text()
    # Match event.type == "..."
    types = set(re.findall(r'event\.type\s*==\s*"([^"]+)"', source))
    # Match event.type in ("...", "...", ...)
    in_matches = re.findall(r"event\.type\s+in\s*\(([^)]+)\)", source)
    for match in in_matches:
        types |= set(re.findall(r'"([^"]+)"', match))
    return types


# Events that are intentionally NOT handled by the projector.
# Each entry must have a justification comment.
INTENTIONALLY_UNHANDLED: dict[str, str] = {
    "lineage.ontology.evolved": (
        "Observability-only event. Ontology data is already captured "
        "in lineage.generation.completed via ontology_snapshot field."
    ),
    "lineage.wonder.degraded": (
        "Observability-only event. Degraded wonder questions are still "
        "recorded in generation.completed. No OntologyLineage field exists "
        "for degradation state."
    ),
}


class TestLineageEventContract:
    """Verify that lineage event factories and projector stay in sync."""

    def test_all_factory_events_are_handled_or_excluded(self) -> None:
        """Every event type in events/lineage.py must be either:
        - handled in projector.py (event.type == "...")
        - listed in INTENTIONALLY_UNHANDLED with justification
        """
        factory_types = _extract_event_types_from_factories(_LINEAGE_EVENTS)
        handled_types = _extract_handled_types_from_projector(_PROJECTOR)
        excluded_types = set(INTENTIONALLY_UNHANDLED.keys())

        unaccounted = factory_types - handled_types - excluded_types

        assert not unaccounted, (
            f"Lineage event types defined in events/lineage.py but neither "
            f"handled by LineageProjector nor listed in INTENTIONALLY_UNHANDLED: "
            f"{sorted(unaccounted)}. "
            f"Either add handling in projector.py or add to "
            f"INTENTIONALLY_UNHANDLED with justification."
        )

    def test_projector_handles_no_phantom_events(self) -> None:
        """Projector must not handle event types that don't exist in factories."""
        factory_types = _extract_event_types_from_factories(_LINEAGE_EVENTS)
        handled_types = _extract_handled_types_from_projector(_PROJECTOR)

        phantom = handled_types - factory_types

        assert not phantom, (
            f"LineageProjector handles event types not defined in "
            f"events/lineage.py: {sorted(phantom)}. "
            f"Remove stale handlers or add missing factory functions."
        )

    def test_exclusion_list_has_no_stale_entries(self) -> None:
        """INTENTIONALLY_UNHANDLED must not contain events that are now handled."""
        handled_types = _extract_handled_types_from_projector(_PROJECTOR)
        excluded_types = set(INTENTIONALLY_UNHANDLED.keys())

        stale = excluded_types & handled_types

        assert not stale, (
            f"Events in INTENTIONALLY_UNHANDLED are now handled by projector: "
            f"{sorted(stale)}. Remove them from the exclusion list."
        )

    def test_exclusion_entries_exist_in_factories(self) -> None:
        """INTENTIONALLY_UNHANDLED entries must reference real factory events."""
        factory_types = _extract_event_types_from_factories(_LINEAGE_EVENTS)
        excluded_types = set(INTENTIONALLY_UNHANDLED.keys())

        invalid = excluded_types - factory_types

        assert not invalid, (
            f"INTENTIONALLY_UNHANDLED references non-existent event types: "
            f"{sorted(invalid)}. Remove invalid entries."
        )
