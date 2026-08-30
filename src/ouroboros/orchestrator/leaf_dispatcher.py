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

from collections.abc import Mapping
from dataclasses import dataclass, replace
import errno
import os
from pathlib import Path
import stat
import threading
import time
from typing import TYPE_CHECKING, Any

import anyio

from ouroboros.core.filesystem_capability import (
    DirectoryChainFingerprint,
    NoFollowDirectoryChain,
    nofollow_directory_capabilities_available,
    open_nofollow_directory_chain,
)
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
from ouroboros.orchestrator.evidence.harness_observation import (
    diff_workspace_snapshots,
    insert_observation_message,
    snapshot_workspace,
)
from ouroboros.orchestrator.evidence.runtime_metadata import (
    HEARTBEAT_INTERVAL_SECONDS,
    STALL_TIMEOUT_SECONDS,
)
from ouroboros.orchestrator.runtime_message_projection import (
    message_tool_name,
    project_runtime_message,
)

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
    lifecycle_event_count: int = 0
    final_message: str = ""
    success: bool = False
    stalled: bool = False


@dataclass(slots=True)
class _PendingBashTarget:
    """Execution-span lease on one lexical shell receiver parent."""

    directory_chain: NoFollowDirectoryChain | None
    leaf_name: str
    reported_path: str
    workspace_relative_path: str
    pre_fingerprint: tuple[int, int, int, int, int, int] | None
    workspace_fingerprint: DirectoryChainFingerprint

    @property
    def parent_fd(self) -> int | None:
        """Return the final held parent fd while this lease owns its chain."""
        if self.directory_chain is None:
            return None
        return self.directory_chain.leaf_fd

    @property
    def descriptor_count(self) -> int:
        """Return the descriptors held by this receiver lease."""
        return self.directory_chain.descriptor_count if self.directory_chain is not None else 0


_MAX_BASH_FILESYSTEM_EFFECTS = 128
_MAX_BASH_PROVENANCE_FDS = 64
_BASH_PROVENANCE_FD_LIMIT_DIVISOR = 4
_MIN_RUNTIME_FD_HEADROOM = 16
_BASH_PROVENANCE_FD_LOCK = threading.Lock()


class _BashProvenanceFDBudgetExceeded(Exception):
    """Signal that a command capture must be abandoned as one unit."""


def _bash_soft_fd_limit() -> int | None:
    """Return the process soft fd limit when it is safely observable."""
    try:
        soft_limit = os.sysconf("SC_OPEN_MAX")
    except (AttributeError, OSError, ValueError):
        return None
    if not isinstance(soft_limit, int) or soft_limit <= 0:
        return None
    return soft_limit


