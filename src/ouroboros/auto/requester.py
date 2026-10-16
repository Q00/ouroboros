"""Ask the requester instead of assuming during ``ooo auto`` interviews.

``ooo auto`` answers every interview question itself. Most answers are grounded
(the goal, repo facts, conventions), but some are the answerer's own guess: the
draft answer source is ``conservative_default`` or ``assumption``. This module
lets a caller route exactly those questions to the person who made the request.

The requester is an external command (opt-in). For each such question the
command is run once, without a shell, with this JSON object on stdin::

    {"question": str, "goal": str, "draft_answer": str,
     "draft_source": str, "round": int}

Its stdout is either a JSON object ``{"answer": str}`` or plain text. A
nonempty answer replaces the draft. A timeout, a nonzero exit, an empty answer,
a JSON object without a string ``answer``, or a launch failure keeps the draft
and is reported through :class:`RequesterReply.outcome`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
import shlex
import time
from typing import Literal, Protocol

REQUESTER_COMMAND_ENV = "OUROBOROS_AUTO_REQUESTER_COMMAND"
REQUESTER_TIMEOUT_ENV = "OUROBOROS_AUTO_REQUESTER_TIMEOUT_SECONDS"
DEFAULT_REQUESTER_TIMEOUT_SECONDS = 300.0

RequesterOutcome = Literal[
    "answered",
    "timeout",
    "nonzero_exit",
    "empty_answer",
    "invalid_output",
    "launch_failed",
]

# Bound on how much stderr is kept in a failure detail.
_DETAIL_LIMIT = 300


@dataclass(frozen=True, slots=True)
class RequesterQuestion:
    """One question routed to the requester."""

    question: str
    goal: str
    draft_answer: str
    draft_source: str
    round: int

    def to_payload(self) -> dict[str, object]:
        return {
            "question": self.question,
            "goal": self.goal,
            "draft_answer": self.draft_answer,
            "draft_source": self.draft_source,
            "round": self.round,
        }


@dataclass(frozen=True, slots=True)
class RequesterReply:
    """Result of asking the requester one question.

    ``answer`` is set only when ``outcome == "answered"``. ``detail`` explains a
    non-answer (exit code, stderr excerpt, parse error) and is empty otherwise.
    """

    outcome: RequesterOutcome
    answer: str | None = None
    detail: str = ""
    elapsed_seconds: float = 0.0


class Requester(Protocol):
    """Anything that can answer a :class:`RequesterQuestion`."""

    async def ask(self, question: RequesterQuestion) -> RequesterReply: ...


@dataclass(frozen=True, slots=True)
class CommandRequester:
    """Ask the requester by running an external command (no shell)."""

    argv: tuple[str, ...]
    timeout_seconds: float = DEFAULT_REQUESTER_TIMEOUT_SECONDS

    async def ask(self, question: RequesterQuestion) -> RequesterReply:
        started = time.monotonic()
        payload = json.dumps(question.to_payload(), ensure_ascii=False).encode("utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            return RequesterReply(
                "launch_failed",
                detail=_clip(f"{type(exc).__name__}: {exc}"),
                elapsed_seconds=time.monotonic() - started,
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=self.timeout_seconds
            )
        except TimeoutError:
            await _kill(process)
            return RequesterReply(
                "timeout",
                detail=f"no reply within {self.timeout_seconds:g}s",
                elapsed_seconds=time.monotonic() - started,
            )
        except asyncio.CancelledError:
            await _kill(process)
            raise
        elapsed = time.monotonic() - started
        if process.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            detail = f"exit code {process.returncode}"
            if stderr_text:
                detail = f"{detail}: {stderr_text}"
            return RequesterReply("nonzero_exit", detail=_clip(detail), elapsed_seconds=elapsed)
        return _parse_reply(stdout.decode("utf-8", errors="replace"), elapsed)


def _parse_reply(stdout: str, elapsed: float) -> RequesterReply:
    """Read ``{"answer": str}`` or plain text from the command's stdout.

    Only a JSON *object* is read as structured output. Any other text,
    including a bare JSON string or number, is the answer itself.
    """
    text = stdout.strip()
    if not text:
        return RequesterReply("empty_answer", detail="empty stdout", elapsed_seconds=elapsed)
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        answer = parsed.get("answer")
        if not isinstance(answer, str):
            return RequesterReply(
                "invalid_output",
                detail='JSON object without a string "answer"',
                elapsed_seconds=elapsed,
            )
        text = answer.strip()
        if not text:
            return RequesterReply("empty_answer", detail='empty "answer"', elapsed_seconds=elapsed)
    return RequesterReply("answered", answer=text, elapsed_seconds=elapsed)


async def _kill(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        return


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= _DETAIL_LIMIT:
        return flat
    return f"{flat[: _DETAIL_LIMIT - 3]}..."


def resolve_requester_command(
    option: str | None,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...] | None:
    """Return the requester argv, or ``None`` when the feature is off.

    The option wins over :data:`REQUESTER_COMMAND_ENV` whenever it is given.
    The command is split with :func:`shlex.split` and never run through a shell.
    """
    env = os.environ if environ is None else environ
    raw = option if option is not None else env.get(REQUESTER_COMMAND_ENV)
    if raw is None or not raw.strip():
        return None
    argv = tuple(shlex.split(raw))
    return argv or None


def resolve_requester_timeout(
    option: float | None,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Return the per-question timeout in seconds (option, then env, then default)."""
    if option is not None:
        value = float(option)
    else:
        env = os.environ if environ is None else environ
        raw = env.get(REQUESTER_TIMEOUT_ENV)
        if raw is None or not raw.strip():
            return DEFAULT_REQUESTER_TIMEOUT_SECONDS
        try:
            value = float(raw)
        except ValueError as exc:
            msg = f"{REQUESTER_TIMEOUT_ENV} must be a number of seconds, got {raw!r}"
            raise ValueError(msg) from exc
    if not value > 0:
        msg = f"requester timeout must be greater than 0 seconds, got {value:g}"
        raise ValueError(msg)
    return value


def build_requester(
    command: str | None = None,
    timeout_seconds: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> CommandRequester | None:
    """Build the requester from an explicit option or the environment.

    Returns ``None`` (requester off, auto unchanged) when neither the option nor
    :data:`REQUESTER_COMMAND_ENV` names a command.
    """
    argv = resolve_requester_command(command, environ)
    if argv is None:
        return None
    return CommandRequester(argv, resolve_requester_timeout(timeout_seconds, environ))
