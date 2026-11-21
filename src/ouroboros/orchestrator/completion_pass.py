"""Review and complete the product of a run whose Seed carries expected experience.

An ``ooo auto`` Seed for new work carries research on what a typical current
product of its kind includes (``SourcedScope.expected_experience``). The run's
one worker attempt is told to build to that list, but it is one attempt, and
the formal evaluation judges only the Seed's criteria. A product that runs but
misses capabilities users expect, or that breaks its own basic use while it
adds them, was therefore approved as it stood.

After such a run completes, two sessions run in the run's working directory,
before the formal evaluation, so the evaluation judges the finished product:

1. a reviewer reads the code (``Read``/``Glob``/``Grep``, permission mode
   ``default``) and lists concrete defects: first anything in the request's
   basic use that does not work for a first-time user, then each expected line
   the code does not do yet;
2. when it lists any, a worker with the run's tools and permission mode fixes
   them, basic use first;
3. after that worker, a second read-only reviewer lists only what now stops the
   app from loading or its basic use from working (a fix that rewrites a file
   can break what worked), and a worker repairs what it lists.

A parent session that delegated the run (``_ooo_parent_*`` arguments) bounds
both sessions as it bounded the run: its permission mode is the worker's, and
neither session gets a tool outside its effective tool set. When that set has
no tool that writes, nothing runs.

The pass is best effort. A Seed without expected experience, a run that did not
complete, a backend that dispatches through a host plugin, or a session that
fails or hangs leaves the product as the run left it. A session that ends in a
provider error (a model at capacity, say) is tried once more after a short wait;
a hung session is not. The pass never changes
the run verdict; it adds a ``completion_pass`` record to the run result.

The pass runs after ``execution.terminal``, while the run job is still open. A
waiter that trusts the durable terminal over an open job after a short window
(direct ``ooo auto``, #1593) would end the process and the pass with it, so each
stage is announced in the execution stream as ``execution.completion_pass.updated``
with the most it can take (``bound_seconds``); stage ``done`` ends the pass.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import inspect
from pathlib import Path
from typing import Any

import structlog
import yaml

from ouroboros.core.seed import Seed
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
)

log = structlog.get_logger(__name__)

REVIEWER_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")
REVIEWER_PERMISSION_MODE = "default"
WORKER_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Bash", "Glob", "Grep")
WRITING_TOOLS = frozenset({"Write", "Edit", "Bash"})
# Hang guard for one session: a runtime that never returns. Not a time budget.
SESSION_TIMEOUT_SECONDS = 1800.0
# One more try for a session that ended in an error, after this wait.
SESSION_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 30.0
MAX_DEFECTS = 40
MAX_DEFECT_CHARS = 400
NO_DEFECTS = "NONE"
STAGE_EVENT = "execution.completion_pass.updated"

REVIEW_PROMPT = """You review an app an AI built for this request: {goal}

Research on current products of this kind found that users typically expect:
{expected}

Read the code paths that run, as a strict reviewer. Do not modify any file. List concrete defects, one per line starting with "- ", naming the file:
1. First, anything in the request's basic use that does not work for a first-time user (for example, something they add that they then cannot see, input that should be rejected but is not, an action that does nothing, an error on load).
2. Then each expected line above that the code does not do yet, or does only in part.
Reply with the list only, or the single word {none}."""

FIX_PROMPT = """This project was built for this request: {goal}

A reviewer read the code and found these defects:
{defects}

Fix every defect above, the ones about the request's basic use first. Keep everything that already works. Do not add a server, network service, account, payment or personal-data feature unless the request asks for it. When you are done, check that every element, file, function and asset the running code uses exists, and run the project's tests if it has any."""

RECHECK_PROMPT = """A worker just changed an app an AI built for this request: {goal}

Read the code paths that run, as a strict reviewer. Do not modify any file. List only what stops the app from loading or its basic use from working for a first-time user, one per line starting with "- ", naming the file: an error on load or in the main loop, an element, file, function, asset or module the running code uses that does not exist or is not loaded, a control that does nothing. Do not list missing features.
Reply with the list only, or the single word {none}."""


def _expected_seed(seed_content: str) -> Seed | None:
    """The Seed when it carries expected experience, else ``None``."""
    try:
        parsed = yaml.safe_load(seed_content)
        seed = Seed.from_dict(parsed) if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001 - an unreadable Seed has nothing to complete against
        return None
    scope = seed.sourced_scope if seed is not None else None
    if scope is None or not scope.expected_experience:
        return None
    return seed


def _delegated_bounds(
    run_arguments: Mapping[str, Any] | None,
) -> tuple[str | None, frozenset[str] | None]:
    """The permission mode and tool set a delegating parent session allowed the run."""
    arguments = run_arguments or {}
    mode = arguments.get(DELEGATED_PARENT_PERMISSION_MODE_ARG)
    tools = arguments.get(DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG)
    allowed = (
        frozenset(t for t in tools if isinstance(t, str) and t) if isinstance(tools, list) else None
    )
    return (mode if isinstance(mode, str) and mode else None), (allowed or None)


def parse_defects(reply: str) -> tuple[str, ...]:
    """The reviewer's ``- `` lines, bounded; none for ``NONE`` or no list."""
    defects = [
        line.strip()[2:].strip()[:MAX_DEFECT_CHARS]
        for line in reply.splitlines()
        if line.strip().startswith("- ") and line.strip()[2:].strip()
    ]
    return tuple(defects[:MAX_DEFECTS])


