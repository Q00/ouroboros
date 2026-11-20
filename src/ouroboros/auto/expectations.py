"""What a typical current product of the requested kind includes, for a new product.

A request names a kind of product ("a racing game", "a note-taking app"). The
user expects what a typical current product of that kind includes, but the
request does not list it and the auto interview cannot ask the user for it, so
a worker given only the request builds less. This module finds that list once
per auto session, beside the interview, and the Seed carries it to the worker
as ``SourcedScope.expected_experience``:

- When: only when the workspace has no existing code
  (``ledger_seed.workspace_has_existing_code`` is false, the same signal as the
  Seed's brownfield context). For a change to existing code nothing runs.
- The call: one agent session through ``create_agent_runtime`` with the auto
  interview stage's backend and permission mode ``default`` (the read-only
  sandbox class the repository investigator uses), with a fresh empty
  temporary directory as its cwd, removed afterwards. Live web search where
  the backend offers it: Codex is launched with ``-c web_search="live"``
  (``create_agent_runtime(web_search=True)``); Claude gets the ``WebSearch``
  and ``WebFetch`` tools for this call only. Without search the model answers
  from what it knows.
- The prompt (``build_prompt``) names no domain: the goal verbatim, a short
  list of what a typical current product of the kind includes, limited to what
  a single local install can do, then the URLs read, in the goal's language.
- The reply is sanitized without a model (``sanitize_reply``): only ``- `` lines,
  links and URLs stripped, at most ``MAX_LINES`` lines of ``MAX_LINE_CHARS``
  characters; the URLs after ``Sources:`` are kept apart (at most
  ``MAX_SOURCES``) and never reach the worker.
- Timing: started with the interview (``start_expected_experience``) and
  awaited after it (``ExpectationsRun.result``), bounded by the smaller of
  ``OUROBOROS_AUTO_EXPECTATIONS_TIMEOUT`` (default 180 s; 0 turns it off) and
  the interview phase budget.
- One ``auto.expectations.researched`` event records the outcome (``ok``,
  ``timeout``, ``failed``, ``skipped_brownfield``, ``disabled``), the line
  count and the elapsed seconds. Nothing here can stop the run: any failure
  leaves the Seed without the list.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import inspect
import math
import os
import re
import tempfile
import time
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from ouroboros.auto.answer_text import LANGUAGE_NAMES, answer_language
from ouroboros.auto.ledger_seed import workspace_has_existing_code

if TYPE_CHECKING:
    from ouroboros.auto.state import AutoPipelineState

log = structlog.get_logger(__name__)

TIMEOUT_ENV = "OUROBOROS_AUTO_EXPECTATIONS_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 180.0
# The read-only sandbox class (see ``auto/investigator.py``).
PERMISSION_MODE = "default"
WEB_TOOLS: tuple[str, ...] = ("WebSearch", "WebFetch")
MAX_LINES = 12
MAX_LINE_CHARS = 180
MAX_SOURCES = 6
EVENT_TYPE = "auto.expectations.researched"

OUTCOME_OK = "ok"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED_BROWNFIELD = "skipped_brownfield"
OUTCOME_DISABLED = "disabled"

_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# A bare site name such as ``example.org/path``; a code file name such as
# ``three.js`` is not one.
_DOMAIN = (
    r"(?:[A-Za-z0-9-]+\.)+(?!(?:js|mjs|cjs|ts|py|json|css|html|md|txt)\b)[A-Za-z]{2,24}"
    r"(?:/[^\s)\]]*)?"
)
# A citation: a link whose text is a site name, or site names in parentheses.
_LINKED_DOMAIN = re.compile(rf"\[\s*{_DOMAIN}\s*\]\([^)]*\)")
_PARENTHESIZED_DOMAINS = re.compile(rf"\(\s*{_DOMAIN}(?:\s*[,;]\s*{_DOMAIN})*\s*\)")
_PARENTHESIZED_URL = re.compile(r"\(\s*<?https?://[^\s)]*>?\s*\)")
_URL = re.compile(r"<?https?://[^\s<>]+>?")
_SOURCE_URL = re.compile(r"https?://[^\s<>\"'`]+")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]")
_SOURCES_LINE = re.compile(r"^[\s*_#>]*sources?[\s*_]*:", re.IGNORECASE)
_URL_TRAILING = ".,;:!?)]}>*_"

EmitEvent = Callable[[str, str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ExpectedExperience:
    """The sanitized research: capability lines, and the URLs it reported reading."""

    lines: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


NO_EXPECTED_EXPERIENCE = ExpectedExperience()


def resolve_timeout(environ: Mapping[str, str] | None = None) -> float:
    """Seconds from :data:`TIMEOUT_ENV`, or the default; ``0`` turns the research off."""
    env = os.environ if environ is None else environ
    raw = env.get(TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{TIMEOUT_ENV} must be a number of seconds, got {raw!r}"
        raise ValueError(msg) from exc
    if not math.isfinite(value) or value < 0:
        msg = f"{TIMEOUT_ENV} must be 0 or a finite number of seconds, got {raw!r}"
        raise ValueError(msg)
    return value


def build_prompt(goal: str) -> str:
    """The research prompt: domain-free, the goal verbatim, the answer in its language."""
    language = LANGUAGE_NAMES[answer_language(goal)]
    return "\n".join(
        [
            f"A user asked: {goal.strip()}",
            "",
            "Describe what a typical, current product of this kind includes today: the "
            "capabilities a user would expect and how it looks and feels in use (presentation, "
            "motion, feedback), most expected first, 6–12 short lines starting "
            "with '- ', each marked (core) or (common).",
            "Only include what the product itself can do in a single local install: nothing "
            "that needs a server, network service, account, payment, purchase or personal data.",
            "After the list, one line 'Sources:' with URLs you actually read, if any.",
            f"Write the answer in {language}.",
        ]
    )


def sanitize_reply(reply: str) -> ExpectedExperience:
    """Keep the ``- `` lines without links or URLs; collect the URLs after ``Sources:``.

    Deterministic. A line keeps its text after ``- ``, with a markdown link
    reduced to its text and any bare URL and site-name citation removed (a
    source never reaches the worker, by name or by address); an empty result
    is dropped.
    At most :data:`MAX_LINES` lines are kept, each cut to
    :data:`MAX_LINE_CHARS` characters. Everything from the ``Sources:`` line on
    is the source list: its URLs (at most :data:`MAX_SOURCES`, without
    duplicates) are kept apart, and none of its lines is a capability.
    """
    lines: list[str] = []
    sources: dict[str, None] = {}
    in_sources = False
    for raw in reply.splitlines():
        text = raw.strip()
        if not in_sources and _SOURCES_LINE.match(text):
            in_sources = True
        if in_sources:
            for url in _SOURCE_URL.findall(text):
                cleaned = url.rstrip(_URL_TRAILING)
                if cleaned and len(sources) < MAX_SOURCES:
                    sources.setdefault(cleaned, None)
            continue
        if not text.startswith("- ") or len(lines) >= MAX_LINES:
            continue
        body = _MARKDOWN_LINK.sub(r"\1", _LINKED_DOMAIN.sub(" ", text[2:]))
        body = _URL.sub(" ", _PARENTHESIZED_URL.sub(" ", body))
        body = _PARENTHESIZED_DOMAINS.sub(" ", body)
        body = " ".join(_EMPTY_BRACKETS.sub(" ", body).split())
        if body:
            lines.append(body[:MAX_LINE_CHARS].rstrip())
    return ExpectedExperience(lines=tuple(lines), sources=tuple(sources))


class ExpectationsResearcher(Protocol):
    """Runs the research for one goal; :class:`AgentExpectationsResearcher` in production."""

    timeout_seconds: float

    async def research(self, goal: str) -> str:
        """Return the model's raw reply; raise on any failure."""
        ...


