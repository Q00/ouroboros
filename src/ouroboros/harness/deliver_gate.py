"""Read-only helpers for the #978 evidence deliver gate.

This module is the first P2-safe bridge between the journal normalizer and the
TraceGuard verdict call. It deliberately does **not** change AC success
semantics: callers receive an :class:`EvidenceManifest` and can evaluate an
explicit deliver claim through an injected TraceGuard-compatible validator while
legacy completion remains untouched until a later gate PR explicitly owns
behavior changes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.core.filesystem_capability import (
    DirectoryChainFingerprint,
    NoFollowDirectoryChain,
    nofollow_directory_capabilities_available,
    open_nofollow_directory_chain,
)
from ouroboros.events.base import BaseEvent
from ouroboros.harness.claim_term_guard import ClaimTermGuard, ClaimTermGuardFact
from ouroboros.harness.journal import (
    EvidenceEntry,
    EvidenceKind,
    EvidenceManifest,
    normalize_events,
)

_COMMAND_ARTIFACT_SCHEMA_VERSION = 1
_COMMAND_ARTIFACT_CAPTURE = "ouroboros.leaf-dispatch.v1"
_MAX_COMMAND_ARTIFACTS = 128
_MAX_COMMAND_ARTIFACT_PATH_CHARS = 4096
_MAX_WORKSPACE_CHAIN_COMPONENTS = 256
RUNTIME_SUCCESS_BOOLEAN_FIELDS = ("success", "ok")
RUNTIME_FAILURE_BOOLEAN_FIELDS = (
    "is_error",
    "is_error_invalid",
    "isError",
    "error",
    "failure",
    "failed",
    "cancel",
    "cancelled",
    "canceled",
    "timeout",
    "timed_out",
    "abort",
    "aborted",
)
RUNTIME_TOOL_EXIT_STATUS_FIELDS = (
    "exit_code",
    "exit_status",
    "exitCode",
    "exitStatus",
    "returncode",
    "return_code",
    "status_code",
    "statusCode",
    "exit",
    "errorCode",
    "error_code",
)
_FILESYSTEM_EFFECT_KEYS = frozenset(
    {
        "capture",
        "path",
        "workspace_relative_path",
        "workspace_chain",
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    }
)
_COMMAND_ARTIFACT_KEYS = frozenset(
    {"path", "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"}
)
_COMMAND_ARTIFACT_PROVENANCE_KEYS = frozenset(
    {
        "schema_version",
        "capture",
        "command_call_id",
        "completion_event_id",
        "retry_attempt",
        "session_attempt_id",
        "workspace_chain",
        "artifacts",
    }
)
_WORKSPACE_CHAIN_COMPONENT_KEYS = frozenset({"name", "st_dev", "st_ino", "st_mode"})


class EventStoreEvidenceReader(Protocol):
    """EventStore read subset required by the deliver-gate manifest loader."""

    async def query_execution_related_events(
        self,
        execution_id: str,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        raise NotImplementedError

    async def query_session_related_events(
        self,
        session_id: str,
        execution_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        raise NotImplementedError


class TraceGuardResultLike(Protocol):
    """Subset returned by ``rlm_forge.traceguard.validate_parent_synthesis``."""

    accepted: bool
    accepted_claims: object
    rejected_claims: object
    allowed_fact_ids: object
    allowed_chunk_ids: object

    @property
    def unsupported_claim_rate(self) -> float:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class TraceGuardEvidenceInput:
    """Duck-typed input compatible with ``TraceGuardEvidence``."""

    fact_id: str
    chunk_id: str
    text: str
    child_call_id: str | None = None


class TraceGuardValidator(Protocol):
    """Callable shape for the injected deterministic TraceGuard validator."""

    def __call__(
        self,
        *,
        evidence_manifest: tuple[TraceGuardEvidenceInput, ...],
        parent_synthesis: dict[str, Any],
    ) -> TraceGuardResultLike:
        raise NotImplementedError


class DeliverEvidenceFact(BaseModel, frozen=True):
    """One leaf-delivery fact the agent claims is backed by evidence."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(..., min_length=1)
    evidence_handle: str = Field(..., min_length=1)
    statement: str = Field(default="")

    @field_validator("fact_id", "evidence_handle")
    @classmethod
    def _identifier_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "deliver evidence fact identifiers must be non-blank"
            raise ValueError(msg)
        return stripped


class DeliverEvidenceClaim(BaseModel, frozen=True):
    """Structured AC completion claim passed to the deliver gate."""

    model_config = ConfigDict(extra="forbid")

    ac_id: str = Field(..., min_length=1)
    facts: tuple[DeliverEvidenceFact, ...] = Field(default_factory=tuple)

    @field_validator("ac_id")
    @classmethod
    def _ac_id_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "DeliverEvidenceClaim.ac_id must be non-blank"
            raise ValueError(msg)
        return stripped

    @field_validator("facts")
    @classmethod
    def _facts_not_empty(
        cls, value: tuple[DeliverEvidenceFact, ...]
    ) -> tuple[DeliverEvidenceFact, ...]:
        if not value:
            msg = "DeliverEvidenceClaim requires at least one fact"
            raise ValueError(msg)
        seen: set[str] = set()
        for fact in value:
            if fact.fact_id in seen:
                msg = f"DeliverEvidenceClaim fact_id {fact.fact_id!r} is duplicated"
                raise ValueError(msg)
            seen.add(fact.fact_id)
        return value


class DeliverGateVerdict(BaseModel, frozen=True):
    """TraceGuard-derived verdict for one AC deliver claim.

    The verdict is intentionally a read-model value. It does not mark the AC
    complete by itself; later #920/#978 PRs can A/B record it or use it to drive
    retry / redispatch / escalation routing.
    """

    model_config = ConfigDict(extra="forbid")

    ac_id: str = Field(..., min_length=1)
    accepted: bool
    unsupported_claim_rate: float = Field(..., ge=0.0, le=1.0)
    accepted_fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    rejected_fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    rejected_reasons: tuple[str, ...] = Field(default_factory=tuple)
    evidence_event_ids: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _accepted_verdict_has_no_rejections(self) -> DeliverGateVerdict:
        if self.accepted and (self.rejected_fact_ids or self.rejected_reasons):
            msg = "accepted DeliverGateVerdict cannot carry rejected claims"
            raise ValueError(msg)
        if not self.accepted and not self.rejected_reasons:
            msg = "rejected DeliverGateVerdict must include rejection reasons"
            raise ValueError(msg)
        return self


