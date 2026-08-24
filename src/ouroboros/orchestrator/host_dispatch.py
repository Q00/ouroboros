"""Host-driven execution dispatch: the ``host`` agent runtime and its bridge.

The ``host`` backend inverts every other runtime in this package. A CLI
runtime spawns a worker process and streams its output; ``HostDispatchRuntime``
publishes a durable dispatch record and *waits* for the MCP host model (the
Claude/dsh/Codex chat session that called ``ouroboros_start_execute_seed``) to
spawn its own subagent, do the work in the project cwd, and hand the result
back through ``ouroboros_submit_fanout_results``. The seam is
``execute_task`` itself, so the executor, retries, decomposition and the
server-side verify gate above it are untouched and cannot tell the difference.

Host-agnostic on purpose: nothing here names a concrete host. The transport is
the same ``host_action=spawn_subagents`` payload contract every MCP host
already honors from ``render_mcp_server_instructions()`` — dsh is the first
consumer, not the design target.

Topology (one bridge per MCP server composition):

    execute_task ── park ──▶ HostDispatchBridge ◀── submit ── fanout handler
         │                        │    ▲
         │                        ▼    │
         │              FanoutRegistry (durable {fanout_id}.json)
         │                        │
         └── heartbeats ◀─────────┴──▶ job_wait/job_status meta
             (defeat the 900s stall scope while parked)

Constraints inherited from the execution engine (see HANDOFF-host-dispatch.md):

- The adapter instance is shared across ACs and retries; all dispatch state is
  keyed by ``dispatch_id`` in the bridge / registry, never on the runtime.
- A parked ``execute_task`` yields no runtime messages on its own, and the
  leaf stall scope (900s) resets only on yielded messages — so the wait loop
  yields synthetic heartbeat messages while parked.
- Retries are fresh dispatches (non-resumable handles); the previous attempt's
  record is poisoned on discard so a late submission reports ``stale_dispatch``
  instead of waking anything.
- The prompt handed to the host is the same assertion-free worker prompt every
  other runtime gets; ``verify_command``/``output_assertion`` never enter a
  payload (the server-side verify gate re-runs them).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
import time
from typing import Any

import structlog

from ouroboros.core.errors import ConfigError
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    ProviderError,
    RuntimeCapabilities,
    RuntimeHandle,
    SubagentOrchestration,
    TaskResult,
)

log = structlog.get_logger(__name__)

# Fan-out re-entry kind for execution dispatches. Defined here (not in
# ``mcp.tools.fanout``) so the mcp layer imports from the orchestrator — the
# existing dependency direction — and both sides speak one literal.
FANOUT_KIND_HOST_EXECUTION = "host_execution"

# One lane per dispatch: the host submits exactly one result entry whose key is
# this literal. Advisory fan-outs correlate many lanes; an execution dispatch
# is a single worker, so ONE literal serves as the lane key, the record's
# correlation key, and the advertised `result_correlation_key` — three values
# would be three ways for a compliant host to fail the fail-closed guards.
HOST_EXECUTION_RESULT_KEY = "result"
HOST_EXECUTION_CORRELATION_KEY = HOST_EXECUTION_RESULT_KEY

# Below the leaf stall threshold (900s) with margin: a parked dispatch must
# yield something often enough that silence is never mistaken for a stall.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 60.0
# How long a dispatch may sit unanswered before the attempt fails. Human-paced
# hosts are slow; machine timeouts above this layer assume machine-paced CLIs,
# so this deadline — not the stall scope — is the real "host went away" signal.
DEFAULT_DISPATCH_DEADLINE_SECONDS = 3600.0


class HostDispatchNotBoundError(RuntimeError):
    """``host`` runtime executed without a composed bridge.

    Raised instead of degrading: without a bridge there is no registry to park
    in and no waiter to wake, so the dispatch could never complete. The two
    legitimate compositions are the MCP server (which builds the bridge) and
    tests; a terminal ``ooo run`` has no host pumping ``job_wait`` and is
    rejected earlier with its own actionable message.
    """


@dataclass
class _PendingDispatch:
    """In-process waiter state for one parked dispatch."""

    dispatch_id: str
    session_id: str
    execution_id: str | None
    payload: dict[str, Any]
    created_at: float
    deadline_at: float | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set by ``submit`` before the event: the waiter reads the field, so a
    # cross-loop ``Event.set`` race degrades to one heartbeat of latency, not
    # a lost result.
    result: Any = None
    submitted: bool = False
    submitted_at: float | None = None
    expired: bool = False
    poisoned: bool = False
    # Actionable delivery is one-shot for a live dispatch. If the announcement
    # is lost, the attempt reaches its deadline and retry creates a fresh id;
    # replaying the same id could run two workers in one working directory.
    announced: bool = False


class HostDispatchBridge:
    """Connects parked ``execute_task`` calls to host result submissions.

    Composed once per MCP server, next to (not inside) ``SessionSignalHub``:
    the durable half is a ``FanoutRegistry`` kind (records survive restarts;
    resume re-issues fresh dispatches), the in-process half is per-dispatch
    waiters plus the pending view ``job_wait``/``job_status`` read.
    """

    def __init__(
        self,
        fanout_registry: Any,
        *,
        max_concurrent: int = 1,
    ) -> None:
        self._registry = fanout_registry
        self._pending: dict[str, _PendingDispatch] = {}
        # Concurrency lives here, not in an executor knob: parallel waves are a
        # bridge policy (P4 raises this), and callers waiting for a slot keep
        # heartbeating through the same loop as callers waiting for a result.
        self._slots = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent

    @property
    def slots(self) -> asyncio.Semaphore:
        return self._slots

    def park(
        self,
        *,
        session_id: str,
        execution_id: str | None,
        payload: dict[str, Any],
        deadline_at: float | None = None,
    ) -> str | None:
        """Durably register one dispatch and return its ``dispatch_id``.

        ``None`` means the registry write failed — no id was issued, so the
        caller fails the attempt honestly instead of waiting on a record no
        submission can redeem (the ``FanoutRegistry.register`` contract).

        ``session_id`` is mandatory. An empty session would bind nothing: the
        submit-side session guard only enforces non-empty record fields, and a
        sessionless pending entry would surface in every job's poll — both
        fail-open. Composition paths that cannot scope a session (evolve,
        validation) must not dispatch through this bridge yet.
        """
        if not session_id:
            raise ValueError(
                "host dispatch requires a bound session_id; refusing a "
                "sessionless record that no submit guard could enforce"
            )
        dispatch_id = self._registry.register(
            kind=FANOUT_KIND_HOST_EXECUTION,
            session_id=session_id,
            correlation_key=HOST_EXECUTION_CORRELATION_KEY,
            expected_keys=[HOST_EXECUTION_RESULT_KEY],
            # The payload is persisted verbatim; it carries the assertion-free
            # worker prompt and nothing else about the grading contract.
            synthesizer_input={"dispatch": dict(payload)},
        )
        if dispatch_id is None:
            return None
        self._pending[dispatch_id] = _PendingDispatch(
            dispatch_id=dispatch_id,
            session_id=session_id,
            execution_id=execution_id,
            payload=dict(payload),
            created_at=time.monotonic(),
            deadline_at=deadline_at,
        )
        log.info(
            "host_dispatch.parked",
            dispatch_id=dispatch_id,
            session_id=session_id,
            execution_id=execution_id,
        )
        return dispatch_id

    def submit(
        self,
        dispatch_id: str,
        provided: Mapping[str, Any],
        *,
        undispatched: bool = False,
    ) -> dict[str, Any]:
        """Deliver a validated host submission to its parked waiter.

        Correlation/session guards already ran in ``prepare_fanout_results``;
        this is the liveness half: a dispatch that is unknown, already
        consumed, or poisoned by a newer attempt reports ``stale_dispatch``
        rather than waking anything — a late submission for attempt N must
        never land after attempt N+1 started.

        ``undispatched=True`` means the shared fan-out contract's declaration
        that the host never spawned a child for this lane
        (``{"key": "result", "undispatched": true}``) was accepted upstream.
        That is a terminal execution failure, not an empty success.

        Malformed or absence-equivalent content remains retryable: it does not
        consume the live dispatch. Only a substantive string/object or an
        explicit ``{"success": false}`` failure may wake the execution engine.
        """
        pending = self._pending.get(dispatch_id)
        if pending is None or pending.poisoned or pending.submitted:
            return {
                "status": "stale_dispatch",
                "fanout_id": dispatch_id,
                "kind": FANOUT_KIND_HOST_EXECUTION,
                "error": (
                    "No live waiter holds this execution dispatch: it already "
                    "completed, was superseded by a retry, or the run that "
                    "issued it ended or restarted. The engine did NOT receive "
                    "this result. Do not retry this dispatch; keep pumping "
                    "ouroboros_job_wait and act on the dispatches it lists."
                ),
            }

        submitted_at = time.monotonic()
        if pending.deadline_at is not None and submitted_at >= pending.deadline_at:
            pending.expired = True
            pending.event.set()
            return {
                "status": "deadline_exceeded",
                "fanout_id": dispatch_id,
                "kind": FANOUT_KIND_HOST_EXECUTION,
                "error": (
                    "The host result arrived after this dispatch's acceptance "
                    "deadline. The engine did NOT receive it; keep pumping "
                    "ouroboros_job_wait for the terminal job state or a fresh dispatch."
                ),
            }

        if undispatched:
            pending.result = {
                "success": False,
                "final_message": (
                    f"Host reported dispatch {dispatch_id} as undispatched: "
                    "no subagent was ever spawned for it, so no work ran."
                ),
            }
        else:
            if HOST_EXECUTION_RESULT_KEY not in provided:
                return _invalid_submission_response(
                    dispatch_id,
                    "The required execution result lane was absent.",
                )
            submitted_result = provided[HOST_EXECUTION_RESULT_KEY]
            validation_error = _host_submission_validation_error(submitted_result)
            if validation_error is not None:
                return _invalid_submission_response(dispatch_id, validation_error)
            pending.result = submitted_result

        pending.submitted = True
        pending.submitted_at = submitted_at
        pending.event.set()
        log.info("host_dispatch.submitted", dispatch_id=dispatch_id)
        return {
            "status": "complete",
            "fanout_id": dispatch_id,
            "kind": FANOUT_KIND_HOST_EXECUTION,
            "detail": (
                "Result delivered to the execution engine. Keep pumping "
                "ouroboros_job_wait for verification progress and further "
                "pending_host_dispatches."
            ),
        }

    def discard(self, dispatch_id: str) -> None:
        """Poison and drop one dispatch (attempt finished, failed, or retried).

        Wakes the waiter too: a parked ``execute_task`` re-reads liveness on
        every wake, so poisoning without a wake would leave it heartbeating
        until its deadline instead of failing fast.
        """
        pending = self._pending.pop(dispatch_id, None)
        if pending is not None:
            pending.poisoned = True
            pending.event.set()

    def pending_for_session(
        self,
        session_id: str | None,
        *,
        announce: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the open dispatches correlated to ``session_id``.

        ``announce=True`` is the active ``job_wait`` delivery path: it claims
        each dispatch exactly once for its whole live attempt. ``announce=False``
        is the read-only ``job_status`` observation path and never consumes
        that actionable announcement.
        """
        if not session_id:
            return []
        now = time.monotonic()
        rows = []
        for pending in self._pending.values():
            if (
                not pending.submitted
                and not pending.poisoned
                and not pending.expired
                and pending.deadline_at is not None
                and now >= pending.deadline_at
            ):
                # Deadline enforcement belongs at the announcement boundary,
                # not only in the parked waiter or submitter.  A host poll can
                # be the first observer after expiry; mark and wake the waiter
                # before deciding whether the dispatch is actionable.
                pending.expired = True
                pending.event.set()
            if (
                not pending.submitted
                and not pending.poisoned
                and not pending.expired
                and pending.session_id == session_id
                and (not announce or not pending.announced)
            ):
                rows.append(pending)
        rows.sort(key=lambda pending: pending.created_at)
        if announce:
            for pending in rows:
                pending.announced = True
        return [
            {
                "dispatch_id": pending.dispatch_id,
                "fanout_id": pending.dispatch_id,
                "host_action": "spawn_subagents",
                "dispatch_mode": "host_driven",
                "result_correlation_key": HOST_EXECUTION_RESULT_KEY,
                "submit_tool": "ouroboros_submit_fanout_results",
                "session_id": pending.session_id,
                "actionable": announce,
                "subagents": [pending.payload],
            }
            for pending in rows
        ]


