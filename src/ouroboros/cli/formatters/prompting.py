"""Prompt helpers for interactive CLI input."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import sys
from typing import TextIO

from ouroboros.cli.formatters import console


async def multiline_prompt_async(prompt_text: str) -> str:
    """Get multiline-safe input while allowing logs above the active prompt."""
    console.print(
        f"[bold green]{prompt_text}[/] [dim](Ctrl+D/Ctrl+Z then Enter: submit; "
        "Enter inserts newline)[/]"
    )
    with _patched_stdio():
        return await asyncio.to_thread(_read_multiline_from_stdin)


def _read_multiline_from_stdin() -> str:
    lines: list[str] = []
    prompt = "> "
    continuation = "  "

    while True:
        try:
            line = input(prompt if not lines else continuation)
        except EOFError:
            print()
            return "\n".join(lines)
        lines.append(line)


@contextmanager
def _patched_stdio() -> TextIO:
    stdout = sys.stdout
    stderr = sys.stderr
    sys.stdout = _StreamProxy(stdout)
    sys.stderr = _StreamProxy(stderr)
    try:
        yield stdout
    finally:
        sys.stdout = stdout
        sys.stderr = stderr


class _StreamProxy:
    def __init__(self, target: TextIO) -> None:
        self._target = target

    def write(self, data: str) -> int:
        return self._target.write(data)

    def flush(self) -> None:
        self._target.flush()

    def isatty(self) -> bool:
        return self._target.isatty()

    @property
    def encoding(self) -> str | None:
        return getattr(self._target, "encoding", None)


__all__ = ["multiline_prompt_async"]
