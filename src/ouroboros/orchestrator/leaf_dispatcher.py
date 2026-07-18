"""Runtime dispatch + streaming/heartbeat consumption for an atomic leaf.

Extracted verbatim from ``ParallelACExecutor._execute_atomic_ac`` (work order
R4). This module owns the stall-scoped runtime dispatch and the per-message
streaming loop: the resettable stall ``CancelScope``, runtime-handle threading,
recovery/lifecycle event emission, heartbeat emission, projected-message
persistence, and tool/thinking event emission.

Stall/heartbeat timing is subtle, so the extraction is a pure structural move:
every await point, deadline reset, exception path, and event emission stays in
exactly the same relative order it had inline. The mutable loop state
(``messages``, ``runtime_handle``, ``ac_session_id``, ...) lives on the shared
:class:`LeafDispatchState` the executor passes in, so the executor's ``except``
and ``finally`` observe the same mid-loop values they did when the loop body was
inline — including on the exception path, where the latest runtime handle and
partial message list must remain visible for teardown.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import TYPE_CHECKING, Any

import anyio

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_is_tool_completion,
    _runtime_message_tool_call_id,
)
from ouroboros.orchestrator.evidence.runtime_metadata import (
    HEARTBEAT_INTERVAL_SECONDS,
    STALL_TIMEOUT_SECONDS,
)
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message

if TYPE_CHECKING:
    from ouroboros.orchestrator.execution_runtime_scope import (
        ACRuntimeIdentity,
        ExecutionNodeIdentity,
    )
    from ouroboros.orchestrator.parallel_executor import ParallelACExecutor


@dataclass
class LeafDispatchState:
    """Mutable streaming state shared between the executor and the dispatcher.

    The executor seeds this with the pre-dispatch runtime handle and its own
    ``messages`` list (by reference), then reads the mutated fields after the
    stream — and, critically, from within its ``except``/``finally`` when the
    runtime raises mid-stream.
    """

    messages: list[AgentMessage]
    runtime_handle: RuntimeHandle | None
    ac_session_id: str | None = None
    message_count: int = 0
    final_message: str = ""
    success: bool = False
    stalled: bool = False
    infra_fatal: bool = False


# Fix 4 (round 3, BLOCKING): some runtime adapters (e.g.
# ``claude_worker_runtime.py`` / ``worker_runtime.py``'s "claude CLI not
# found" and ``pi_runtime.py``'s model/auth failures reported as an assistant
# message with ``stopReason: "error"``) report genuinely infra-fatal
# conditions as an ORDINARY final error message in the stream instead of
# raising. Only a RAISED exception reaching ``_execute_atomic_ac``'s
# catch-all set ``infra_fatal=True`` before this fix, so a missing CLI or bad
# auth credential -- reported as a structured error RESULT, never a raised
# exception -- fell through to ``infra_fatal=False`` and entered the ordinary
# same-runtime retry / lateral-escalation-ladder loop forever instead of
# surfacing immediately as an infra-fatal condition. This classifies the
# error message's KIND (its declared ``error_type`` when an adapter
# propagates ``type(exc).__name__``, plus well-known infra-fatal phrases in
# its content) so the same "genuinely fatal, redispatching cannot help"
# judgment applies whether the failure arrived as a raised exception or a
# returned result. Deliberately conservative: missing a real infra-fatal
# message here just keeps the pre-existing (already-tolerated) retry
# behavior, while a false positive would wrongly skip real retry/escalation
# opportunities for an ordinary AC failure -- the worse outcome.
#
# Fix 4 redo (round 3 follow-up review): the content-pattern scan used to
# also search ``message.content`` -- but ``content`` is the agent/model's own
# free-text final message, which routinely quotes a failing build/test's
# stderr ("npm ERR! ... no such file or directory") or narrates a legitimate
# business-logic 401 returned by an API call it invoked. Scanning that
# narrative for infra-fatal phrases produced false positives that
# short-circuited ``_is_retryable_failure()`` to ``False`` on an ORDINARY
# task failure -- exactly the "fail while escalation remains untried"
# outcome this project forbids. Both adapters that need this classification
# put a genuine infra failure into the dedicated, structured
# ``data["error"]`` field instead: ``worker_runtime.py`` mirrors
# ``WorkerTurn.error`` there (covers ``claude_worker_runtime.py``'s "claude
# CLI not found"), and ``pi_runtime.py`` mirrors its extracted
# ``errorMessage``/``error`` field there for the ``stopReason: "error"``
# case. The content-pattern scan is therefore restricted to that structured
# field only -- never the free-text narrative in ``message.content``.
_INFRA_FATAL_ERROR_TYPES = frozenset(
    {
        "FileNotFoundError",
        "PermissionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        # Round-5 fix: ``adapter.py`` reports a missing ``claude_agent_sdk``
        # Python package as a structured error result (never a raised
        # exception), and its narrative-only message previously carried no
        # ``error_type``/``error`` tag this classifier could see — so a
        # condition retrying can never cure (the SDK cannot install itself)
        # entered the ordinary retry/escalation ladder forever. The adapter
        # now tags that result with this SPECIFIC type; exact-matching it here
        # is safe because it is only ever emitted for the ImportError of the
        # SDK package itself, never for ordinary task failures.
        "SDKNotInstalledError",
        # Round-7 fix (Finding #1): ``adapter.py``'s
        # ``_execution_dispatch_error_message`` already tags a stale /
        # backend-incompatible runtime-handle dispatch failure with this
        # SPECIFIC purpose-built type (never raised by task code, only
        # constructed for the unknown/unsupported-runtime-backend dispatch
        # failure) — but the allowlist was never updated, so the correctly
        # structured signal stayed invisible and the unusable handle entered
        # the ordinary retry / parking ladder forever. Redispatching with the
        # same handle can never cure a handle the runtime cannot decode.
        "RuntimeHandleError",
        # Round-7 fix (Finding #1, "exhausted SDK exceptions" half): when the
        # Claude Agent SDK's ``query()`` raises because the ``claude`` CLI
        # binary itself is missing, ``adapter.py``'s terminal error path
        # propagates the SDK's own purpose-built exception class name via
        # ``type(e).__name__`` — ``CLINotFoundError`` (claude_agent_sdk
        # ``_errors.py``; subclass of ``ClaudeSDKError``, only raised when the
        # CLI executable cannot be located). Retrying can never install the
        # CLI, exactly like ``SDKNotInstalledError`` above. NOTE: the review
        # also suggested allowlisting bare ``"RuntimeError"`` for this path —
        # deliberately NOT done: the installed ``claude_agent_sdk`` never
        # raises ``RuntimeError`` (verified against its sources), while this
        # codebase and arbitrary task-level code raise the generic builtin for
        # ordinary non-infra failures in dozens of places. Blanket-matching a
        # generic builtin name would mark retryable/escalatable failures
        # infra-fatal — the forbidden false-negative — matching the same
        # caution that kept generic ``"PiError"`` out of this set in a prior
        # round.
        "CLINotFoundError",
    }
)

_INFRA_FATAL_CONTENT_PATTERNS = (
    "cli not found",
    "command not found",
    "no such file or directory",
    "unauthorized",
    "authentication failed",
    "invalid api key",
    "api key not valid",
    "401 unauthorized",
    "403 forbidden",
    "model not found",
    "unknown model",
    "no such model",
    # Round-4 fix: the shapes below are the REAL auth/authorization failure
    # strings Pi's underlying provider libraries produce, verified against
    # the installed @earendil-works/pi-ai provider sources -- none of the
    # generic phrases above matched them, so a genuine Pi 401 retried
    # forever instead of failing immediately.
    #
    # 1. pi-ai's OpenAI-responses/Azure/Mistral providers format HTTP-level
    #    failures as ``"<Provider> API error (<status>): <detail>"`` (e.g.
    #    ``"OpenAI API error (401)"``). Only 401/403 are matched -- other
    #    statuses (429, 5xx) are transient, not infra-fatal.
    "api error (401",
    "api error (403",
    # 2. The OpenAI SDK (pi-ai's openai-completions provider surfaces its
    #    ``error.message`` verbatim) phrases a bad key as
    #    ``"401 Incorrect API key provided: ..."``.
    "incorrect api key",
    # 3. The Anthropic SDK formats API errors as ``"<status> <json body>"``
    #    where an auth failure's body carries ``"type":"authentication_error"``
    #    (401, message ``"invalid x-api-key"``) or ``"type":"permission_error"``
    #    (403). These typed tags never appear in ordinary task narration.
    "authentication_error",
    "permission_error",
    "invalid x-api-key",
)


def _is_infra_fatal_error_message(message: AgentMessage) -> bool:
    """Classify a FINAL error message's KIND rather than its raise/return site.

    Only meaningful for ``message.is_final and message.is_error`` — an
    ordinary tool-call/narrative message is never inspected.

    The content-pattern scan below is deliberately restricted to the
    structured ``data["error"]`` field. ``message.content`` is the agent's
    own free-text final message and MUST NOT be scanned: it routinely quotes
    a failing build/test's stderr or narrates a legitimate business-logic
    error the agent's own tool call received, and matching infra-fatal
    phrases against that narrative produces false positives that wrongly
    skip real retry/escalation opportunities for an ordinary AC failure.
    """
    error_type = message.data.get("error_type")
    if isinstance(error_type, str) and error_type in _INFRA_FATAL_ERROR_TYPES:
        return True
    error_detail = message.data.get("error")
    if not isinstance(error_detail, str) or not error_detail:
        return False
    content = error_detail.lower()
    return any(pattern in content for pattern in _INFRA_FATAL_CONTENT_PATTERNS)


def _correlated_tool_result_name(
    messages: list[AgentMessage],
    result_message: AgentMessage,
) -> str | None:
    """Resolve a result's tool name from one exact prior call-id match.

    Claude ToolResultBlock carries ``tool_use_id`` but no tool name. Missing ids,
    duplicate ids with different names, and otherwise ambiguous histories fail
    closed so a completion event can never be attached to the wrong mutation.
    """
    result_call_id = _runtime_message_tool_call_id(result_message)
    if result_call_id is None:
        return None
    names = {
        message.tool_name
        for message in messages[:-1]
        if message.tool_name is not None
        and not _runtime_message_is_tool_completion(message)
        and _runtime_message_tool_call_id(message) == result_call_id
    }
    return next(iter(names)) if len(names) == 1 else None


class LeafDispatcher:
    """Dispatch one atomic leaf to the runtime and consume its message stream."""

    def __init__(self, executor: ParallelACExecutor) -> None:
        self._executor = executor

    async def stream(
        self,
        *,
        state: LeafDispatchState,
        prompt: str,
        tools: list[str],
        system_prompt: str,
        execute_effort_kwargs: dict[str, Any],
        runtime_identity: ACRuntimeIdentity,
        execution_context_id: str,
        session_id: str,
        ac_index: int,
        ac_content: str,
        is_sub_ac: bool,
        parent_ac_index: int | None,
        sub_ac_index: int | None,
        node_identity: ExecutionNodeIdentity | None,
        retry_attempt: int,
        semantic_ac_key: str,
        label: str,
        indent: str,
        execution_counters: dict[str, int] | None,
    ) -> None:
        """Run the stall-scoped dispatch loop, mutating ``state`` in place."""
        executor = self._executor

        lifecycle_event_type = (
            "execution.session.resumed"
            if executor._is_resumable_runtime_handle(state.runtime_handle)
            else "execution.session.started"
        )
        lifecycle_emitted = False
        emitted_recovery_turn_ids: set[str] = set()

        # Stall detection: CancelScope with resettable deadline (RC6)
        last_heartbeat = time.monotonic()
        exec_start = time.monotonic()

        with anyio.CancelScope(
            deadline=anyio.current_time() + STALL_TIMEOUT_SECONDS,
        ) as stall_scope:
            async for message in executor._adapter.execute_task(
                prompt=prompt,
                tools=tools,
                system_prompt=system_prompt,
                resume_handle=state.runtime_handle,
                **execute_effort_kwargs,
            ):
                # Reset stall deadline on every message (RC6 core)
                stall_scope.deadline = anyio.current_time() + STALL_TIMEOUT_SECONDS
                if message.resume_handle is not None:
                    state.runtime_handle = executor._remember_ac_runtime_handle(
                        ac_index,
                        message.resume_handle,
                        execution_context_id=execution_context_id,
                        is_sub_ac=is_sub_ac,
                        parent_ac_index=parent_ac_index,
                        sub_ac_index=sub_ac_index,
                        node_identity=node_identity,
                        retry_attempt=retry_attempt,
                    )

                if state.runtime_handle is not None and state.runtime_handle.native_session_id:
                    state.ac_session_id = state.runtime_handle.native_session_id
                elif (
                    message.resume_handle is None
                    and isinstance(message.data.get("session_id"), str)
                    and message.data["session_id"]
                ):
                    state.ac_session_id = message.data["session_id"]

                state.runtime_handle = executor._with_native_session_id(
                    state.runtime_handle, state.ac_session_id
                )
                if state.runtime_handle is not None and message.resume_handle is not None:
                    message = replace(message, resume_handle=state.runtime_handle)

                recovery_discontinuity = executor._runtime_recovery_discontinuity(
                    state.runtime_handle
                )
                if recovery_discontinuity is not None:
                    replacement = recovery_discontinuity.get("replacement", {})
                    replacement_turn_id = replacement.get("turn_id")
                    if isinstance(replacement_turn_id, str) and replacement_turn_id:
                        if replacement_turn_id not in emitted_recovery_turn_ids:
                            await executor._emit_ac_runtime_event(
                                event_type="execution.session.recovered",
                                runtime_identity=runtime_identity,
                                ac_content=ac_content,
                                runtime_handle=state.runtime_handle,
                                execution_id=execution_context_id,
                                session_id=state.ac_session_id,
                            )
                            emitted_recovery_turn_ids.add(replacement_turn_id)

                state.messages.append(message)
                state.message_count += 1
                if execution_counters is not None:
                    async with executor._execution_counters_lock:
                        execution_counters["messages_count"] = (
                            execution_counters.get("messages_count", 0) + 1
                        )

                # RC1: Emit heartbeat piggybacking on message flow
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
                    await executor._event_emitter.emit_heartbeat(
                        session_id=session_id,
                        ac_index=ac_index,
                        ac_id=runtime_identity.ac_id,
                        elapsed_seconds=now - exec_start,
                        message_count=state.message_count,
                        node_identity=node_identity,
                    )
                    last_heartbeat = now

                projected = project_runtime_message(message)
                await executor._event_emitter.observe_ac_activity(
                    runtime_identity=runtime_identity,
                    execution_id=execution_context_id,
                    session_id=session_id,
                    semantic_ac_key=semantic_ac_key,
                    projected=projected,
                    is_final=message.is_final,
                )

                persisted_session_id = executor._runtime_resume_session_id(state.runtime_handle)
                if not lifecycle_emitted and persisted_session_id:
                    await executor._emit_ac_runtime_event(
                        event_type=lifecycle_event_type,
                        runtime_identity=runtime_identity,
                        ac_content=ac_content,
                        runtime_handle=state.runtime_handle,
                        execution_id=execution_context_id,
                        session_id=persisted_session_id,
                    )
                    lifecycle_emitted = True
                    executor._remember_ac_runtime_handle(
                        ac_index,
                        state.runtime_handle,
                        execution_context_id=execution_context_id,
                        is_sub_ac=is_sub_ac,
                        parent_ac_index=parent_ac_index,
                        sub_ac_index=sub_ac_index,
                        node_identity=node_identity,
                        retry_attempt=retry_attempt,
                    )

                session_tool_event = executor._build_session_tool_called_event(
                    session_id,
                    projected=projected,
                )
                if session_tool_event is not None:
                    await executor._event_store.append(session_tool_event)

                if executor._should_emit_session_progress_event(
                    message,
                    projected=projected,
                    messages_processed=len(state.messages),
                ):
                    session_progress_event = executor._build_session_progress_event(
                        session_id,
                        message,
                        projected=projected,
                    )
                    await executor._event_store.append(session_progress_event)

                if projected.is_tool_call and projected.tool_name is not None:
                    # RC6: Tool invocations prove liveness — reset stall
                    # deadline so long-running tools (Bash, external APIs)
                    # are not falsely detected as stalls.
                    stall_scope.deadline = anyio.current_time() + STALL_TIMEOUT_SECONDS
                    if execution_counters is not None:
                        async with executor._execution_counters_lock:
                            execution_counters["tool_calls_count"] = (
                                execution_counters.get("tool_calls_count", 0) + 1
                            )
                    tool_input = projected.tool_input
                    tool_detail = executor._format_tool_detail(projected.tool_name, tool_input)
                    executor._console.print(f"{indent}[yellow]{label} → {tool_detail}[/yellow]")
                    executor._flush_console()

                    await executor._event_emitter.emit_atomic_tool_started(
                        runtime_identity=runtime_identity,
                        tool_name=projected.tool_name,
                        tool_detail=tool_detail,
                        tool_input=tool_input,
                        runtime_metadata=executor._runtime_event_metadata(message),
                    )

                if projected.message_type == "tool_result":
                    completed_tool_name = projected.tool_name or _correlated_tool_result_name(
                        state.messages,
                        message,
                    )
                else:
                    completed_tool_name = None
                if completed_tool_name is not None:
                    await executor._event_emitter.emit_atomic_tool_completed(
                        runtime_identity=runtime_identity,
                        tool_name=completed_tool_name,
                        tool_result_text=projected.content,
                        runtime_metadata=executor._runtime_event_metadata(message),
                    )

                if projected.thinking:
                    await executor._event_emitter.emit_atomic_thinking(
                        runtime_identity=runtime_identity,
                        thinking_text=projected.thinking,
                        runtime_metadata=executor._runtime_event_metadata(message),
                    )

                if message.is_final:
                    state.final_message = message.content
                    state.success = not message.is_error
                    if message.is_error and _is_infra_fatal_error_message(message):
                        state.infra_fatal = True

        # Check if stall was detected (CancelScope ate the Cancelled)
        state.stalled = stall_scope.cancelled_caught