def bind_host_dispatch_bridge(
    adapter: Any,
    bridge: HostDispatchBridge | None,
    *,
    session_id: str | None = None,
    execution_id: str | None = None,
) -> None:
    """Attach the composed bridge (and dispatch scope) to a ``host`` runtime.

    A no-op for every other runtime, so composition roots and handlers call it
    unconditionally after ``create_agent_runtime`` instead of branching on the
    backend name.
    """
    if bridge is None or not isinstance(adapter, HostDispatchRuntime):
        return
    adapter.bind_bridge(bridge)
    adapter.bind_dispatch_scope(session_id=session_id, execution_id=execution_id)


def compose_host_dispatch_bridge(adapter: Any, fanout_registry: Any) -> HostDispatchBridge:
    """Build and bind the server-owned bridge for a composed runtime."""
    bridge = HostDispatchBridge(fanout_registry)
    bind_host_dispatch_bridge(adapter, bridge)
    return bridge


def reject_host_runtime_for_evolve(backend: str, *, phase: str) -> None:
    """Fail fast when ``host`` is selected for evolve/Ralph execution.

    Evolve/Ralph jobs have no per-session dispatch scope: ``bind_dispatch_scope``
    requires a bound ``session_id``, and ``EvolutionaryLoop`` carries no session
    identity through its executor callback. Binding without scope would park a
    dispatch that can never be discovered or submitted against — it would die
    later with ``host_dispatch_scope_missing`` instead of failing here.
    """
    if backend != "host":
        return
    raise ConfigError(
        f"The 'host' agent runtime is not supported for evolve/Ralph {phase}: "
        "these jobs have no per-session dispatch scope for the host "
        "bridge to correlate submissions against. Use an executable "
        "runtime (claude-cli, codex, opencode, ...) for evolve/ralph."
    )