RuntimeFactory = Callable[..., Any]


@dataclass(slots=True)
class AgentExpectationsResearcher:
    """One read-only agent session in an empty temporary directory, with web search."""

    runtime_backend: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    runtime_factory: RuntimeFactory | None = None

    async def research(self, goal: str) -> str:
        factory = self.runtime_factory
        if factory is None:
            from ouroboros.orchestrator.runtime_factory import create_agent_runtime

            factory = create_agent_runtime
        from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime

        with tempfile.TemporaryDirectory(
            prefix="ouroboros-expectations-", ignore_cleanup_errors=True
        ) as workspace:
            runtime = await asyncio.to_thread(
                factory,
                backend=self.runtime_backend,
                model=None,
                permission_mode=PERMISSION_MODE,
                cwd=workspace,
                web_search=True,
            )
            try:
                blocker = preflight_agent_runtime(runtime)
                if blocker is not None:
                    msg = f"runtime unavailable: {blocker}"
                    raise RuntimeError(msg)
                result = await runtime.execute_task_to_result(
                    build_prompt(goal), tools=list(WEB_TOOLS)
                )
            finally:
                await _close(runtime)
        if result.is_err:
            msg = f"research call failed: {result.error}"
            raise RuntimeError(msg)
        return result.value.final_message or ""