def _bash_provenance_fd_budget() -> int:
    """Return one conservative per-tracker cap below the process soft limit."""
    soft_limit = _bash_soft_fd_limit()
    if soft_limit is None:
        return 0
    return min(_MAX_BASH_PROVENANCE_FDS, soft_limit // _BASH_PROVENANCE_FD_LIMIT_DIVISOR)


def _bash_open_fd_count() -> int | None:
    """Count process descriptors without retaining the enumeration handle."""
    for candidate in (Path("/proc/self/fd"), Path("/dev/fd")):
        if not candidate.exists():
            continue
        try:
            with os.scandir(candidate) as entries:
                return sum(1 for _entry in entries)
        except OSError:
            continue
    return None


def _bash_provenance_headroom_available(required_fds: int) -> bool:
    """Keep at least half the process limit free for runtime and pipe I/O."""
    soft_limit = _bash_soft_fd_limit()
    open_fds = _bash_open_fd_count()
    if soft_limit is None or open_fds is None or required_fds < 0:
        return False
    reserved_headroom = max(_MIN_RUNTIME_FD_HEADROOM, soft_limit // 2)
    return open_fds + required_fds <= soft_limit - reserved_headroom


def _absolute_directory_fd_count(path: str | os.PathLike[str]) -> int:
    """Return descriptors required for a root-anchored absolute directory walk."""
    return len(Path(os.path.abspath(path)).parts)


def _pending_bash_filesystem_targets(
    message: AgentMessage,
    *,
    task_cwd: str | None,
    workspace_chain: NoFollowDirectoryChain | None = None,
    target_fd_budget: int | None = None,
) -> tuple[_PendingBashTarget, ...]:
    """Lease Bash receiver parents and capture pre-execution file identity."""
    with _BASH_PROVENANCE_FD_LOCK:
        return _pending_bash_filesystem_targets_locked(
            message,
            task_cwd=task_cwd,
            workspace_chain=workspace_chain,
            target_fd_budget=target_fd_budget,
        )


def _pending_bash_filesystem_targets_locked(
    message: AgentMessage,
    *,
    task_cwd: str | None,
    workspace_chain: NoFollowDirectoryChain | None,
    target_fd_budget: int | None,
) -> tuple[_PendingBashTarget, ...]:
    """Lease receivers while process-wide provenance admission is serialized."""
    if message.tool_name != "Bash" or _runtime_message_is_tool_completion(message):
        return ()
    effective_cwd = _runtime_message_effective_cwd(message, task_cwd=task_cwd)
    if effective_cwd is None or task_cwd is None:
        return ()
    owned_workspace_chain: NoFollowDirectoryChain | None = None
    if workspace_chain is None:
        total_budget = _bash_provenance_fd_budget()
        workspace_fd_count = _absolute_directory_fd_count(task_cwd)
        if workspace_fd_count > total_budget or not _bash_provenance_headroom_available(
            workspace_fd_count
        ):
            return ()
        try:
            owned_workspace_chain = open_nofollow_directory_chain(Path(os.path.abspath(task_cwd)))
        except (OSError, RuntimeError, ValueError):
            return ()
        workspace_chain = owned_workspace_chain
        target_fd_budget = total_budget - workspace_chain.descriptor_count
    elif target_fd_budget is None:
        target_fd_budget = _bash_provenance_fd_budget()
    targets: list[_PendingBashTarget] = []
    try:
        for command in _runtime_message_command_values(message):
            for target in _shell_command_mutation_targets(command):
                try:
                    pending = _lease_bash_target(
                        target,
                        task_cwd=task_cwd,
                        effective_cwd=effective_cwd,
                        workspace_chain=workspace_chain,
                        fd_budget=target_fd_budget,
                    )
                except _BashProvenanceFDBudgetExceeded:
                    _close_pending_targets(tuple(targets))
                    return ()
                if pending is not None:
                    targets.append(pending)
                    target_fd_budget -= pending.descriptor_count
                    if len(targets) > _MAX_BASH_FILESYSTEM_EFFECTS or len(
                        {item.workspace_relative_path for item in targets}
                    ) != len(targets):
                        _close_pending_targets(tuple(targets))
                        return ()
        return tuple(targets)
    finally:
        if owned_workspace_chain is not None:
            owned_workspace_chain.close()


def _lease_bash_target(
    target: str,
    *,
    task_cwd: str,
    effective_cwd: str,
    workspace_chain: NoFollowDirectoryChain,
    fd_budget: int,
) -> _PendingBashTarget | None:
    """Open a no-follow dirfd chain that survives path-component replacement."""
    directory_chain: NoFollowDirectoryChain | None = None
    try:
        if not nofollow_directory_capabilities_available():
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
        required_fds = workspace_chain.descriptor_count + len(relative.parts[:-1])
        if required_fds > fd_budget or not _bash_provenance_headroom_available(required_fds):
            raise _BashProvenanceFDBudgetExceeded
        directory_chain = open_nofollow_directory_chain(
            workspace,
            relative_components=relative.parts[:-1],
        )
        if not workspace_chain.matches_opened_prefix(directory_chain):
            return None
        workspace_fingerprint = workspace_chain.fingerprint()
        parent_fd = directory_chain.leaf_fd
        try:
            pre = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pre_fingerprint = None
        else:
            pre_fingerprint = _stat_fingerprint(pre)
        pending = _PendingBashTarget(
            directory_chain=directory_chain,
            leaf_name=relative.name,
            reported_path=target,
            workspace_relative_path=relative.as_posix(),
            pre_fingerprint=pre_fingerprint,
            workspace_fingerprint=workspace_fingerprint,
        )
        directory_chain = None
        return pending
    except OSError as exc:
        if exc.errno in {errno.EMFILE, errno.ENFILE}:
            raise _BashProvenanceFDBudgetExceeded from exc
        return None
    except (RuntimeError, ValueError):
        return None
    finally:
        if directory_chain is not None:
            directory_chain.close()


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
        directory_chain = target.directory_chain
        if parent_fd is None or directory_chain is None:
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
            if not directory_chain.postvalidate():
                continue
            effects.append(
                {
                    "capture": "ouroboros.leaf-dispatch.v1",
                    "path": target.reported_path,
                    "workspace_relative_path": target.workspace_relative_path,
                    "workspace_chain": [
                        {
                            "name": name,
                            "st_dev": device,
                            "st_ino": inode,
                            "st_mode": mode,
                        }
                        for name, device, inode, mode in target.workspace_fingerprint
                    ],
                    "st_dev": identity.st_dev,
                    "st_ino": identity.st_ino,
                    "st_mode": identity.st_mode,
                    "st_size": identity.st_size,
                    "st_mtime_ns": identity.st_mtime_ns,
                    "st_ctime_ns": identity.st_ctime_ns,
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


def _strip_internal_filesystem_effects(
    message: AgentMessage,
    *,
    force_snapshot: bool = False,
) -> AgentMessage:
    """Sever adapter data ownership and remove its reserved provenance field."""
    if (
        not force_snapshot
        and message_tool_name(message) != "Bash"
        and not _runtime_message_is_tool_completion(message)
        and "filesystem_effects" not in message.data
    ):
        return message
    sanitized = {
        key: _snapshot_adapter_value(value)
        for key, value in message.data.items()
        if key != "filesystem_effects"
    }
    return replace(message, data=sanitized)


def _snapshot_adapter_value(value: object) -> object:
    """Recursively copy plain containers used by provenance-sensitive metadata."""
    if isinstance(value, Mapping):
        return {key: _snapshot_adapter_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_snapshot_adapter_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_snapshot_adapter_value(item) for item in value)
    if isinstance(value, set):
        return {_snapshot_adapter_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_snapshot_adapter_value(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def _runtime_message_has_malformed_tool_call_aliases(message: AgentMessage) -> bool:
    """Reject present correlation aliases that are not non-blank strings."""
    containers: list[Mapping[str, object]] = [message.data]
    message_meta = message.data.get("meta")
    if isinstance(message_meta, Mapping):
        containers.append(message_meta)
    tool_result = message.data.get("tool_result")
    if isinstance(tool_result, Mapping):
        containers.append(tool_result)
        result_meta = tool_result.get("meta")
        if isinstance(result_meta, Mapping):
            containers.append(result_meta)
    return any(
        key in container
        and (not isinstance(container[key], str) or not str(container[key]).strip())
        for container in containers
        for key in ("tool_call_id", "tool_use_id", "call_id")
    )


def _close_pending_target(target: _PendingBashTarget) -> None:
    directory_chain = target.directory_chain
    if directory_chain is None:
        return
    target.directory_chain = None
    directory_chain.close()


def _close_pending_targets(targets: tuple[_PendingBashTarget, ...]) -> None:
    for target in targets:
        _close_pending_target(target)


class _BashFilesystemLeaseTracker:
    """Own receiver leases across streamed Bash call/completion pairs."""

    def __init__(self, *, task_cwd: str | None) -> None:
        self._task_cwd = task_cwd
        self._provenance_fd_budget = _bash_provenance_fd_budget()
        self._workspace_chain: NoFollowDirectoryChain | None = None
        self._pending_by_id: dict[str, tuple[_PendingBashTarget, ...]] = {}
        self._seen_call_ids: set[str] = set()
        self._pending_bash_call_ids: set[str] = set()
        self._idless: tuple[_PendingBashTarget, ...] | None = None
        self._idless_active = False
        self._idless_bash_active = False
        self._idless_tool_name: str | None = None

    def __enter__(self) -> _BashFilesystemLeaseTracker:
        if self._task_cwd is not None and self._workspace_chain is None:
            workspace_fd_count = _absolute_directory_fd_count(self._task_cwd)
            with _BASH_PROVENANCE_FD_LOCK:
                if workspace_fd_count > self._provenance_fd_budget or not (
                    _bash_provenance_headroom_available(workspace_fd_count)
                ):
                    return self
                try:
                    self._workspace_chain = open_nofollow_directory_chain(
                        Path(os.path.abspath(self._task_cwd))
                    )
                except (OSError, RuntimeError, ValueError):
                    self._workspace_chain = None
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _poison_pending_named_bash_leases(self) -> None:
        """Close every active named Bash lease without making it reusable."""
        for pending_id in tuple(self._pending_bash_call_ids):
            _close_pending_targets(self._pending_by_id.pop(pending_id, ()))
            self._seen_call_ids.add(pending_id)
            self._pending_by_id[pending_id] = ()
        self._pending_bash_call_ids.clear()

    def _held_provenance_fd_count(self) -> int:
        """Count every workspace and receiver descriptor owned by this tracker."""
        count = self._workspace_chain.descriptor_count if self._workspace_chain else 0
        count += sum(
            target.descriptor_count
            for targets in self._pending_by_id.values()
            for target in targets
        )
        if self._idless is not None:
            count += sum(target.descriptor_count for target in self._idless)
        return count

    def observe(self, message: AgentMessage) -> AgentMessage:
        """Lease calls, attach completion effects, and reject ambiguous pairing."""
        message = _strip_internal_filesystem_effects(
            message,
            force_snapshot=bool(self._pending_bash_call_ids) or self._idless_bash_active,
        )
        malformed_alias = _runtime_message_has_malformed_tool_call_aliases(message)
        if malformed_alias or _runtime_message_has_conflicting_tool_call_ids(message):
            # Every alias named by an ambiguous message is poisoned. Close any
            # prior lease it could otherwise consume, and retain an empty
            # sentinel so a later call/result cannot revive or donate one.
            for conflicting_id in _runtime_message_tool_call_ids(message):
                _close_pending_targets(self._pending_by_id.pop(conflicting_id, ()))
                self._seen_call_ids.add(conflicting_id)
                self._pending_by_id[conflicting_id] = ()
                self._pending_bash_call_ids.discard(conflicting_id)
            if malformed_alias and not _runtime_message_tool_call_ids(message):
                is_bash_candidate = (
                    _runtime_message_is_tool_completion(message)
                    or message_tool_name(message) == "Bash"
                )
                if is_bash_candidate:
                    self._poison_pending_named_bash_leases()
                    _close_pending_targets(self._idless or ())
                    self._idless = ()
                    self._idless_active = True
                    self._idless_bash_active = False
                    self._idless_tool_name = None
            return message
        call_id = _runtime_message_tool_call_id(message)
        tool_name = message_tool_name(message)
        if tool_name is not None and not _runtime_message_is_tool_completion(message):
            targets = (
                _pending_bash_filesystem_targets(
                    message,
                    task_cwd=self._task_cwd,
                    workspace_chain=self._workspace_chain,
                    target_fd_budget=max(
                        0,
                        self._provenance_fd_budget - self._held_provenance_fd_count(),
                    ),
                )
                if tool_name == "Bash" and self._workspace_chain is not None
                else ()
            )
            if call_id is not None:
                if call_id in self._seen_call_ids:
                    _close_pending_targets(self._pending_by_id.get(call_id, ()))
                    _close_pending_targets(targets)
                    self._pending_by_id[call_id] = ()
                    self._pending_bash_call_ids.discard(call_id)
                else:
                    self._seen_call_ids.add(call_id)
                    self._pending_by_id[call_id] = targets
                    if tool_name == "Bash":
                        self._pending_bash_call_ids.add(call_id)
            elif not self._idless_active:
                self._idless_active = True
                self._idless = targets
                self._idless_bash_active = tool_name == "Bash"
                self._idless_tool_name = tool_name
            else:
                _close_pending_targets(self._idless or ())
                _close_pending_targets(targets)
                self._idless = ()
                self._idless_bash_active = False
                self._idless_tool_name = None
            return message
        if not _runtime_message_is_tool_completion(message):
            return message
        if call_id is not None:
            targets = self._pending_by_id.pop(call_id, ())
            self._pending_bash_call_ids.discard(call_id)
        else:
            unique_idless_tool = self._idless_tool_name if self._idless_active else None
            normalized_completion_tool = (
                tool_name.strip() if isinstance(tool_name, str) and tool_name.strip() else None
            )
            orphan_bash_completion = normalized_completion_tool == "Bash" or (
                normalized_completion_tool is None and unique_idless_tool is None
            )
            if orphan_bash_completion and unique_idless_tool != "Bash":
                # A terminal that cannot be assigned to one active id-less Bash
                # call could belong to any concurrent named Bash call.  It
                # closes every such authority boundary; a later matching ID
                # must not extend or revive one of their receiver leases.
                self._poison_pending_named_bash_leases()
            targets = self._idless or ()
            self._idless = None
            self._idless_active = False
            self._idless_bash_active = False
            self._idless_tool_name = None
        normalized_tool_name = (
            tool_name.strip() if isinstance(tool_name, str) and tool_name.strip() else None
        )
        if normalized_tool_name not in {None, "Bash"}:
            _close_pending_targets(targets)
            return message
        return _attach_bash_filesystem_effects(message, targets) if targets else message

    def close(self) -> None:
        """Close all unmatched leases exactly once on every stream exit path."""
        for targets in self._pending_by_id.values():
            _close_pending_targets(targets)
        self._pending_by_id.clear()
        self._seen_call_ids.clear()
        self._pending_bash_call_ids.clear()
        if self._idless is not None:
            _close_pending_targets(self._idless)
            self._idless = None
        self._idless_active = False
        self._idless_bash_active = False
        self._idless_tool_name = None
        if self._workspace_chain is not None:
            self._workspace_chain.close()
            self._workspace_chain = None


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

        # Fingerprint the workspace before the leaf touches it. The matching
        # snapshot after the stream ends lets the verifier accept files_touched
        # claims for paths the harness itself saw change, whatever tool shape
        # the runtime used to write them (see evidence/harness_observation.py).
        workspace_before = snapshot_workspace(task_cwd)

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
                            state.lifecycle_event_count += 1
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
                    state.lifecycle_event_count += 1
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

        # Only a transcript that actually arrived gets the observation: an empty
        # stream must keep reading as a collection fault (transcript missing),
        # not as a transcript consisting of the harness's own note.
        if any(not message.is_final for message in state.messages):
            observation = diff_workspace_snapshots(workspace_before, snapshot_workspace(task_cwd))
            if observation is not None:
                insert_observation_message(state.messages, observation)