class HostDispatchRuntime:
    """``AgentRuntime`` that delegates execution to the calling MCP host model.

    Stateless across calls by construction: per-dispatch state lives in the
    bridge keyed by ``dispatch_id``; the instance holds only configuration and
    the job-scoped identity stamped by ``bind_dispatch_scope``. Handles are
    non-resumable — a retry is a fresh dispatch.
    """

    def __init__(
        self,
        *,
        bridge: HostDispatchBridge | None = None,
        cwd: Any = None,
        permission_mode: str | None = None,
        llm_backend: str | None = None,
        model: str | None = None,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        dispatch_deadline_seconds: float = DEFAULT_DISPATCH_DEADLINE_SECONDS,
    ) -> None:
        self._bridge = bridge
        # ``cwd`` may arrive as the factory's ResolvedWorkerCwd wrapper; unwrap
        # to the canonical path string — ``str(wrapper)`` would be its repr,
        # which the executor's cwd-ownership gate rightly rejects.
        raw_cwd = getattr(cwd, "value", cwd)
        self._cwd = str(raw_cwd) if raw_cwd is not None else None
        self._permission_mode = permission_mode
        self._llm_backend = llm_backend
        self._model = model
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._dispatch_deadline_seconds = dispatch_deadline_seconds
        self._session_id: str | None = None
        self._execution_id: str | None = None

    # -- identity / capability surface -------------------------------------

    @property
    def runtime_backend(self) -> str:
        return "host"

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            skill_dispatch=False,
            targeted_resume=False,
            structured_output=False,
            # The system prompt is folded into the single subagent prompt —
            # honored, but not in the form it was supplied.
            system_prompt_support=ParamSupport.TRANSLATED,
            tool_restriction_support=ParamSupport.IGNORED,
            permission_mode_support=ParamSupport.IGNORED,
            # Landmine: this runtime must never join the passive-bridge or
            # leader-driven orchestration sets; the host spawns, we wait.
            subagent_orchestration=SubagentOrchestration.NONE,
        )

    @property
    def llm_backend(self) -> str | None:
        return self._llm_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    # -- late binding -------------------------------------------------------

    def bind_bridge(self, bridge: HostDispatchBridge) -> None:
        self._bridge = bridge

    def bind_dispatch_scope(
        self,
        *,
        session_id: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        """Stamp the job identity dispatch records correlate under.

        Job-scoped, not call-scoped: the adapter is one-per-job, so the scope
        set at job start holds for every AC and retry the job dispatches.
        """
        if session_id:
            self._session_id = session_id
        if execution_id:
            self._execution_id = execution_id

    # -- execution ----------------------------------------------------------

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        **_ignored: Any,
    ) -> AsyncIterator[AgentMessage]:
        """Publish one dispatch and stream heartbeats until the host submits."""
        bridge = self._bridge
        if bridge is None:
            raise HostDispatchNotBoundError(
                "The 'host' agent runtime dispatches execution to the calling "
                "MCP host and requires a composed HostDispatchBridge. It is "
                "only usable through the Ouroboros MCP server "
                "(ouroboros_start_execute_seed from a host chat session)."
            )

        if not self._session_id:
            # A sessionless dispatch would bind no submit guard and surface in
            # no job poll; fail the attempt instead of parking it invisibly.
            yield AgentMessage(
                type="result",
                content=(
                    "Host dispatch requires a bound session scope "
                    "(bind_dispatch_scope was never called with a session_id); "
                    "this composition path cannot dispatch to a host yet."
                ),
                data={"subtype": "error", "error_code": "host_dispatch_scope_missing"},
            )
            return

        payload = self._build_payload(prompt=prompt, system_prompt=system_prompt)
        deadline = time.monotonic() + self._dispatch_deadline_seconds

        # Wait for a concurrency slot with the same heartbeat cadence as the
        # result wait: a call queued behind the semaphore is just as silent as
        # a parked one, and the stall scope cannot tell them apart. ONE acquire
        # task is created and observed across heartbeats — re-issuing
        # ``acquire()`` per tick would re-queue this waiter behind newcomers
        # every 60s and risk discarding a granted permit on timeout.
        acquire = asyncio.ensure_future(bridge.slots.acquire())
        acquired = False
        try:
            while not acquire.done():
                await asyncio.wait({acquire}, timeout=self._heartbeat_interval_seconds)
                if acquire.done():
                    break
                if time.monotonic() >= deadline:
                    yield self._deadline_result("queued")
                    return
                yield self._heartbeat("queued behind earlier host dispatches")
            await acquire  # propagate acquire errors, if any
            acquired = True
        finally:
            if not acquired:
                # Cancelled or deadline-failed while queued: return the permit
                # if the grant raced our exit, otherwise just drop the waiter.
                if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                    bridge.slots.release()
                else:
                    acquire.cancel()

        dispatch_id: str | None = None
        try:
            dispatch_id = bridge.park(
                session_id=self._session_id,
                execution_id=self._execution_id,
                payload=payload,
                deadline_at=deadline,
            )
            if dispatch_id is None:
                yield AgentMessage(
                    type="result",
                    content=(
                        "Host dispatch registration failed: the fan-out "
                        "registry could not persist the dispatch record."
                    ),
                    data={"subtype": "error", "error_code": "host_dispatch_register_failed"},
                )
                return

            yield self._heartbeat(f"dispatch {dispatch_id} published; awaiting host", dispatch_id)

            while True:
                # Re-read liveness every wake: ``discard`` (cancellation, a
                # retry superseding this attempt) pops the entry and wakes the
                # event, and a stale local reference would park until deadline.
                pending = bridge._pending.get(dispatch_id)
                if pending is None or pending.poisoned:
                    yield AgentMessage(
                        type="result",
                        content="Host dispatch was cancelled before a result arrived.",
                        data={"subtype": "error", "error_code": "host_dispatch_cancelled"},
                    )
                    return
                if pending.expired:
                    yield self._deadline_result(dispatch_id)
                    return
                if pending.submitted:
                    break
                if time.monotonic() >= deadline:
                    pending.expired = True
                    yield self._deadline_result(dispatch_id)
                    return
                try:
                    await asyncio.wait_for(
                        pending.event.wait(),
                        timeout=self._heartbeat_interval_seconds,
                    )
                except TimeoutError:
                    yield self._heartbeat(
                        f"dispatch {dispatch_id} still awaiting host submission",
                        dispatch_id,
                    )

            content, success = _read_submission(pending.result)
            if success:
                yield AgentMessage(type="assistant", content=content)
                yield AgentMessage(
                    type="result",
                    content=content,
                    data={"subtype": "success", "dispatch_id": dispatch_id},
                )
            else:
                # Host-reported failure: keep the host's own words OUT of the
                # terminal message content. Error results grow a runtime shape
                # downstream, and recovery classifiers pattern-match content —
                # a host string like "usage limit reached" must not be able to
                # steer the engine into its quota-pause path.
                yield AgentMessage(
                    type="assistant",
                    content=f"Host reported failure for dispatch {dispatch_id}.",
                    data={"host_report": content},
                )
                yield AgentMessage(
                    type="result",
                    content=f"Host reported failure for dispatch {dispatch_id}.",
                    data={
                        "subtype": "error",
                        "error_code": "host_dispatch_reported_failure",
                        "dispatch_id": dispatch_id,
                        "host_report": content,
                    },
                )
        finally:
            if dispatch_id is not None:
                bridge.discard(dispatch_id)
            bridge.slots.release()

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        **kwargs: Any,
    ) -> Result[TaskResult, ProviderError]:
        """Collect the dispatch stream into a single ``TaskResult``."""
        messages: list[AgentMessage] = []
        try:
            async for message in self.execute_task(
                prompt,
                tools=tools,
                system_prompt=system_prompt,
                resume_handle=resume_handle,
                resume_session_id=resume_session_id,
                **kwargs,
            ):
                messages.append(message)
        except HostDispatchNotBoundError as exc:
            return Result.err(ProviderError(message=str(exc), provider="host"))
        final = messages[-1] if messages else None
        if final is None or not final.is_final:
            return Result.err(
                ProviderError(
                    message="host dispatch ended without a terminal result message",
                    provider="host",
                )
            )
        return Result.ok(
            TaskResult(
                success=not final.is_error,
                final_message=final.content,
                messages=tuple(messages),
            )
        )

    # -- helpers ------------------------------------------------------------

    def _build_payload(self, *, prompt: str, system_prompt: str | None) -> dict[str, Any]:
        """Build the host-facing subagent payload (SubagentPayload shape).

        The grading contract never enters this dict: the prompt is the same
        assertion-free worker prompt every runtime receives, and ``context``
        carries only correlation identity — no seed, no ``ac_spec``.
        """
        combined = prompt
        if system_prompt:
            combined = f"{system_prompt.rstrip()}\n\n{prompt}"
        if self._cwd:
            # Imperative and inside the prompt itself: the engine may have
            # prepared a task worktree, and the verify gate re-runs there. A
            # host subagent that works in the user's project tree instead
            # would fail every artifact check while dirtying the real repo.
            combined = (
                f"WORKING DIRECTORY (mandatory): do ALL work inside "
                f"`{self._cwd}` — create, edit, and run everything there, "
                f"not in any other checkout of this project.\n\n{combined}"
            )
        return {
            "tool_name": "ouroboros_execute_seed",
            "title": "Ouroboros execution worker",
            "agent": "general",
            "prompt": combined,
            "model": self._model,
            "context": {
                "kind": FANOUT_KIND_HOST_EXECUTION,
                "working_directory": self._cwd,
                "session_id": self._session_id,
            },
            "timeout": None,
        }

    def _heartbeat(self, detail: str, dispatch_id: str | None = None) -> AgentMessage:
        return AgentMessage(
            type="system",
            content=f"[host-dispatch] {detail}",
            data={
                "subtype": "host_dispatch_heartbeat",
                **({"dispatch_id": dispatch_id} if dispatch_id else {}),
            },
        )

    def _deadline_result(self, dispatch_id: str) -> AgentMessage:
        return AgentMessage(
            type="result",
            content=(
                f"Host dispatch {dispatch_id} exceeded its "
                f"{int(self._dispatch_deadline_seconds)}s deadline without a "
                "host submission."
            ),
            data={"subtype": "error", "error_code": "host_dispatch_deadline_exceeded"},
        )


