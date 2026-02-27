"""Lineage detail screen with split view: tree + generation detail panel."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.persistence.event_store import EventStore

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import Footer, Header, Label, Static

from ouroboros.events.lineage import lineage_rewound
from ouroboros.tui.screens.confirm_rewind import ConfirmRewindScreen

from ouroboros.core.lineage import (
    GenerationRecord,
    LineageStatus,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.tui.widgets.lineage_tree import GenerationNodeSelected, LineageTreeWidget


# =============================================================================
# GENERATION DETAIL PANEL
# =============================================================================


class GenerationDetailPanel(Static):
    """Panel showing detailed information about a selected generation."""

    DEFAULT_CSS = """
    GenerationDetailPanel {
        width: 100%;
        height: 100%;
        border: heavy $primary;
        background: $surface;
        padding: 1;
    }

    GenerationDetailPanel > .panel-title {
        text-align: center;
        text-style: bold;
        color: $primary;
        width: 100%;
        margin-bottom: 1;
    }

    GenerationDetailPanel > .detail-row {
        width: 100%;
        height: 1;
    }

    GenerationDetailPanel > .detail-row > .label {
        width: 14;
        color: $text-muted;
    }

    GenerationDetailPanel > .detail-row > .value {
        width: 1fr;
        color: $text;
    }

    GenerationDetailPanel > .section-header {
        width: 100%;
        margin-top: 1;
        padding-top: 1;
        border-top: dashed $primary-darken-2;
        color: $text-muted;
        text-style: bold;
    }

    GenerationDetailPanel > .field-item {
        width: 100%;
        height: 1;
        padding-left: 2;
    }

    GenerationDetailPanel > .empty-state {
        width: 100%;
        height: 100%;
        text-align: center;
        color: $text-muted;
        padding: 4;
    }

    GenerationDetailPanel > .output-preview {
        width: 100%;
        max-height: 8;
        padding-left: 2;
        color: $text;
    }

    GenerationDetailPanel > .wonder-item {
        width: 100%;
        height: auto;
        padding-left: 2;
        color: $accent;
    }

    GenerationDetailPanel > .delta-item {
        width: 100%;
        height: 1;
        padding-left: 2;
    }
    """

    selected_generation: reactive[GenerationRecord | None] = reactive(None)
    previous_generation: reactive[GenerationRecord | None] = reactive(None)

    def compose(self) -> ComposeResult:
        yield Label(
            "\u2554\u2550\u2550 GENERATION DETAIL \u2550\u2550\u2557",
            classes="panel-title",
        )

        if self.selected_generation is None:
            yield Static(
                "[dim]Select a generation from the tree[/]\n"
                "[dim]to view details[/]",
                classes="empty-state",
            )
            return

        gen = self.selected_generation

        # Basic info
        with Horizontal(classes="detail-row"):
            yield Label("Generation:", classes="label")
            yield Static(f"[cyan]{gen.generation_number}[/]", classes="value")

        with Horizontal(classes="detail-row"):
            yield Label("Phase:", classes="label")
            phase_color = "green" if gen.phase == "completed" else "yellow" if gen.phase == "executing" else "red" if gen.phase == "failed" else "dim"
            yield Static(f"[{phase_color}]{gen.phase.value}[/]", classes="value")

        with Horizontal(classes="detail-row"):
            yield Label("Seed ID:", classes="label")
            sid = gen.seed_id[:20] + "..." if len(gen.seed_id) > 20 else gen.seed_id
            yield Static(f"[dim]{sid}[/]", classes="value")

        with Horizontal(classes="detail-row"):
            yield Label("Created:", classes="label")
            ts = gen.created_at
            time_str = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
            yield Static(f"[dim]{time_str}[/]", classes="value")

        # Ontology fields
        yield Label("Ontology Fields:", classes="section-header")
        onto = gen.ontology_snapshot
        yield Static(f"  [bold]{onto.name}[/]: {onto.description[:60]}", classes="field-item")

        for field in onto.fields:
            req = "[green]*[/]" if field.required else "[dim]o[/]"
            yield Static(
                f"  {req} {field.name}: [cyan]{field.field_type}[/] "
                f"[dim]- {field.description[:40]}[/]",
                classes="field-item",
            )

        # Evaluation
        if gen.evaluation_summary:
            ev = gen.evaluation_summary
            yield Label("Evaluation:", classes="section-header")

            approved_str = "[bold green]APPROVED[/]" if ev.final_approved else "[bold red]REJECTED[/]"
            with Horizontal(classes="detail-row"):
                yield Label("Result:", classes="label")
                yield Static(approved_str, classes="value")

            if ev.score is not None:
                score_color = "green" if ev.score >= 0.8 else "yellow" if ev.score >= 0.5 else "red"
                with Horizontal(classes="detail-row"):
                    yield Label("Score:", classes="label")
                    yield Static(f"[{score_color}]{ev.score:.2f}[/]", classes="value")

            with Horizontal(classes="detail-row"):
                yield Label("Stage:", classes="label")
                yield Static(f"{ev.highest_stage_passed}/3", classes="value")

            if ev.drift_score is not None:
                with Horizontal(classes="detail-row"):
                    yield Label("Drift:", classes="label")
                    yield Static(f"{ev.drift_score:.3f}", classes="value")

            if ev.failure_reason:
                with Horizontal(classes="detail-row"):
                    yield Label("Failure:", classes="label")
                    reason = ev.failure_reason[:50] + "..." if len(ev.failure_reason) > 50 else ev.failure_reason
                    yield Static(f"[red]{reason}[/]", classes="value")

        # Wonder questions
        if gen.wonder_questions:
            yield Label("Wonder Questions:", classes="section-header")
            for i, q in enumerate(gen.wonder_questions, 1):
                display_q = q[:70] + "..." if len(q) > 70 else q
                yield Static(f"  {i}. \"{display_q}\"", classes="wonder-item")

        # Delta vs previous
        if self.previous_generation is not None:
            prev_onto = self.previous_generation.ontology_snapshot
            delta = OntologyDelta.compute(prev_onto, gen.ontology_snapshot)
            yield Label(
                f"Delta vs Gen {self.previous_generation.generation_number}:",
                classes="section-header",
            )
            with Horizontal(classes="detail-row"):
                yield Label("Similarity:", classes="label")
                sim_color = "green" if delta.similarity >= 0.8 else "yellow" if delta.similarity >= 0.5 else "red"
                yield Static(f"[{sim_color}]{delta.similarity:.3f}[/]", classes="value")

            for field in delta.added_fields:
                yield Static(
                    f"  [green]+[/] {field.name} ({field.field_type})",
                    classes="delta-item",
                )
            for field_name in delta.removed_fields:
                yield Static(f"  [red]-[/] {field_name}", classes="delta-item")
            for mod in delta.modified_fields:
                yield Static(
                    f"  [yellow]~[/] {mod.field_name}: "
                    f"{mod.old_type}\u2192{mod.new_type}",
                    classes="delta-item",
                )

        # Execution output preview
        if gen.execution_output:
            yield Label("Execution Output:", classes="section-header")
            preview = gen.execution_output[:500]
            if len(gen.execution_output) > 500:
                preview += "\n..."
            yield Static(f"[dim]{preview}[/]", classes="output-preview")

    def watch_selected_generation(
        self, _new_value: GenerationRecord | None
    ) -> None:
        self.refresh(recompose=True)

    def watch_previous_generation(
        self, _new_value: GenerationRecord | None
    ) -> None:
        self.refresh(recompose=True)


# =============================================================================
# LINEAGE DETAIL SCREEN
# =============================================================================


class LineageDetailScreen(Screen[None]):
    """Split-view screen showing lineage tree and generation detail.

    Layout:
        +---------------------------+---------------------------+
        | LineageTreeWidget (2fr)   | GenerationDetailPanel     |
        |                           | (1fr, min-width 35)       |
        +---------------------------+---------------------------+
    """

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
        Binding("d", "show_diff", "Diff"),
        Binding("g", "show_git_tag", "Git Tag"),
        Binding("t", "focus_tree", "Tree"),
        Binding("r", "rewind", "Rewind"),
    ]

    DEFAULT_CSS = """
    LineageDetailScreen {
        layout: vertical;
        background: $background;
    }

    LineageDetailScreen > .main-area {
        width: 100%;
        height: 1fr;
        padding: 1;
    }

    LineageDetailScreen > .main-area > .content-row {
        width: 100%;
        height: 100%;
    }

    LineageDetailScreen > .main-area > .content-row > .tree-panel {
        width: 2fr;
        height: 100%;
        margin-right: 1;
    }

    LineageDetailScreen > .main-area > .content-row > .detail-scroll {
        width: 1fr;
        min-width: 35;
        height: 100%;
    }
    """

    def __init__(
        self,
        lineage: OntologyLineage,
        *,
        event_store: EventStore | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._lineage = lineage
        self._event_store = event_store
        self._tree_widget: LineageTreeWidget | None = None
        self._detail_panel: GenerationDetailPanel | None = None
        self._selected_gen_num: int | None = None

    def compose(self) -> ComposeResult:
        yield Header()

        with Container(classes="main-area"), Horizontal(classes="content-row"):
            with Container(classes="tree-panel"):
                self._tree_widget = LineageTreeWidget(lineage=self._lineage)
                yield self._tree_widget

            with VerticalScroll(classes="detail-scroll"):
                self._detail_panel = GenerationDetailPanel()
                yield self._detail_panel

        yield Footer()

    def on_generation_node_selected(
        self, message: GenerationNodeSelected
    ) -> None:
        gen_num = message.generation_number
        self._selected_gen_num = gen_num

        # Find the generation record
        gen = self._find_generation(gen_num)
        if gen is None:
            return

        # Find previous generation
        prev_gen = self._find_generation(gen_num - 1)

        if self._detail_panel:
            self._detail_panel.previous_generation = prev_gen
            self._detail_panel.selected_generation = gen

    def _find_generation(self, gen_num: int) -> GenerationRecord | None:
        for g in self._lineage.generations:
            if g.generation_number == gen_num:
                return g
        return None

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_show_diff(self) -> None:
        if self._selected_gen_num is None:
            self.notify("Select a generation first", severity="warning")
            return

        gen = self._find_generation(self._selected_gen_num)
        prev_gen = self._find_generation(self._selected_gen_num - 1)

        if gen is None:
            return

        if prev_gen is None:
            self.notify(
                f"Gen {self._selected_gen_num} is the first generation (no diff)",
                severity="information",
            )
            return

        delta = OntologyDelta.compute(
            prev_gen.ontology_snapshot, gen.ontology_snapshot
        )
        parts = [
            f"Delta Gen {prev_gen.generation_number} \u2192 Gen {gen.generation_number}:",
            f"  Similarity: {delta.similarity:.3f}",
        ]
        if delta.added_fields:
            parts.append(f"  Added: {', '.join(f.name for f in delta.added_fields)}")
        if delta.removed_fields:
            parts.append(f"  Removed: {', '.join(delta.removed_fields)}")
        if delta.modified_fields:
            parts.append(
                f"  Modified: {', '.join(m.field_name for m in delta.modified_fields)}"
            )
        self.notify("\n".join(parts), title="Ontology Diff")

    def action_show_git_tag(self) -> None:
        if self._selected_gen_num is None:
            self.notify("Select a generation first", severity="warning")
            return

        tag = f"ooo/{self._lineage.lineage_id}/gen_{self._selected_gen_num}"
        self.notify(f"Git tag: {tag}", title="Git Tag")

    def action_focus_tree(self) -> None:
        if self._tree_widget:
            try:
                tree = self._tree_widget.query_one("#lineage-tree")
                tree.focus()
            except Exception:
                pass

    def action_rewind(self) -> None:
        """Initiate rewind to the selected generation."""
        if self._selected_gen_num is None:
            self.notify("Select a generation first", severity="warning")
            return

        if self._event_store is None:
            self.notify("No event store available for rewind", severity="error")
            return

        if self._selected_gen_num >= self._lineage.current_generation:
            self.notify(
                f"Cannot rewind to current generation ({self._selected_gen_num})",
                severity="warning",
            )
            return

        self.app.push_screen(
            ConfirmRewindScreen(
                self._lineage.lineage_id,
                self._lineage.current_generation,
                self._selected_gen_num,
            ),
            callback=self._on_rewind_confirmed,
        )

    def _on_rewind_confirmed(self, confirmed: bool) -> None:
        """Handle rewind confirmation result."""
        if confirmed and self._selected_gen_num is not None:
            asyncio.create_task(self._perform_rewind(self._selected_gen_num))

    async def _perform_rewind(self, to_generation: int) -> None:
        """Execute the rewind operation.

        1. Emit lineage_rewound event
        2. Check git dirty state
        3. Check git tag exists
        4. Git checkout the target tag
        5. Notify and pop screen
        """
        lineage_id = self._lineage.lineage_id
        from_gen = self._lineage.current_generation

        # 1. Emit rewind event
        try:
            assert self._event_store is not None
            await self._event_store.append(
                lineage_rewound(lineage_id, from_gen, to_generation)
            )
        except Exception as e:
            self.notify(f"Failed to emit rewind event: {e}", severity="error")
            return

        # 2. Check git dirty state
        tag_name = f"ooo/{lineage_id}/gen_{to_generation}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if stdout.strip():
                self.notify(
                    f"Rewound to Gen {to_generation} (event recorded). "
                    f"Git checkout skipped — working tree is dirty.",
                    severity="warning",
                    title="Rewind (partial)",
                )
                self.app.pop_screen()
                return
        except Exception:
            pass  # If git check fails, try checkout anyway

        # 3. Verify tag exists
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--verify", f"refs/tags/{tag_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0:
                self.notify(
                    f"Rewound to Gen {to_generation} (event recorded). "
                    f"Git tag '{tag_name}' not found — checkout skipped.",
                    severity="warning",
                    title="Rewind (partial)",
                )
                self.app.pop_screen()
                return
        except Exception:
            pass

        # 4. Git checkout
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "checkout", tag_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                err_msg = stderr.decode().strip() if stderr else "unknown error"
                self.notify(
                    f"Git checkout failed: {err_msg}",
                    severity="error",
                    title="Rewind Error",
                )
                self.app.pop_screen()
                return
        except Exception as e:
            self.notify(f"Git checkout error: {e}", severity="error")
            self.app.pop_screen()
            return

        # 5. Success notification
        self.notify(
            f"Rewound to Gen {to_generation}. "
            f"Run `ralph.sh --lineage-id {lineage_id}` to resume.",
            title="Rewind Complete",
        )
        self.app.pop_screen()


__all__ = ["GenerationDetailPanel", "LineageDetailScreen"]
