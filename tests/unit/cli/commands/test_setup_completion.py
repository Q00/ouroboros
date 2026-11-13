"""`ouroboros setup` ends with next steps; the console line needs the command."""

from __future__ import annotations

import importlib.util
from io import StringIO

import pytest
from rich.console import Console

from ouroboros.cli.commands import setup_completion


def _render(runtime: str) -> str:
    buffer = StringIO()
    setup_completion.print_setup_completion(
        Console(file=buffer, force_terminal=False, width=120), runtime
    )
    return buffer.getvalue()


@pytest.mark.parametrize("runtime", ["codex", "host"])
def test_console_line_is_shown_when_the_command_ships(
    monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    monkeypatch.setattr(setup_completion, "console_command_available", lambda: True)

    output = _render(runtime)

    assert "ouroboros gui   # open the Ouroboros console in your browser" in output
    assert output.endswith("browser\n\n")


@pytest.mark.parametrize(
    ("runtime", "last_step"),
    [
        ("codex", "ouroboros run workflow seed.yaml"),
        ("host", "In your MCP host chat, type: ooo run"),
    ],
)
def test_without_the_command_next_steps_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, runtime: str, last_step: str
) -> None:
    monkeypatch.setattr(setup_completion, "console_command_available", lambda: False)

    output = _render(runtime)

    assert "ouroboros gui" not in output
    assert output.endswith(f"  {last_step}\n\n")


@pytest.mark.parametrize("found", [True, False])
def test_availability_follows_the_gui_command_module(
    monkeypatch: pytest.MonkeyPatch, found: bool
) -> None:
    seen: list[str] = []

    def fake_find_spec(name: str) -> object | None:
        seen.append(name)
        return object() if found else None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    assert setup_completion.console_command_available() is found
    assert seen == ["ouroboros.cli.commands.gui"]


@pytest.mark.parametrize("error", [ImportError("broken parent"), ValueError("no spec")])
def test_unresolvable_module_counts_as_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def raising(name: str) -> object:
        raise error

    monkeypatch.setattr(importlib.util, "find_spec", raising)

    assert setup_completion.console_command_available() is False


def test_availability_matches_this_checkout() -> None:
    expected = importlib.util.find_spec("ouroboros.cli.commands.gui") is not None
    assert setup_completion.console_command_available() is expected
