"""AC-scoped runtime-handle lifecycle management for parallel execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
import inspect
import json
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
_AC_ATTEMPT_DISPATCHED_EVENT = "execution.ac.attempt.dispatched"
_AC_REUSABLE_RUNTIME_EVENT_TYPES = _REUSABLE_RUNTIME_EVENT_TYPES | {_AC_ATTEMPT_DISPATCHED_EVENT}
_VERIFY_GATE_OUTCOME_KEYS = frozenset({"passed", "reason", "output_tail", "missing_artifacts"})
_MAX_VERIFY_GATE_OUTCOME_CHARS = 65_536
_AC_EFFECT_EVENT_TYPES = frozenset(
    {
        "execution.tool.started",
        "execution.tool.completed",
    }
)

#: Provider-entry phases a durable dispatch boundary may open. ``primary``
#: replays the original AC prompt on resume; ``session_signal_followup`` replays
#: a Synapse signal turn. An absent value on a persisted dispatch predates the
#: field and is treated as ``primary`` (follow-up dispatches never existed
#: without it), so recovery never infers a new phase for legacy records.
_AC_DISPATCH_KINDS = frozenset({"primary", "session_signal_followup"})

#: Seal marker for a completed provider turn a follow-up dispatch is about to
#: supersede. A sealed dispatch that ends up the active recovery head (i.e. its
#: follow-up never durably completed) fails closed instead of replaying the AC.
_AC_DISPATCH_SEALED_EVENT = "execution.ac.dispatch.sealed"


class AmbiguousACExecutionError(RuntimeError):
    """A prior AC attempt may have external effects but cannot be resumed safely."""


class CompletedACExecutionError(RuntimeError):
    """The exact AC attempt already recorded a completed provider dispatch."""

    def __init__(
        self,
        message: str,
        *,
        result_summary: str | None = None,
        session_id: str | None = None,
        verify_gate_outcome: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.result_summary = result_summary
        self.session_id = session_id
        self.verify_gate_outcome = verify_gate_outcome


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
        """Select one attempt without depending on replay timestamp ordering."""
        attempt_events: list[Any] = []
        for event in events:
            event_data = event.data if isinstance(getattr(event, "data", None), dict) else {}
            observed_attempt = cls._event_retry_attempt(event_data)
            if observed_attempt == retry_attempt:
                attempt_events.append(event)
        return attempt_events

    @staticmethod
    def _validate_ac_dispatch_id(value: object) -> str:
        """Return a canonical dispatch correlation id or reject persisted drift."""
        if (
            not isinstance(value, str)
            or len(value) != 32
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("durable AC dispatch id is malformed")
        return value

    @classmethod
    def _event_ac_dispatch_id(cls, event_data: Mapping[str, Any]) -> str | None:
        """Read dispatch correlation from the event and its runtime payload."""
        event_value = event_data.get("ac_dispatch_id")
        runtime_value: object = None
        runtime_payload = event_data.get("runtime")
        if isinstance(runtime_payload, Mapping):
            metadata = runtime_payload.get("metadata")
            if isinstance(metadata, Mapping):
                runtime_value = metadata.get("ac_dispatch_id")

        event_dispatch_id = (
            cls._validate_ac_dispatch_id(event_value) if event_value is not None else None
        )
        runtime_dispatch_id = (
            cls._validate_ac_dispatch_id(runtime_value) if runtime_value is not None else None
        )
        if (
            event_dispatch_id is not None
            and runtime_dispatch_id is not None
            and event_dispatch_id != runtime_dispatch_id
        ):
            raise ValueError("durable runtime handle dispatch id disagrees with its event")
        return event_dispatch_id or runtime_dispatch_id

    @staticmethod
    def _parse_verify_gate_outcome(value: object) -> dict[str, Any] | None:
        """Parse the bounded exact-shape verify result used for crash recovery."""
        if value is None:
            return None
        if not isinstance(value, Mapping) or set(value) != _VERIFY_GATE_OUTCOME_KEYS:
            raise ValueError("durable completed AC verify outcome has an invalid shape")
        passed = value.get("passed")
        reason = value.get("reason")
        output_tail = value.get("output_tail")
        missing_artifacts = value.get("missing_artifacts")
        if not isinstance(passed, bool):
            raise ValueError("durable completed AC verify outcome has an invalid verdict")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("durable completed AC verify outcome has an invalid reason")
        if not isinstance(output_tail, str):
            raise ValueError("durable completed AC verify outcome has invalid output")
        if not isinstance(missing_artifacts, list) or any(
            not isinstance(item, str) for item in missing_artifacts
        ):
            raise ValueError("durable completed AC verify outcome has invalid artifacts")
        normalized = {
            "passed": passed,
            "reason": reason,
            "output_tail": output_tail,
            "missing_artifacts": list(missing_artifacts),
        }
        encoded = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(encoded) > _MAX_VERIFY_GATE_OUTCOME_CHARS:
            raise ValueError("durable completed AC verify outcome exceeds the size limit")
        return normalized

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

            unresolved_effect_event = any(
                event.type in _AC_EFFECT_EVENT_TYPES
                and self._event_matches_ac_runtime_identity(
                    event.data if isinstance(event.data, dict) else {},
                    runtime_identity,
                )
                for event in attempt_events
            )

            if expected_capsule_fingerprint is None:
                if cached_handle is not None:
                    return cached_handle
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
                        if unresolved_effect_event:
                            raise AmbiguousACExecutionError(
                                "AC attempt recorded tool effects without a reusable runtime "
                                "handle; refusing fresh redispatch because non-idempotent "
                                "effects may duplicate"
                            )
                        return None
                    if event.type not in _REUSABLE_RUNTIME_EVENT_TYPES:
                        continue
                    runtime_handle = RuntimeHandle.from_dict(event_data.get("runtime"))
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
                    if runtime_handle is not None:
                        self.runtime_handles[runtime_identity.cache_key] = runtime_handle
                        return runtime_handle
                if unresolved_effect_event:
                    raise AmbiguousACExecutionError(
                        "AC attempt recorded tool effects without a reusable runtime handle; "
                        "refusing fresh redispatch because non-idempotent effects may duplicate"
                    )
                continue

            matching_events: list[Any] = []
            dispatch_events: dict[str, Any] = {}
            dispatch_predecessors: dict[str, str | None] = {}
            dispatch_kinds: dict[str, str] = {}
            sealed_dispatch_ids: set[str] = set()
            for event in attempt_events:
                event_data = event.data if isinstance(event.data, dict) else {}
                if not self._event_matches_ac_runtime_identity(event_data, runtime_identity):
                    continue
                matching_events.append(event)
                if event.type == _AC_DISPATCH_SEALED_EVENT:
                    sealed_id = self._event_ac_dispatch_id(event_data)
                    if sealed_id is not None:
                        sealed_dispatch_ids.add(sealed_id)
                    continue
                if event.type != _AC_ATTEMPT_DISPATCHED_EVENT:
                    continue
                dispatch_id = self._event_ac_dispatch_id(event_data)
                if dispatch_id is None:
                    raise ValueError("durable AC dispatch id is missing")
                if dispatch_id in dispatch_events:
                    raise ValueError("durable AC dispatch id is duplicated")
                if "previous_ac_dispatch_id" not in event_data:
                    raise ValueError("durable AC dispatch predecessor is missing")
                predecessor_value = event_data.get("previous_ac_dispatch_id")
                predecessor_id = (
                    self._validate_ac_dispatch_id(predecessor_value)
                    if predecessor_value is not None
                    else None
                )
                if predecessor_id == dispatch_id:
                    raise ValueError("durable AC dispatch predecessor references itself")
                if event_data.get("capsule_fingerprint") != expected_capsule_fingerprint:
                    raise ValueError(
                        "durable AC dispatch fingerprint disagrees with the current capsule"
                    )
                if event_data.get("session_origin") not in {
                    "fresh",
                    "restored_same_attempt",
                }:
                    raise ValueError("durable AC dispatch session origin is invalid")
                # Resolve the provider-entry PHASE this dispatch opened. A dispatch
                # persisted before this field existed is, by construction, a
                # ``primary`` AC dispatch (SessionSignal follow-up dispatches never
                # existed without it), so an absent value keeps that historical
                # meaning without inferring anything new. An explicitly persisted
                # follow-up must carry its signal identity/mode and exact input
                # digest, or the record is corrupt and rejected — recovery must not
                # resume a follow-up phase whose exact input it cannot prove.
                dispatch_kind = event_data.get("dispatch_kind")
                if dispatch_kind is None:
                    dispatch_kind = "primary"
                elif dispatch_kind not in _AC_DISPATCH_KINDS:
                    raise ValueError("durable AC dispatch kind is invalid")
                if dispatch_kind == "session_signal_followup":
                    signal_id = event_data.get("signal_id")
                    signal_mode = event_data.get("signal_mode")
                    input_digest = event_data.get("follow_up_input_digest")
                    if (
                        not isinstance(signal_id, str)
                        or not signal_id
                        or not isinstance(signal_mode, str)
                        or not signal_mode
                        or not isinstance(input_digest, str)
                        or not input_digest.startswith("sha256:")
                    ):
                        raise ValueError(
                            "durable SessionSignal follow-up dispatch is missing phase identity"
                        )
                dispatch_events[dispatch_id] = event
                dispatch_predecessors[dispatch_id] = predecessor_id
                dispatch_kinds[dispatch_id] = dispatch_kind

            active_dispatch_id: str | None = None
            if dispatch_events:
                successor_by_dispatch: dict[str, str] = {}
                for dispatch_id, predecessor_id in dispatch_predecessors.items():
                    if predecessor_id is None:
                        continue
                    if predecessor_id not in dispatch_events:
                        raise ValueError(
                            "durable AC dispatch predecessor references an unknown dispatch id"
                        )
                    known_successor = successor_by_dispatch.get(predecessor_id)
                    if known_successor not in {None, dispatch_id}:
                        raise ValueError(
                            "durable AC dispatch history branches from one predecessor"
                        )
                    successor_by_dispatch[predecessor_id] = dispatch_id

                roots = [
                    dispatch_id
                    for dispatch_id, predecessor_id in dispatch_predecessors.items()
                    if predecessor_id is None
                ]
                heads = [
                    dispatch_id
                    for dispatch_id in dispatch_events
                    if dispatch_id not in successor_by_dispatch
                ]
                if len(roots) != 1 or len(heads) != 1:
                    raise ValueError("durable AC dispatch history is not one linear chain")

                active_dispatch_id = heads[0]
                visited_dispatches: set[str] = set()
                cursor: str | None = active_dispatch_id
                while cursor is not None:
                    if cursor in visited_dispatches:
                        raise ValueError("durable AC dispatch history contains a cycle")
                    visited_dispatches.add(cursor)
                    cursor = dispatch_predecessors[cursor]
                if len(visited_dispatches) != len(dispatch_events):
                    raise ValueError("durable AC dispatch history is disconnected")

            candidate_handles: dict[str, list[tuple[int, RuntimeHandle]]] = {
                dispatch_id: [] for dispatch_id in dispatch_events
            }
            legacy_candidates: list[tuple[int, RuntimeHandle]] = []
            completed_dispatches: dict[str, list[Mapping[str, Any]]] = {
                dispatch_id: [] for dispatch_id in dispatch_events
            }
            failed_dispatches: set[str] = set()
            recovery_successors: dict[str, str] = {}

            def _load_candidate(
                event: Any,
                event_data: Mapping[str, Any],
                *,
                dispatch_id: str | None,
            ) -> RuntimeHandle | None:
                runtime_payload = event_data.get("runtime")
                if (
                    event.type != _AC_ATTEMPT_DISPATCHED_EVENT
                    and isinstance(runtime_payload, Mapping)
                    and isinstance(runtime_payload.get("cwd"), str)
                    and expected_capsule_workspace is not None
                ):
                    persisted_workspace = os.path.realpath(
                        os.path.expanduser(runtime_payload["cwd"])
                    )
                    if persisted_workspace != expected_capsule_workspace:
                        raise ValueError("persisted runtime handle workspace authority changed")
                try:
                    runtime_handle = (
                        RuntimeHandle.from_dispatch_recovery_dict(runtime_payload)
                        if event.type == _AC_ATTEMPT_DISPATCHED_EVENT
                        else RuntimeHandle.from_persisted_recovery_projection(runtime_payload)
                    )
                except ValueError as exc:
                    if event.type == _AC_ATTEMPT_DISPATCHED_EVENT:
                        raise ValueError(
                            "durable AC dispatch recovery handle is malformed"
                        ) from exc
                    log.warning(
                        "parallel_executor.persisted_runtime_handle_invalid",
                        aggregate_id=event.aggregate_id,
                        event_type=event.type,
                        error=str(exc),
                        runtime_keys=sorted(runtime_payload)
                        if isinstance(runtime_payload, dict)
                        else None,
                    )
                    return None
                if runtime_handle is None:
                    return None
                runtime_handle = replace(runtime_handle, cwd=expected_capsule_workspace)
                self._validate_resumable_handle_authority(
                    runtime_handle,
                    expected_workspace=expected_capsule_workspace,
                )
                runtime_fingerprint = runtime_handle.metadata.get("ac_capsule_fingerprint")
                runtime_attempt_id = runtime_handle.metadata.get("session_attempt_id")
                if runtime_fingerprint != expected_capsule_fingerprint:
                    raise ValueError(
                        "persisted runtime handle disagrees with the durable AC capsule"
                    )
                if runtime_attempt_id != runtime_identity.session_attempt_id:
                    raise ValueError(
                        "persisted runtime handle disagrees with the durable AC attempt"
                    )
                runtime_dispatch_id = runtime_handle.metadata.get("ac_dispatch_id")
                if dispatch_id is not None and runtime_dispatch_id != dispatch_id:
                    raise ValueError(
                        "persisted runtime handle disagrees with the durable AC dispatch"
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
                if not self._is_resumable_runtime_handle(runtime_handle):
                    return None
                return runtime_handle

            recovery_priority = {
                _AC_ATTEMPT_DISPATCHED_EVENT: 0,
                "execution.session.failed": 1,
                "execution.session.started": 2,
                "execution.session.resumed": 3,
                "execution.session.recovered": 4,
            }
            recovery_event_types = _AC_REUSABLE_RUNTIME_EVENT_TYPES | {"execution.session.failed"}
            for event in matching_events:
                if event.type not in recovery_event_types | {"execution.session.completed"}:
                    continue
                event_data = event.data if isinstance(event.data, dict) else {}
                event_dispatch_id = self._event_ac_dispatch_id(event_data)
                if dispatch_events:
                    if event_dispatch_id is None:
                        continue
                    if event_dispatch_id not in dispatch_events:
                        raise ValueError(
                            "durable AC lifecycle event references an unknown dispatch id"
                        )
                if event.type == "execution.session.completed":
                    if event_data.get("success") is not True:
                        raise ValueError("durable completed AC lifecycle is not successful")
                    if event_dispatch_id is not None:
                        completed_dispatches[event_dispatch_id].append(event_data)
                    elif not dispatch_events:
                        verify_gate_outcome = self._parse_verify_gate_outcome(
                            event_data.get("verify_gate_outcome")
                        )
                        raise CompletedACExecutionError(
                            "AC attempt already completed; refusing same-attempt redispatch",
                            result_summary=event_data.get("result_summary")
                            if isinstance(event_data.get("result_summary"), str)
                            else None,
                            session_id=event_data.get("session_id")
                            if isinstance(event_data.get("session_id"), str)
                            else None,
                            verify_gate_outcome=verify_gate_outcome,
                        )
                    continue
                if event.type == "execution.session.failed" and event_dispatch_id is not None:
                    failed_dispatches.add(event_dispatch_id)
                runtime_handle = _load_candidate(
                    event,
                    event_data,
                    dispatch_id=event_dispatch_id if dispatch_events else None,
                )
                if runtime_handle is None:
                    continue
                if event.type == "execution.session.recovered":
                    discontinuity = event_data.get("recovery_discontinuity")
                    if not isinstance(discontinuity, Mapping):
                        raise ValueError("durable recovered lifecycle is missing linkage")
                    failed = discontinuity.get("failed")
                    replacement = discontinuity.get("replacement")
                    if not isinstance(failed, Mapping) or not isinstance(replacement, Mapping):
                        raise ValueError("durable recovered lifecycle linkage is malformed")
                    failed_resume_id = failed.get("resume_session_id")
                    replacement_resume_id = replacement.get("resume_session_id")
                    if (
                        not isinstance(failed_resume_id, str)
                        or not failed_resume_id
                        or not isinstance(replacement_resume_id, str)
                        or not replacement_resume_id
                    ):
                        raise ValueError("durable recovered lifecycle linkage is incomplete")
                    if self._runtime_resume_session_id(runtime_handle) != replacement_resume_id:
                        raise ValueError(
                            "durable recovered lifecycle replacement disagrees with its handle"
                        )
                    known_successor = recovery_successors.get(failed_resume_id)
                    if known_successor not in {None, replacement_resume_id}:
                        raise AmbiguousACExecutionError(
                            "AC recovery history maps one failed runtime session to conflicting "
                            "replacement sessions"
                        )
                    recovery_successors[failed_resume_id] = replacement_resume_id
                priority = recovery_priority[event.type]
                if event_dispatch_id is None:
                    legacy_candidates.append((priority, runtime_handle))
                else:
                    candidate_handles[event_dispatch_id].append((priority, runtime_handle))

            if cached_handle is not None and self._is_resumable_runtime_handle(cached_handle):
                cached_fingerprint = cached_handle.metadata.get("ac_capsule_fingerprint")
                cached_attempt_id = cached_handle.metadata.get("session_attempt_id")
                if (
                    cached_fingerprint != expected_capsule_fingerprint
                    or cached_attempt_id != runtime_identity.session_attempt_id
                ):
                    self.runtime_handles.pop(runtime_identity.cache_key, None)
                    cached_handle = None
                else:
                    cached_dispatch_value = cached_handle.metadata.get("ac_dispatch_id")
                    cached_dispatch_id = (
                        self._validate_ac_dispatch_id(cached_dispatch_value)
                        if cached_dispatch_value is not None
                        else None
                    )
                    if dispatch_events:
                        if cached_dispatch_id not in dispatch_events:
                            raise ValueError(
                                "cached runtime handle references an unknown AC dispatch"
                            )
                        candidate_handles[cached_dispatch_id].append((5, cached_handle))
                    else:
                        legacy_candidates.append((5, cached_handle))

            active_completed_events = (
                completed_dispatches[active_dispatch_id] if active_dispatch_id is not None else []
            )
            if active_completed_events:
                self._forget_ac_runtime_handle(
                    ac_index,
                    execution_context_id=execution_context_id,
                    is_sub_ac=is_sub_ac,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=sub_ac_index,
                    node_identity=node_identity,
                    retry_attempt=retry_attempt,
                )
                completed_facts = {
                    (
                        event_data.get("result_summary")
                        if isinstance(event_data.get("result_summary"), str)
                        else None,
                        event_data.get("session_id")
                        if isinstance(event_data.get("session_id"), str)
                        else None,
                    )
                    for event_data in active_completed_events
                }
                if len(completed_facts) != 1:
                    raise ValueError("durable completed AC lifecycle events disagree")
                result_summary, completed_session_id = next(iter(completed_facts))
                parsed_verify_outcomes = [
                    self._parse_verify_gate_outcome(event_data.get("verify_gate_outcome"))
                    for event_data in active_completed_events
                ]
                completed_verify_outcomes = {
                    json.dumps(
                        outcome,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    for outcome in parsed_verify_outcomes
                }
                if len(completed_verify_outcomes) != 1:
                    raise ValueError("durable completed AC verify outcomes disagree")
                completed_verify_outcome = parsed_verify_outcomes[0]
                raise CompletedACExecutionError(
                    "AC attempt already completed; refusing same-attempt redispatch",
                    result_summary=result_summary,
                    session_id=completed_session_id,
                    verify_gate_outcome=completed_verify_outcome,
                )

            if dispatch_events:
                if active_dispatch_id is None:  # pragma: no cover - guarded by chain validation
                    raise ValueError("durable AC dispatch history has no active boundary")
                # Fail closed on a non-primary chain head. The caller
                # (``_execute_atomic_ac``) always replays the ORIGINAL AC prompt
                # after this handle is returned, so resuming a SessionSignal
                # follow-up head would re-run the AC's acceptance work. We do not
                # reconstruct the exact follow-up phase from persisted metadata
                # here, so the only safe outcome is to refuse resumption rather
                # than repeat a possibly non-idempotent AC prompt.
                active_dispatch_kind = dispatch_kinds[active_dispatch_id]
                if active_dispatch_kind != "primary":
                    raise AmbiguousACExecutionError(
                        "AC recovery resolved a non-primary provider-entry phase "
                        f"({active_dispatch_kind}) as the active dispatch head; refusing to "
                        "resume because replaying the original AC prompt would repeat "
                        "acceptance work in the wrong phase"
                    )
                # A sealed head is a completed provider turn that a follow-up
                # dispatch tried to supersede without durably completing (the
                # completed/failed terminal check above already returned for a
                # cleanly finalized attempt). Resuming it would replay the
                # original AC prompt over already-done work, so fail closed.
                if active_dispatch_id in sealed_dispatch_ids:
                    raise AmbiguousACExecutionError(
                        "AC recovery resolved a sealed provider-entry phase as the active "
                        "dispatch head; refusing to resume because a follow-up superseded it "
                        "without a durable terminal outcome and replaying the AC prompt would "
                        "repeat completed work"
                    )
                active_dispatch_candidates = candidate_handles[active_dispatch_id]
                if not active_dispatch_candidates:
                    if active_dispatch_id in failed_dispatches:
                        raise AmbiguousACExecutionError(
                            "AC provider dispatch failed without a reusable runtime handle; "
                            "refusing fresh redispatch because external effects may have occurred"
                        )
                    raise AmbiguousACExecutionError(
                        "AC attempt crossed the provider dispatch boundary without a reusable "
                        "runtime handle; refusing fresh redispatch because external effects may "
                        "have occurred"
                    )
                all_candidates = active_dispatch_candidates
            else:
                all_candidates = legacy_candidates

            resume_ids = {
                self._runtime_resume_session_id(runtime_handle)
                for _, runtime_handle in all_candidates
            }
            resume_ids.discard(None)
            resolved_resume_ids: set[str] = set()
            for resume_id in resume_ids:
                current_id = resume_id
                visited: set[str] = set()
                while current_id in recovery_successors:
                    if current_id in visited:
                        raise AmbiguousACExecutionError(
                            "AC recovery history contains a runtime-session replacement cycle"
                        )
                    visited.add(current_id)
                    current_id = recovery_successors[current_id]
                resolved_resume_ids.add(current_id)
            if len(resolved_resume_ids) > 1:
                raise AmbiguousACExecutionError(
                    "AC attempt has conflicting reusable runtime sessions; refusing to guess "
                    "which provider session owns the external effects"
                )
            if all_candidates:
                active_resume_id = next(iter(resolved_resume_ids))
                active_candidates = [
                    candidate
                    for candidate in all_candidates
                    if self._runtime_resume_session_id(candidate[1]) == active_resume_id
                ]
                if not active_candidates:
                    raise AmbiguousACExecutionError(
                        "AC recovery history names a replacement runtime session without a "
                        "reusable handle"
                    )
                highest_priority = max(priority for priority, _ in active_candidates)
                highest_candidates = [
                    runtime_handle
                    for priority, runtime_handle in active_candidates
                    if priority == highest_priority
                ]
                continuity_facts = {
                    (
                        runtime_handle.backend,
                        runtime_handle.native_session_id,
                        runtime_handle.server_session_id,
                        runtime_handle.conversation_id,
                        runtime_handle.previous_response_id,
                        runtime_handle.approval_mode,
                    )
                    for runtime_handle in highest_candidates
                }
                if len(continuity_facts) != 1:
                    raise AmbiguousACExecutionError(
                        "AC recovery history has equally authoritative but conflicting runtime "
                        "continuation handles"
                    )
                runtime_handle = highest_candidates[0]
                self.runtime_handles[runtime_identity.cache_key] = runtime_handle
                return runtime_handle

            if unresolved_effect_event:
                raise AmbiguousACExecutionError(
                    "AC attempt recorded tool effects without a reusable runtime handle; "
                    "refusing fresh redispatch because non-idempotent effects may duplicate"
                )
            if any(event.type == "execution.session.failed" for event in matching_events):
                raise AmbiguousACExecutionError(
                    "AC provider dispatch failed without a reusable runtime handle; refusing "
                    "fresh redispatch because external effects may have occurred"
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
        dispatch_id: str | None = None,
        ac_content: str,
        runtime_handle: RuntimeHandle | None,
        execution_id: str | None = None,
        session_id: str | None = None,
        result_summary: str | None = None,
        success: bool | None = None,
        error: str | None = None,
        verify_gate_outcome: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist AC-scoped runtime lifecycle events using normalized metadata."""
        from ouroboros.events.base import BaseEvent

        effective_session_id = session_id or self._runtime_resume_session_id(runtime_handle)
        server_session_id = runtime_handle.server_session_id if runtime_handle is not None else None
        identity_metadata = runtime_identity.to_metadata()
        runtime_dispatch_value = (
            runtime_handle.metadata.get("ac_dispatch_id") if runtime_handle is not None else None
        )
        runtime_dispatch_id = (
            self._validate_ac_dispatch_id(runtime_dispatch_value)
            if runtime_dispatch_value is not None
            else None
        )
        if dispatch_id is not None:
            dispatch_id = self._validate_ac_dispatch_id(dispatch_id)
        if (
            dispatch_id is not None
            and runtime_dispatch_id is not None
            and dispatch_id != runtime_dispatch_id
        ):
            raise ValueError("runtime lifecycle dispatch id disagrees with its handle")
        effective_dispatch_id = dispatch_id or runtime_dispatch_id

        persisted_verify_gate_outcome = self._parse_verify_gate_outcome(verify_gate_outcome)
        event = BaseEvent(
            type=event_type,
            aggregate_type=runtime_identity.runtime_scope.aggregate_type,
            aggregate_id=runtime_identity.session_scope_id,
            data={
                **identity_metadata,
                **(
                    {"ac_dispatch_id": effective_dispatch_id}
                    if effective_dispatch_id is not None
                    else {}
                ),
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
                "verify_gate_outcome": (persisted_verify_gate_outcome),
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
