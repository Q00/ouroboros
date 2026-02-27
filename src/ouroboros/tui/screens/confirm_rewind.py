"""Confirmation modal for lineage rewind operation."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ConfirmRewindScreen(ModalScreen[bool]):
    """Modal dialog confirming a lineage rewind.

    Displays:
    - Lineage ID
    - From generation -> To generation
    - Git tag that will be checked out
    - Warning about truncated generations

    Bindings:
        y -> dismiss(True)
        n / escape -> dismiss(False)
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n", "cancel", "No", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmRewindScreen {
        align: center middle;
    }

    ConfirmRewindScreen > #rewind-dialog {
        width: 60;
        height: auto;
        max-height: 22;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }

    ConfirmRewindScreen > #rewind-dialog > .dialog-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }

    ConfirmRewindScreen > #rewind-dialog > .dialog-row {
        width: 100%;
        height: 1;
    }

    ConfirmRewindScreen > #rewind-dialog > .dialog-warning {
        width: 100%;
        margin-top: 1;
        color: $warning;
        text-style: bold;
    }

    ConfirmRewindScreen > #rewind-dialog > .button-row {
        width: 100%;
        height: 3;
        margin-top: 1;
        align: center middle;
    }

    ConfirmRewindScreen > #rewind-dialog > .button-row > #btn-confirm {
        margin-right: 2;
    }

    ConfirmRewindScreen > #rewind-dialog > .button-row > #btn-cancel {
        margin-left: 2;
    }
    """

    def __init__(
        self,
        lineage_id: str,
        from_generation: int,
        to_generation: int,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._lineage_id = lineage_id
        self._from_generation = from_generation
        self._to_generation = to_generation

    def compose(self) -> ComposeResult:
        with Vertical(id="rewind-dialog"):
            yield Label("\u26a0 REWIND LINEAGE \u26a0", classes="dialog-title")

            yield Static(
                f"Lineage:  [bold]{self._lineage_id}[/]",
                classes="dialog-row",
            )
            yield Static(
                f"Rewind:   Gen {self._from_generation} \u2192 Gen {self._to_generation}",
                classes="dialog-row",
            )
            yield Static(
                f"Git tag:  [cyan]ooo/{self._lineage_id}/gen_{self._to_generation}[/]",
                classes="dialog-row",
            )

            if self._from_generation > self._to_generation:
                truncated = f"{self._to_generation + 1}~{self._from_generation}"
                yield Static(
                    f"Generations {truncated} will be truncated!",
                    classes="dialog-warning",
                )

            with Horizontal(classes="button-row"):
                yield Button("Confirm (y)", variant="error", id="btn-confirm")
                yield Button("Cancel (n)", variant="default", id="btn-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-confirm":
            self.dismiss(True)
        else:
            self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


__all__ = ["ConfirmRewindScreen"]
