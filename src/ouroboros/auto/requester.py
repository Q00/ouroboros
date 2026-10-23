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
from dataclasses import dataclass, replace
import json
import os
import shlex
import time
from typing import TYPE_CHECKING, Literal, Protocol

import structlog

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource
from ouroboros.auto.ledger import (
    REQUESTER_KEY_SUFFIX,
    DecisionProvenance,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
)

if TYPE_CHECKING:
    from ouroboros.auto.interview_driver import AutoInterviewDriver, InterviewTurn
    from ouroboros.auto.state import AutoPipelineState

log = structlog.get_logger(__name__)

REQUESTER_COMMAND_ENV = "OUROBOROS_AUTO_REQUESTER_COMMAND"
REQUESTER_TIMEOUT_ENV = "OUROBOROS_AUTO_REQUESTER_TIMEOUT_SECONDS"
DEFAULT_REQUESTER_TIMEOUT_SECONDS = 300.0
# Transcript prefix of a requester answer (``AutoAnswer.prefixed_text`` for
# source ``requester``). The interview handler reads it to treat the answer as
# the requester's own decision rather than a generated one.
REQUESTER_ANSWER_PREFIX = "[from-auto][requester]"

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
# Text fields in ``state.requester_log`` are cut like ``auto_answer_log``.
_LOG_TEXT_LIMIT = 200
# Generous bound: one entry per routed question, and an interview has at most
# ``max_interview_rounds`` (default 50) rounds per run.
_REQUESTER_LOG_LIMIT = 500
# Draft sources where auto would otherwise assume; only these are routed.
ROUTED_SOURCES = frozenset({AutoAnswerSource.CONSERVATIVE_DEFAULT, AutoAnswerSource.ASSUMPTION})


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


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return f"{flat[: limit - 3]}..."


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


# -- interview driver integration ---------------------------------------------


async def ask_requester(
    driver: AutoInterviewDriver,
    answer: AutoAnswer,
    question: str,
    turn: InterviewTurn,
    state: AutoPipelineState,
    round_number: int,
) -> AutoAnswer:
    """Ask the requester instead of assuming; keep the draft on any failure.

    Called by the interview driver for every round. Returns ``answer`` unchanged
    unless a requester is wired, ``question`` is the interviewer's own question
    (not the driver's synthetic gap-reopen prompt), and the draft is an
    assumption (source in :data:`ROUTED_SOURCES`, no blocker). Every routed
    question is recorded in ``state.requester_log`` and emitted as an
    ``auto.interview.requester_asked`` event, answered or not.

    A draft kept because the requester gave no answer has its ledger entries
    marked ``requester_unanswered``: the requester already said the issue does
    not settle the question, so the worker sees neither the draft nor the
    question, which is kept in ``Seed.open_questions``
    (``ledger_seed.ledger_open_questions``).
    """
    requester = driver.requester
    if (
        requester is None
        or question != turn.question
        or answer.blocker is not None
        or answer.source not in ROUTED_SOURCES
    ):
        return answer
    state.mark_progress(f"asking requester (round {round_number}/{driver.max_rounds})")
    driver._save(state)
    request = RequesterQuestion(
        question=question,
        goal=state.goal,
        draft_answer=answer.text,
        draft_source=answer.source.value,
        round=round_number,
    )
    try:
        reply = await requester.ask(request)
    except Exception as exc:  # noqa: BLE001 - requester is best-effort
        reply = RequesterReply("launch_failed", detail=_clip(f"{type(exc).__name__}: {exc}"))
    await _record_reply(driver, state, request, reply)
    if reply.outcome != "answered" or not reply.answer:
        return replace(
            answer,
            ledger_updates=[
                (section, replace(entry, requester_unanswered=True))
                for section, entry in answer.ledger_updates
            ],
        )
    return requester_answer(answer, reply.answer, round_number, question=question)


async def _record_reply(
    driver: AutoInterviewDriver,
    state: AutoPipelineState,
    request: RequesterQuestion,
    reply: RequesterReply,
) -> None:
    answered = reply.outcome == "answered"
    entry: dict[str, object] = {
        "round": request.round,
        "question": _clip(request.question, _LOG_TEXT_LIMIT),
        "draft_source": request.draft_source,
        "draft_answer": _clip(request.draft_answer, _LOG_TEXT_LIMIT),
        "outcome": reply.outcome,
        "used": answered,
        "answer": _clip(reply.answer or "", _LOG_TEXT_LIMIT),
        "detail": reply.detail,
        "elapsed_seconds": round(reply.elapsed_seconds, 3),
    }
    state.requester_log.append(entry)
    if len(state.requester_log) > _REQUESTER_LOG_LIMIT:
        del state.requester_log[: len(state.requester_log) - _REQUESTER_LOG_LIMIT]
    suffix = "" if answered else "; keeping auto draft"
    state.mark_progress(
        f"requester {reply.outcome} (round {request.round}/{driver.max_rounds}){suffix}"
    )
    driver._save(state)
    (log.info if answered else log.warning)(
        "auto.interview.requester_asked",
        auto_session_id=state.auto_session_id,
        round=request.round,
        draft_source=request.draft_source,
        outcome=reply.outcome,
        detail=reply.detail,
    )
    await driver._emit_event(
        "auto.interview.requester_asked",
        state.auto_session_id,
        aggregate_type="auto_session",
        round=request.round,
        draft_source=request.draft_source,
        outcome=reply.outcome,
        used=answered,
        detail=reply.detail,
        elapsed_seconds=entry["elapsed_seconds"],
        question=entry["question"],
    )


def requester_answer(
    draft: AutoAnswer, text: str, round_number: int, *, question: str = ""
) -> AutoAnswer:
    """Replace ``draft`` with the requester's answer, keeping its sections.

    Each ledger section the draft would have filled gets the requester's text
    as a confirmed user decision, keyed ``<draft key>`` + ``REQUESTER_KEY_SUFFIX``.
    The draft key names the facet the question targeted, so a later requester
    answer for the same facet supersedes the earlier one in
    ``SeedDraftLedger.add_entry`` (the earlier entry becomes WEAK) instead of
    tying with it as CONFLICTING. The transcript keeps every answer in order.

    Each entry also keeps ``question``, the question the requester answered, so
    the Seed can quote the pair verbatim (``ledger_seed.ledger_requester_answers``).
    """
    updates = [
        (
            section,
            LedgerEntry(
                key=f"{entry.key}{REQUESTER_KEY_SUFFIX}",
                value=text,
                source=LedgerSource.USER_PREFERENCE,
                confidence=0.83,
                status=LedgerStatus.CONFIRMED,
                rationale="The requester answered this interview question.",
                evidence=[f"requester answer, interview round {round_number}"],
                provenance=DecisionProvenance.USER_CONFIRMED,
                question=question,
            ),
        )
        for section, entry in draft.ledger_updates
    ]
    non_goals = [text] if any(section == "non_goals" for section, _ in updates) else []
    return AutoAnswer(
        text=text,
        source=AutoAnswerSource.REQUESTER,
        confidence=0.83,
        ledger_updates=updates,
        non_goals=non_goals,
    )
