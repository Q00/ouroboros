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
import os
from pathlib import Path
import stat
import time
from typing import TYPE_CHECKING, Any

import anyio

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.evidence.claims import (
    _runtime_message_command_values,
    _runtime_message_effective_cwd,
    _runtime_message_has_conflicting_tool_call_ids,
    _runtime_message_is_tool_completion,
    _runtime_message_tool_call_id,
    _runtime_message_tool_call_ids,
    _shell_command_mutation_targets,
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


@dataclass(slots=True)
class _PendingBashTarget:
    """Execution-span lease on one lexical shell receiver parent."""

    parent_fd: int | None
    leaf_name: str
    reported_path: str
    pre_fingerprint: tuple[int, int, int, int, int, int] | None


def _pending_bash_filesystem_targets(
    message: AgentMessage,
    *,
    task_cwd: str | None,
) -> tuple[_PendingBashTarget, ...]:
    """Lease Bash receiver parents and capture pre-execution file identity."""
    if message.tool_name != "Bash" or _runtime_message_is_tool_completion(message):
        return ()
    effective_cwd = _runtime_message_effective_cwd(message, task_cwd=task_cwd)
    if effective_cwd is None:
        return ()
    targets: list[_PendingBashTarget] = []
    for command in _runtime_message_command_values(message):
        for target in _shell_command_mutation_targets(command):
            pending = _lease_bash_target(
                target,
                task_cwd=task_cwd,
                effective_cwd=effective_cwd,
            )
            if pending is not None:
                targets.append(pending)
    return tuple(targets)


def _lease_bash_target(
    target: str,
    *,
    task_cwd: str,
    effective_cwd: str,
) -> _PendingBashTarget | None:
    """Open a no-follow dirfd chain that survives path-component replacement."""
    parent_fd: int | None = None
    try:
        if (
            not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_NOFOLLOW")
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.stat not in os.supports_follow_symlinks
        ):
            return None
        workspace = Path(os.path.abspath(task_cwd))
        candidate = Path(target)
        absolute = Path(
            os.path.abspath(
                candidate if candidate.is_absolute() else Path(effective_cwd) / candidate
            )
        )
        relative = absolute.relative_to(workspace)
        if not relative.parts or relative.name in {"", ".", ".."}:
            return None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(workspace, flags)
        for part in relative.parts[:-1]:
            if part in {"", ".", ".."}:
                return None
            next_fd = os.open(part, flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        try:
            pre = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pre_fingerprint = None
        else:
            pre_fingerprint = _stat_fingerprint(pre)
        leased_fd = parent_fd
        parent_fd = None
        return _PendingBashTarget(
            parent_fd=leased_fd,
            leaf_name=relative.name,
            reported_path=target,
            pre_fingerprint=pre_fingerprint,
        )
    except (OSError, RuntimeError, ValueError):
        return None
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _stat_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _attach_bash_filesystem_effects(
    message: AgentMessage,
    targets: tuple[_PendingBashTarget, ...],
) -> AgentMessage:
    """Attach effects only when the leased receiver changed across execution."""
    message = _strip_internal_filesystem_effects(message)
    effects: list[dict[str, object]] = []
    for target in targets:
        parent_fd = target.parent_fd
        if parent_fd is None:
            continue
        try:
            try:
                identity = os.stat(
                    target.leaf_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except (OSError, RuntimeError, ValueError):
                continue
            if not stat.S_ISREG(identity.st_mode):
                continue
            post_fingerprint = _stat_fingerprint(identity)
            if target.pre_fingerprint is not None:
                pre_identity = target.pre_fingerprint[:3]
                post_identity = post_fingerprint[:3]
                if (
                    pre_identity[2] != stat.S_IFREG
                    or post_identity != pre_identity
                    or post_fingerprint == target.pre_fingerprint
                ):
                    continue
            effects.append(
                {
                    "capture": "ouroboros.leaf-dispatch.v1",
                    "path": target.reported_path,
                    "st_dev": identity.st_dev,
                    "st_ino": identity.st_ino,
                    "st_mode": identity.st_mode,
                }
            )
        finally:
            _close_pending_target(target)
    if not effects:
        return message
    return replace(
        message,
        data={**message.data, "filesystem_effects": effects},
    )


def _strip_internal_filesystem_effects(message: AgentMessage) -> AgentMessage:
    """Remove adapter-supplied provenance reserved for local lease capture."""
    if "filesystem_effects" not in message.data:
        return message
    sanitized = dict(message.data)
    sanitized.pop("filesystem_effects", None)
    return replace(message, data=sanitized)


def _close_pending_target(target: _PendingBashTarget) -> None:
    fd = target.parent_fd
    if fd is None:
        return
    target.parent_fd = None
    try:
        os.close(fd)
    except OSError:
        pass


def _close_pending_targets(targets: tuple[_PendingBashTarget, ...]) -> None:
    for target in targets:
        _close_pending_target(target)


class _BashFilesystemLeaseTracker:
    """Own receiver leases across streamed Bash call/completion pairs."""

    def __init__(self, *, task_cwd: str | None) -> None:
        self._task_cwd = task_cwd
        self._pending_by_id: dict[str, tuple[_PendingBashTarget, ...]] = {}
        self._seen_call_ids: set[str] = set()
        self._idless: tuple[_PendingBashTarget, ...] | None = None
        self._idless_active = False

    def __enter__(self) -> _BashFilesystemLeaseTracker:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def observe(self, message: AgentMessage) -> AgentMessage:
        """Lease calls, attach completion effects, and reject ambiguous pairing."""
        message = _strip_internal_filesystem_effects(message)
        if _runtime_message_has_conflicting_tool_call_ids(message):
            # Every alias named by an ambiguous message is poisoned. Close any
            # prior lease it could otherwise consume, and retain an empty
            # sentinel so a later call/result cannot revive or donate one.
            for conflicting_id in _runtime_message_tool_call_ids(message):
                _close_pending_targets(self._pending_by_id.pop(conflicting_id, ()))
                self._seen_call_ids.add(conflicting_id)
                self._pending_by_id[conflicting_id] = ()
            return message
        call_id = _runtime_message_tool_call_id(message)
        if message.tool_name is not None and not _runtime_message_is_tool_completion(message):
            targets = (
                _pending_bash_filesystem_targets(message, task_cwd=self._task_cwd)
                if message.tool_name == "Bash"
                else ()
            )
            if call_id is not None:
                if call_id in self._seen_call_ids:
                    _close_pending_targets(self._pending_by_id.get(call_id, ()))
                    _close_pending_targets(targets)
                    self._pending_by_id[call_id] = ()
                else:
                    self._seen_call_ids.add(call_id)
                    self._pending_by_id[call_id] = targets
            elif not self._idless_active:
                self._idless_active = True
                self._idless = targets
            else:
                _close_pending_targets(self._idless or ())
                _close_pending_targets(targets)
                self._idless = ()
            return message
        if not _runtime_message_is_tool_completion(message):
            return message
        if call_id is not None:
            targets = self._pending_by_id.pop(call_id, ())
        else:
            targets = self._idless or ()
            self._idless = None
            self._idless_active = False
        if message.tool_name not in {None, "Bash"}:
            _close_pending_targets(targets)
            return message
        return _attach_bash_filesystem_effects(message, targets) if targets else message

    def close(self) -> None:
        """Close all unmatched leases exactly once on every stream exit path."""
        for targets in self._pending_by_id.values():
            _close_pending_targets(targets)
        self._pending_by_id.clear()
        self._seen_call_ids.clear()
        if self._idless is not None:
            _close_pending_targets(self._idless)
            self._idless = None
        self._idless_active = False


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
    if result_call_id is None or _runtime_message_has_conflicting_tool_call_ids(result_message):
        return None
    if any(
        _runtime_message_has_conflicting_tool_call_ids(message)
        and result_call_id in _runtime_message_tool_call_ids(message)
        for message in messages[:-1]
    ):
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
        task_cwd = executor._task_cwd or executor._adapter.working_directory

        # Stall detection: CancelScope with resettable deadline (RC6)
        last_heartbeat = time.monotonic()
        exec_start = time.monotonic()

        with (
            _BashFilesystemLeaseTracker(task_cwd=task_cwd) as identity_tracker,
            anyio.CancelScope(
                deadline=anyio.current_time() + STALL_TIMEOUT_SECONDS,
            ) as stall_scope,
        ):
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
                    augmented_handle = executor._augment_ac_runtime_handle(
                        message.resume_handle,
                        runtime_identity=runtime_identity,
                        previous_handle=state.runtime_handle,
                    )
                    state.runtime_handle = executor._remember_ac_runtime_handle(
                        ac_index,
                        augmented_handle,
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

                message = identity_tracker.observe(message)

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
                                orchestrator_session_id=session_id,
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
                        orchestrator_session_id=session_id,
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

        # Check if stall was detected (CancelScope ate the Cancelled)
        state.stalled = stall_scope.cancelled_caught
