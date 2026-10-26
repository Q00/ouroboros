"""The candidate step: write k candidate solutions, run their probes, build cards.

Opt-in (``examples.enabled``, off by default); nothing on the ``ooo auto``
path calls it yet. One call of ``run_candidate_step``:

1. States readings of the goal with one model call (``examples.readings``).
   With no reading kept, candidates get the goal alone: pair diversity still
   applies.
2. Plans k candidate slots over the (backend, model) pairs that pass their
   runtime preflight (``plan_candidates``): one slot per pair first, then the
   next unused reading on the next pair; a reading is reused only when every
   reading is in use, and then on a pair that has not had it yet when one
   exists. Candidate ids are opaque (``cand-1`` ...); the pair and the reading
   behind each id are kept in the result and the ``examples.candidate.*``
   events only.
3. Gives each slot its own isolated git worktree at one pinned base commit
   (``orchestrator.n_version_tournament.RunWorktreeManager``) and runs one
   agent runtime there (``create_agent_runtime(backend=..., model=...)``)
   with the worker's permission mode. At most ``examples.parallel`` run at
   once. Each has a wall-clock cap (``candidate_timeout``, default 600 s);
   the whole step has one too (``step_timeout``, default 900 s, readings call
   and probe runs included). A candidate still running at its cap is
   cancelled and skipped. The base tree for the probes is one more clean
   worktree at the same commit.
4. Reads at most ``MAX_PROBES_PER_CANDIDATE`` probes each finished candidate
   wrote into ``PROBE_DIR`` (``<name>.py``: a ``call``; ``<name>.json`` with
   ``argv`` and ``stdin``: a ``command``), then removes that directory, so
   probes are neither part of the candidate's tree nor of its patch digest.
5. With at least two finished candidates and at least one probe, runs the
   pooled probes on the base and on every finished candidate under the
   step's deadline (``examples.probe_runner.run_probe_pool``), finds where
   they disagree (``examples.disagreement``) and builds the ordered question
   cards (``examples.cards``), split into asked and recorded by
   ``examples.max_cards``. With fewer than two finished candidates nothing is
   run and no card is built.
6. Removes every worktree, on success, failure, timeout and cancellation.

A candidate is finished when its runtime returned a successful result before
its cap and its working-tree diff could be exported; the diff's sha256 is the
patch digest (``export_worktree_diff``: changes against the worktree ``HEAD``,
untracked files included). Cost is the runtime's reported ``total_cost_usd``
on its final message, when it reports one.

Nothing here reads a candidate's code to decide anything, no model judges an
output, and no pattern or keyword rule classifies anything. Known residuals,
as for every worker: a candidate runtime is confined by its own permission
mode and working directory, not by the controller, so reads outside its
worktree are not prevented; a cancelled runtime relies on its own
cancellation handling to stop its process (as the check constructor does).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import inspect
import json
import math
import os
from pathlib import Path
import shutil
import stat
import time
from typing import Any

from ouroboros.config.models import ExamplesConfig
from ouroboros.events.base import BaseEvent
from ouroboros.events.examples import (
    create_candidate_completed_event,
    create_candidate_skipped_event,
    create_candidate_started_event,
    create_readings_stated_event,
)
from ouroboros.examples.cards import QuestionCard, build_cards, select_cards
from ouroboros.examples.disagreement import DisagreementReport, find_disagreements
from ouroboros.examples.probe_runner import Probe, ProbeKind, ProbePoolRun, run_probe_pool
from ouroboros.examples.readings import Reading, ReadingsResult, state_readings
from ouroboros.examples.record import BASE_TREE_ID, sha256_hex
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import DEFAULT_TOOLS, AgentRuntime, TaskResult
from ouroboros.orchestrator.n_version_tournament import (
    RunWorktreeManager,
    export_worktree_diff,
)
from ouroboros.orchestrator.runtime_factory import (
    create_agent_runtime,
    preflight_agent_runtime,
)
from ouroboros.providers.base import CompletionConfig, LLMAdapter

log = get_logger(__name__)

DEFAULT_CANDIDATE_TIMEOUT_SECONDS = 600.0
DEFAULT_STEP_TIMEOUT_SECONDS = 900.0
MAX_CANDIDATES = 8
MAX_PROBES_PER_CANDIDATE = 6
MAX_PROBE_FILE_BYTES = 64 * 1024
PROBE_DIR = ".ouroboros-example-probes"
DEFAULT_PAIR_ALLOWLIST: tuple[str, ...] = ("codex", "claude")
"""Runtimes added after the execute-stage pair when ``examples.candidates`` is empty."""

_CALL_SUFFIX = ".py"
_COMMAND_SUFFIX = ".json"

RuntimeFactory = Callable[..., AgentRuntime]
ProbePoolRunner = Callable[..., Awaitable[ProbePoolRun]]
EventSink = Callable[[BaseEvent], Awaitable[None]]


class SkipReason(StrEnum):
    """Why a pair or a candidate was not used."""

    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    PREFLIGHT_FAILED = "preflight_failed"
    WORKTREE_FAILED = "worktree_failed"
    TIMED_OUT = "timed_out"
    STEP_DEADLINE = "step_deadline"
    RUNTIME_FAILED = "runtime_failed"
    PATCH_EXPORT_FAILED = "patch_export_failed"


@dataclass(frozen=True, slots=True)
class CandidatePair:
    """A runtime backend and the model it runs (``None``: the runtime's default)."""

    backend: str
    model: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateSlot:
    """One planned candidate: an opaque id, its pair, and its reading (or none)."""

    candidate_id: str
    pair: CandidatePair
    reading: Reading | None


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """What one candidate did. ``skipped`` is ``None`` when it finished."""

    slot: CandidateSlot
    skipped: SkipReason | None
    wall_clock_seconds: float | None = None
    cost_usd: float | None = None
    patch_digest: str | None = None
    probes: tuple[Probe, ...] = ()
    probes_refused: int = 0
    detail: str = ""

    @property
    def finished(self) -> bool:
        return self.skipped is None


@dataclass(frozen=True, slots=True)
class SkippedPair:
    """A pair left out before planning, and why."""

    pair: CandidatePair
    reason: SkipReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CandidateStepResult:
    """Everything the step did. ``enabled`` is ``False`` when the flag is off."""

    enabled: bool
    failure: str | None = None
    readings: ReadingsResult | None = None
    skipped_pairs: tuple[SkippedPair, ...] = ()
    outcomes: tuple[CandidateOutcome, ...] = ()
    pool: ProbePoolRun | None = None
    report: DisagreementReport | None = None
    asked: tuple[QuestionCard, ...] = ()
    recorded: tuple[QuestionCard, ...] = ()
    events: tuple[BaseEvent, ...] = field(default=(), repr=False)

    @property
    def finished(self) -> tuple[CandidateOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.finished)


def resolve_pairs(
    config: ExamplesConfig,
    *,
    execute_backend: str,
    execute_model: str | None,
    allowlist: Sequence[str] = DEFAULT_PAIR_ALLOWLIST,
) -> tuple[CandidatePair, ...]:
    """The configured pairs, or the default list: the execute pair, then the allowlist.

    Duplicate pairs are kept once, first occurrence first.
    """
    if config.candidates:
        pairs = [CandidatePair(item.backend, item.model) for item in config.candidates]
    else:
        pairs = [CandidatePair(execute_backend, execute_model)]
        pairs += [CandidatePair(name) for name in allowlist if name != execute_backend]
    unique: list[CandidatePair] = []
    for pair in pairs:
        if pair not in unique:
            unique.append(pair)
    return tuple(unique)


def plan_candidates(
    pairs: Sequence[CandidatePair], readings: Sequence[Reading], k: int
) -> tuple[CandidateSlot, ...]:
    """Assign k slots: pairs round-robin; each slot takes the next unused reading.

    When every reading is in use, a slot reuses the first reading (in order)
    its pair has not had yet, else the reading its pair has had least often
    (first in order on a tie). With no reading every slot gets ``None``.
    Deterministic.
    """
    if not pairs:
        return ()
    if not 1 <= k <= MAX_CANDIDATES:
        raise ValueError(f"k must be between 1 and {MAX_CANDIDATES}")
    used: dict[tuple[CandidatePair, str], int] = {}
    slots: list[CandidateSlot] = []
    for index in range(k):
        pair = pairs[index % len(pairs)]
        reading: Reading | None = None
        if readings:
            if index < len(readings):
                reading = readings[index]
            else:
                reading = min(
                    readings,
                    key=lambda item: (used.get((pair, item.reading_id), 0), readings.index(item)),
                )
            key = (pair, reading.reading_id)
            used[key] = used.get(key, 0) + 1
        slots.append(CandidateSlot(f"cand-{index + 1}", pair, reading))
    return tuple(slots)


def candidate_prompt(goal: str, reading: Reading | None) -> str:
    """The candidate's task: the goal, its one reading, and the probe instructions."""
    if reading is None:
        stated = "No reading was stated. Implement the goal as you understand it."
    else:
        stated = (
            f'The goal says: "{reading.span}"\nRead it this way and only this way: {reading.text}'
        )
    return (
        "Write one candidate solution for the goal below, under the stated reading.\n\n"
        f"Goal:\n{goal}\n\nReading:\n{stated}\n\n"
        "Steps:\n"
        "1. Change the code in the current directory to implement the goal under this "
        "reading. Do not commit.\n"
        f"2. Then write at most {MAX_PROBES_PER_CANDIDATE} probes into the directory "
        f"`{PROBE_DIR}/` at the repository root. A probe shows what your change does on "
        "one input a reviewer would ask about. A probe holds inputs only: never write an "
        "expected value or an assertion.\n"
        "   - A call probe is a file `<name>.py` that defines `probe()` with no arguments. "
        "It imports the project's code, calls it with concrete inputs, and returns the "
        "result. The repository root and its `src` directory are on `sys.path`.\n"
        '   - A command probe is a file `<name>.json` holding {"argv": [...], "stdin": '
        '"..."}. It runs without a shell from the repository root.\n'
        "Work only inside the current directory."
    )


def _read_probe(path: Path) -> Probe | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROBE_FILE_BYTES:
            return None
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        if path.suffix == _CALL_SUFFIX:
            return Probe(ProbeKind.CALL, source=text)
        if path.suffix == _COMMAND_SUFFIX:
            data = json.loads(text)
            if not isinstance(data, dict) or not set(data) <= {"argv", "stdin"}:
                return None
            argv, stdin = data.get("argv"), data.get("stdin", "")
            if not isinstance(argv, list):
                return None
            return Probe(ProbeKind.COMMAND, argv=tuple(argv), stdin=stdin)
    except (ValueError, TypeError, RecursionError):
        return None
    return None


def take_probes(tree: Path) -> tuple[tuple[Probe, ...], int]:
    """Read the probes a candidate wrote into ``PROBE_DIR``, then remove the directory.

    Entries are read in name order; only regular files (no links) of at most
    ``MAX_PROBE_FILE_BYTES`` with a declared suffix count. The first
    ``MAX_PROBES_PER_CANDIDATE`` well-formed probes are kept; every other
    entry is counted as refused. Returns (probes, refused).
    """
    directory = tree / PROBE_DIR
    try:
        info = directory.lstat()
    except OSError:
        return (), 0
    if not stat.S_ISDIR(info.st_mode):
        directory.unlink(missing_ok=True)
        return (), 1
    probes: list[Probe] = []
    refused = 0
    try:
        for name in sorted(os.listdir(directory)):
            probe = (
                None if len(probes) >= MAX_PROBES_PER_CANDIDATE else _read_probe(directory / name)
            )
            if probe is None:
                refused += 1
            else:
                probes.append(probe)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return tuple(probes), refused


def reported_cost(result: TaskResult) -> float | None:
    """The ``total_cost_usd`` of the last message that reports one, if any."""
    for message in reversed(result.messages):
        value: Any = message.data.get("total_cost_usd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(value):
                return float(value)
    return None


@dataclass
class _Step:
    """Mutable state of one step run: the event log and the clock."""

    session_id: str
    sink: EventSink | None
    deadline: float
    events: list[BaseEvent] = field(default_factory=list)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    async def emit(self, event: BaseEvent) -> None:
        self.events.append(event)
        if self.sink is None:
            return
        try:
            await self.sink(event)
        except Exception as exc:  # noqa: BLE001 - an event store failure must not end the step
            log.warning("examples.event_emit_failed", event_type=event.type, error=str(exc))

    async def skipped(
        self,
        slot: CandidateSlot,
        reason: SkipReason,
        seconds: float | None = None,
        cost: float | None = None,
        detail: str = "",
    ) -> CandidateOutcome:
        await self.emit(
            create_candidate_skipped_event(
                self.session_id,
                candidate_id=slot.candidate_id,
                backend=slot.pair.backend,
                model=slot.pair.model,
                reading_id=slot.reading.reading_id if slot.reading else None,
                reason=reason.value,
                wall_clock_seconds=seconds,
                cost_usd=cost,
                detail=detail or None,
            )
        )
        return CandidateOutcome(slot, reason, seconds, cost, detail=detail)


async def _close(runtime: AgentRuntime) -> None:
    closer = getattr(runtime, "aclose", None)
    if closer is not None and inspect.iscoroutinefunction(closer):
        try:
            await closer()
        except Exception as exc:  # noqa: BLE001 - closing is best effort
            log.warning("examples.runtime_close_failed", error=str(exc))


async def _preflight_pairs(
    pairs: Sequence[CandidatePair], cwd: Path, runtime_factory: RuntimeFactory
) -> tuple[list[CandidatePair], list[SkippedPair]]:
    usable: list[CandidatePair] = []
    skipped: list[SkippedPair] = []
    for pair in pairs:
        try:
            runtime = await asyncio.to_thread(
                runtime_factory, backend=pair.backend, model=pair.model, cwd=cwd
            )
        except Exception as exc:  # noqa: BLE001 - a pair that cannot be built is skipped
            skipped.append(SkippedPair(pair, SkipReason.RUNTIME_UNAVAILABLE, type(exc).__name__))
            continue
        try:
            blocker = preflight_agent_runtime(runtime)
        finally:
            await _close(runtime)
        if blocker is not None:
            skipped.append(SkippedPair(pair, SkipReason.PREFLIGHT_FAILED, blocker))
        else:
            usable.append(pair)
    return usable, skipped


async def _run_candidate(
    step: _Step,
    slot: CandidateSlot,
    tree: Path,
    *,
    goal: str,
    runtime_factory: RuntimeFactory,
    candidate_timeout: float,
    gate: asyncio.Semaphore,
) -> CandidateOutcome:
    async with gate:
        budget = min(candidate_timeout, step.remaining())
        if budget <= 0:
            return await step.skipped(slot, SkipReason.STEP_DEADLINE)
        bound_by_step = budget < candidate_timeout
        try:
            runtime = await asyncio.to_thread(
                runtime_factory, backend=slot.pair.backend, model=slot.pair.model, cwd=tree
            )
        except Exception as exc:  # noqa: BLE001 - a runtime that cannot be built skips this slot
            return await step.skipped(
                slot, SkipReason.RUNTIME_UNAVAILABLE, detail=type(exc).__name__
            )
        reading_id = slot.reading.reading_id if slot.reading else None
        await step.emit(
            create_candidate_started_event(
                step.session_id,
                candidate_id=slot.candidate_id,
                backend=slot.pair.backend,
                model=slot.pair.model,
                reading_id=reading_id,
            )
        )
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                runtime.execute_task_to_result(
                    candidate_prompt(goal, slot.reading), tools=list(DEFAULT_TOOLS)
                ),
                timeout=budget,
            )
        except TimeoutError:
            reason = SkipReason.STEP_DEADLINE if bound_by_step else SkipReason.TIMED_OUT
            return await step.skipped(slot, reason, time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - a failed runtime skips this slot
            return await step.skipped(
                slot,
                SkipReason.RUNTIME_FAILED,
                time.monotonic() - started,
                detail=type(exc).__name__,
            )
        finally:
            await _close(runtime)
        seconds = time.monotonic() - started
    if result.is_err:
        detail = type(result.error).__name__
        return await step.skipped(slot, SkipReason.RUNTIME_FAILED, seconds, None, detail)
    cost = reported_cost(result.value)
    if not result.value.success:
        return await step.skipped(slot, SkipReason.RUNTIME_FAILED, seconds, cost, "unsuccessful")
    probes, refused = await asyncio.to_thread(take_probes, tree)
    diff = await asyncio.to_thread(export_worktree_diff, tree)
    if diff is None:
        return await step.skipped(slot, SkipReason.PATCH_EXPORT_FAILED, seconds, cost)
    outcome = CandidateOutcome(slot, None, seconds, cost, sha256_hex(diff), probes, refused)
    await step.emit(
        create_candidate_completed_event(
            step.session_id,
            candidate_id=slot.candidate_id,
            backend=slot.pair.backend,
            model=slot.pair.model,
            reading_id=reading_id,
            wall_clock_seconds=seconds,
            cost_usd=cost,
            patch_digest=outcome.patch_digest or "",
            probe_count=len(probes),
            probes_refused=refused,
        )
    )
    return outcome


async def _candidates_and_probes(
    step: _Step,
    manager: RunWorktreeManager,
    *,
    goal: str,
    readings: ReadingsResult,
    pairs: Sequence[CandidatePair],
    k: int,
    parallel: int,
    max_cards: int,
    runtime_factory: RuntimeFactory,
    probe_pool_runner: ProbePoolRunner,
    candidate_timeout: float,
) -> CandidateStepResult:
    try:
        commit = await asyncio.to_thread(manager.head_commit)
        base_tree = await asyncio.to_thread(manager.create, BASE_TREE_ID, ref=commit)
    except Exception as exc:  # noqa: BLE001 - no base tree means no step
        return CandidateStepResult(True, f"base_worktree_failed:{type(exc).__name__}", readings)
    usable, skipped_pairs = await _preflight_pairs(pairs, base_tree, runtime_factory)
    for item in skipped_pairs:
        await step.emit(
            create_candidate_skipped_event(
                step.session_id,
                candidate_id=None,
                backend=item.pair.backend,
                model=item.pair.model,
                reading_id=None,
                reason=item.reason.value,
                wall_clock_seconds=None,
                cost_usd=None,
                detail=item.detail or None,
            )
        )
    slots = plan_candidates(usable, readings.readings, k)
    outcomes: dict[str, CandidateOutcome] = {}
    trees: dict[str, Path] = {}
    for slot in slots:
        try:
            trees[slot.candidate_id] = await asyncio.to_thread(
                manager.create, slot.candidate_id, ref=commit
            )
        except Exception as exc:  # noqa: BLE001 - a slot without a worktree is skipped
            outcomes[slot.candidate_id] = await step.skipped(
                slot, SkipReason.WORKTREE_FAILED, detail=type(exc).__name__
            )
    gate = asyncio.Semaphore(parallel)
    ran = await asyncio.gather(
        *(
            _run_candidate(
                step,
                slot,
                trees[slot.candidate_id],
                goal=goal,
                runtime_factory=runtime_factory,
                candidate_timeout=candidate_timeout,
                gate=gate,
            )
            for slot in slots
            if slot.candidate_id in trees
        )
    )
    outcomes.update({outcome.slot.candidate_id: outcome for outcome in ran})
    ordered = tuple(outcomes[slot.candidate_id] for slot in slots)
    result = CandidateStepResult(
        True, None, readings, tuple(skipped_pairs), ordered, events=tuple(step.events)
    )
    finished = [outcome for outcome in ordered if outcome.finished]
    probes = [probe for outcome in finished for probe in outcome.probes]
    if len(finished) < 2 or not probes:
        return result
    pool_trees = {BASE_TREE_ID: base_tree}
    pool_trees.update(
        {outcome.slot.candidate_id: trees[outcome.slot.candidate_id] for outcome in finished}
    )
    pool = await probe_pool_runner(probes, pool_trees, deadline=step.deadline)
    report = find_disagreements(pool.records, [outcome.slot.candidate_id for outcome in finished])
    asked, recorded = select_cards(build_cards(report.disagreements), max_cards)
    return CandidateStepResult(
        True,
        None,
        readings,
        tuple(skipped_pairs),
        ordered,
        pool,
        report,
        asked,
        recorded,
        tuple(step.events),
    )


async def run_candidate_step(
    config: ExamplesConfig,
    *,
    workspace: Path,
    goal: str,
    repo_facts: Sequence[str],
    session_id: str,
    llm: LLMAdapter,
    readings_config: CompletionConfig,
    execute_backend: str,
    execute_model: str | None = None,
    event_sink: EventSink | None = None,
    runtime_factory: RuntimeFactory = create_agent_runtime,
    probe_pool_runner: ProbePoolRunner = run_probe_pool,
    candidate_timeout: float = DEFAULT_CANDIDATE_TIMEOUT_SECONDS,
    step_timeout: float = DEFAULT_STEP_TIMEOUT_SECONDS,
) -> CandidateStepResult:
    """Run the candidate step on ``workspace`` (a git checkout); see the module docstring.

    Returns ``CandidateStepResult(enabled=False)`` at once, calling nothing,
    when ``config.enabled`` is off. ``event_sink`` receives every event as it
    is made (an append to the event store); a failing sink is logged and the
    step goes on. The result carries the same events.
    """
    if not config.enabled:
        return CandidateStepResult(False)
    if candidate_timeout <= 0 or step_timeout <= 0:
        raise ValueError("timeouts must be positive")
    step = _Step(session_id, event_sink, time.monotonic() + step_timeout)
    try:
        readings = await asyncio.wait_for(
            state_readings(
                llm,
                readings_config,
                goal=goal,
                repo_facts=repo_facts,
                max_readings=config.k,
            ),
            timeout=max(step.remaining(), 0.0),
        )
    except TimeoutError:
        readings = ReadingsResult((), 0, "call_timed_out")
    await step.emit(
        create_readings_stated_event(
            session_id,
            readings=[reading.as_event_data() for reading in readings.readings],
            dropped=readings.dropped,
            call_failed=readings.call_failed,
        )
    )
    pairs = resolve_pairs(config, execute_backend=execute_backend, execute_model=execute_model)
    try:
        manager = await asyncio.to_thread(RunWorktreeManager, workspace)
    except Exception as exc:  # noqa: BLE001 - no worktree root means no step
        return CandidateStepResult(
            True, f"worktree_root_failed:{type(exc).__name__}", readings, events=tuple(step.events)
        )
    try:
        result = await _candidates_and_probes(
            step,
            manager,
            goal=goal,
            readings=readings,
            pairs=pairs,
            k=config.k,
            parallel=config.parallel,
            max_cards=config.max_cards,
            runtime_factory=runtime_factory,
            probe_pool_runner=probe_pool_runner,
            candidate_timeout=candidate_timeout,
        )
    finally:
        # Shielded: a cancelled step still removes every worktree it made.
        await asyncio.shield(asyncio.to_thread(manager.cleanup_all))
    if result.failure is not None:
        return CandidateStepResult(True, result.failure, readings, events=tuple(step.events))
    return result


__all__ = [
    "DEFAULT_CANDIDATE_TIMEOUT_SECONDS",
    "DEFAULT_PAIR_ALLOWLIST",
    "DEFAULT_STEP_TIMEOUT_SECONDS",
    "MAX_PROBES_PER_CANDIDATE",
    "MAX_PROBE_FILE_BYTES",
    "PROBE_DIR",
    "CandidateOutcome",
    "CandidatePair",
    "CandidateSlot",
    "CandidateStepResult",
    "SkipReason",
    "SkippedPair",
    "candidate_prompt",
    "plan_candidates",
    "reported_cost",
    "resolve_pairs",
    "run_candidate_step",
    "take_probes",
]