async def load_ac_evidence_manifest(
    event_store: EventStoreEvidenceReader,
    *,
    ac_id: str,
    execution_id: str | None = None,
    session_id: str | None = None,
    scope_id: str | None = None,
    limit: int | None = None,
    admit_accepted_tool_starts: bool = False,
    accepted_retry_attempt: int | None = None,
    accepted_session_attempt_id: str | None = None,
) -> EvidenceManifest:
    """Load and normalize EventStore evidence for one AC deliver-gate check.

    ``execution_id`` is required so the deliver-gate input is bounded to one
    execution. When ``session_id`` is also available the loader uses the
    session-related query with the execution correlation filter; otherwise it
    uses the execution-only query. Session-only reads are rejected because a
    session can contain multiple executions/retries that must not be spliced
    into one verifier input.

    Args:
        event_store: Read-capable EventStore or test double.
        ac_id: Acceptance-criterion identifier to normalize.
        execution_id: Required execution aggregate anchor.
        session_id: Optional session aggregate anchor used as an additional
            ownership filter.
        scope_id: Optional event-scope token to filter by when the public AC
            id differs from the runtime aggregate/phase token used by the
            recorder. Defaults to ``ac_id``.
        limit: Optional EventStore query cap. The default ``None`` reads the
            full related event set so the manifest is not silently truncated
            before TraceGuard sees it.
        admit_accepted_tool_starts: Add AC-scoped ``execution.tool.started``
            events as journal entries after the caller has established accepted
            leaf + exact typed-evidence match + verifier PASS. Mutation tools and
            Bash commands are admitted only when explicit success is not vetoed
            by any correlated failure or ambiguity; otherwise an exact successful
            ``execution.tool.completed`` event is required.
        accepted_retry_attempt: Optional accepted leaf retry attempt. When set,
            tool starts from failed/older attempts are excluded.
        accepted_session_attempt_id: Optional exact implementation-attempt id,
            providing the strongest filter when runtime metadata carries it.

    Raises:
        ValueError: If ``ac_id`` is blank, if ``execution_id`` is missing
            or blank, or if optional anchors are whitespace-only.

    Returns:
        A per-AC :class:`EvidenceManifest` in chronological event order.
    """
    normalized_ac_id = ac_id.strip()
    if not normalized_ac_id:
        msg = "load_ac_evidence_manifest requires a non-blank ac_id"
        raise ValueError(msg)
    normalized_execution_id = _normalize_optional_anchor("execution_id", execution_id)
    normalized_session_id = _normalize_optional_anchor("session_id", session_id)
    normalized_scope_id = _normalize_optional_anchor("scope_id", scope_id) or normalized_ac_id
    if normalized_execution_id is None:
        msg = "load_ac_evidence_manifest requires execution_id"
        raise ValueError(msg)

    if normalized_session_id is not None:
        events = await event_store.query_session_related_events(
            normalized_session_id,
            execution_id=normalized_execution_id,
            limit=limit,
        )
    else:
        assert normalized_execution_id is not None
        events = await event_store.query_execution_related_events(
            normalized_execution_id,
            limit=limit,
        )

    filtered_events = _filter_events_by_anchors(
        events,
        execution_id=normalized_execution_id,
        session_id=normalized_session_id,
    )
    chronological = _chronological_events(filtered_events)
    manifest = normalize_events(chronological, ac_id=normalized_scope_id)
    if admit_accepted_tool_starts:
        admitted_entries = _accepted_tool_start_entries(
            chronological,
            scope_id=normalized_scope_id,
            execution_id=normalized_execution_id,
            retry_attempt=accepted_retry_attempt,
            session_attempt_id=accepted_session_attempt_id,
        )
        if admitted_entries:
            manifest = EvidenceManifest(
                ac_id=normalized_scope_id,
                entries=tuple(
                    sorted(
                        (*manifest.entries, *admitted_entries),
                        key=lambda entry: (entry.started_at, entry.source_event_ids),
                    )
                ),
                metadata={
                    **dict(manifest.metadata),
                    "accepted_tool_starts_admitted": True,
                },
            )
    if normalized_scope_id == normalized_ac_id:
        return manifest
    return EvidenceManifest(
        ac_id=normalized_ac_id,
        entries=manifest.entries,
        normalized_at=manifest.normalized_at,
        metadata=manifest.metadata,
    )


