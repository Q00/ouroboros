"""AC-scoped runtime-handle lifecycle management for parallel execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
import inspect
import os
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
_AC_EFFECT_EVENT_TYPES = frozenset(
    {
        "execution.tool.started",
        "execution.tool.completed",
    }
)


class AmbiguousACExecutionError(RuntimeError):
    """A prior AC attempt may have external effects but cannot be resumed safely."""


class ACRuntimeHandleManager:
    """Owns AC runtime-handle cache, scope rebinding, and lifecycle events."""

    def __init__(
        self,
        adapter: AgentRuntime,
        event_store: EventStore,
        *,
        task_cwd: str | None,
    ) -> None:
        self._adapter = adapter
        self._event_store = event_store
        self._task_cwd = task_cwd
        self.runtime_handles: dict[str, RuntimeHandle] = {}
        self._event_replay_cache: dict[tuple[str, str], list[Any]] = {}
        self._event_replay_cursors: dict[tuple[str, str], int] = {}

    async def _replay_runtime_scope_events(
        self,
        aggregate_type: str,
        aggregate_id: str,
    ) -> list[Any]:
        """Incrementally replay one runtime scope when the store supports cursors."""
        cache_key = (aggregate_type, aggregate_id)
        incremental_descriptor = inspect.getattr_static(
            type(self._event_store),
            "get_events_after",
            None,
        )
        if incremental_descriptor is None:
            return await self._event_store.replay(aggregate_type, aggregate_id)

        getter = object.__getattribute__(self._event_store, "get_events_after")
        cursor = self._event_replay_cursors.get(cache_key, 0)
        result = await getter(aggregate_type, aggregate_id, cursor)
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], list)
            or not isinstance(result[1], int)
        ):
            return await self._event_store.replay(aggregate_type, aggregate_id)
        new_events, next_cursor = result
        cached_events = self._event_replay_cache.setdefault(cache_key, [])
        cached_events.extend(new_events)
        self._event_replay_cursors[cache_key] = next_cursor
        return list(cached_events)

    @staticmethod
    def _event_retry_attempt(event_data: Mapping[str, Any]) -> int | None:
        value = event_data.get("retry_attempt")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        attempt_number = event_data.get("attempt_number")
        if (
            isinstance(attempt_number, int)
            and not isinstance(attempt_number, bool)
            and attempt_number >= 1
        ):
            return attempt_number - 1
        session_attempt_id = event_data.get("session_attempt_id")
        if isinstance(session_attempt_id, str):
            marker, separator, suffix = session_attempt_id.rpartition("_attempt_")
            if marker and separator and suffix.isdigit() and int(suffix) >= 1:
                return int(suffix) - 1
        runtime_payload = event_data.get("runtime")
        if isinstance(runtime_payload, Mapping):
            metadata = runtime_payload.get("metadata")
            if isinstance(metadata, Mapping):
                value = metadata.get("retry_attempt")
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
        return None

    @classmethod
    def _events_for_runtime_attempt(
        cls,
        events: list[Any],
        *,
        retry_attempt: int,
    ) -> list[Any]:
        """Slice the append-only AC stream down to the current attempt tail."""
        attempt_events: list[Any] = []
        for event in reversed(events):
            event_data = event.data if isinstance(getattr(event, "data", None), dict) else {}
            observed_attempt = cls._event_retry_attempt(event_data)
            if observed_attempt == retry_attempt:
                attempt_events.append(event)
                continue
            if observed_attempt is not None and observed_attempt < retry_attempt:
                break
        attempt_events.reverse()
        return attempt_events

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

    def _validate_resumable_handle_authority(
        self,
        runtime_handle: RuntimeHandle,
        *,
        expected_workspace: str | None,
    ) -> None:
        """Reject provider continuity from a different runtime authority."""
        if not self._is_resumable_runtime_handle(runtime_handle):
            return
        runtime_backend = getattr(self._adapter, "runtime_backend", None)
        if isinstance(runtime_backend, str) and runtime_backend:
            if runtime_handle.backend != runtime_backend:
                raise ValueError("persisted runtime handle backend authority changed")
        permission_mode = getattr(self._adapter, "permission_mode", None)
        if isinstance(permission_mode, str) and permission_mode:
            if runtime_handle.approval_mode != permission_mode:
                raise ValueError("persisted runtime handle permission authority changed")
        if expected_workspace is not None:
            if not runtime_handle.cwd:
                raise ValueError("persisted runtime handle workspace authority is missing")
            observed_workspace = os.path.realpath(os.path.expanduser(runtime_handle.cwd))
            if observed_workspace != expected_workspace:
                raise ValueError("persisted runtime handle workspace authority changed")

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
        expected_capsule_workspace: str | None = None,
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
        if cached_runtime_handle is not None and expected_capsule_fingerprint is not None:
            self._validate_resumable_handle_authority(
                cached_runtime_handle,
                expected_workspace=expected_capsule_workspace,
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

        candidate_scope_ids = (
            (runtime_identity.session_scope_id,)
            if expected_capsule_fingerprint is not None
            else (
                runtime_identity.session_scope_id,
                *runtime_identity.legacy_session_scope_ids,
            )
        )
        for candidate_scope_id in dict.fromkeys(candidate_scope_ids):
            try:
                events = await self._replay_runtime_scope_events(
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
            attempt_events = (
                self._events_for_runtime_attempt(
                    events,
                    retry_attempt=runtime_identity.retry_attempt,
                )
                if expected_capsule_fingerprint is not None
                else events
            )

            capsule_authorized = expected_capsule_fingerprint is None
            if expected_capsule_fingerprint is not None:
                for event in attempt_events:
                    if event.type != "execution.ac.capsule.compiled":
                        continue
                    event_data = event.data if isinstance(event.data, dict) else {}
                    if (
                        event_data.get("ac_id") != runtime_identity.ac_id
                        or event_data.get("session_attempt_id")
                        != runtime_identity.session_attempt_id
                    ):
                        continue
                    try:
                        manifest = ACExecutionCapsuleManifest.from_contract_data(
                            event_data.get("capsule_manifest")
                        )
                    except ValueError as exc:
                        raise ValueError("durable AC capsule manifest is malformed") from exc
                    persisted_fingerprint = event_data.get("capsule_fingerprint")
                    if persisted_fingerprint != manifest.fingerprint:
                        raise ValueError(
                            "durable AC capsule fingerprint disagrees with its manifest"
                        )
                    if (
                        manifest.ac_id != runtime_identity.ac_id
                        or manifest.session_attempt_id != runtime_identity.session_attempt_id
                    ):
                        raise ValueError(
                            "durable AC capsule manifest disagrees with its event identity"
                        )
                    if manifest.fingerprint != expected_capsule_fingerprint:
                        raise ValueError(
                            "durable AC capsule fingerprint disagrees with the current dispatch"
                        )
                    capsule_authorized = True

            if not capsule_authorized:
                continue

            if cached_handle is not None:
                cached_fingerprint = cached_handle.metadata.get("ac_capsule_fingerprint")
                cached_attempt_id = cached_handle.metadata.get("session_attempt_id")
                if (
                    cached_fingerprint == expected_capsule_fingerprint
                    and cached_attempt_id == runtime_identity.session_attempt_id
                ):
                    return cached_handle
                self.runtime_handles.pop(runtime_identity.cache_key, None)
                cached_handle = None

            unresolved_effect_event = any(
                event.type in _AC_EFFECT_EVENT_TYPES
                and self._event_matches_ac_runtime_identity(
                    event.data if isinstance(event.data, dict) else {},
                    runtime_identity,
                )
                for event in attempt_events
            )

            for event in reversed(attempt_events):
                event_data = event.data if isinstance(event.data, dict) else {}
                if not self._event_matches_ac_runtime_identity(event_data, runtime_identity):
                    continue

                if event.type in _NON_REUSABLE_RUNTIME_EVENT_TYPES:
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
                    self._validate_resumable_handle_authority(
                        runtime_handle,
                        expected_workspace=expected_capsule_workspace,
                    )
                    runtime_fingerprint = runtime_handle.metadata.get("ac_capsule_fingerprint")
                    runtime_attempt_id = runtime_handle.metadata.get("session_attempt_id")
                    if runtime_fingerprint not in {None, expected_capsule_fingerprint}:
                        raise ValueError(
                            "persisted runtime handle disagrees with the durable AC capsule"
                        )
                    if (
                        runtime_fingerprint is None
                        or runtime_attempt_id != runtime_identity.session_attempt_id
                    ):
                        continue
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

            if unresolved_effect_event:
                raise AmbiguousACExecutionError(
                    "AC attempt recorded tool effects without a reusable runtime handle; "
                    "refusing fresh redispatch because non-idempotent effects may duplicate"
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
        metadata = (
            {
                **dict(previous_handle.metadata),
                **dict(runtime_handle.metadata),
            }
            if previous_handle is not None
            else dict(runtime_handle.metadata)
        )
        metadata.setdefault("turn_number", cls._runtime_turn_number(runtime_handle))
        metadata.setdefault(
            "turn_id",
            cls._runtime_turn_id(runtime_handle, runtime_identity=runtime_identity),
        )

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
