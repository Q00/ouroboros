"""Lineage generation tree widget.

Displays the evolutionary lineage as a tree with generation nodes,
showing ontology deltas, wonder questions, and evaluation scores.
"""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, Static, Tree
from textual.widgets.tree import TreeNode

from ouroboros.core.lineage import (
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.core.seed import OntologySchema

# Status icons for generation phases
PHASE_ICONS = {
    GenerationPhase.COMPLETED: "[bold green]\u25cf[/]",
    GenerationPhase.FAILED: "[bold red]\u2716[/]",
    GenerationPhase.EXECUTING: "[bold yellow]\u25d0[/]",
    GenerationPhase.WONDERING: "[cyan]\u25cb[/]",
    GenerationPhase.REFLECTING: "[cyan]\u25d4[/]",
    GenerationPhase.SEEDING: "[blue]\u25c6[/]",
    GenerationPhase.EVALUATING: "[magenta]\u25d0[/]",
}

LINEAGE_STATUS_ICONS = {
    LineageStatus.ACTIVE: "[yellow]\u25b6[/]",
    LineageStatus.CONVERGED: "[bold green]\u2714[/]",
    LineageStatus.EXHAUSTED: "[red]\u25a0[/]",
    LineageStatus.ABORTED: "[dim]\u2718[/]",
}


class GenerationNodeSelected(Message):
    """Message emitted when a generation node is selected in the tree."""

    def __init__(self, generation_number: int) -> None:
        super().__init__()
        self.generation_number = generation_number


class LineageTreeWidget(Widget):
    """Widget displaying the evolutionary lineage as a generation tree.

    Shows each generation with its ontology delta, evaluation score,
    wonder questions, and convergence status.
    """

    DEFAULT_CSS = """
    LineageTreeWidget {
        height: 100%;
        width: 100%;
        padding: 0;
    }

    LineageTreeWidget > .tree-title {
        text-align: center;
        text-style: bold;
        color: $secondary;
        width: 100%;
        padding: 1;
        border-bottom: solid $secondary;
    }

    LineageTreeWidget > #lineage-tree {
        width: 100%;
        height: 1fr;
        padding: 1;
    }

    LineageTreeWidget > .empty-message {
        text-align: center;
        color: $text-muted;
        padding: 4;
    }
    """

    lineage: reactive[OntologyLineage | None] = reactive(None)

    def __init__(
        self,
        lineage: OntologyLineage | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        self._gen_node_map: dict[int, TreeNode[dict[str, Any]]] = {}
        super().__init__(name=name, id=id, classes=classes)
        if lineage:
            self.lineage = lineage

    def compose(self) -> ComposeResult:
        yield Label("\u2554\u2550\u2550 EVOLUTION TREE \u2550\u2550\u2557", classes="tree-title")

        if self.lineage is None or not self.lineage.generations:
            yield Static("[dim]No lineage data available[/]", classes="empty-message")
        else:
            tree: Tree[dict[str, Any]] = Tree(
                self._format_root_label(),
                id="lineage-tree",
            )
            tree.root.expand()
            yield tree

    def on_mount(self) -> None:
        self._rebuild_tree()

    def _format_root_label(self) -> str:
        if self.lineage is None:
            return "Lineage"
        status_icon = LINEAGE_STATUS_ICONS.get(self.lineage.status, "")
        goal = self.lineage.goal[:40]
        if len(self.lineage.goal) > 40:
            goal += "..."
        return f"{status_icon} [bold]{goal}[/]"

    def _rebuild_tree(self) -> None:
        if self.lineage is None or not self.lineage.generations:
            return

        try:
            tree = self.query_one("#lineage-tree", Tree)
        except Exception:
            return

        tree.clear()
        tree.root.label = self._format_root_label()
        self._gen_node_map.clear()

        prev_ontology: OntologySchema | None = None

        for gen in self.lineage.generations:
            gen_label = self._format_gen_label(gen, prev_ontology)
            gen_data = {"generation_number": gen.generation_number}
            gen_node = tree.root.add(gen_label, data=gen_data)
            self._gen_node_map[gen.generation_number] = gen_node

            # Add delta details as children
            if prev_ontology is not None:
                delta = OntologyDelta.compute(prev_ontology, gen.ontology_snapshot)
                for field in delta.added_fields:
                    gen_node.add_leaf(
                        f"  [green]+Added:[/] {field.name} ({field.field_type})"
                    )
                for field_name in delta.removed_fields:
                    gen_node.add_leaf(
                        f"  [red]-Removed:[/] {field_name}"
                    )
                for mod in delta.modified_fields:
                    gen_node.add_leaf(
                        f"  [yellow]~Modified:[/] {mod.field_name} "
                        f"({mod.old_type}\u2192{mod.new_type})"
                    )

            # Add wonder questions
            for q in gen.wonder_questions[:3]:
                display_q = q[:60] + "..." if len(q) > 60 else q
                gen_node.add_leaf(f"  [cyan]Wonder:[/] \"{display_q}\"")

            # Show convergence marker on last generation
            if (
                gen == self.lineage.generations[-1]
                and self.lineage.status == LineageStatus.CONVERGED
            ):
                gen_node.add_leaf("  [bold green][CONVERGED][/]")

            gen_node.expand()
            prev_ontology = gen.ontology_snapshot

        tree.root.expand()

    def _format_gen_label(
        self,
        gen: GenerationRecord,
        prev_ontology: OntologySchema | None,
    ) -> str:
        phase_icon = PHASE_ICONS.get(gen.phase, "\u25cb")
        field_count = len(gen.ontology_snapshot.fields)
        onto_name = gen.ontology_snapshot.name

        label_parts = [
            f"{phase_icon} Gen {gen.generation_number}: {onto_name} "
            f"({field_count} fields)"
        ]

        # Add evaluation score if available
        if gen.evaluation_summary and gen.evaluation_summary.score is not None:
            score = gen.evaluation_summary.score
            score_color = "green" if score >= 0.8 else "yellow" if score >= 0.5 else "red"
            label_parts.append(f" [{score_color}]score={score:.2f}[/]")

        # Add similarity vs previous
        if prev_ontology is not None:
            delta = OntologyDelta.compute(prev_ontology, gen.ontology_snapshot)
            label_parts.append(f" [dim]sim={delta.similarity:.2f}[/]")

        return "".join(label_parts)

    def watch_lineage(self, new_lineage: OntologyLineage | None) -> None:
        self.refresh(recompose=True)

    def on_tree_node_selected(
        self, event: Tree.NodeSelected[dict[str, Any]]
    ) -> None:
        if event.node.data and "generation_number" in event.node.data:
            self.post_message(
                GenerationNodeSelected(event.node.data["generation_number"])
            )


__all__ = ["GenerationNodeSelected", "LineageTreeWidget"]
