"""Prompt helpers for interactive CLI input."""

from __future__ import annotations

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.patch_stdout import patch_stdout

from ouroboros.cli.formatters import console


async def multiline_prompt_async(prompt_text: str) -> str:
    """Get multiline-safe input while allowing logs above the active prompt."""
    bindings = KeyBindings()

    @bindings.add("c-j")
    def insert_newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("c-m")
    def submit(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    console.print(f"[bold green]{prompt_text}[/] [dim](Enter: submit, Ctrl+J: newline)[/]")

    session: PromptSession[str] = PromptSession(
        message="> ",
        multiline=True,
        prompt_continuation="  ",
        key_bindings=bindings,
    )

    # prompt_toolkit proxies both stdout and stderr above the prompt.
    with patch_stdout(raw=True):
        return await session.prompt_async()


__all__ = ["multiline_prompt_async"]