def build_review_prompt(seed: Seed) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    expected = "\n".join(f"- {line}" for line in scope.expected_experience)
    return REVIEW_PROMPT.format(goal=scope.goal or seed.goal, expected=expected, none=NO_DEFECTS)


def build_recheck_prompt(seed: Seed) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    return RECHECK_PROMPT.format(goal=scope.goal or seed.goal, none=NO_DEFECTS)


def build_fix_prompt(seed: Seed, defects: tuple[str, ...]) -> str:
    scope = seed.sourced_scope
    assert scope is not None
    listed = "\n".join(f"- {defect}" for defect in defects)
    return FIX_PROMPT.format(goal=scope.goal or seed.goal, defects=listed)


async def _close(runtime: Any) -> None:
    closer = getattr(runtime, "aclose", None)
    if closer is not None and inspect.iscoroutinefunction(closer):
        await closer()


async def _session(
    factory: Callable[..., Any],
    prompt: str,
    *,
    tools: tuple[str, ...],
    timeout_seconds: float,
    **runtime_kwargs: Any,
) -> tuple[str | None, bool]:
    """Run one session: its final message (``None`` when it did not finish) and
    whether the failure was an error worth one more try."""
    from ouroboros.orchestrator.runtime_factory import preflight_agent_runtime

    try:
        runtime = await asyncio.to_thread(factory, **runtime_kwargs)
    except Exception as exc:  # noqa: BLE001 - best effort: no runtime, no pass
        log.warning("execution.completion_pass.runtime_failed", error=str(exc))
        return None, False
    try:
        if preflight_agent_runtime(runtime) is not None:
            return None, False
        result = await asyncio.wait_for(
            runtime.execute_task_to_result(prompt, tools=list(tools)),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        log.warning("execution.completion_pass.session_hung", timeout_seconds=timeout_seconds)
        return None, False
    except Exception as exc:  # noqa: BLE001 - a failed session ends the pass
        log.warning("execution.completion_pass.session_failed", error=str(exc))
        return None, True
    finally:
        await _close(runtime)
    if result.is_err:
        log.warning("execution.completion_pass.session_failed", error=str(result.error))
        return None, True
    return result.value.final_message or "", False


async def _stage(
    event_store: Any,
    execution_id: str | None,
    stage: str,
    factory: Callable[..., Any],
    prompt: str,
    *,
    tools: tuple[str, ...],
    timeout_seconds: float,
    data: Mapping[str, Any],
    **runtime_kwargs: Any,
) -> str | None:
    """Announce and run one stage's session, once more after an error."""
    for attempt in range(1, SESSION_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(RETRY_DELAY_SECONDS)
        await _announce(event_store, execution_id, stage, timeout_seconds, attempt=attempt, **data)
        reply, retryable = await _session(
            factory, prompt, tools=tools, timeout_seconds=timeout_seconds, **runtime_kwargs
        )
        if reply is not None or not retryable:
            return reply
    return None


async def _announce(
    event_store: Any, execution_id: str | None, stage: str, bound_seconds: float, **data: Any
) -> None:
    """Record a stage and the most it can take; best effort."""
    if event_store is None or not execution_id:
        return
    event = BaseEvent(
        type=STAGE_EVENT,
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={"stage": stage, "bound_seconds": bound_seconds, **data},
    )
    try:
        await event_store.append(event)
    except Exception as exc:  # noqa: BLE001 - the pass itself does not depend on the record
        log.warning("execution.completion_pass.announce_failed", stage=stage, error=str(exc))


_RECHECK_TEXT = {
    "clean": " A second review found nothing broken.",
    "repaired": " A second review found {n} broken part(s); a worker repaired them.",
    "repair_failed": " A second review found {n} broken part(s); the repair did not finish.",
    "recheck_failed": " The second review did not finish.",
}


def _with_record(run_result: MCPToolResult, record: dict[str, Any]) -> MCPToolResult:
    status, found = record["status"], f"a reviewer found {record['defects']} defect(s)"
    if status == "clean":
        text = "the reviewer found no defects."
    elif status == "review_failed":
        text = "review_failed; the product is as the run left it."
    else:
        text = f"{found}; " + (
            "a worker fixed them." if status == "fixed" else "the fix did not finish."
        )
        text += _RECHECK_TEXT[record["recheck"]].format(n=len(record.get("regressions", ())))
    text = f"\nCompletion pass: {text}\n"
    return MCPToolResult(
        content=(*run_result.content, MCPContentItem(type=ContentType.TEXT, text=text)),
        is_error=run_result.is_error,
        meta={**run_result.meta, "completion_pass": record},
        structured_content=run_result.structured_content,
    )


async def complete_after_run(
    run_result: MCPToolResult,
    *,
    seed_content: str,
    working_dir: Path,
    runtime_backend: str | None,
    opencode_mode: str | None = None,
    llm_backend: str | None = None,
    event_store: Any = None,
    execution_id: str | None = None,
    run_arguments: Mapping[str, Any] | None = None,
    runtime_factory: Callable[..., Any] | None = None,
    timeout_seconds: float = SESSION_TIMEOUT_SECONDS,
) -> MCPToolResult:
    """Review the finished product against its expected experience and fix what is missing."""
    if run_result.is_error or run_result.meta.get("status") != "completed":
        return run_result
    seed = _expected_seed(seed_content)
    if seed is None or not runtime_backend:
        return run_result
    from ouroboros.mcp.tools.subagent import should_dispatch_via_plugin

    if should_dispatch_via_plugin(runtime_backend, opencode_mode):
        return run_result
    parent_mode, parent_tools = _delegated_bounds(run_arguments)
    reviewer_tools = tuple(t for t in REVIEWER_TOOLS if parent_tools is None or t in parent_tools)
    worker_tools = tuple(t for t in WORKER_TOOLS if parent_tools is None or t in parent_tools)
    if not WRITING_TOOLS.intersection(worker_tools):
        return run_result
    from ouroboros.config.loader import resolve_execution_model
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    factory = runtime_factory or create_agent_runtime

    async def review(stage: str, prompt: str) -> str | None:
        return await _stage(
            event_store,
            execution_id,
            stage,
            factory,
            prompt,
            tools=reviewer_tools,
            timeout_seconds=timeout_seconds,
            data={},
            backend=runtime_backend,
            model=None,
            permission_mode=REVIEWER_PERMISSION_MODE,
            cwd=working_dir,
        )

    async def fix(stage: str, defects: tuple[str, ...]) -> str | None:
        return await _stage(
            event_store,
            execution_id,
            stage,
            factory,
            build_fix_prompt(seed, defects),
            tools=worker_tools,
            timeout_seconds=timeout_seconds,
            data={"defects": len(defects)},
            backend=runtime_backend,
            model=resolve_execution_model(runtime_backend),
            cwd=working_dir,
            llm_backend=llm_backend,
            startup_output_timeout_seconds=0,
            stdout_idle_timeout_seconds=0,
            **({"permission_mode": parent_mode} if parent_mode else {}),
        )

    reply = await review("review", build_review_prompt(seed))
    defects = parse_defects(reply) if reply is not None else ()
    record: dict[str, Any] = {"status": "review_failed", "defects": 0}
    if reply is not None:
        log.info("execution.completion_pass.reviewed", defects=len(defects), cwd=str(working_dir))
        record = {"status": "clean", "defects": 0}
    if defects:
        fixed = await fix("fix", defects)
        status = "fixed" if fixed is not None else "fix_failed"
        record = {"status": status, "defects": len(defects), "defect_list": list(defects)}
        # A worker that rewrote a file, or stopped part-way, can break what worked.
        recheck = await review("recheck", build_recheck_prompt(seed))
        regressions = parse_defects(recheck) if recheck is not None else ()
        record["recheck"] = "recheck_failed" if recheck is None else "clean"
        if regressions:
            repaired = await fix("repair", regressions)
            record["recheck"] = "repaired" if repaired is not None else "repair_failed"
            record["regressions"] = list(regressions)
        log.info(
            "execution.completion_pass.completed",
            status=status,
            defects=len(defects),
            recheck=record["recheck"],
            regressions=len(regressions),
        )
    await _announce(event_store, execution_id, "done", 0.0, status=record["status"])
    return _with_record(run_result, record)


__all__ = [
    "FIX_PROMPT",
    "NO_DEFECTS",
    "RECHECK_PROMPT",
    "REVIEW_PROMPT",
    "REVIEWER_PERMISSION_MODE",
    "REVIEWER_TOOLS",
    "STAGE_EVENT",
    "WORKER_TOOLS",
    "build_fix_prompt",
    "build_recheck_prompt",
    "build_review_prompt",
    "complete_after_run",
    "parse_defects",
]