def _accepted_tool_start_entries(
    events: Iterable[BaseEvent],
    *,
    scope_id: str,
    execution_id: str,
    retry_attempt: int | None,
    session_attempt_id: str | None,
) -> tuple[EvidenceEntry, ...]:
    """Project accepted-leaf tool dispatches into claim-independent entries.

    The accepted-leaf + exact-match + verifier-PASS preconditions are necessary
    but not sufficient for file mutation or command success: a failed Edit/Write
    or Bash call can still be followed by an overall successful assistant result.
    Mutation and command starts therefore require their own explicit success
    signal or one exact successful completion. Missing, failed, or ambiguous
    correlation is omitted fail-closed.
    """
    chronological = tuple(events)
    event_id_counts = Counter(event.id for event in chronological)
    idless_completion_by_start = _owned_idless_tool_completions(
        chronological,
        scope_id=scope_id,
        execution_id=execution_id,
        retry_attempt=retry_attempt,
        session_attempt_id=session_attempt_id,
        event_id_counts=event_id_counts,
    )
    owned_idless_completion_indexes = frozenset(idless_completion_by_start.values())
    entries: list[EvidenceEntry] = []
    for index, event in enumerate(chronological):
        if event.type != "execution.tool.started" or not _event_matches_scope(event, scope_id):
            continue
        if event_id_counts[event.id] != 1:
            continue
        data = event.data
        if retry_attempt is not None and data.get("retry_attempt") != retry_attempt:
            continue
        if session_attempt_id is not None and data.get("session_attempt_id") != session_attempt_id:
            continue
        if _event_has_conflicting_tool_call_ids(event):
            continue
        tool_name = data.get("tool_name") if isinstance(data, Mapping) else None
        tool_input = data.get("tool_input") if isinstance(data, Mapping) else None
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
        normalized_tool_name = tool_name.strip()
        completion: BaseEvent | None = None
        if normalized_tool_name in {"Bash", "Edit", "Write", "NotebookEdit"}:
            require_command_verdict = normalized_tool_name == "Bash"
            call_id = _event_tool_call_id(event)
            if call_id is not None:
                matching_starts = tuple(
                    candidate
                    for candidate in chronological
                    if candidate.type == "execution.tool.started"
                    and _event_matches_accepted_attempt(
                        candidate,
                        scope_id=scope_id,
                        execution_id=execution_id,
                        retry_attempt=retry_attempt,
                        session_attempt_id=session_attempt_id,
                    )
                    and _event_tool_call_id(candidate) == call_id
                )
                if len(matching_starts) != 1:
                    continue
            start_success = _event_has_explicit_tool_success(
                event,
                require_command_verdict=require_command_verdict,
            )
            completion, completion_veto = _correlated_tool_completion(
                chronological,
                start_index=index,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
                require_command_verdict=require_command_verdict,
                event_id_counts=event_id_counts,
                idless_completion_by_start=idless_completion_by_start,
                owned_idless_completion_indexes=owned_idless_completion_indexes,
            )
            if completion_veto or (completion is None and not start_success):
                continue
        normalized_input = dict(tool_input) if isinstance(tool_input, Mapping) else {}
        payload: dict[str, Any] = {
            "tool_name": normalized_tool_name,
            "accepted_leaf_dispatch": True,
        }
        accepted_call_id = _event_tool_call_id(event)
        if accepted_call_id is not None:
            payload["tool_call_id"] = accepted_call_id
        if retry_attempt is not None:
            payload["retry_attempt"] = retry_attempt
        if session_attempt_id is not None:
            payload["session_attempt_id"] = session_attempt_id
        if normalized_input:
            payload["args_preview"] = json.dumps(
                normalized_input,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        command = normalized_input.get("command")
        if isinstance(command, str) and command.strip():
            payload["command"] = command.strip()
        raw_path = next(
            (
                normalized_input[key]
                for key in ("file_path", "path", "notebook_path")
                if isinstance(normalized_input.get(key), str) and str(normalized_input[key]).strip()
            ),
            None,
        )
        if isinstance(raw_path, str):
            payload["file_path"] = raw_path.strip()
            relative_path = _event_workspace_relative_path(raw_path, data)
            if relative_path is not None:
                payload["workspace_relative_path"] = relative_path
        if normalized_tool_name == "Bash" and completion is not None and completion.id != event.id:
            command_artifacts = _command_artifacts_from_completion(
                completion,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            )
            if command_artifacts is not None:
                payload["command_artifacts"] = command_artifacts
        result_preview = _event_result_preview(event, completion)
        if result_preview is not None:
            payload["result_preview"] = result_preview
        entries.append(
            EvidenceEntry(
                kind=EvidenceKind.TOOL_INVOCATION,
                ok=True,
                started_at=event.timestamp,
                ended_at=completion.timestamp if completion is not None else event.timestamp,
                payload=payload,
                source_event_ids=(
                    (event.id, completion.id) if completion is not None else (event.id,)
                ),
            )
        )
    return tuple(entries)


def _command_artifacts_from_completion(
    completion: BaseEvent,
    *,
    retry_attempt: int | None,
    session_attempt_id: str | None,
) -> dict[str, Any] | None:
    """Validate and project the locally captured Bash receiver authority.

    The LeafDispatcher owns capture.  This loader only closes the durable
    schema around that existing completion payload and binds it to the accepted
    attempt.  Any malformed entry invalidates the whole command authority.
    """
    if retry_attempt is None or session_attempt_id is None:
        return None
    if (
        isinstance(retry_attempt, bool)
        or not isinstance(retry_attempt, int)
        or retry_attempt < 0
        or not isinstance(session_attempt_id, str)
        or not session_attempt_id.strip()
    ):
        return None
    call_id = _event_tool_call_id(completion)
    if call_id is None or not isinstance(completion.data, Mapping):
        return None
    raw_effects = completion.data.get("filesystem_effects")
    if (
        not isinstance(raw_effects, (list, tuple))
        or not raw_effects
        or len(raw_effects) > _MAX_COMMAND_ARTIFACTS
    ):
        return None
    artifacts: list[dict[str, int | str]] = []
    seen_paths: set[str] = set()
    workspace_fingerprint: DirectoryChainFingerprint | None = None
    for raw in raw_effects:
        if not isinstance(raw, Mapping) or frozenset(raw) != _FILESYSTEM_EFFECT_KEYS:
            return None
        path = raw.get("workspace_relative_path")
        lexical_path = raw.get("path")
        if (
            raw.get("capture") != _COMMAND_ARTIFACT_CAPTURE
            or not isinstance(lexical_path, str)
            or not lexical_path.strip()
            or not isinstance(path, str)
            or not _is_canonical_command_artifact_path(path)
            or path in seen_paths
        ):
            return None
        numeric: dict[str, int] = {}
        for key in _COMMAND_ARTIFACT_KEYS - {"path"}:
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            numeric[key] = value
        if numeric["st_ino"] == 0 or not stat.S_ISREG(numeric["st_mode"]):
            return None
        current_workspace_fingerprint = _workspace_chain_fingerprint(raw.get("workspace_chain"))
        if current_workspace_fingerprint is None:
            return None
        if workspace_fingerprint is None:
            workspace_fingerprint = current_workspace_fingerprint
        elif current_workspace_fingerprint != workspace_fingerprint:
            return None
        seen_paths.add(path)
        artifacts.append({"path": path, **numeric})
    if workspace_fingerprint is None:
        return None
    return {
        "schema_version": _COMMAND_ARTIFACT_SCHEMA_VERSION,
        "capture": _COMMAND_ARTIFACT_CAPTURE,
        "command_call_id": call_id,
        "completion_event_id": completion.id,
        "retry_attempt": retry_attempt,
        "session_attempt_id": session_attempt_id.strip(),
        "workspace_chain": _serialize_workspace_chain_fingerprint(workspace_fingerprint),
        "artifacts": artifacts,
    }


def _workspace_chain_fingerprint(value: object) -> DirectoryChainFingerprint | None:
    """Validate the closed durable workspace capability snapshot."""
    if (
        not isinstance(value, (list, tuple))
        or not value
        or len(value) > _MAX_WORKSPACE_CHAIN_COMPONENTS
    ):
        return None
    fingerprint: list[tuple[str, int, int, int]] = []
    rendered_path_chars = 0
    for index, component in enumerate(value):
        if (
            not isinstance(component, Mapping)
            or frozenset(component) != _WORKSPACE_CHAIN_COMPONENT_KEYS
        ):
            return None
        name = component.get("name")
        if not isinstance(name, str) or (
            (index == 0 and name != os.sep)
            or (index > 0 and (name in {"", ".", ".."} or Path(name).name != name))
        ):
            return None
        rendered_path_chars += len(name) + int(index > 0)
        if rendered_path_chars > _MAX_COMMAND_ARTIFACT_PATH_CHARS:
            return None
        numeric: list[int] = []
        for key in ("st_dev", "st_ino", "st_mode"):
            item = component.get(key)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                return None
            numeric.append(item)
        if numeric[1] == 0 or not stat.S_ISDIR(numeric[2]):
            return None
        fingerprint.append((name, *numeric))
    return tuple(fingerprint)


def _serialize_workspace_chain_fingerprint(
    fingerprint: DirectoryChainFingerprint,
) -> list[dict[str, int | str]]:
    return [
        {"name": name, "st_dev": device, "st_ino": inode, "st_mode": mode}
        for name, device, inode, mode in fingerprint
    ]


def _is_canonical_command_artifact_path(value: str) -> bool:
    if not value or len(value) > _MAX_COMMAND_ARTIFACT_PATH_CHARS or "\\" in value:
        return False
    path = Path(value)
    return (
        not path.is_absolute()
        and value == path.as_posix()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _journal_entry_proves_command_artifact(
    entry: EvidenceEntry,
    *,
    relative_path: str,
    task_cwd: str | None,
) -> bool:
    """Verify one Phase-1 command artifact still names its captured file."""
    payload = entry.payload
    provenance = payload.get("command_artifacts")
    if (
        task_cwd is None
        or not isinstance(provenance, Mapping)
        or frozenset(provenance) != _COMMAND_ARTIFACT_PROVENANCE_KEYS
        or provenance.get("schema_version") != _COMMAND_ARTIFACT_SCHEMA_VERSION
        or provenance.get("capture") != _COMMAND_ARTIFACT_CAPTURE
        or not isinstance(provenance.get("command_call_id"), str)
        or not str(provenance["command_call_id"]).strip()
        or provenance.get("command_call_id") != payload.get("tool_call_id")
        or not isinstance(provenance.get("completion_event_id"), str)
        or not str(provenance["completion_event_id"]).strip()
        or len(entry.source_event_ids) != 2
        or len(set(entry.source_event_ids)) != 2
        or provenance.get("completion_event_id") != entry.source_event_ids[-1]
        or isinstance(provenance.get("retry_attempt"), bool)
        or not isinstance(provenance.get("retry_attempt"), int)
        or int(provenance["retry_attempt"]) < 0
        or provenance.get("retry_attempt") != payload.get("retry_attempt")
        or not isinstance(provenance.get("session_attempt_id"), str)
        or not str(provenance["session_attempt_id"]).strip()
        or provenance.get("session_attempt_id") != payload.get("session_attempt_id")
    ):
        return False
    artifacts = provenance.get("artifacts")
    if (
        not isinstance(artifacts, (list, tuple))
        or not artifacts
        or len(artifacts) > _MAX_COMMAND_ARTIFACTS
    ):
        return False
    validated: list[Mapping[str, object]] = []
    seen_paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or frozenset(artifact) != _COMMAND_ARTIFACT_KEYS:
            return False
        path = artifact.get("path")
        if (
            not isinstance(path, str)
            or not _is_canonical_command_artifact_path(path)
            or path in seen_paths
        ):
            return False
        for key in _COMMAND_ARTIFACT_KEYS - {"path"}:
            value = artifact.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
        if artifact.get("st_ino") == 0 or not stat.S_ISREG(int(artifact["st_mode"])):
            return False
        seen_paths.add(path)
        validated.append(artifact)
    matches = [artifact for artifact in validated if artifact["path"] == relative_path]
    if len(matches) != 1:
        return False
    artifact = matches[0]
    if frozenset(artifact) != _COMMAND_ARTIFACT_KEYS:
        return False
    workspace_fingerprint = _workspace_chain_fingerprint(provenance.get("workspace_chain"))
    if workspace_fingerprint is None:
        return False
    return _nofollow_workspace_artifact_matches(
        task_cwd,
        relative_path,
        expected=artifact,
        expected_workspace=workspace_fingerprint,
    )


def _nofollow_workspace_artifact_matches(
    task_cwd: str,
    relative_path: str,
    *,
    expected: Mapping[str, object],
    expected_workspace: DirectoryChainFingerprint,
) -> bool:
    """Match an artifact while its no-follow directory and leaf fds stay held."""
    leased_chain: NoFollowDirectoryChain | None = None
    current_chain: NoFollowDirectoryChain | None = None
    leaf_fds: list[int] = []
    try:
        if not nofollow_directory_capabilities_available():
            return False
        workspace = Path(os.path.abspath(Path(task_cwd).expanduser()))
        relative = Path(relative_path)
        if not _is_canonical_command_artifact_path(relative_path):
            return False
        leased_chain = open_nofollow_directory_chain(
            workspace,
            relative_components=relative.parts[:-1],
        )
        workspace_component_count = len(workspace.parts)
        if tuple(leased_chain.fingerprint()[:workspace_component_count]) != expected_workspace:
            return False

        # A parent can be renamed and replaced after ``open`` returns.  Rewalk
        # the current lexical path and bind every opened directory identity,
        # plus the leaf fingerprint, to the held no-follow traversal.
        current_chain = open_nofollow_directory_chain(
            workspace,
            relative_components=relative.parts[:-1],
        )
        if tuple(
            current_chain.fingerprint()[:workspace_component_count]
        ) != expected_workspace or not leased_chain.matches_opened_directories(current_chain):
            return False
        leased_fd = leased_chain.leaf_fd
        current_fd = current_chain.leaf_fd
        leaf_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        leased_leaf_fd = os.open(relative.name, leaf_flags, dir_fd=leased_fd)
        leaf_fds.append(leased_leaf_fd)
        leased_leaf = os.fstat(leased_leaf_fd)
        if not stat.S_ISREG(leased_leaf.st_mode) or any(
            expected.get(key) != getattr(leased_leaf, key)
            for key in _COMMAND_ARTIFACT_KEYS - {"path"}
        ):
            return False
        current_leaf_fd = os.open(relative.name, leaf_flags, dir_fd=current_fd)
        leaf_fds.append(current_leaf_fd)
        current_leaf = os.fstat(current_leaf_fd)
        if _command_artifact_stat_fingerprint(current_leaf) != _command_artifact_stat_fingerprint(
            leased_leaf
        ):
            return False

        # Revalidate names after reading the leaf.  The lexical workspace or a
        # child directory can be replaced after its second ``open`` and the
        # immediate fd comparison above; held fds alone would then keep seeing
        # the displaced tree.  Every current parent must still name the opened
        # child, the final parent must still name the same leaf fingerprint,
        # and the absolute workspace path must still name the current root.
        named_leaf = os.stat(relative.name, dir_fd=current_fd, follow_symlinks=False)
        if _command_artifact_stat_fingerprint(named_leaf) != _command_artifact_stat_fingerprint(
            current_leaf
        ):
            return False
        return leased_chain.postvalidate() and current_chain.postvalidate()
    except (OSError, RuntimeError, ValueError):
        return False
    finally:
        for directory_fd in reversed(leaf_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass
        if current_chain is not None:
            current_chain.close()
        if leased_chain is not None:
            leased_chain.close()


def _command_artifact_stat_fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(value, key) for key in _COMMAND_ARTIFACT_KEYS - {"path"})


_TOOL_CALL_ID_KEYS = ("tool_call_id", "tool_use_id", "call_id")


def _event_tool_call_aliases(event: BaseEvent) -> tuple[tuple[str, ...], bool]:
    """Collect every supported correlation alias and flag malformed values."""
    if not isinstance(event.data, Mapping):
        return (), True
    containers: list[Mapping[str, Any]] = [event.data]
    malformed = False
    if "meta" in event.data:
        message_meta = event.data["meta"]
        if not isinstance(message_meta, Mapping):
            malformed = True
        else:
            containers.append(message_meta)
    if "tool_result" in event.data:
        tool_result = event.data["tool_result"]
        if not isinstance(tool_result, Mapping):
            malformed = True
        else:
            containers.append(tool_result)
            if "meta" in tool_result:
                meta = tool_result["meta"]
                if not isinstance(meta, Mapping):
                    malformed = True
                else:
                    containers.append(meta)
    values: list[str] = []
    for container in containers:
        for key in _TOOL_CALL_ID_KEYS:
            if key not in container:
                continue
            value = container[key]
            if not isinstance(value, str) or not value.strip():
                malformed = True
            else:
                values.append(value.strip())
    return tuple(values), malformed


def _event_has_conflicting_tool_call_ids(event: BaseEvent) -> bool:
    aliases, malformed = _event_tool_call_aliases(event)
    return malformed or len(set(aliases)) > 1


def _event_tool_call_id(event: BaseEvent) -> str | None:
    """Return the sole normalized correlation id, rejecting alias conflicts."""
    aliases, malformed = _event_tool_call_aliases(event)
    distinct = set(aliases)
    return next(iter(distinct)) if not malformed and len(distinct) == 1 else None


def _event_has_explicit_tool_success(
    event: BaseEvent,
    *,
    require_command_verdict: bool = False,
) -> bool:
    """Return True only for machine-readable non-error completion evidence.

    Command execution additionally requires a command-specific result bit or
    zero exit status. Generic lifecycle completion is sufficient for mutation
    tools, but cannot prove that a Bash body ran successfully.
    """
    data = event.data
    if not isinstance(data, Mapping):
        return False
    verdict_containers: list[Mapping[str, Any]] = [data]
    if "meta" in data:
        data_meta = data["meta"]
        if not isinstance(data_meta, Mapping):
            return False
        verdict_containers.append(data_meta)
    if data.get("is_error_invalid") is True:
        return False
    if "is_error" in data and not isinstance(data["is_error"], bool):
        return False
    if data.get("is_error") is True:
        return False
    tool_result = data.get("tool_result")
    if "tool_result" in data:
        if not isinstance(tool_result, Mapping):
            return False
        verdict_containers.append(tool_result)
        if tool_result.get("is_error_invalid") is True:
            return False
        if "is_error" in tool_result and not isinstance(tool_result["is_error"], bool):
            return False
        if tool_result.get("is_error") is True:
            return False
        if "meta" in tool_result:
            meta = tool_result["meta"]
            if not isinstance(meta, Mapping):
                return False
            verdict_containers.append(meta)
    if any(_mapping_has_failure_verdict(container) for container in verdict_containers):
        return False
    is_completion_event = event.type == "execution.tool.completed"
    command_success_signal = is_completion_event and (
        data.get("is_error") is False
        or (isinstance(tool_result, Mapping) and tool_result.get("is_error") is False)
    )
    success_signal = command_success_signal
    if "exit_code" in data:
        exit_code = data["exit_code"]
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return False
        if exit_code != 0:
            return False
        command_success_signal = True
        success_signal = True
    if isinstance(tool_result, Mapping):
        meta = tool_result.get("meta")
        if isinstance(meta, Mapping):
            if "exit_status" in meta:
                exit_status = meta["exit_status"]
                if isinstance(exit_status, bool) or not isinstance(exit_status, int):
                    return False
                if exit_status != 0:
                    return False
                command_success_signal = True
                success_signal = True
    subtype = data.get("subtype")
    if isinstance(subtype, str) and subtype.strip().lower() == "success":
        success_signal = True
    status = data.get("status")
    if isinstance(status, str):
        normalized_status = status.strip().lower()
        if normalized_status in {"failed", "error"}:
            return False
        if normalized_status in {"completed", "success", "succeeded"}:
            success_signal = True
    runtime_event_type = data.get("runtime_event_type")
    if isinstance(runtime_event_type, str):
        normalized = runtime_event_type.strip().lower()
        if normalized.endswith((".failed", ".error")):
            return False
        if normalized.endswith((".completed", ".succeeded")):
            success_signal = True
    return command_success_signal if require_command_verdict else success_signal


def _mapping_has_failure_verdict(value: Mapping[str, Any]) -> bool:
    """Treat explicit failure flags, contradictions, and malformed verdicts as vetoes."""
    for key in RUNTIME_SUCCESS_BOOLEAN_FIELDS:
        if key not in value:
            continue
        success = value[key]
        if not isinstance(success, bool) or not success:
            return True
    for key in RUNTIME_FAILURE_BOOLEAN_FIELDS:
        if key not in value:
            continue
        flag = value[key]
        if not isinstance(flag, bool) or flag:
            return True
    for key in RUNTIME_TOOL_EXIT_STATUS_FIELDS:
        if key not in value:
            continue
        exit_value = value[key]
        if isinstance(exit_value, bool) or not isinstance(exit_value, int) or exit_value != 0:
            return True
    allowed_statuses = {"completed", "success", "succeeded"}
    allowed_values = {
        "subtype": {*allowed_statuses, "tool_result"},
        "status": allowed_statuses,
        "runtime_status": allowed_statuses,
        "runtime_signal": {"tool_completed", "session_completed"},
        "runtime_event_type": {
            "tool.completed",
            "tool.output",
            "tool.result",
            "result.completed",
            "run.completed",
            "session.completed",
            "task.completed",
            "turn.completed",
        },
    }
    for key, accepted in allowed_values.items():
        if key not in value:
            continue
        status = value[key]
        if not isinstance(status, str) or not status.strip():
            return True
        normalized = status.strip().lower()
        if (
            key == "runtime_status"
            and normalized == "running"
            and isinstance(value.get("runtime_signal"), str)
            and str(value["runtime_signal"]).strip().lower() == "tool_completed"
        ):
            continue
        if normalized not in accepted:
            return True
    return False


def _event_matches_accepted_attempt(
    event: BaseEvent,
    *,
    scope_id: str,
    execution_id: str,
    retry_attempt: int | None,
    session_attempt_id: str | None,
) -> bool:
    if not _event_matches_scope(event, scope_id):
        return False
    data = event.data
    if not isinstance(data, Mapping):
        return False
    for key in ("execution_id", "parent_execution_id"):
        if key in data and data.get(key) != execution_id:
            return False
    if data.get("execution_id") != execution_id:
        return False
    if retry_attempt is not None:
        if data.get("retry_attempt") != retry_attempt:
            return False
        if "attempt_number" in data and data.get("attempt_number") != retry_attempt + 1:
            return False
    return not (
        session_attempt_id is not None and data.get("session_attempt_id") != session_attempt_id
    )


def _normalized_event_tool_name(event: BaseEvent) -> str | None:
    """Return one non-blank tool name for correlation-only comparisons."""
    if not isinstance(event.data, Mapping):
        return None
    value = event.data.get("tool_name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _owned_idless_tool_completions(
    events: tuple[BaseEvent, ...],
    *,
    scope_id: str,
    execution_id: str,
    retry_attempt: int | None,
    session_attempt_id: str | None,
    event_id_counts: Mapping[str, int],
) -> dict[int, int]:
    """Pair id-less terminals owned by exactly one accepted-attempt start.

    ID-bearing peers are independent and do not break the legacy id-less slot.
    A duplicate, malformed, or second id-less start poisons that slot until the
    next id-less terminal consumes it, matching the live lease tracker.
    """
    owned: dict[int, int] = {}
    active = False
    active_start_index: int | None = None
    active_tool: str | None = None
    for index, event in enumerate(events):
        if event.type not in {"execution.tool.started", "execution.tool.completed"}:
            continue
        if not _event_matches_accepted_attempt(
            event,
            scope_id=scope_id,
            execution_id=execution_id,
            retry_attempt=retry_attempt,
            session_attempt_id=session_attempt_id,
        ):
            continue
        aliases, malformed_alias = _event_tool_call_aliases(event)
        if aliases:
            continue
        if malformed_alias or event_id_counts.get(event.id) != 1:
            active = True
            active_start_index = None
            active_tool = None
            continue
        if event.type == "execution.tool.started":
            tool_name = _normalized_event_tool_name(event)
            if active or tool_name is None:
                active = True
                active_start_index = None
                active_tool = None
            else:
                active = True
                active_start_index = index
                active_tool = tool_name
            continue

        completion_tool = _normalized_event_tool_name(event)
        if (
            active
            and active_start_index is not None
            and active_tool is not None
            and (completion_tool is None or completion_tool == active_tool)
        ):
            owned[active_start_index] = index
        active = False
        active_start_index = None
        active_tool = None
    return owned


def _correlated_tool_completion(
    events: tuple[BaseEvent, ...],
    *,
    start_index: int,
    scope_id: str,
    execution_id: str,
    retry_attempt: int | None,
    session_attempt_id: str | None,
    require_command_verdict: bool,
    event_id_counts: Mapping[str, int],
    idless_completion_by_start: Mapping[int, int],
    owned_idless_completion_indexes: frozenset[int],
) -> tuple[BaseEvent | None, bool]:
    """Return an exact completion and whether failure/ambiguity vetoes the start."""
    start = events[start_index]
    if event_id_counts.get(start.id) != 1:
        return None, True
    start_data = start.data
    if not isinstance(start_data, Mapping):
        return None, True
    if _event_has_conflicting_tool_call_ids(start):
        return None, True
    start_tool = start_data.get("tool_name")
    if not isinstance(start_tool, str):
        return None, True
    start_call_id = _event_tool_call_id(start)

    if start_call_id is not None:
        matching_starts = tuple(
            candidate
            for candidate in events
            if candidate.type == "execution.tool.started"
            and not _event_has_conflicting_tool_call_ids(candidate)
            and _event_matches_accepted_attempt(
                candidate,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            )
            and _event_tool_call_id(candidate) == start_call_id
        )
        if len(matching_starts) != 1:
            return None, True
        if any(
            candidate.type == "execution.tool.completed"
            and _event_matches_accepted_attempt(
                candidate,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            )
            and _event_has_conflicting_tool_call_ids(candidate)
            and start_call_id in _event_tool_call_aliases(candidate)[0]
            for candidate in events
        ):
            return None, True
        matches = tuple(
            (candidate_index, candidate)
            for candidate_index, candidate in enumerate(events)
            if candidate.type == "execution.tool.completed"
            and _event_matches_accepted_attempt(
                candidate,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            )
            and _event_tool_call_id(candidate) == start_call_id
        )
        if not matches:
            return None, False
        if len(matches) != 1:
            return None, True
        completion_index, completion = matches[0]
        if completion_index <= start_index or event_id_counts.get(completion.id) != 1:
            return None, True
        for candidate_index, candidate in enumerate(
            events[start_index + 1 : completion_index],
            start=start_index + 1,
        ):
            if candidate.type != "execution.tool.completed" or not _event_matches_accepted_attempt(
                candidate,
                scope_id=scope_id,
                execution_id=execution_id,
                retry_attempt=retry_attempt,
                session_attempt_id=session_attempt_id,
            ):
                continue
            if _event_tool_call_id(candidate) is not None:
                continue
            if candidate_index in owned_idless_completion_indexes:
                continue
            candidate_tool_value = (
                candidate.data.get("tool_name") if isinstance(candidate.data, Mapping) else None
            )
            candidate_tool = (
                candidate_tool_value.strip()
                if isinstance(candidate_tool_value, str) and candidate_tool_value.strip()
                else None
            )
            if candidate_tool in {None, start_tool.strip()}:
                # An id-less same-tool (or nameless) terminal could have closed
                # this named command.  Do not let a later matching completion
                # revive its authority, even for a crafted durable journal.
                return None, True
        if (
            not isinstance(completion.data, Mapping)
            or completion.data.get("tool_name") != start_tool
        ):
            return None, True
        if not _event_has_explicit_tool_success(
            completion,
            require_command_verdict=require_command_verdict,
        ):
            return None, True
        return completion, False

    # Legacy id-less streams keep one independent slot across concurrent named
    # peers. Only a uniquely owned id-less terminal may close the start.
    owned_completion_index = idless_completion_by_start.get(start_index)
    if owned_completion_index is not None:
        completion = events[owned_completion_index]
        if event_id_counts.get(completion.id) != 1 or not _event_has_explicit_tool_success(
            completion,
            require_command_verdict=require_command_verdict,
        ):
            return None, True
        return completion, False

    for candidate in events[start_index + 1 :]:
        if candidate.type not in {"execution.tool.started", "execution.tool.completed"}:
            continue
        if not _event_matches_accepted_attempt(
            candidate,
            scope_id=scope_id,
            execution_id=execution_id,
            retry_attempt=retry_attempt,
            session_attempt_id=session_attempt_id,
        ):
            continue
        aliases, _malformed = _event_tool_call_aliases(candidate)
        if not aliases:
            return None, True
    return None, False


def _event_result_preview(start: BaseEvent, completion: BaseEvent | None) -> str | None:
    """Return bounded runtime-produced output for an admitted tool call."""
    parts: list[str] = []
    for event in (start, completion):
        if event is None or not isinstance(event.data, Mapping):
            continue
        data = event.data
        for key in ("result_preview", "output", "stdout", "stderr", "tool_result_text"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        tool_result = data.get("tool_result")
        if isinstance(tool_result, Mapping):
            for key in ("text_content", "content", "output", "stdout", "stderr"):
                value = tool_result.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
    if not parts:
        return None
    return "\n".join(dict.fromkeys(parts))[-16_000:]


def _event_matches_scope(event: BaseEvent, scope_id: str) -> bool:
    if (
        event.aggregate_type != "execution"
        or event.aggregate_id != scope_id
        or not isinstance(event.data, Mapping)
    ):
        return False
    aliases: list[str] = []
    for key in ("ac_id", "session_scope_id"):
        if key not in event.data:
            continue
        value = event.data[key]
        if not isinstance(value, str) or not value.strip():
            return False
        aliases.append(value.strip())
    return all(value == scope_id for value in aliases)


def _event_workspace_relative_path(raw_path: str, data: Mapping[str, Any]) -> str | None:
    """Return a contained POSIX path relative to the runtime cwd, else ``None``."""
    runtime = data.get("runtime")
    runtime_cwd = runtime.get("cwd") if isinstance(runtime, Mapping) else None
    if not isinstance(runtime_cwd, str) or not runtime_cwd.strip():
        return _safe_relative_path(raw_path)
    try:
        root = Path(runtime_cwd).expanduser().resolve(strict=False)
        candidate_path = Path(raw_path).expanduser()
        candidate = (
            candidate_path.resolve(strict=False)
            if candidate_path.is_absolute()
            else (root / candidate_path).resolve(strict=False)
        )
        return candidate.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None


def _safe_relative_path(raw_path: str) -> str | None:
    path = Path(raw_path.strip())
    if not raw_path.strip() or path.is_absolute() or ".." in path.parts:
        return None
    normalized = path.as_posix()
    return normalized if normalized not in {"", "."} else None


def evaluate_deliver_claim(
    manifest: EvidenceManifest,
    claim: DeliverEvidenceClaim,
    *,
    traceguard_validator: TraceGuardValidator,
    claim_term_guard: ClaimTermGuard | None = None,
    journal_bound: bool = False,
) -> DeliverGateVerdict:
    """Evaluate a typed AC deliver claim with a TraceGuard-compatible validator.

    This is the narrow verdict-adapter slice for #978 P2. It converts
    Ouroboros' journal-derived :class:`EvidenceManifest` into TraceGuard's
    canonical ``fact_id`` / ``chunk_id`` manifest shape and converts the leaf
    claim into TraceGuard's ``parent_synthesis`` claim surface. The deterministic
    validator is injected so this PR does not add a hard runtime dependency or
    alter live AC success semantics.

    ``journal_bound=True`` is the fail-closed live mode.  The canonical
    TraceGuard manifest is then built entirely from successful journal entries:
    each entry's journal-generated handle is both its fact and chunk identity.
    The leaf's arbitrary ``fact_id`` is never copied into the manifest; it is
    retained only for the returned diagnostics.  The parent synthesis cites the
    journal handle and accepted handles are mapped back to *all* original facts
    that cited them.  This avoids the circular legacy shape where a claim could
    mint a fact id and the adapter would insert that same id into the evidence
    manifest before validating it.
    """
    if manifest.ac_id != claim.ac_id:
        msg = (
            "DeliverEvidenceClaim.ac_id must match EvidenceManifest.ac_id "
            f"({claim.ac_id!r} != {manifest.ac_id!r})"
        )
        raise ValueError(msg)
    if journal_bound and claim_term_guard is None:
        msg = "journal_bound deliver validation requires a claim_term_guard"
        raise ValueError(msg)

    traceguard_manifest, source_events_by_handle = _traceguard_manifest(
        manifest,
        claim,
        journal_bound=journal_bound,
    )
    evidence_text_by_handle = {
        entry.chunk_id: entry.text for entry in traceguard_manifest if entry.chunk_id
    }
    missing_evidence = _missing_evidence_summaries(
        claim,
        available_handles=frozenset(source_events_by_handle),
    )
    parent_synthesis = _parent_synthesis_from_claim(claim, journal_bound=journal_bound)
    raw_result = traceguard_validator(
        evidence_manifest=traceguard_manifest,
        parent_synthesis=parent_synthesis,
    )
    raw_rejected = _rejected_claim_summaries(raw_result)
    rejected = missing_evidence + (
        _remap_journal_rejections(raw_rejected, claim) if journal_bound else raw_rejected
    )
    accepted_claims = getattr(raw_result, "accepted_claims", ())
    raw_accepted_fact_ids = _claim_fact_ids(accepted_claims)
    if not raw_accepted_fact_ids:
        raw_accepted_fact_ids = _string_tuple(getattr(raw_result, "allowed_fact_ids", ()))
    raw_accepted_handles = _claim_chunk_ids(accepted_claims)
    if not raw_accepted_handles:
        raw_accepted_handles = _string_tuple(getattr(raw_result, "allowed_chunk_ids", ()))
    if journal_bound:
        accepted_handles = _journal_accepted_handles(
            raw_fact_ids=raw_accepted_fact_ids,
            raw_handles=raw_accepted_handles,
            available_handles=frozenset(source_events_by_handle),
        )
        accepted_fact_ids = _claim_fact_ids_for_handles(claim, accepted_handles)
    else:
        accepted_fact_ids = raw_accepted_fact_ids
        accepted_handles = raw_accepted_handles
    journal_mapping_missing = journal_bound and bool(raw_result.accepted) and not accepted_handles
    if journal_mapping_missing:
        rejected += (
            (
                None,
                None,
                "traceguard_mapping_missing: accepted result named no journal evidence handle",
            ),
        )
    claim_term_guard_verdict = None
    # In mixed rejected TraceGuard results, chunk-only fallbacks are provenance,
    # not per-fact acceptance. Semantic checks must not fan out to every claim
    # sharing a structurally allowed evidence handle.
    should_run_claim_term_guard = bool(raw_result.accepted) or bool(accepted_fact_ids)
    if claim_term_guard is not None and should_run_claim_term_guard:
        claim_term_guard_verdict = claim_term_guard(
            ac_id=manifest.ac_id,
            facts=_claim_term_guard_facts(
                claim,
                accepted_fact_ids=accepted_fact_ids,
                accepted_handles=accepted_handles,
                evidence_text_by_handle=evidence_text_by_handle,
            ),
        )
        if not claim_term_guard_verdict.accepted:
            rejected_fact_ids_by_index = claim_term_guard_verdict.rejected_fact_ids
            rejected += tuple(
                (
                    (
                        rejected_fact_ids_by_index[index]
                        if index < len(rejected_fact_ids_by_index)
                        else None
                    ),
                    None,
                    reason,
                )
                for index, reason in enumerate(claim_term_guard_verdict.rejected_reasons)
            )
    rejected_fact_ids = _dedupe_strings(
        fact_id for fact_id, _, _ in rejected if fact_id is not None
    )
    unsupported_claim_rate = _unsupported_claim_rate(
        raw_rate=float(raw_result.unsupported_claim_rate),
        rejected=rejected,
        total_claims=len(claim.facts),
    )
    final_accepted_fact_ids = _final_accepted_fact_ids(
        accepted_fact_ids=accepted_fact_ids,
        rejected_fact_ids=rejected_fact_ids,
    )
    final_accepted_handles = _final_accepted_handles(
        claim,
        accepted_fact_ids=accepted_fact_ids,
        final_accepted_fact_ids=final_accepted_fact_ids,
        accepted_handles=accepted_handles,
    )

    return DeliverGateVerdict(
        ac_id=manifest.ac_id,
        accepted=(
            bool(raw_result.accepted)
            and not missing_evidence
            and not journal_mapping_missing
            and (claim_term_guard_verdict is None or claim_term_guard_verdict.accepted)
        ),
        unsupported_claim_rate=unsupported_claim_rate,
        accepted_fact_ids=final_accepted_fact_ids,
        rejected_fact_ids=rejected_fact_ids,
        rejected_reasons=_dedupe_strings(reason for _, _, reason in rejected),
        evidence_event_ids=_evidence_event_ids_for_handles(
            final_accepted_handles,
            source_events_by_handle=source_events_by_handle,
        ),
    )


def _final_accepted_fact_ids(
    *,
    accepted_fact_ids: tuple[str, ...],
    rejected_fact_ids: tuple[str, ...],
) -> tuple[str, ...]:
    rejected_fact_set = frozenset(rejected_fact_ids)
    return tuple(fact_id for fact_id in accepted_fact_ids if fact_id not in rejected_fact_set)


def _final_accepted_handles(
    claim: DeliverEvidenceClaim,
    *,
    accepted_fact_ids: tuple[str, ...],
    final_accepted_fact_ids: tuple[str, ...],
    accepted_handles: tuple[str, ...],
) -> tuple[str, ...]:
    if not accepted_fact_ids:
        return accepted_handles
    final_accepted_fact_set = frozenset(final_accepted_fact_ids)
    accepted_handle_set = frozenset(accepted_handles)
    return tuple(
        fact.evidence_handle
        for fact in claim.facts
        if fact.fact_id in final_accepted_fact_set
        and (not accepted_handle_set or fact.evidence_handle in accepted_handle_set)
    )


def _filter_events_by_anchors(
    events: Iterable[BaseEvent],
    *,
    execution_id: str | None,
    session_id: str | None,
) -> tuple[BaseEvent, ...]:
    return tuple(
        event
        for event in events
        if _event_matches_required_anchors(
            event,
            execution_id=execution_id,
            session_id=session_id,
        )
    )


def _event_matches_required_anchors(
    event: BaseEvent,
    *,
    execution_id: str | None,
    session_id: str | None,
) -> bool:
    if execution_id is not None and not _event_matches_anchor(
        event,
        execution_id,
        keys=("execution_id", "parent_execution_id"),
    ):
        return False
    return session_id is None or _event_matches_optional_session_anchor(event, session_id)


def _event_matches_optional_session_anchor(event: BaseEvent, session_id: str) -> bool:
    """Return True when an event does not contradict the optional session anchor.

    The I/O journal recorder allows execution-scoped tool/LLM events to carry
    ``execution_id`` without also carrying ``session_id``. Once an execution
    anchor has matched, those rows are valid evidence and must not be pruned
    merely because the optional session correlation field is absent. Explicit
    session-scoped rows, or rows that do carry a ``session_id`` payload, still
    have to match the requested session so broad EventStore OR queries cannot
    splice another session's evidence into the manifest.
    """
    if event.aggregate_type == "session":
        return event.aggregate_id == session_id
    if isinstance(event.data, dict):
        value = event.data.get("session_id")
        if isinstance(value, str) and value.strip():
            return value.strip() == session_id
    return True


def _event_matches_anchor(event: BaseEvent, anchor: str, *, keys: tuple[str, ...]) -> bool:
    if event.aggregate_id == anchor:
        return True
    if isinstance(event.data, dict):
        for key in keys:
            value = event.data.get(key)
            if isinstance(value, str) and value.strip() == anchor:
                return True
    return False


def _normalize_optional_anchor(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        msg = f"load_ac_evidence_manifest received blank {name}"
        raise ValueError(msg)
    return stripped


def _traceguard_manifest(
    manifest: EvidenceManifest,
    claim: DeliverEvidenceClaim,
    *,
    journal_bound: bool,
) -> tuple[tuple[TraceGuardEvidenceInput, ...], dict[str, tuple[str, ...]]]:
    entries_by_handle = {entry.handle: entry for entry in manifest.entries if entry.ok is True}
    entries: list[TraceGuardEvidenceInput] = []
    source_events_by_handle: dict[str, tuple[str, ...]] = {}
    if journal_bound:
        # The evidence side must be independent of the claim.  Journal handles
        # are stable identities derived by ``journal.EvidenceEntry`` from the
        # recorded source events; using them on both TraceGuard axes gives the
        # validator a genuine manifest membership check without inventing a fact
        # id from the text being judged.
        for entry in entries_by_handle.values():
            entries.append(
                TraceGuardEvidenceInput(
                    fact_id=entry.handle,
                    chunk_id=entry.handle,
                    text=_evidence_text(entry.payload),
                    child_call_id=",".join(entry.source_event_ids),
                )
            )
            source_events_by_handle[entry.handle] = entry.source_event_ids
        return tuple(entries), source_events_by_handle

    for fact in claim.facts:
        entry = entries_by_handle.get(fact.evidence_handle)
        if entry is None:
            continue
        text = _evidence_text(entry.payload)
        entries.append(
            TraceGuardEvidenceInput(
                fact_id=fact.fact_id,
                chunk_id=fact.evidence_handle,
                text=text,
                child_call_id=",".join(entry.source_event_ids),
            )
        )
        source_events_by_handle[entry.handle] = entry.source_event_ids
    return tuple(entries), source_events_by_handle


def _claim_term_guard_facts(
    claim: DeliverEvidenceClaim,
    *,
    accepted_fact_ids: tuple[str, ...],
    accepted_handles: tuple[str, ...],
    evidence_text_by_handle: dict[str, str],
) -> tuple[ClaimTermGuardFact, ...]:
    accepted_fact_set = frozenset(accepted_fact_ids)
    accepted_handle_set = frozenset(accepted_handles)
    facts: list[ClaimTermGuardFact] = []
    for fact in claim.facts:
        if accepted_fact_set and fact.fact_id not in accepted_fact_set:
            continue
        if accepted_handle_set and fact.evidence_handle not in accepted_handle_set:
            continue
        evidence_text = evidence_text_by_handle.get(fact.evidence_handle)
        if evidence_text is None:
            continue
        facts.append(
            ClaimTermGuardFact(
                fact_id=fact.fact_id,
                evidence_handle=fact.evidence_handle,
                statement=fact.statement,
                evidence_text=evidence_text,
            )
        )
    return tuple(facts)


def _evidence_text(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return str(payload)
    context_parts: list[str] = []
    workspace_relative_path = payload.get("workspace_relative_path")
    if isinstance(workspace_relative_path, str) and workspace_relative_path.strip():
        context_parts.append(f"path={workspace_relative_path.strip()}")
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        context_parts.append(f"command={command.strip()}")
    child_ac_id = payload.get("child_ac_id")
    if isinstance(child_ac_id, str) and child_ac_id.strip():
        context_parts.append(f"child_ac_id={child_ac_id.strip()}")

    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name in {"Edit", "Write", "NotebookEdit"}:
        for key in ("args_preview", "result_preview"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                context_parts.append(value.strip())
        if context_parts:
            return "; ".join(context_parts)

    preview_parts: list[str] = []
    for key in ("result_preview", "args_preview"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            preview_parts.append(value.strip())
    if preview_parts:
        return "; ".join([*context_parts, *preview_parts])

    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name.strip():
        return "; ".join([*context_parts, tool_name.strip()])
    if context_parts:
        return "; ".join(context_parts)
    return str(dict(payload))


def _parent_synthesis_from_claim(
    claim: DeliverEvidenceClaim,
    *,
    journal_bound: bool,
) -> dict[str, Any]:
    return {
        "result": {
            "observed_facts": [
                {
                    # In live journal-bound mode the leaf may label its claim for
                    # diagnostics, but only the independently generated journal
                    # handle participates in TraceGuard membership.
                    "fact_id": fact.evidence_handle if journal_bound else fact.fact_id,
                    "chunk_id": fact.evidence_handle,
                    "statement": fact.statement,
                }
                for fact in claim.facts
            ]
        }
    }


def _journal_accepted_handles(
    *,
    raw_fact_ids: tuple[str, ...],
    raw_handles: tuple[str, ...],
    available_handles: frozenset[str],
) -> tuple[str, ...]:
    """Return accepted journal handles from a journal-bound TraceGuard result."""
    return _dedupe_strings(
        value for value in (*raw_handles, *raw_fact_ids) if value in available_handles
    )


def _claim_fact_ids_for_handles(
    claim: DeliverEvidenceClaim,
    handles: tuple[str, ...],
) -> tuple[str, ...]:
    """Map every accepted handle back to every original claim fact safely.

    Multiple facts may cite one journal entry.  We deliberately retain all of
    them (in claim order) so the strict term guard checks every statement; a
    single semantic miss rejects the final verdict instead of silently choosing
    one ambiguous 1:1 mapping.
    """
    accepted = frozenset(handles)
    return tuple(fact.fact_id for fact in claim.facts if fact.evidence_handle in accepted)


def _remap_journal_rejections(
    rejected: tuple[tuple[str | None, str | None, str], ...],
    claim: DeliverEvidenceClaim,
) -> tuple[tuple[str | None, str | None, str], ...]:
    """Map canonical handle rejections back to all citing claim facts."""
    remapped: list[tuple[str | None, str | None, str]] = []
    for fact_id, chunk_id, reason in rejected:
        handle = chunk_id or fact_id
        matched = (
            tuple(fact for fact in claim.facts if fact.evidence_handle == handle)
            if handle is not None
            else ()
        )
        if not matched:
            remapped.append((fact_id, chunk_id, reason))
            continue
        remapped.extend((fact.fact_id, fact.evidence_handle, reason) for fact in matched)
    return tuple(remapped)


def _claim_fact_ids(claims: object) -> tuple[str, ...]:
    return tuple(
        fact_id
        for fact_id in (_claim_attr(claim, "fact_id") for claim in _iter_result_items(claims))
        if fact_id is not None
    )


def _claim_chunk_ids(claims: object) -> tuple[str, ...]:
    return tuple(
        chunk_id
        for chunk_id in (_claim_attr(claim, "chunk_id") for claim in _iter_result_items(claims))
        if chunk_id is not None
    )


def _rejected_claim_summaries(
    result: TraceGuardResultLike,
) -> tuple[tuple[str | None, str | None, str], ...]:
    summaries: list[tuple[str | None, str | None, str]] = []
    for rejection in _iter_result_items(getattr(result, "rejected_claims", ())):
        claim = _object_value(rejection, "claim")
        reason = _object_value(rejection, "reason")
        detail = _object_value(rejection, "detail")
        summaries.append(
            (
                _claim_attr(claim, "fact_id"),
                _claim_attr(claim, "chunk_id"),
                _join_reason(reason, detail),
            )
        )
    return tuple(summaries)


def _missing_evidence_summaries(
    claim: DeliverEvidenceClaim,
    *,
    available_handles: frozenset[str],
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            fact.fact_id,
            fact.evidence_handle,
            f"missing_evidence_handle: {fact.evidence_handle} is not present in manifest",
        )
        for fact in claim.facts
        if fact.evidence_handle not in available_handles
    )


def _unsupported_claim_rate(
    *,
    raw_rate: float,
    rejected: tuple[tuple[str | None, str | None, str], ...],
    total_claims: int,
) -> float:
    if total_claims <= 0:
        return raw_rate
    rejected_keys = {
        _rejection_key(item) for item in rejected if _counts_toward_unsupported_claim_rate(item)
    }
    rejected_count = len(rejected_keys)
    if rejected_count:
        return round(min(1.0, rejected_count / total_claims), 4)
    return raw_rate


def _counts_toward_unsupported_claim_rate(
    item: tuple[str | None, str | None, str],
) -> bool:
    return _reason_code(item[2]) != "semantic_miss"


def _rejection_key(item: tuple[str | None, str | None, str]) -> tuple[str, str]:
    fact_id, chunk_id, reason = item
    if fact_id is not None:
        return ("fact", fact_id)
    if chunk_id is not None:
        return ("chunk", chunk_id)
    return ("reason", reason)


def _reason_code(reason: str) -> str:
    return reason.split(":", maxsplit=1)[0].strip()


def _evidence_event_ids_for_handles(
    handles: tuple[str, ...],
    *,
    source_events_by_handle: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for handle in handles:
        for event_id in source_events_by_handle.get(handle, ()):
            if event_id not in seen:
                ordered.append(event_id)
                seen.add(event_id)
    return tuple(ordered)


def _iter_result_items(value: object) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, str | bytes | Mapping):
        return ()
    if isinstance(value, Iterable):
        return tuple(value)
    return ()


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in _iter_result_items(value) if isinstance(item, str) and item.strip()
    )


def _dedupe_strings(values: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            ordered.append(value)
            seen.add(value)
    return tuple(ordered)


def _claim_attr(claim: object, name: str) -> str | None:
    value = _object_value(claim, name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _object_value(item: object, name: str) -> object:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _join_reason(reason: object, detail: object) -> str:
    reason_text = reason if isinstance(reason, str) and reason.strip() else "traceguard_rejected"
    detail_text = detail if isinstance(detail, str) and detail.strip() else ""
    if detail_text:
        return f"{reason_text}: {detail_text}"
    return reason_text


def _chronological_events(events: Iterable[BaseEvent]) -> tuple[BaseEvent, ...]:
    """Return events oldest-first regardless of EventStore query ordering.

    Timestamp ties must preserve causal start-before-return ordering for
    journal pairs. ``BaseEvent.id`` is a UUID-like string, not a monotonic
    sequence, so it must never be used as a causality tie-breaker.
    """
    return tuple(sorted(events, key=_event_chronology_key))


def _event_chronology_key(event: BaseEvent) -> tuple[object, int]:
    return (event.timestamp, _event_phase_order(event.type))


def _event_phase_order(event_type: str) -> int:
    if event_type in {
        "tool.call.started",
        "llm.call.requested",
        "execution.tool.started",
    }:
        return 0
    if event_type in {
        "tool.call.returned",
        "llm.call.returned",
        "execution.tool.completed",
    }:
        return 1
    return 2


__all__ = [
    "DeliverEvidenceClaim",
    "DeliverEvidenceFact",
    "DeliverGateVerdict",
    "EventStoreEvidenceReader",
    "RUNTIME_FAILURE_BOOLEAN_FIELDS",
    "RUNTIME_SUCCESS_BOOLEAN_FIELDS",
    "RUNTIME_TOOL_EXIT_STATUS_FIELDS",
    "TraceGuardResultLike",
    "TraceGuardEvidenceInput",
    "TraceGuardValidator",
    "evaluate_deliver_claim",
    "load_ac_evidence_manifest",
]
