"""AC-scoped runtime-handle lifecycle management for parallel execution."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import os
import re
from typing import TYPE_CHECKING, Any

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.ac_execution_capsule import ACExecutionCapsuleManifest
from ouroboros.orchestrator.adapter import (
    RuntimeHandle,
    runtime_handle_tool_catalog,
)
from ouroboros.orchestrator.capabilities import (
    build_capability_graph,
    serialize_capability_graph,
)
from ouroboros.orchestrator.control_plane import (
    build_control_plane_state,
    serialize_control_plane_state,
)
from ouroboros.orchestrator.evidence.runtime_metadata import (
    _AC_RUNTIME_OWNERSHIP_METADATA_KEYS,
    _AC_RUNTIME_RESUME_METADATA_KEYS,
    _AC_RUNTIME_SCOPE_METADATA_KEYS,
    _NON_REUSABLE_RUNTIME_EVENT_TYPES,
    _REUSABLE_RUNTIME_EVENT_TYPES,
)
from ouroboros.orchestrator.execution_runtime_scope import (
    ACRuntimeIdentity,
    ExecutionNodeIdentity,
    build_ac_runtime_identity,
)
from ouroboros.orchestrator.mcp_tools import serialize_tool_catalog
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    evaluate_capability_policy,
)

if TYPE_CHECKING:
    from ouroboros.mcp.types import MCPToolDefinition
    from ouroboros.orchestrator.adapter import AgentRuntime
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)

_IMPLEMENTATION_SESSION_KIND = "implementation_session"
_AC_CAPSULE_COMPILED_EVENT = "execution.ac.capsule.compiled"
_AC_ATTEMPT_DISPATCHED_EVENT = "execution.ac.attempt.dispatched"
_AC_DISPATCH_SEALED_EVENT = "execution.ac.dispatch.sealed"


class AmbiguousACExecutionError(RuntimeError):
    """A provider boundary exists but cannot be resumed safely."""


class ACRuntimeHandleManager:
    """Owns AC runtime-handle cache, scope rebinding, and lifecycle events."""

    def __init__(
        self,
        adapter: AgentRuntime,
        event_store: EventStore,
        *,
        task_cwd: str | None,
        process_local_resume_nonce: str | None = None,
    ) -> None:
        self._adapter = adapter
        self._event_store = event_store
        self._task_cwd = task_cwd
        self._process_local_resume_nonce = process_local_resume_nonce
        self.runtime_handles: dict[str, RuntimeHandle] = {}
        # A provider boundary becomes permanently non-replayable in this
        # process as soon as we attempt to seal it.  This local poison bit is
        # deliberately recorded before the event-store append: if the append
        # fails, the in-memory handle must not be accepted on a same-executor
        # retry as though the boundary were still safe to resume.
        self._non_replayable_dispatch_ids: set[str] = set()

    def mark_dispatch_non_replayable(self, dispatch_id: str) -> None:
        """Poison a dispatch before attempting a potentially failing seal."""
        if isinstance(dispatch_id, str) and dispatch_id:
            self._non_replayable_dispatch_ids.add(dispatch_id)

    def is_dispatch_non_replayable(self, dispatch_id: str | None) -> bool:
        """Return whether this process has observed an unsafe seal boundary."""
        return isinstance(dispatch_id, str) and dispatch_id in self._non_replayable_dispatch_ids

    @staticmethod
    def _build_expected_ac_runtime_metadata(
        runtime_scope: Any,
        *,
        ac_index: int,
        is_sub_ac: bool,
        parent_ac_index: int | None,
        sub_ac_index: int | None,
        node_identity: ExecutionNodeIdentity | None,
        retry_attempt: int,
    ) -> dict[str, Any]:
        """Build metadata that binds a runtime handle to a single AC execution scope."""
        identity = build_ac_runtime_identity(
            ac_index,
            execution_context_id=node_identity.execution_context_id
            if node_identity is not None
            else None,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )
        if identity.runtime_scope != runtime_scope:
            identity = replace(identity, runtime_scope=runtime_scope)
        return identity.to_metadata()

    @staticmethod
    def _metadata_value_matches_expected_scope(
        key: str,
        observed_value: Any,
        expected_metadata: dict[str, Any],
    ) -> bool:
        """Return True when observed metadata matches canonical or legacy scope."""
        if observed_value == expected_metadata.get(key):
            return True

        if key in {"ac_id", "session_scope_id"}:
            legacy_scope_ids = expected_metadata.get("legacy_session_scope_ids")
            if isinstance(legacy_scope_ids, (list, tuple)) and observed_value in legacy_scope_ids:
                return True
            return observed_value == expected_metadata.get("legacy_session_scope_id")

        if key == "session_state_path":
            legacy_state_paths = expected_metadata.get("legacy_session_state_paths")
            if (
                isinstance(legacy_state_paths, (list, tuple))
                and observed_value in legacy_state_paths
            ):
                return True
            return observed_value == expected_metadata.get("legacy_session_state_path")

        if key == "node_id":
            legacy_node_aliases = expected_metadata.get("legacy_node_aliases")
            if (
                isinstance(legacy_node_aliases, (list, tuple))
                and observed_value in legacy_node_aliases
            ):
                return True
            return observed_value == expected_metadata.get("legacy_node_id")

        if key == "parent_node_id":
            legacy_parent_node_aliases = expected_metadata.get("legacy_parent_node_aliases")
            if (
                isinstance(legacy_parent_node_aliases, (list, tuple))
                and observed_value in legacy_parent_node_aliases
            ):
                return True
            return observed_value == expected_metadata.get("legacy_parent_node_id")

        return False

    @staticmethod
    def _runtime_handle_claims_foreign_ac_scope(
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        is_sub_ac: bool,
    ) -> bool:
        """Return True when the handle explicitly belongs to another AC scope."""
        if runtime_handle is None:
            return False

        metadata = runtime_handle.metadata
        for key in _AC_RUNTIME_SCOPE_METADATA_KEYS:
            if (
                key in metadata
                and not ACRuntimeHandleManager._metadata_value_matches_expected_scope(
                    key,
                    metadata.get(key),
                    expected_metadata,
                )
            ):
                return True

        if is_sub_ac:
            return metadata.get("ac_index") is not None

        return (
            metadata.get("parent_ac_index") is not None or metadata.get("sub_ac_index") is not None
        )

    @classmethod
    def _runtime_handle_matches_ac_scope_for_resume(
        cls,
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        is_sub_ac: bool,
    ) -> bool:
        """Return True when a resumable handle is fully owned by the current AC scope."""
        if runtime_handle is None or cls._runtime_resume_session_id(runtime_handle) is None:
            return False

        metadata = runtime_handle.metadata
        matched_scope_key = False
        for key in _AC_RUNTIME_SCOPE_METADATA_KEYS:
            if key not in metadata:
                continue
            matched_scope_key = True
            if not cls._metadata_value_matches_expected_scope(
                key,
                metadata.get(key),
                expected_metadata,
            ):
                return False

        if not matched_scope_key:
            return False

        if is_sub_ac:
            return (
                metadata.get("parent_ac_index") == expected_metadata.get("parent_ac_index")
                and metadata.get("sub_ac_index") == expected_metadata.get("sub_ac_index")
                and metadata.get("ac_index") is None
            )

        return (
            metadata.get("ac_index") == expected_metadata.get("ac_index")
            and metadata.get("parent_ac_index") is None
            and metadata.get("sub_ac_index") is None
        )

    @staticmethod
    def _bind_runtime_handle_to_ac_scope(
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        scrub_resume_state: bool = False,
    ) -> RuntimeHandle | None:
        """Overlay normalized AC ownership metadata onto a runtime handle."""
        if runtime_handle is None:
            return None

        metadata = dict(runtime_handle.metadata)
        for key in _AC_RUNTIME_OWNERSHIP_METADATA_KEYS:
            metadata.pop(key, None)
        if scrub_resume_state:
            for key in _AC_RUNTIME_RESUME_METADATA_KEYS:
                metadata.pop(key, None)
        metadata.update(expected_metadata)

        return replace(
            runtime_handle,
            native_session_id=None if scrub_resume_state else runtime_handle.native_session_id,
            conversation_id=None if scrub_resume_state else runtime_handle.conversation_id,
            previous_response_id=None
            if scrub_resume_state
            else runtime_handle.previous_response_id,
            transcript_path=None if scrub_resume_state else runtime_handle.transcript_path,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    @staticmethod
    def _canonical_runtime_backend(value: object) -> str | None:
        """Resolve a runtime backend selector without changing a persisted handle."""
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return RuntimeHandle(backend=value).backend
        except ValueError:
            return None

    def _raw_provider_identity_matches_runtime(
        self,
        runtime_handle: RuntimeHandle,
        *,
        require_workspace: bool,
    ) -> bool:
        """Validate provider identity before AC normalization can overwrite it.

        A persisted handle is provider continuity, not merely configuration.  In
        particular, ``_build_ac_runtime_handle`` must not turn a Claude handle
        into a Codex handle (or replace its approval/workspace identity) before
        the capsule boundary has checked the original values.
        """
        expected_backend = self._canonical_runtime_backend(
            getattr(self._adapter, "runtime_backend", None)
        )
        if expected_backend is None:
            # Without a concrete adapter backend there is no safe comparison to
            # make (legacy adapters/test doubles); retain the prior behavior.
            return True
        if runtime_handle.backend != expected_backend:
            return False

        # Provider adapters commonly return the generic kind on the first
        # streamed message; both kinds are valid, but arbitrary persisted kinds
        # are not an acceptable continuity identity.
        if runtime_handle.kind not in {_IMPLEMENTATION_SESSION_KIND, "agent_runtime"}:
            return False

        continuity_values = (
            runtime_handle.native_session_id,
            runtime_handle.conversation_id,
            runtime_handle.previous_response_id,
            runtime_handle.transcript_path,
            runtime_handle.server_session_id,
        )
        raw_server_session_id = runtime_handle.metadata.get("server_session_id")
        if raw_server_session_id is not None and (
            not isinstance(raw_server_session_id, str) or not raw_server_session_id.strip()
        ):
            return False
        if not any(isinstance(value, str) and value.strip() for value in continuity_values):
            # A configuration-only capsule seed has no provider identity to
            # resume; its cwd/approval values are intentionally rebound below.
            return True
        if any(
            value is not None and (not isinstance(value, str) or not value.strip())
            for value in continuity_values
        ):
            return False

        expected_approval_mode = getattr(self._adapter, "permission_mode", None)
        if (
            isinstance(expected_approval_mode, str)
            and expected_approval_mode.strip()
            and runtime_handle.approval_mode is not None
            and runtime_handle.approval_mode != expected_approval_mode.strip()
        ):
            return False

        if require_workspace:
            expected_workspace = self._task_cwd or getattr(
                self._adapter,
                "working_directory",
                None,
            )
            if not isinstance(expected_workspace, (str, os.PathLike)):
                # Test doubles and legacy adapters may not expose a concrete
                # workspace; there is no trustworthy value to compare here.
                return True
            if not isinstance(runtime_handle.cwd, str) or not runtime_handle.cwd.strip():
                return False
            try:
                if os.path.realpath(os.path.expanduser(runtime_handle.cwd)) != os.path.realpath(
                    os.path.expanduser(os.fspath(expected_workspace))
                ):
                    return False
            except (OSError, TypeError, ValueError):
                return False

        return True

    def _normalize_ac_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_scope: Any,
        ac_index: int,
        is_sub_ac: bool,
        parent_ac_index: int | None,
        sub_ac_index: int | None,
        node_identity: ExecutionNodeIdentity | None,
        retry_attempt: int,
        source: str,
        require_resume_scope_match: bool,
    ) -> RuntimeHandle | None:
        """Bind a runtime handle to the active AC scope and reject foreign resumes."""
        if runtime_handle is None:
            return None

        if not self._raw_provider_identity_matches_runtime(
            runtime_handle,
            require_workspace=(
                require_resume_scope_match and self._is_resumable_runtime_handle(runtime_handle)
            ),
        ):
            log.warning(
                "parallel_executor.ac.runtime_handle_provider_identity_rejected",
                source=source,
                observed_backend=runtime_handle.backend,
                observed_kind=runtime_handle.kind,
                observed_approval_mode=runtime_handle.approval_mode,
                observed_cwd=runtime_handle.cwd,
                expected_backend=getattr(self._adapter, "runtime_backend", None),
            )
            return None

        expected_metadata = self._build_expected_ac_runtime_metadata(
            runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )

        if require_resume_scope_match and self._is_resumable_runtime_handle(runtime_handle):
            if not self._runtime_handle_matches_ac_scope_for_resume(
                runtime_handle,
                expected_metadata=expected_metadata,
                is_sub_ac=is_sub_ac,
            ):
                log.warning(
                    "parallel_executor.ac.runtime_handle_scope_rejected",
                    source=source,
                    ac_index=ac_index,
                    is_sub_ac=is_sub_ac,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=sub_ac_index,
                    retry_attempt=retry_attempt,
                    expected_session_scope_id=runtime_scope.aggregate_id,
                    observed_session_scope_id=runtime_handle.metadata.get("session_scope_id"),
                    observed_ac_index=runtime_handle.metadata.get("ac_index"),
                    observed_parent_ac_index=runtime_handle.metadata.get("parent_ac_index"),
                    observed_sub_ac_index=runtime_handle.metadata.get("sub_ac_index"),
                )
                return None

        scrub_resume_state = self._runtime_handle_claims_foreign_ac_scope(
            runtime_handle,
            expected_metadata=expected_metadata,
            is_sub_ac=is_sub_ac,
        )
        if scrub_resume_state:
            log.warning(
                "parallel_executor.ac.runtime_handle_scope_scrubbed",
                source=source,
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                retry_attempt=retry_attempt,
                expected_session_scope_id=runtime_scope.aggregate_id,
                observed_session_scope_id=runtime_handle.metadata.get("session_scope_id"),
                observed_ac_index=runtime_handle.metadata.get("ac_index"),
                observed_parent_ac_index=runtime_handle.metadata.get("parent_ac_index"),
                observed_sub_ac_index=runtime_handle.metadata.get("sub_ac_index"),
            )

        normalized_handle = self._bind_runtime_handle_to_ac_scope(
            runtime_handle,
            expected_metadata=expected_metadata,
            scrub_resume_state=scrub_resume_state,
        )
        approval_mode = getattr(self._adapter, "permission_mode", None)
        if normalized_handle is not None and isinstance(approval_mode, str):
            normalized_approval_mode = approval_mode.strip()
            if normalized_approval_mode:
                normalized_handle = replace(
                    normalized_handle,
                    approval_mode=normalized_approval_mode,
                )
        return normalized_handle

    def _build_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
        tool_catalog: tuple[MCPToolDefinition, ...] | None = None,
    ) -> RuntimeHandle | None:
        """Build an AC-scoped runtime handle for implementation work."""
        runtime_identity = self._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )
        cached_seeded_handle = self.runtime_handles.get(runtime_identity.cache_key)
        seeded_handle = self._normalize_ac_runtime_handle(
            cached_seeded_handle,
            runtime_scope=runtime_identity.runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            source="cache",
            require_resume_scope_match=True,
        )
        if cached_seeded_handle is not None and seeded_handle is None:
            self.runtime_handles.pop(runtime_identity.cache_key, None)
        backend = self._adapter.runtime_backend
        if not backend:
            return None

        cwd = self._task_cwd or self._adapter.working_directory
        approval_mode = getattr(self._adapter, "permission_mode", None)
        metadata: dict[str, Any] = dict(seeded_handle.metadata) if seeded_handle is not None else {}
        metadata.update(runtime_identity.to_metadata())
        if self._process_local_resume_nonce is not None:
            metadata["process_local_resume_nonce"] = self._process_local_resume_nonce
        metadata.setdefault("turn_number", 1)
        metadata.setdefault(
            "turn_id",
            self._default_turn_id(runtime_identity, int(metadata["turn_number"])),
        )
        if tool_catalog is not None:
            metadata["tool_catalog"] = serialize_tool_catalog(tool_catalog)
            capability_graph = build_capability_graph(tool_catalog)
            policy_context = PolicyContext(
                runtime_backend=backend,
                session_role=PolicySessionRole.IMPLEMENTATION,
                execution_phase=PolicyExecutionPhase.IMPLEMENTATION,
            )
            metadata["capability_graph"] = serialize_capability_graph(capability_graph)
            metadata["control_plane"] = serialize_control_plane_state(
                build_control_plane_state(
                    capability_graph,
                    evaluate_capability_policy(capability_graph, policy_context),
                )
            )

        if seeded_handle is not None:
            return replace(
                seeded_handle,
                backend=backend,
                kind=seeded_handle.kind or _IMPLEMENTATION_SESSION_KIND,
                cwd=seeded_handle.cwd
                if seeded_handle.cwd
                else cwd
                if isinstance(cwd, str) and cwd
                else None,
                approval_mode=approval_mode
                if isinstance(approval_mode, str) and approval_mode
                else seeded_handle.approval_mode,
                updated_at=datetime.now(UTC).isoformat(),
                metadata=metadata,
            )

        return RuntimeHandle(
            backend=backend,
            kind=_IMPLEMENTATION_SESSION_KIND,
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            approval_mode=approval_mode
            if isinstance(approval_mode, str) and approval_mode
            else None,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    async def _load_persisted_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
        expected_capsule_fingerprint: str | None = None,
        expected_process_local_resume_nonce: str | None = None,
    ) -> RuntimeHandle | None:
        """Load the latest reusable AC-scoped runtime handle from execution events."""
        runtime_identity = self._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )
        cached_runtime_handle = self.runtime_handles.get(runtime_identity.cache_key)
        cached_dispatch_id = (
            cached_runtime_handle.metadata.get("ac_dispatch_id")
            if cached_runtime_handle is not None
            else None
        )
        if expected_capsule_fingerprint is not None and self.is_dispatch_non_replayable(
            cached_dispatch_id
        ):
            raise AmbiguousACExecutionError(
                "cached AC dispatch crossed an unsafe seal boundary; refusing replay"
            )
        cached_handle = self._normalize_ac_runtime_handle(
            cached_runtime_handle,
            runtime_scope=runtime_identity.runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            source="cache",
            require_resume_scope_match=True,
        )
        if cached_runtime_handle is not None and cached_handle is None:
            self.runtime_handles.pop(runtime_identity.cache_key, None)
        if cached_handle is not None and expected_capsule_fingerprint is None:
            return cached_handle

        if expected_capsule_fingerprint is not None:
            cached_fingerprint = (
                cached_handle.metadata.get("ac_capsule_fingerprint") if cached_handle else None
            )
            if cached_fingerprint not in {None, expected_capsule_fingerprint}:
                raise AmbiguousACExecutionError(
                    "cached runtime handle disagrees with the current AC capsule"
                )
            cached_nonce = (
                cached_handle.metadata.get("process_local_resume_nonce") if cached_handle else None
            )
            if (
                expected_process_local_resume_nonce is not None
                and cached_handle is not None
                and self._is_resumable_runtime_handle(cached_handle)
                and cached_nonce != expected_process_local_resume_nonce
            ):
                raise AmbiguousACExecutionError(
                    "cached runtime handle belongs to a different process-local authority"
                )
            # A resumable cached handle without a capsule fingerprint belongs to
            # the pre-capsule lifecycle.  It cannot be silently reclassified as
            # this attempt: doing so would inherit provider continuity without a
            # durable capsule/dispatch authority.  Configuration-only handles
            # (no provider continuity) remain reusable as fresh-session inputs.
            if (
                cached_handle is not None
                and cached_fingerprint is None
                and self._is_resumable_runtime_handle(cached_handle)
            ):
                self.runtime_handles.pop(runtime_identity.cache_key, None)
                cached_handle = None

        candidate_scope_ids = (
            (runtime_identity.session_scope_id,)
            if expected_capsule_fingerprint is not None
            else (runtime_identity.session_scope_id, *runtime_identity.legacy_session_scope_ids)
        )
        for candidate_scope_id in dict.fromkeys(candidate_scope_ids):
            try:
                events = await self._event_store.replay(
                    runtime_identity.runtime_scope.aggregate_type,
                    candidate_scope_id,
                )
            except Exception:
                log.exception(
                    "parallel_executor.ac.runtime_handle_load_failed",
                    ac_index=ac_index,
                    is_sub_ac=is_sub_ac,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=sub_ac_index,
                    retry_attempt=retry_attempt,
                    session_scope_id=candidate_scope_id,
                )
                if expected_capsule_fingerprint is not None:
                    raise
                continue

            if expected_capsule_fingerprint is not None:
                compiled_indices, dispatch_indices, seal_indices = (
                    self._validate_capsule_dispatch_chain(
                        events,
                        runtime_identity=runtime_identity,
                        expected_capsule_fingerprint=expected_capsule_fingerprint,
                    )
                )
                if not compiled_indices:
                    continue
                latest_dispatch_id: str | None = None
                if dispatch_indices:
                    latest_dispatch_data = events[dispatch_indices[-1]].data
                    if isinstance(latest_dispatch_data, dict):
                        raw_latest_dispatch_id = latest_dispatch_data.get("ac_dispatch_id")
                        if isinstance(raw_latest_dispatch_id, str):
                            latest_dispatch_id = raw_latest_dispatch_id
                        latest_dispatch_kind = latest_dispatch_data.get("dispatch_kind")
                    else:
                        latest_dispatch_kind = None
                else:
                    latest_dispatch_kind = None
                if self.is_dispatch_non_replayable(latest_dispatch_id):
                    raise AmbiguousACExecutionError(
                        "latest AC dispatch crossed an unsafe seal boundary; refusing replay"
                    )
                matching_indices = [
                    index
                    for index, event in enumerate(events)
                    if self._event_matches_ac_runtime_identity(
                        event.data if isinstance(event.data, dict) else {}, runtime_identity
                    )
                ]
                last_terminal_index = max(
                    (
                        index
                        for index in matching_indices
                        if events[index].type in _NON_REUSABLE_RUNTIME_EVENT_TYPES
                    ),
                        default=-1,
                    )
                last_seal_index = max(seal_indices, default=-1)
                last_dispatch_index = max(dispatch_indices, default=-1)
                if last_seal_index > last_terminal_index and last_seal_index >= last_dispatch_index:
                    raise AmbiguousACExecutionError(
                        "AC dispatch boundary is sealed and cannot be replayed"
                    )
                # The executor can only reconstruct the primary AC prompt on
                # entry.  A crash after a SessionSignal follow-up dispatch
                # would otherwise restore its runtime handle and resend the
                # original AC, losing the signal turn while claiming
                # same-attempt recovery.  Until exact follow-up input replay is
                # implemented, fail closed whenever that is the latest
                # unsealed/non-terminal phase.
                if (
                    latest_dispatch_kind == "session_signal_followup"
                    and last_dispatch_index > last_seal_index
                    and last_dispatch_index > last_terminal_index
                ):
                    raise AmbiguousACExecutionError(
                        "latest AC dispatch is a SessionSignal follow-up whose phase cannot be resumed"
                    )

            for event in reversed(events):
                event_data = event.data if isinstance(event.data, dict) else {}
                if not self._event_matches_ac_runtime_identity(event_data, runtime_identity):
                    continue

                if event.type in _NON_REUSABLE_RUNTIME_EVENT_TYPES:
                    if expected_capsule_fingerprint is not None:
                        raise AmbiguousACExecutionError(
                            "AC attempt already has a terminal lifecycle; refusing redispatch"
                        )
                    self._forget_ac_runtime_handle(
                        ac_index,
                        execution_context_id=execution_context_id,
                        is_sub_ac=is_sub_ac,
                        parent_ac_index=parent_ac_index,
                        sub_ac_index=sub_ac_index,
                        node_identity=node_identity,
                        retry_attempt=retry_attempt,
                    )
                    return None
                if event.type not in _REUSABLE_RUNTIME_EVENT_TYPES:
                    continue

                runtime_payload = event_data.get("runtime")
                try:
                    runtime_handle = RuntimeHandle.from_dict(runtime_payload)
                except ValueError as exc:
                    log.warning(
                        "parallel_executor.persisted_runtime_handle_invalid",
                        aggregate_id=event.aggregate_id,
                        event_type=event.type,
                        error=str(exc),
                        runtime_keys=sorted(runtime_payload)
                        if isinstance(runtime_payload, dict)
                        else None,
                    )
                    continue
                if runtime_handle is None:
                    continue
                if expected_capsule_fingerprint is not None:
                    persisted_fingerprint = runtime_handle.metadata.get("ac_capsule_fingerprint")
                    if persisted_fingerprint != expected_capsule_fingerprint:
                        raise AmbiguousACExecutionError(
                            "persisted runtime handle disagrees with the durable AC capsule"
                        )
                    persisted_dispatch_id = runtime_handle.metadata.get("ac_dispatch_id")
                    if latest_dispatch_id is None or persisted_dispatch_id != latest_dispatch_id:
                        raise AmbiguousACExecutionError(
                            "persisted runtime handle does not belong to the latest AC dispatch"
                        )
                    persisted_nonce = runtime_handle.metadata.get("process_local_resume_nonce")
                    if (
                        expected_process_local_resume_nonce is not None
                        and persisted_nonce != expected_process_local_resume_nonce
                    ):
                        raise AmbiguousACExecutionError(
                            "persisted runtime handle belongs to a different process-local authority"
                        )
                runtime_handle = self._normalize_ac_runtime_handle(
                    runtime_handle,
                    runtime_scope=runtime_identity.runtime_scope,
                    ac_index=ac_index,
                    is_sub_ac=is_sub_ac,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=sub_ac_index,
                    node_identity=node_identity,
                    retry_attempt=retry_attempt,
                    source="persisted_event",
                    require_resume_scope_match=True,
                )
                if runtime_handle is None:
                    continue

                self.runtime_handles[runtime_identity.cache_key] = runtime_handle
                return runtime_handle

            if expected_capsule_fingerprint is not None:
                if any(
                    event.type == _AC_ATTEMPT_DISPATCHED_EVENT
                    and self._event_matches_ac_runtime_identity(
                        event.data if isinstance(event.data, dict) else {}, runtime_identity
                    )
                    for event in events
                ):
                    raise AmbiguousACExecutionError(
                        "AC provider boundary exists without a reusable same-attempt handle"
                    )

        return None

    def _remember_ac_runtime_handle(
        self,
        ac_index: int,
        runtime_handle: RuntimeHandle | None,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
    ) -> RuntimeHandle | None:
        """Cache the latest reusable AC-scoped runtime handle."""
        if runtime_handle is None:
            return None

        runtime_identity = self._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )
        normalized_handle = self._normalize_ac_runtime_handle(
            runtime_handle,
            runtime_scope=runtime_identity.runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            source="runtime",
            require_resume_scope_match=False,
        )
        if normalized_handle is None:
            return None

        previous_handle = self.runtime_handles.get(runtime_identity.cache_key)
        normalized_previous_handle = self._normalize_ac_runtime_handle(
            previous_handle,
            runtime_scope=runtime_identity.runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            source="cache",
            require_resume_scope_match=False,
        )
        normalized_handle = self._augment_ac_runtime_handle(
            normalized_handle,
            runtime_identity=runtime_identity,
            previous_handle=normalized_previous_handle,
        )
        self.runtime_handles[runtime_identity.cache_key] = normalized_handle
        return normalized_handle

    def _forget_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
    ) -> None:
        """Drop live cached handle state once an AC scope is no longer resumable."""
        runtime_identity = self._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )
        self.runtime_handles.pop(runtime_identity.cache_key, None)

    async def _terminate_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_scope_id: str,
    ) -> None:
        """Best-effort termination for live AC-scoped runtimes."""
        if runtime_handle is None or not runtime_handle.can_terminate:
            return

        try:
            terminated = await runtime_handle.terminate()
        except Exception as exc:
            log.warning(
                "parallel_executor.runtime_handle_terminate_failed",
                runtime_scope_id=runtime_scope_id,
                backend=runtime_handle.backend,
                error=str(exc),
            )
            return

        if terminated:
            log.info(
                "parallel_executor.runtime_handle_terminated",
                runtime_scope_id=runtime_scope_id,
                backend=runtime_handle.backend,
            )

    @staticmethod
    def _resolve_ac_runtime_identity(
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
    ) -> ACRuntimeIdentity:
        """Return the normalized AC runtime identity for one implementation attempt."""
        return build_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )

    @staticmethod
    def _event_matches_ac_runtime_identity(
        event_data: dict[str, Any],
        runtime_identity: ACRuntimeIdentity,
    ) -> bool:
        """Return True when an event belongs to the requested AC attempt."""
        runtime_payload = event_data.get("runtime")
        runtime_metadata: dict[str, Any] = {}
        if isinstance(runtime_payload, dict):
            raw_metadata = runtime_payload.get("metadata")
            if isinstance(raw_metadata, dict):
                runtime_metadata = raw_metadata

        expected_metadata = runtime_identity.to_metadata()
        matched_identity_key = False
        for key in _AC_RUNTIME_OWNERSHIP_METADATA_KEYS:
            if key in event_data:
                observed_value = event_data.get(key)
            elif key in runtime_metadata:
                observed_value = runtime_metadata.get(key)
            else:
                continue

            matched_identity_key = True
            if not ACRuntimeHandleManager._metadata_value_matches_expected_scope(
                key,
                observed_value,
                expected_metadata,
            ):
                return False

        return matched_identity_key

    @classmethod
    def _validate_capsule_dispatch_chain(
        cls,
        events: list[Any],
        *,
        runtime_identity: ACRuntimeIdentity,
        expected_capsule_fingerprint: str,
    ) -> tuple[list[int], list[int], list[int]]:
        """Validate the ordered durable capsule → dispatch → seal chain.

        A matching event is not authority by itself: dispatch IDs, predecessor
        links, capsule fingerprints, runtime bindings, and event ordering must
        all agree before recovery can consider a provider handle reusable.
        """
        compiled_indices: list[int] = []
        dispatch_indices: list[int] = []
        seal_indices: list[int] = []
        dispatch_ids: set[str] = set()
        dispatch_index_by_id: dict[str, int] = {}
        previous_dispatch_id: str | None = None

        matching_compiled_exists = any(
            event.type == _AC_CAPSULE_COMPILED_EVENT
            and cls._event_matches_ac_runtime_identity(
                event.data if isinstance(event.data, dict) else {}, runtime_identity
            )
            for event in events
        )
        matching_dispatch_exists = any(
            event.type == _AC_ATTEMPT_DISPATCHED_EVENT
            and cls._event_matches_ac_runtime_identity(
                event.data if isinstance(event.data, dict) else {}, runtime_identity
            )
            for event in events
        )
        if not matching_compiled_exists:
            if matching_dispatch_exists:
                raise AmbiguousACExecutionError(
                    "durable AC dispatch exists without capsule authority"
                )
            return compiled_indices, dispatch_indices, seal_indices

        for index, event in enumerate(events):
            event_data = event.data if isinstance(event.data, dict) else {}
            if not cls._event_matches_ac_runtime_identity(event_data, runtime_identity):
                continue

            if event.type == _AC_CAPSULE_COMPILED_EVENT:
                manifest = ACExecutionCapsuleManifest.from_contract_data(
                    event_data.get("capsule_manifest")
                )
                persisted_fingerprint = event_data.get("capsule_fingerprint")
                if persisted_fingerprint != manifest.fingerprint:
                    raise AmbiguousACExecutionError(
                        "durable AC capsule fingerprint disagrees with its manifest"
                    )
                if manifest.fingerprint != expected_capsule_fingerprint:
                    raise AmbiguousACExecutionError(
                        "durable AC capsule disagrees with the current dispatch"
                    )
                if (
                    manifest.ac_id != runtime_identity.ac_id
                    or manifest.session_attempt_id != runtime_identity.session_attempt_id
                ):
                    raise AmbiguousACExecutionError(
                        "durable AC capsule identity disagrees with the current attempt"
                    )
                compiled_indices.append(index)
                continue

            if event.type == _AC_ATTEMPT_DISPATCHED_EVENT:
                if not any(compiled_index < index for compiled_index in compiled_indices):
                    raise AmbiguousACExecutionError(
                        "AC dispatch precedes its capsule authority"
                    )
                if event_data.get("capsule_fingerprint") != expected_capsule_fingerprint:
                    raise AmbiguousACExecutionError(
                        "AC dispatch capsule fingerprint disagrees with capsule authority"
                    )
                dispatch_id = event_data.get("ac_dispatch_id")
                if not isinstance(dispatch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", dispatch_id):
                    raise AmbiguousACExecutionError("AC dispatch id is malformed")
                if dispatch_id in dispatch_ids:
                    raise AmbiguousACExecutionError("AC dispatch chain contains a duplicate id")
                predecessor = event_data.get("previous_ac_dispatch_id")
                if predecessor != previous_dispatch_id:
                    raise AmbiguousACExecutionError("AC dispatch predecessor chain is invalid")
                dispatch_kind = event_data.get("dispatch_kind")
                if dispatch_kind not in {"primary", "session_signal_followup"}:
                    raise AmbiguousACExecutionError("AC dispatch kind is invalid")
                signal_fields = (
                    event_data.get("signal_id"),
                    event_data.get("signal_mode"),
                    event_data.get("follow_up_input_digest"),
                )
                if dispatch_kind == "primary" and any(
                    value is not None for value in signal_fields
                ):
                    raise AmbiguousACExecutionError(
                        "primary AC dispatch carries unexpected follow-up identity"
                    )
                if dispatch_kind == "session_signal_followup":
                    signal_id, signal_mode, follow_up_input_digest = signal_fields
                    if (
                        not isinstance(signal_id, str)
                        or not signal_id.strip()
                        or signal_mode not in {"inform", "after_turn"}
                        or not isinstance(follow_up_input_digest, str)
                        or not re.fullmatch(
                            r"sha256:[0-9a-f]{64}",
                            follow_up_input_digest,
                        )
                    ):
                        raise AmbiguousACExecutionError(
                            "SessionSignal follow-up dispatch identity is invalid"
                        )
                runtime_payload = event_data.get("runtime")
                if isinstance(runtime_payload, dict):
                    runtime_metadata = runtime_payload.get("metadata")
                    if not isinstance(runtime_metadata, dict):
                        raise AmbiguousACExecutionError(
                            "AC dispatch runtime binding is missing metadata"
                        )
                    if runtime_metadata.get("ac_dispatch_id") != dispatch_id:
                        raise AmbiguousACExecutionError(
                            "AC dispatch runtime binding disagrees with dispatch id"
                        )
                    if runtime_metadata.get("ac_capsule_fingerprint") != expected_capsule_fingerprint:
                        raise AmbiguousACExecutionError(
                            "AC dispatch runtime binding disagrees with capsule authority"
                        )
                dispatch_ids.add(dispatch_id)
                dispatch_index_by_id[dispatch_id] = index
                previous_dispatch_id = dispatch_id
                dispatch_indices.append(index)
                continue

            if event.type == _AC_DISPATCH_SEALED_EVENT:
                if event_data.get("capsule_fingerprint") != expected_capsule_fingerprint:
                    raise AmbiguousACExecutionError(
                        "AC dispatch seal capsule fingerprint disagrees with capsule authority"
                    )
                dispatch_id = event_data.get("ac_dispatch_id")
                if not isinstance(dispatch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", dispatch_id):
                    raise AmbiguousACExecutionError("AC dispatch seal id is malformed")
                dispatch_index = dispatch_index_by_id.get(dispatch_id)
                if dispatch_index is None or dispatch_index >= index:
                    raise AmbiguousACExecutionError(
                        "AC dispatch seal does not follow its dispatch"
                    )
                seal_indices.append(index)

        if not compiled_indices:
            if dispatch_indices:
                raise AmbiguousACExecutionError(
                    "durable AC dispatch exists without capsule authority"
                )
            return compiled_indices, dispatch_indices, seal_indices
        return compiled_indices, dispatch_indices, seal_indices

    @staticmethod
    def _default_turn_id(
        runtime_identity: ACRuntimeIdentity,
        turn_number: int,
    ) -> str:
        """Build a stable logical turn identifier within one AC session attempt."""
        return f"{runtime_identity.session_attempt_id}:turn_{turn_number}"

    @staticmethod
    def _runtime_turn_number(runtime_handle: RuntimeHandle | None) -> int:
        """Return the 1-based logical turn number carried by a runtime handle."""
        if runtime_handle is None:
            return 1

        value = runtime_handle.metadata.get("turn_number")
        if isinstance(value, int) and value > 0:
            return value
        return 1

    @classmethod
    def _runtime_turn_id(
        cls,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_identity: ACRuntimeIdentity,
    ) -> str:
        """Return the stable logical turn identifier for a runtime handle."""
        if runtime_handle is not None:
            value = runtime_handle.metadata.get("turn_id")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return cls._default_turn_id(
            runtime_identity,
            cls._runtime_turn_number(runtime_handle),
        )

    @staticmethod
    def _runtime_recovery_discontinuity(
        runtime_handle: RuntimeHandle | None,
    ) -> dict[str, Any] | None:
        """Return persisted recovery discontinuity metadata when present."""
        if runtime_handle is None:
            return None

        value = runtime_handle.metadata.get("recovery_discontinuity")
        return dict(value) if isinstance(value, dict) else None

    @classmethod
    def _runtime_handle_same_session(
        cls,
        previous_handle: RuntimeHandle | None,
        current_handle: RuntimeHandle | None,
    ) -> bool:
        """Return True when two runtime handles identify the same backend session."""
        if previous_handle is None or current_handle is None:
            return False

        previous_native = previous_handle.native_session_id
        current_native = current_handle.native_session_id
        if previous_native and current_native:
            return previous_native == current_native

        previous_server = previous_handle.server_session_id
        current_server = current_handle.server_session_id
        if previous_server and current_server:
            return previous_server == current_server

        previous_resume = previous_handle.resume_session_id
        current_resume = current_handle.resume_session_id
        if previous_resume and current_resume:
            return previous_resume == current_resume

        return False

    @classmethod
    def _build_recovery_discontinuity(
        cls,
        *,
        previous_handle: RuntimeHandle | None,
        current_handle: RuntimeHandle,
        runtime_identity: ACRuntimeIdentity,
    ) -> dict[str, Any] | None:
        """Build failed-to-replacement session/turn linkage for soft recovery."""
        if previous_handle is None or previous_handle.resume_session_id is None:
            return None
        if cls._runtime_handle_same_session(previous_handle, current_handle):
            return None

        current_event_type = current_handle.metadata.get("runtime_event_type")
        replacement_event = isinstance(
            current_event_type, str
        ) and current_event_type.strip().lower() in {"session.started", "thread.started"}
        previous_native = previous_handle.native_session_id
        current_native = current_handle.native_session_id
        previous_server = previous_handle.server_session_id
        current_server = current_handle.server_session_id
        native_changed = bool(
            previous_native and current_native and previous_native != current_native
        )
        server_changed = bool(
            previous_server and current_server and previous_server != current_server
        )
        if not replacement_event and not native_changed and not server_changed:
            return None

        failed_turn_number = cls._runtime_turn_number(previous_handle)
        replacement_turn_number = max(
            cls._runtime_turn_number(current_handle),
            failed_turn_number + 1,
        )

        return {
            "reason": "replacement_session",
            "failed": {
                "session_id": previous_native,
                "server_session_id": previous_server,
                "resume_session_id": previous_handle.resume_session_id,
                "turn_id": cls._runtime_turn_id(
                    previous_handle,
                    runtime_identity=runtime_identity,
                ),
                "turn_number": failed_turn_number,
            },
            "replacement": {
                "session_id": current_native,
                "server_session_id": current_server,
                "resume_session_id": current_handle.resume_session_id,
                "turn_id": cls._default_turn_id(runtime_identity, replacement_turn_number),
                "turn_number": replacement_turn_number,
            },
        }

    @classmethod
    def _augment_ac_runtime_handle(
        cls,
        runtime_handle: RuntimeHandle,
        *,
        runtime_identity: ACRuntimeIdentity,
        previous_handle: RuntimeHandle | None,
    ) -> RuntimeHandle:
        """Carry forward logical turn state and record same-attempt recovery linkage."""
        metadata = dict(runtime_handle.metadata)
        metadata.setdefault("turn_number", cls._runtime_turn_number(runtime_handle))
        metadata.setdefault(
            "turn_id",
            cls._runtime_turn_id(runtime_handle, runtime_identity=runtime_identity),
        )
        if previous_handle is not None:
            for key in (
                "ac_capsule_fingerprint",
                "ac_dispatch_id",
                "ac_session_origin",
                "process_local_resume_nonce",
            ):
                if key not in metadata and key in previous_handle.metadata:
                    metadata[key] = previous_handle.metadata[key]

        if previous_handle is not None and cls._runtime_handle_same_session(
            previous_handle,
            runtime_handle,
        ):
            previous_turn_number = cls._runtime_turn_number(previous_handle)
            if previous_turn_number > cls._runtime_turn_number(runtime_handle):
                metadata["turn_number"] = previous_turn_number
                metadata["turn_id"] = cls._runtime_turn_id(
                    previous_handle,
                    runtime_identity=runtime_identity,
                )

            previous_recovery_discontinuity = cls._runtime_recovery_discontinuity(previous_handle)
            if previous_recovery_discontinuity is not None:
                metadata.setdefault(
                    "recovery_discontinuity",
                    previous_recovery_discontinuity,
                )

        recovery_discontinuity = cls._build_recovery_discontinuity(
            previous_handle=previous_handle,
            current_handle=runtime_handle,
            runtime_identity=runtime_identity,
        )
        if recovery_discontinuity is not None:
            replacement = recovery_discontinuity["replacement"]
            metadata["turn_number"] = replacement["turn_number"]
            metadata["turn_id"] = replacement["turn_id"]
            metadata["recovery_discontinuity"] = recovery_discontinuity

        if metadata == runtime_handle.metadata:
            return runtime_handle

        return replace(
            runtime_handle,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    @staticmethod
    def _with_native_session_id(
        runtime_handle: RuntimeHandle | None,
        native_session_id: str | None,
    ) -> RuntimeHandle | None:
        """Attach a discovered native session id to an existing runtime handle."""
        if runtime_handle is None or not native_session_id:
            return runtime_handle
        if runtime_handle.native_session_id == native_session_id:
            return runtime_handle

        return replace(
            runtime_handle,
            native_session_id=native_session_id,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=dict(runtime_handle.metadata),
        )

    @staticmethod
    def _is_resumable_runtime_handle(runtime_handle: RuntimeHandle | None) -> bool:
        """Return True when the handle can reconnect to an existing backend session."""
        return ACRuntimeHandleManager._runtime_resume_session_id(runtime_handle) is not None

    @staticmethod
    def _runtime_resume_session_id(runtime_handle: RuntimeHandle | None) -> str | None:
        """Return the minimal persisted session identifier used for reconnect/resume."""
        if runtime_handle is None:
            return None
        return runtime_handle.resume_session_id

    async def _emit_ac_runtime_event(
        self,
        *,
        event_type: str,
        runtime_identity: ACRuntimeIdentity,
        ac_content: str,
        runtime_handle: RuntimeHandle | None,
        execution_id: str | None = None,
        session_id: str | None = None,
        orchestrator_session_id: str | None = None,
        result_summary: str | None = None,
        success: bool | None = None,
        error: str | None = None,
    ) -> None:
        """Persist AC-scoped runtime lifecycle events using normalized metadata."""
        from ouroboros.events.base import BaseEvent

        effective_session_id = session_id or self._runtime_resume_session_id(runtime_handle)
        server_session_id = runtime_handle.server_session_id if runtime_handle is not None else None
        identity_metadata = runtime_identity.to_metadata()

        event = BaseEvent(
            type=event_type,
            aggregate_type=runtime_identity.runtime_scope.aggregate_type,
            aggregate_id=runtime_identity.session_scope_id,
            data={
                **identity_metadata,
                "ac_id": runtime_identity.ac_id,
                "acceptance_criterion": ac_content,
                "scope": runtime_identity.scope,
                "session_role": runtime_identity.session_role,
                "retry_attempt": runtime_identity.retry_attempt,
                "attempt_number": runtime_identity.attempt_number,
                "execution_id": execution_id,
                "session_scope_id": runtime_identity.session_scope_id,
                "session_attempt_id": runtime_identity.session_attempt_id,
                "session_state_path": runtime_identity.session_state_path,
                "runtime_backend": (runtime_handle.backend if runtime_handle is not None else None),
                "runtime": (
                    runtime_handle.to_persisted_dict() if runtime_handle is not None else None
                ),
                "session_id": effective_session_id,
                "orchestrator_session_id": orchestrator_session_id,
                "server_session_id": server_session_id,
                "success": success,
                "result_summary": result_summary,
                "error": error,
            },
        )
        if runtime_handle is not None:
            turn_id = runtime_handle.metadata.get("turn_id")
            if isinstance(turn_id, str) and turn_id.strip():
                event.data["turn_id"] = turn_id.strip()

            turn_number = runtime_handle.metadata.get("turn_number")
            if isinstance(turn_number, int) and turn_number > 0:
                event.data["turn_number"] = turn_number

            recovery_discontinuity = self._runtime_recovery_discontinuity(runtime_handle)
            if recovery_discontinuity is not None:
                event.data["recovery_discontinuity"] = recovery_discontinuity
        tool_catalog = runtime_handle_tool_catalog(runtime_handle)
        if tool_catalog is not None:
            event.data["tool_catalog"] = tool_catalog
        await self._event_store.append(event)
        if success is True and execution_id:
            try:
                await self._event_store.append(
                    BaseEvent(
                        type="execution.ac.completed",
                        aggregate_type="execution",
                        aggregate_id=execution_id,
                        data={
                            **identity_metadata,
                            "ac_id": runtime_identity.ac_id,
                            "acceptance_criterion": ac_content,
                            "execution_id": execution_id,
                            "session_id": effective_session_id,
                            "session_scope_id": runtime_identity.session_scope_id,
                            "retry_attempt": runtime_identity.retry_attempt,
                            "attempt_number": runtime_identity.attempt_number,
                            "success": True,
                            "result_summary": result_summary,
                        },
                    )
                )
            except Exception as exc:
                log.warning(
                    "parallel_executor.execution_ac_completed_append_failed",
                    ac_id=runtime_identity.ac_id,
                    execution_id=execution_id,
                    error=str(exc),
                )