def _invalid_submission_response(dispatch_id: str, error: str) -> dict[str, Any]:
    return {
        "status": "invalid_result_entry",
        "fanout_id": dispatch_id,
        "kind": FANOUT_KIND_HOST_EXECUTION,
        "error": error,
        "retry_contract": (
            "The live dispatch was not consumed. Retry the same fanout_id with "
            "a non-empty worker result, or declare the lane undispatched if no child ran."
        ),
    }


def _host_submission_validation_error(result: Any) -> str | None:
    """Return a fail-closed validation error for unusable host output."""
    if isinstance(result, str):
        return None if result.strip() else "Host execution content must not be empty."
    if not isinstance(result, Mapping):
        return "Host execution content must be a non-empty string or object."
    if not result:
        return "Host execution content must not be an empty object."
    if "success" in result and not isinstance(result["success"], bool):
        return "Host execution content.success must be a boolean when provided."
    if result.get("success") is False:
        return None
    substantive = any(
        key != "success"
        and value is not None
        and (not isinstance(value, str) or bool(value.strip()))
        and (not isinstance(value, (Mapping, list, tuple)) or bool(value))
        for key, value in result.items()
    )
    if not substantive:
        return "Successful host execution content must include substantive output."
    return None


def _read_submission(result: Any) -> tuple[str, bool]:
    """Normalize one already-validated host result into message and success."""
    validation_error = _host_submission_validation_error(result)
    if validation_error is not None:
        return "Host dispatch returned an invalid result.", False
    if isinstance(result, Mapping):
        success = result.get("success") is not False
        for key in ("final_message", "content", "summary", "output"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value, success
        import json

        return json.dumps(dict(result), ensure_ascii=False, sort_keys=True), success
    return result, True


__all__ = [
    "DEFAULT_DISPATCH_DEADLINE_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "FANOUT_KIND_HOST_EXECUTION",
    "HOST_EXECUTION_CORRELATION_KEY",
    "HOST_EXECUTION_RESULT_KEY",
    "HostDispatchBridge",
    "HostDispatchNotBoundError",
    "HostDispatchRuntime",
    "bind_host_dispatch_bridge",
    "compose_host_dispatch_bridge",
]
