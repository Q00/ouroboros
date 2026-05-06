"""Tests for CLI debug rendering of provider tool-start callbacks."""

from __future__ import annotations

from ouroboros.cli.commands import init as init_command
from ouroboros.cli.commands import pm as pm_command


class _FakeConsole:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, message: str) -> None:
        self.messages.append(message)


def test_init_debug_callback_renders_tool_started(monkeypatch) -> None:
    console = _FakeConsole()
    monkeypatch.setattr(init_command, "console", console)

    callback = init_command._make_message_callback(debug=True)
    assert callback is not None
    callback("tool_started", "mcp__ouroboros__ouroboros_interview")

    assert console.messages == ["  [cyan]▶ mcp__ouroboros__ouroboros_interview[/cyan]"]


def test_pm_debug_callback_renders_tool_started(monkeypatch) -> None:
    console = _FakeConsole()
    monkeypatch.setattr(pm_command, "console", console)

    callback = pm_command._make_message_callback(debug=True)
    assert callback is not None
    callback("tool_started", "mcp__ouroboros__ouroboros_pm")

    assert console.messages == ["  [cyan]tool started:[/] mcp__ouroboros__ouroboros_pm"]