async def _close(runtime: Any) -> None:
    closer = getattr(runtime, "aclose", None)
    if closer is not None and inspect.iscoroutinefunction(closer):
        await closer()


def build_expectations_researcher(
    runtime_backend: str | None,
    opencode_mode: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> AgentExpectationsResearcher | None:
    """The researcher for one auto session, or ``None`` when no runtime can run it.

    ``None`` when no backend is named (or the name is unknown) or the stage
    dispatches through a host plugin, which the synchronous pipeline cannot
    consume. :data:`TIMEOUT_ENV` is read here; an invalid value raises
    ``ValueError``, and ``0`` still builds a researcher so the run records
    the ``disabled`` outcome.
    """
    timeout = resolve_timeout(environ)
    if not runtime_backend:
        return None
    from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin
    from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend

    try:
        resolved = resolve_agent_runtime_backend(runtime_backend)
    except ValueError:
        return None
    if should_dispatch_via_plugin(runtime_backend, opencode_mode):
        return None
    return AgentExpectationsResearcher(runtime_backend=resolved, timeout_seconds=timeout)


async def _research(
    researcher: ExpectationsResearcher, goal: str, timeout: float
) -> tuple[str, ExpectedExperience, float]:
    """Run one research call within ``timeout``; never raises but on cancellation."""
    started = time.monotonic()
    try:
        reply = await asyncio.wait_for(researcher.research(goal), timeout=timeout)
    except TimeoutError:
        return OUTCOME_TIMEOUT, NO_EXPECTED_EXPERIENCE, time.monotonic() - started
    except Exception as exc:  # noqa: BLE001 - research is optional; never block auto on it
        log.warning("auto.expectations.call_failed", error_type=type(exc).__name__)
        return OUTCOME_FAILED, NO_EXPECTED_EXPERIENCE, time.monotonic() - started
    found = sanitize_reply(reply if isinstance(reply, str) else "")
    elapsed = time.monotonic() - started
    if not found.lines:
        return OUTCOME_FAILED, NO_EXPECTED_EXPERIENCE, elapsed
    return OUTCOME_OK, found, elapsed


@dataclass(slots=True)
class ExpectationsRun:
    """The research for one auto session, started beside its interview.

    ``result`` awaits it (the task is bounded by its own timeout), records one
    event and returns what it found; later calls return the same value without
    another event. A run with neither a task nor an outcome is inactive (no
    researcher wired): it records nothing and finds nothing. ``cancel`` stops
    a pending task on a path that will not use it.
    """

    auto_session_id: str
    emit: EmitEvent | None = None
    outcome: str | None = None
    timeout_seconds: float = 0.0
    task: asyncio.Task[tuple[str, ExpectedExperience, float]] | None = None
    _found: ExpectedExperience | None = field(default=None, init=False, repr=False)

    async def result(self) -> ExpectedExperience:
        if self._found is not None:
            return self._found
        if self.task is None and self.outcome is None:
            self._found = NO_EXPECTED_EXPERIENCE
            return self._found
        outcome, found, elapsed = self.outcome or OUTCOME_FAILED, NO_EXPECTED_EXPERIENCE, 0.0
        if self.task is not None:
            if self.task.cancelled():
                outcome = OUTCOME_FAILED
            else:
                outcome, found, elapsed = await self.task
        self._found = found
        payload: dict[str, Any] = {
            "auto_session_id": self.auto_session_id,
            "outcome": outcome,
            "line_count": len(found.lines),
            "elapsed_seconds": round(elapsed, 3),
            "timeout_seconds": self.timeout_seconds,
        }
        log.info(EVENT_TYPE, **payload)
        if self.emit is not None:
            try:
                await self.emit(EVENT_TYPE, self.auto_session_id, payload)
            except Exception as exc:  # noqa: BLE001 - the record must not stop the run
                log.warning(f"{EVENT_TYPE}.emit_failed", error_type=type(exc).__name__)
        return found

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()


def keep(state: AutoPipelineState, found: ExpectedExperience) -> None:
    """Record ``found`` on ``state``, so it is saved with the interview it ran beside."""
    state.expected_experience = list(found.lines)
    state.expected_experience_sources = list(found.sources)


def kept(state: AutoPipelineState) -> ExpectedExperience:
    """The research recorded on ``state``; a resumed session reads it from its saved state."""
    return ExpectedExperience(
        lines=tuple(state.expected_experience), sources=tuple(state.expected_experience_sources)
    )


def start_expected_experience(
    researcher: ExpectationsResearcher | None,
    state: AutoPipelineState,
    interview_timeout: float,
    emit: EmitEvent | None = None,
) -> ExpectationsRun:
    """Start the research for ``state``'s goal beside its interview.

    No researcher: an inactive run. A researcher whose timeout is 0:
    ``disabled``. A workspace with existing code: ``skipped_brownfield``.
    Otherwise one call starts now, bounded by the smaller of the researcher's
    timeout and ``interview_timeout``.
    """
    run = ExpectationsRun(state.auto_session_id, emit)
    if researcher is None:
        return run
    if researcher.timeout_seconds <= 0:
        run.outcome = OUTCOME_DISABLED
    elif workspace_has_existing_code(state.cwd):
        run.outcome = OUTCOME_SKIPPED_BROWNFIELD
    else:
        run.timeout_seconds = max(0.0, min(researcher.timeout_seconds, interview_timeout))
        run.task = asyncio.create_task(_research(researcher, state.goal, run.timeout_seconds))
    return run


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EVENT_TYPE",
    "MAX_LINES",
    "MAX_LINE_CHARS",
    "MAX_SOURCES",
    "NO_EXPECTED_EXPERIENCE",
    "OUTCOME_DISABLED",
    "OUTCOME_FAILED",
    "OUTCOME_OK",
    "OUTCOME_SKIPPED_BROWNFIELD",
    "OUTCOME_TIMEOUT",
    "PERMISSION_MODE",
    "TIMEOUT_ENV",
    "WEB_TOOLS",
    "AgentExpectationsResearcher",
    "ExpectationsResearcher",
    "ExpectationsRun",
    "ExpectedExperience",
    "build_expectations_researcher",
    "build_prompt",
    "keep",
    "kept",
    "resolve_timeout",
    "sanitize_reply",
    "start_expected_experience",
]
