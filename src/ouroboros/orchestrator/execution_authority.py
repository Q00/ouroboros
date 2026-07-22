"""Finite execution-authority identity for the AC runtime.

Foundation A deliberately identifies a small, explicit component boundary. It
does not attempt to derive authority from an arbitrary Python callable graph:
closures, globals, descriptors, module monkeypatches, caches, and runtime
handles are volatile. A custom verifier can therefore be bound only to its
exact live object for this process; it is never made portable by introspection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
from threading import RLock
from typing import Any
import uuid

from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from ouroboros.orchestrator.runtime_param_negotiation import runtime_capabilities_for
from ouroboros.orchestrator.verifier import Verifier, structural_atomic_verifier
from ouroboros.orchestrator.zcode_cli_runtime import ZcodeCLIRuntime

EXECUTION_AUTHORITY_VERSION = 6
EXECUTION_AUTHORITY_BOUNDARY_VERSION = 6

_MAX_IDENTITY_DEPTH = 8
_MAX_IDENTITY_ITEMS = 256
_MAX_IDENTITY_SCALAR_CHARS = 8_192
_MAX_IDENTITY_JSON_CHARS = 64_000
_MAX_RUNTIME_EXECUTABLE_BYTES = 64 * 1024 * 1024

# No legacy runtime is a portable implementation in Foundation A.  In
# particular, a public CLI entry point that dynamically resolves ``self._...``
# helpers cannot be promoted by adding an ever-growing helper manifest.  A
# future sealed execution kernel may populate a separate reviewed table, but
# the current table is intentionally empty.
_CLOSED_RUNTIME_IMPLEMENTATIONS: dict[type[object], tuple[str, object, object]] = {}

_EXECUTOR_COMPONENT_VERSIONS = {
    "parallel_ac_executor": "parallel-ac-executor/v2",
    "leaf_dispatcher": "leaf-dispatcher/v1",
    "level_coordinator": "level-coordinator/v1",
    "rate_limit_gate": "rate-limit-gate/v1",
}
_BUILTIN_TRANSCRIPT_VERIFIER = "runtime-transcript-verifier/v1"
_BUILTIN_STRUCTURAL_VERIFIER = "structural-atomic-verifier/v1"
_BUILTIN_STRUCTURAL_VERIFIER_CODE = structural_atomic_verifier.__code__
_RATE_GATE_BUCKET_HELPER_NAMES = (
    "_prune",
    "_tokens_in_window",
    "_snapshot",
    "_request_wait_seconds",
    "_token_wait_seconds",
)


_PROCESS_LOCAL_AUTHORITY_CONSTRUCTION_TOKEN = object()
_SAFE_PROCESS_LOCAL_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_LOG = logging.getLogger(__name__)


class _ProcessLocalAuthorityGeneration:
    """An opaque, unpickleable capability minted only by the local registry.

    The correlation id is diagnostic data.  It is deliberately insufficient to
    construct a usable generation: the registry records the exact object that
    it minted and the PID that minted it.  This is a process-local lifecycle
    primitive, not a boundary against code that can monkeypatch this module.
    """

    __slots__ = ("_correlation_id",)

    def __init__(self, construction_token: object, correlation_id: str) -> None:
        if construction_token is not _PROCESS_LOCAL_AUTHORITY_CONSTRUCTION_TOKEN:
            raise TypeError("process-local authority generations are registry-minted")
        self._correlation_id = correlation_id

    @property
    def correlation_id(self) -> str:
        """Return a diagnostics-only correlation id."""
        return self._correlation_id

    def __reduce__(self) -> object:
        raise TypeError("process-local authority generations cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("process-local authority generations cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("process-local authority generations cannot be copied")


@dataclass(frozen=True, slots=True)
class _ProcessLocalAuthorityIssuance:
    """The registry-private mint record used to reject reconstructed objects."""

    generation: _ProcessLocalAuthorityGeneration
    correlation_id: str
    creator_pid: int


@dataclass(frozen=True, slots=True)
class _ProcessLocalAuthorityRegistration:
    """One exact live binding for a process-local session."""

    execution_id: str
    generation: _ProcessLocalAuthorityGeneration
    adapter: object
    creator_pid: int


class _ProcessLocalAuthorityRegistry:
    """Keep opaque Foundation A capabilities in one process and PID epoch."""

    def __init__(self) -> None:
        self._issued: dict[int, _ProcessLocalAuthorityIssuance] = {}
        self._registrations: dict[str, _ProcessLocalAuthorityRegistration] = {}
        self._claims: dict[str, _ProcessLocalAuthorityRegistration] = {}
        # A public terminalization reserves an otherwise-unclaimed owner while
        # its durable terminal record is being written.  This prevents a
        # paused runner from acquiring effects between cancellation preflight
        # and persistence.
        self._terminalizations: dict[str, _ProcessLocalAuthorityRegistration] = {}
        # A public terminalization surface may not hold the creating runner.
        # Keep only local cleanup callbacks alongside the opaque registration,
        # so a durable terminal record can retire the whole local lifecycle.
        self._terminal_finalizers: dict[str, dict[object, Callable[[], object]]] = {}
        self._lock = RLock()

    @staticmethod
    def _valid_session_or_execution_id(value: object, *, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"process-local authority requires a {label}")
        if label == "session id" and _SAFE_PROCESS_LOCAL_SESSION_ID.fullmatch(value) is None:
            raise ValueError("process-local authority requires a canonical safe session id")
        return value

    def mint(self) -> _ProcessLocalAuthorityGeneration:
        """Mint an unforgeable-in-this-registry generation for one new run."""
        generation = _ProcessLocalAuthorityGeneration(
            _PROCESS_LOCAL_AUTHORITY_CONSTRUCTION_TOKEN,
            uuid.uuid4().hex,
        )
        issuance = _ProcessLocalAuthorityIssuance(
            generation=generation,
            correlation_id=generation.correlation_id,
            creator_pid=os.getpid(),
        )
        with self._lock:
            self._issued[id(generation)] = issuance
        return generation

    def _is_issued_here(self, generation: object) -> bool:
        if not isinstance(generation, _ProcessLocalAuthorityGeneration):
            return False
        issuance = self._issued.get(id(generation))
        return (
            issuance is not None
            and issuance.generation is generation
            and issuance.correlation_id == generation.correlation_id
            and issuance.creator_pid == os.getpid()
        )

    def contract(self, generation: _ProcessLocalAuthorityGeneration) -> dict[str, object]:
        """Return persisted correlation data for an issued live generation."""
        with self._lock:
            if not self._is_issued_here(generation):
                raise ValueError("process-local authority generation is not live in this process")
            return {
                "version": 1,
                "scope": "process_local",
                "correlation_id": generation.correlation_id,
            }

    def register(
        self,
        session_id: str,
        execution_id: str,
        generation: _ProcessLocalAuthorityGeneration,
        adapter: object,
    ) -> None:
        """Bind an issued generation to exactly one session and adapter."""
        session_id = self._valid_session_or_execution_id(session_id, label="session id")
        execution_id = self._valid_session_or_execution_id(execution_id, label="execution id")
        with self._lock:
            if not self._is_issued_here(generation):
                raise ValueError("process-local authority requires a registry-issued generation")
            current = self._registrations.get(session_id)
            registration = _ProcessLocalAuthorityRegistration(
                execution_id=execution_id,
                generation=generation,
                adapter=adapter,
                creator_pid=os.getpid(),
            )
            if current is not None and not (
                current.execution_id == registration.execution_id
                and current.generation is registration.generation
                and current.adapter is registration.adapter
                and current.creator_pid == registration.creator_pid
            ):
                raise ValueError("process-local authority session is already registered")
            self._registrations[session_id] = registration
            self._terminal_finalizers.setdefault(session_id, {})

    def begin_terminalization(
        self,
        session_id: str,
        execution_id: str,
        value: object,
    ) -> tuple[bool, bool]:
        """Reserve an unclaimed owner while another surface writes terminal state.

        The first result means this caller owns the terminalization reservation.
        The second means an effectful claim or another terminalization is already
        live, so the caller must signal it rather than write terminal state
        underneath it.
        """
        if not valid_process_local_authority_contract(value):
            return False, False
        with self._lock:
            entry = self._registrations.get(session_id)
            if (
                entry is None
                or entry.creator_pid != os.getpid()
                or entry.execution_id != execution_id
                or not self._is_issued_here(entry.generation)
                or entry.generation.correlation_id != value.get("correlation_id")
            ):
                return False, False
            if (
                self._claims.get(session_id) is not None
                or self._terminalizations.get(session_id) is not None
            ):
                return False, True
            self._terminalizations[session_id] = entry
            return True, False

    def abort_terminalization(
        self,
        session_id: str,
        execution_id: str,
        value: object,
    ) -> None:
        """Release this caller's terminalization reservation after write failure."""
        if not valid_process_local_authority_contract(value):
            return
        with self._lock:
            entry = self._registrations.get(session_id)
            reservation = self._terminalizations.get(session_id)
            if (
                entry is not None
                and reservation is entry
                and entry.creator_pid == os.getpid()
                and entry.execution_id == execution_id
                and self._is_issued_here(entry.generation)
                and entry.generation.correlation_id == value.get("correlation_id")
            ):
                self._terminalizations.pop(session_id, None)

    def add_terminal_finalizer(
        self,
        session_id: str,
        execution_id: str,
        value: object,
        adapter: object,
        finalizer_key: object,
        finalizer: Callable[[], object],
    ) -> bool:
        """Attach idempotent local cleanup to one exact live registration."""
        if not valid_process_local_authority_contract(value):
            return False
        with self._lock:
            entry = self._registrations.get(session_id)
            if (
                entry is None
                or entry.creator_pid != os.getpid()
                or entry.execution_id != execution_id
                or entry.adapter is not adapter
                or not self._is_issued_here(entry.generation)
                or entry.generation.correlation_id != value.get("correlation_id")
            ):
                return False
            self._terminal_finalizers.setdefault(session_id, {})[finalizer_key] = finalizer
            return True

    def live_generation(
        self,
        session_id: str,
        execution_id: str,
        value: object,
        adapter: object,
    ) -> _ProcessLocalAuthorityGeneration | None:
        """Return the exact live generation only for this creating process."""
        if not valid_process_local_authority_contract(value):
            return None
        with self._lock:
            entry = self._registrations.get(session_id)
            if (
                entry is None
                or entry.creator_pid != os.getpid()
                or entry.execution_id != execution_id
                or entry.adapter is not adapter
                or not self._is_issued_here(entry.generation)
                or entry.generation.correlation_id != value.get("correlation_id")
            ):
                return None
            return entry.generation

    def has_live_registration(
        self,
        session_id: str,
        execution_id: str,
        value: object,
    ) -> bool:
        """Report a live local binding without making it transferable.

        A different adapter in the same PID must not receive the capability,
        but it also must not mistake the original adapter's live binding for a
        crashed process and terminalize the persisted session.
        """
        if not valid_process_local_authority_contract(value):
            return False
        with self._lock:
            entry = self._registrations.get(session_id)
            return (
                entry is not None
                and entry.creator_pid == os.getpid()
                and entry.execution_id == execution_id
                and self._is_issued_here(entry.generation)
                and entry.generation.correlation_id == value.get("correlation_id")
            )

    def claim(
        self,
        session_id: str,
        execution_id: str,
        value: object,
        adapter: object,
    ) -> tuple[_ProcessLocalAuthorityGeneration | None, bool]:
        """Atomically claim one live session for effectful execution.

        The bool reports an already-live authority that is currently claimed.
        It is intentionally distinct from a missing capability: a concurrent
        caller must not terminally invalidate the original active session.
        """
        if not valid_process_local_authority_contract(value):
            return None, False
        with self._lock:
            entry = self._registrations.get(session_id)
            if (
                entry is None
                or entry.creator_pid != os.getpid()
                or entry.execution_id != execution_id
                or entry.adapter is not adapter
                or not self._is_issued_here(entry.generation)
                or entry.generation.correlation_id != value.get("correlation_id")
            ):
                return None, False
            claim = self._claims.get(session_id)
            if claim is not None or self._terminalizations.get(session_id) is not None:
                return None, True
            self._claims[session_id] = entry
            return entry.generation, False

    def release(self, session_id: str, execution_id: str, adapter: object) -> None:
        """Release a paused session's exclusive execution claim."""
        with self._lock:
            claim = self._claims.get(session_id)
            if (
                claim is not None
                and claim.creator_pid == os.getpid()
                and claim.execution_id == execution_id
                and claim.adapter is adapter
            ):
                self._claims.pop(session_id, None)

    def is_claimed(self, session_id: str, execution_id: str, value: object) -> bool:
        """Report whether the exact durable correlation currently owns effects."""
        if not valid_process_local_authority_contract(value):
            return False
        with self._lock:
            entry = self._registrations.get(session_id)
            claim = self._claims.get(session_id)
            return (
                entry is not None
                and claim is entry
                and entry.creator_pid == os.getpid()
                and entry.execution_id == execution_id
                and self._is_issued_here(entry.generation)
                and entry.generation.correlation_id == value.get("correlation_id")
            )

    def retire(self, session_id: str, execution_id: str, adapter: object) -> bool:
        """Retire an exact session binding and report whether this owner did it."""
        with self._lock:
            entry = self._registrations.get(session_id)
            if (
                entry is None
                or entry.creator_pid != os.getpid()
                or entry.execution_id != execution_id
                or entry.adapter is not adapter
            ):
                return False
            self._registrations.pop(session_id, None)
            self._claims.pop(session_id, None)
            self._terminalizations.pop(session_id, None)
            self._terminal_finalizers.pop(session_id, None)
            issuance = self._issued.get(id(entry.generation))
            if issuance is not None and issuance.generation is entry.generation:
                self._issued.pop(id(entry.generation), None)
            return True

    def retire_after_terminal_persistence(
        self,
        session_id: str,
        execution_id: str,
        value: object,
    ) -> tuple[bool, bool, tuple[Callable[[], object], ...]]:
        """Retire an unclaimed binding after a durable terminal transition.

        The first boolean reports whether this call retired the exact binding;
        the second reports an active effectful claim.  Callers must signal an
        active worker rather than terminalizing underneath it.
        """
        if not valid_process_local_authority_contract(value):
            return False, False, ()
        with self._lock:
            entry = self._registrations.get(session_id)
            if (
                entry is None
                or entry.creator_pid != os.getpid()
                or entry.execution_id != execution_id
                or not self._is_issued_here(entry.generation)
                or entry.generation.correlation_id != value.get("correlation_id")
            ):
                return False, False, ()
            if self._claims.get(session_id) is not None:
                return False, True, ()
            self._registrations.pop(session_id, None)
            self._terminalizations.pop(session_id, None)
            finalizers = tuple(self._terminal_finalizers.pop(session_id, {}).values())
            issuance = self._issued.get(id(entry.generation))
            if issuance is not None and issuance.generation is entry.generation:
                self._issued.pop(id(entry.generation), None)
            return True, False, finalizers

    def discard(self, generation: _ProcessLocalAuthorityGeneration) -> None:
        """Discard an unregistered generation after failed session preparation."""
        with self._lock:
            issuance = self._issued.get(id(generation))
            if issuance is not None and issuance.generation is generation:
                if not any(item.generation is generation for item in self._registrations.values()):
                    self._issued.pop(id(generation), None)

    def clear_after_fork(self) -> None:
        """Do not let a fork inherit the parent process's capabilities."""
        # This hook runs in a post-fork child.  A parent thread may have held
        # the inherited lock at fork time, so acquiring it here can deadlock
        # forever because that owner no longer exists in the child.
        self._issued = {}
        self._registrations = {}
        self._claims = {}
        self._terminalizations = {}
        self._terminal_finalizers = {}
        self._lock = RLock()


_PROCESS_LOCAL_AUTHORITY_REGISTRY = _ProcessLocalAuthorityRegistry()
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_PROCESS_LOCAL_AUTHORITY_REGISTRY.clear_after_fork)


def _mint_process_local_authority_generation() -> _ProcessLocalAuthorityGeneration:
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.mint()


def _process_local_authority_contract(
    generation: _ProcessLocalAuthorityGeneration,
) -> dict[str, object]:
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.contract(generation)


def valid_process_local_authority_contract(value: object) -> bool:
    """Return whether a persisted process-local correlation record is canonical."""
    return (
        isinstance(value, Mapping)
        and set(value) == {"version", "scope", "correlation_id"}
        and isinstance(value.get("version"), int)
        and not isinstance(value.get("version"), bool)
        and value.get("version") == 1
        and value.get("scope") == "process_local"
        and isinstance(value.get("correlation_id"), str)
        and len(value["correlation_id"]) == 32
        and all(character in "0123456789abcdef" for character in value["correlation_id"])
    )


def _register_process_local_authority_generation(
    session_id: str,
    execution_id: str,
    generation: _ProcessLocalAuthorityGeneration,
    adapter: object,
) -> None:
    _PROCESS_LOCAL_AUTHORITY_REGISTRY.register(session_id, execution_id, generation, adapter)


def _live_process_local_authority_generation(
    session_id: str,
    execution_id: str,
    value: object,
    adapter: object,
) -> _ProcessLocalAuthorityGeneration | None:
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.live_generation(
        session_id,
        execution_id,
        value,
        adapter,
    )


def _has_live_process_local_authority_registration(
    session_id: str,
    execution_id: str,
    value: object,
) -> bool:
    """Report an in-process binding without exposing its capability object."""
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.has_live_registration(
        session_id,
        execution_id,
        value,
    )


def _process_local_authority_is_claimed(
    session_id: str,
    execution_id: str,
    value: object,
) -> bool:
    """Return whether this local authority is actively executing effects."""
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.is_claimed(session_id, execution_id, value)


def _begin_process_local_authority_terminalization(
    session_id: str,
    execution_id: str,
    value: object,
) -> tuple[bool, bool]:
    """Reserve a local owner while durable terminalization is in progress."""
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.begin_terminalization(
        session_id,
        execution_id,
        value,
    )


def _abort_process_local_authority_terminalization(
    session_id: str,
    execution_id: str,
    value: object,
) -> None:
    """Release a failed public terminalization reservation."""
    _PROCESS_LOCAL_AUTHORITY_REGISTRY.abort_terminalization(
        session_id,
        execution_id,
        value,
    )


def _register_process_local_authority_terminal_finalizer(
    session_id: str,
    execution_id: str,
    value: object,
    adapter: object,
    finalizer_key: object,
    finalizer: Callable[[], object],
) -> bool:
    """Register local cleanup for a terminal transition observed elsewhere."""
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.add_terminal_finalizer(
        session_id,
        execution_id,
        value,
        adapter,
        finalizer_key,
        finalizer,
    )


def _claim_process_local_authority_generation(
    session_id: str,
    execution_id: str,
    value: object,
    adapter: object,
) -> tuple[_ProcessLocalAuthorityGeneration | None, bool]:
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.claim(
        session_id,
        execution_id,
        value,
        adapter,
    )


def _release_process_local_authority_generation(
    session_id: str,
    execution_id: str,
    adapter: object,
) -> None:
    _PROCESS_LOCAL_AUTHORITY_REGISTRY.release(session_id, execution_id, adapter)


def _retire_process_local_authority_generation(
    session_id: str,
    execution_id: str,
    adapter: object,
) -> bool:
    return _PROCESS_LOCAL_AUTHORITY_REGISTRY.retire(session_id, execution_id, adapter)


async def _retire_process_local_authority_after_terminal_persistence(
    session_id: str,
    execution_id: str,
    value: object,
) -> tuple[bool, bool]:
    """Release an unclaimed local owner after a durable terminal transition.

    A public cancellation handler can call this without receiving the opaque
    generation or adapter.  It succeeds only for the exact correlation and
    deliberately refuses to race an active effectful claim.
    """
    retired, claimed, finalizers = (
        _PROCESS_LOCAL_AUTHORITY_REGISTRY.retire_after_terminal_persistence(
            session_id,
            execution_id,
            value,
        )
    )
    if not retired:
        return False, claimed

    cancellation: asyncio.CancelledError | None = None
    try:
        for finalizer in finalizers:
            try:
                result = finalizer()
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(result)
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError as exc:
                            # A terminal transition has already invalidated the
                            # registry entry.  Finish every registered cleanup
                            # callback before propagating cancellation so a
                            # cancelled caller cannot leak a retained store.
                            cancellation = cancellation or exc
                    try:
                        task.result()
                    except asyncio.CancelledError as exc:
                        cancellation = cancellation or exc
                        _LOG.warning(
                            "process_local_authority.terminal_finalizer_cancelled",
                            extra={"session_id": session_id, "execution_id": execution_id},
                        )
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                _LOG.warning(
                    "process_local_authority.terminal_finalizer_cancelled",
                    extra={"session_id": session_id, "execution_id": execution_id},
                )
            except Exception:
                _LOG.exception(
                    "process_local_authority.terminal_finalizer_failed",
                    extra={"session_id": session_id, "execution_id": execution_id},
                )
    finally:
        from ouroboros.orchestrator.heartbeat import release_if_owned_by_current_process

        release_if_owned_by_current_process(session_id)
    if cancellation is not None:
        raise cancellation
    return True, False


def _discard_process_local_authority_generation(
    generation: _ProcessLocalAuthorityGeneration,
) -> None:
    _PROCESS_LOCAL_AUTHORITY_REGISTRY.discard(generation)


def _canonical_json(value: object, *, field: str) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not canonical JSON") from exc


def _canonical_object(value: object, *, field: str) -> dict[str, Any]:
    normalized = json.loads(_canonical_json(value, field=field))
    if not isinstance(normalized, dict):
        raise ValueError(f"{field} is not an object")
    return normalized


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_identity_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _is_sensitive_identity_key(value: str) -> bool:
    """Return whether an explicit identity key can carry a credential value."""
    compact = _normalized_identity_key(value)
    return (
        any(
            marker in compact
            for marker in (
                "apikey",
                "credential",
                "password",
                "authorization",
                "bearer",
                "privatekey",
                "clientsecret",
                "secretkey",
                "accesskey",
                "authkey",
                "signingkey",
                "encryptionkey",
                "accountkey",
                "masterkey",
                "connectionstring",
                "accesstoken",
                "refreshtoken",
                "sessiontoken",
            )
        )
        or compact.startswith("secret")
        or compact.endswith(("token", "tokenvalue", "keyvalue", "secret", "secretvalue"))
    )


def _looks_like_credential(value: str) -> bool:
    """Recognize opaque credential shapes without redacting ordinary prose."""
    normalized = value.strip()
    lowered = normalized.lower()
    if normalized.startswith("AIza") and len(normalized) >= 35:
        return True
    if lowered.startswith(
        (
            "sk-",
            "sk_live_",
            "sk_test_",
            "ghp_",
            "github_pat_",
            "rk_live_",
            "rk_test_",
            "xoxb-",
            "xoxp-",
        )
    ):
        return len(normalized) >= 16
    if lowered.startswith("bearer "):
        return len(normalized.split(maxsplit=1)[-1]) >= 16
    return False


def _project_explicit_identity(
    value: object,
    *,
    field: str,
    depth: int = 0,
    seen: set[int] | None = None,
) -> object:
    """Accept only bounded JSON data that is safe to digest.

    This validates an *explicit descriptor*, not object implementation state.
    Unsupported values make the corresponding component process-local.
    """
    if depth > _MAX_IDENTITY_DEPTH:
        raise ValueError(f"{field} exceeds identity depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} contains a non-finite float")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_IDENTITY_SCALAR_CHARS:
            raise ValueError(f"{field} contains oversized text")
        if _looks_like_credential(value):
            raise ValueError(f"{field} contains credential-shaped text")
        return value

    seen = set() if seen is None else seen
    value_id = id(value)
    if value_id in seen:
        raise ValueError(f"{field} contains cyclic state")
    seen.add(value_id)
    try:
        if isinstance(value, Mapping):
            if len(value) > _MAX_IDENTITY_ITEMS:
                raise ValueError(f"{field} contains too many mapping items")
            projected: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not key:
                    raise ValueError(f"{field} contains a non-string or empty key")
                if len(key) > _MAX_IDENTITY_SCALAR_CHARS:
                    raise ValueError(f"{field} contains an oversized key")
                if _is_sensitive_identity_key(key):
                    raise ValueError(f"{field} contains a credential-bearing key")
                projected[key] = _project_explicit_identity(
                    item,
                    field=f"{field}.{key}",
                    depth=depth + 1,
                    seen=seen,
                )
            return projected
        if isinstance(value, (list, tuple)):
            if len(value) > _MAX_IDENTITY_ITEMS:
                raise ValueError(f"{field} contains too many sequence items")
            return [
                _project_explicit_identity(
                    item,
                    field=f"{field}[{index}]",
                    depth=depth + 1,
                    seen=seen,
                )
                for index, item in enumerate(value)
            ]
        raise ValueError(f"{field} is not canonical JSON data")
    finally:
        seen.remove(value_id)


def _safe_identity_digest(value: object, *, field: str) -> str | None:
    try:
        projected = _project_explicit_identity(value, field=field)
        encoded = _canonical_json(projected, field=field)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if len(encoded) > _MAX_IDENTITY_JSON_CHARS:
        return None
    return _sha256(encoded)


def _digest_descriptor(value: object, *, field: str) -> dict[str, object]:
    digest = _safe_identity_digest(value, field=field)
    if digest is None:
        return {"observed": False}
    return {"observed": True, "digest": digest}


def _valid_digest_descriptor(value: object) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("observed"), bool):
        return False
    if value.get("observed") is False:
        return set(value) == {"observed"}
    digest = value.get("digest")
    return (
        set(value) == {"observed", "digest"}
        and isinstance(digest, str)
        and digest.startswith("sha256:")
        and len(digest) == 71
    )


def execution_authority_boundary_contract() -> dict[str, object]:
    """Return the finite ownership matrix embedded in every baseline."""
    return {
        "version": EXECUTION_AUTHORITY_BOUNDARY_VERSION,
        "portable": [
            "executor_components",
            "workspace_descriptor",
            "static_execution_policy",
            "built_in_verifier",
        ],
        "per_attempt": [
            "ac",
            "prompt",
            "tool_catalog",
            "selected_route",
            "selected_effort",
            "runtime_handle",
            "checkpoint",
            "session_state",
        ],
        "process_local": [
            "legacy_runtime_descriptor",
        ],
        "volatile": [
            "custom_callable_graph",
            "event_store",
            "event_emitter",
            "cache",
            "queue",
            "lock",
            "signal_hub",
            "module_monkeypatch",
        ],
    }


def canonical_workspace_authority(workspace: str | None) -> dict[str, object]:
    """Return a digest-only identity for the effect-owning workspace."""
    if not isinstance(workspace, str) or not workspace.strip():
        return {"version": 1, "stability": "process_local", "observed": False}
    try:
        canonical = str(Path(workspace).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return {"version": 1, "stability": "process_local", "observed": False}
    descriptor = _digest_descriptor(canonical, field="workspace identity")
    if descriptor["observed"] is not True:
        return {"version": 1, "stability": "process_local", "observed": False}
    return {
        "version": 1,
        "stability": "durable",
        "observed": True,
        "identity_digest": descriptor["digest"],
    }


def _valid_workspace_authority(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return False
    observed = value.get("observed")
    stability = value.get("stability")
    if observed is False:
        return stability == "process_local" and set(value) == {"version", "stability", "observed"}
    digest = value.get("identity_digest")
    return (
        observed is True
        and stability == "durable"
        and set(value) == {"version", "stability", "observed", "identity_digest"}
        and isinstance(digest, str)
        and digest.startswith("sha256:")
        and len(digest) == 71
    )


def constructor_model_contract(adapter: object) -> dict[str, object]:
    """Return the normalized constructor-level model pin, when observable."""
    try:
        raw_model = inspect.getattr_static(adapter, "_model")
    except AttributeError:
        return {"observed": False}
    if raw_model is None:
        return {"observed": True, "model": None}
    if not isinstance(raw_model, str):
        return {"observed": False}

    normalized_model: object = raw_model.strip() or None
    normalizer_descriptor = inspect.getattr_static(type(adapter), "_normalize_model", None)
    if normalizer_descriptor is not None:
        try:
            normalizer = object.__getattribute__(adapter, "_normalize_model")
            normalized_model = normalizer(raw_model)
        except Exception:
            return {"observed": False}
    if normalized_model is None:
        return {"observed": True, "model": None}
    if not isinstance(normalized_model, str) or not normalized_model.strip():
        return {"observed": False}
    return {"observed": True, "model": normalized_model.strip()}


def valid_constructor_model_contract(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("observed") is not True:
        return False
    model = value.get("model")
    return set(value) == {"observed", "model"} and (
        model is None or isinstance(model, str) and bool(model.strip())
    )


def runtime_execution_identity_contract(adapter: object) -> dict[str, object]:
    """Return the adapter's explicit execution identity for runner resume."""
    provider_descriptor = inspect.getattr_static(type(adapter), "execution_identity_contract", None)
    if provider_descriptor is None:
        return {"version": 1, "observed": False}

    provider = object.__getattribute__(adapter, "execution_identity_contract")
    identity = provider()
    if not isinstance(identity, Mapping):
        raise ValueError("runtime execution identity contract is not a mapping")
    normalized = _canonical_object(
        dict(identity),
        field="runtime execution identity contract",
    )
    # An empty object carries no execution identity. Treat it like a missing
    # provider declaration instead of allowing a digest of ``{}`` to witness
    # portability.
    if not normalized:
        return {"version": 1, "observed": False}
    return {"version": 1, "observed": True, "identity": normalized}


def valid_runtime_execution_identity_contract(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    version = value.get("version")
    observed = value.get("observed")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != 1
        or not isinstance(observed, bool)
    ):
        return False
    if not observed:
        return set(value) == {"version", "observed"}
    identity = value.get("identity")
    if set(value) != {"version", "observed", "identity"} or not isinstance(identity, Mapping):
        return False
    try:
        _canonical_json(dict(identity), field="runtime execution identity contract")
    except ValueError:
        return False
    return bool(identity)


def runtime_execution_proves_effective_model(value: object) -> bool:
    if not valid_runtime_execution_identity_contract(value):
        return False
    if not isinstance(value, Mapping) or value.get("observed") is not True:
        return False
    identity = value.get("identity")
    return isinstance(identity, Mapping) and identity.get("effective_model_observed") is True


def _runtime_capabilities_descriptor(adapter: object) -> dict[str, object]:
    try:
        capabilities = runtime_capabilities_for(adapter)
        value = {
            "skill_dispatch": capabilities.skill_dispatch,
            "targeted_resume": capabilities.targeted_resume,
            "structured_output": capabilities.structured_output,
            "system_prompt_support": capabilities.system_prompt_support.value,
            "tool_restriction_support": capabilities.tool_restriction_support.value,
            "permission_mode_support": capabilities.permission_mode_support.value,
            "reasoning_effort_support": capabilities.reasoning_effort_support.value,
            "enforceable_reasoning_efforts": (
                sorted(capabilities.enforceable_reasoning_efforts)
                if capabilities.enforceable_reasoning_efforts is not None
                else None
            ),
            "model_override_support": capabilities.model_override_support.value,
            "subagent_orchestration": capabilities.subagent_orchestration.value,
            "session_signals": capabilities.session_signals.to_event_data(),
        }
    except Exception:
        return {"observed": False}
    return _digest_descriptor(value, field="runtime capabilities")


def _runtime_label_descriptor(adapter: object, name: str) -> dict[str, object]:
    try:
        value = object.__getattribute__(adapter, name)
    except (AttributeError, TypeError):
        return {"observed": False}
    if not isinstance(value, str) or not value.strip():
        return {"observed": False}
    return _digest_descriptor(value.strip(), field=f"runtime {name}")


def _runtime_implementation_descriptor(adapter: object) -> dict[str, object]:
    """Return a reviewed identity for one exact built-in runtime type."""
    runtime_type = type(adapter)
    implementation = _CLOSED_RUNTIME_IMPLEMENTATIONS.get(runtime_type)
    if implementation is None:
        return {"observed": False}
    version, expected_dispatch_root, expected_dispatch_code = implementation
    dispatch_root = _static_callable_root(adapter, "execute_task")
    if (
        dispatch_root is not expected_dispatch_root
        or _callable_code_identity(dispatch_root) is not expected_dispatch_code
    ):
        return {"observed": False}
    return _digest_descriptor(
        {
            "type": f"{runtime_type.__module__}.{runtime_type.__qualname__}",
            "version": version,
        },
        field="runtime implementation",
    )


def _runtime_executable_descriptor(adapter: object) -> dict[str, object]:
    """Digest one bounded, resolved CLI executable without serializing its path."""
    try:
        configured_path = object.__getattribute__(adapter, "_cli_path")
    except (AttributeError, TypeError):
        return {"observed": False}
    if not isinstance(configured_path, str) or not configured_path.strip():
        return {"observed": False}

    try:
        candidate = Path(configured_path).expanduser()
        if not candidate.is_absolute():
            resolved_from_path = shutil.which(configured_path)
            if resolved_from_path is None:
                return {"observed": False}
            candidate = Path(resolved_from_path)
        executable_path = candidate.resolve(strict=True)
        executable_stat = executable_path.stat()
        executable_mode = stat.S_IMODE(executable_stat.st_mode)
        if (
            not stat.S_ISREG(executable_stat.st_mode)
            or not executable_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            or executable_stat.st_size > _MAX_RUNTIME_EXECUTABLE_BYTES
        ):
            return {"observed": False}
        digest = hashlib.sha256()
        with executable_path.open("rb") as executable_file:
            while chunk := executable_file.read(1024 * 1024):
                digest.update(chunk)
    except (OSError, ValueError):
        return {"observed": False}

    return _digest_descriptor(
        {
            "path": str(executable_path),
            "generation": {
                "device": executable_stat.st_dev,
                "inode": executable_stat.st_ino,
                "size": executable_stat.st_size,
                "mtime_ns": executable_stat.st_mtime_ns,
                "mode": executable_mode,
            },
            "content_digest": "sha256:" + digest.hexdigest(),
        },
        field="runtime executable",
    )


def _runtime_configuration_descriptor(adapter: object) -> dict[str, object]:
    """Digest the finite mutable execution settings of the CLI runtime family."""
    runtime_type = type(adapter)
    if runtime_type is ZcodeCLIRuntime:
        # Zcode can launch an app-bundle Electron executable or a PATH-resolved
        # Node executable before its configured script. That external launcher
        # chain is not a finite portable descriptor in Foundation A.
        return {"observed": False}

    try:
        skills_dir = object.__getattribute__(adapter, "_skills_dir")
        skill_dispatcher = object.__getattribute__(adapter, "_skill_dispatcher")
        if skills_dir is not None or skill_dispatcher is not None:
            # User-provided skill directories and dispatchers are live behavior,
            # not a closed portable component in Foundation A.
            return {"observed": False}
        cwd = object.__getattribute__(adapter, "_cwd")
        startup_timeout = object.__getattribute__(adapter, "_startup_output_timeout_seconds")
        stdout_timeout = object.__getattribute__(adapter, "_stdout_idle_timeout_seconds")
        process_shutdown_timeout = object.__getattribute__(
            adapter,
            "_process_shutdown_timeout_seconds",
        )
        completed_shutdown_timeout = object.__getattribute__(
            adapter,
            "_completed_process_group_shutdown_timeout_seconds",
        )
        max_resume_retries = object.__getattribute__(adapter, "_max_resume_retries")
        max_depth = object.__getattribute__(adapter, "_max_ouroboros_depth")
        max_stderr_lines = object.__getattribute__(adapter, "_max_stderr_lines")
        use_process_group = object.__getattribute__(adapter, "_use_process_group")
        child_session_env_keys = object.__getattribute__(adapter, "_child_session_env_keys")
        copilot_runtime_profile = (
            object.__getattribute__(adapter, "_runtime_profile")
            if runtime_type is CopilotCliRuntime
            else None
        )
        copilot_agent = (
            object.__getattribute__(adapter, "_copilot_agent")
            if runtime_type is CopilotCliRuntime
            else None
        )
    except (AttributeError, TypeError):
        return {"observed": False}

    timeouts = (
        startup_timeout,
        stdout_timeout,
        process_shutdown_timeout,
        completed_shutdown_timeout,
    )
    if (
        not isinstance(cwd, str)
        or not cwd.strip()
        or not all(value is None or _is_finite_number(value) for value in timeouts)
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (max_resume_retries, max_depth, max_stderr_lines)
        )
        or not isinstance(use_process_group, bool)
        or not isinstance(child_session_env_keys, (tuple, list, frozenset))
        or not all(isinstance(value, str) for value in child_session_env_keys)
        or (
            runtime_type is CopilotCliRuntime
            and (
                copilot_runtime_profile is not None
                and (
                    not isinstance(copilot_runtime_profile, str)
                    or not copilot_runtime_profile.strip()
                )
                or copilot_agent is not None
                and (not isinstance(copilot_agent, str) or not copilot_agent.strip())
            )
        )
    ):
        return {"observed": False}
    try:
        canonical_cwd = str(Path(cwd).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return {"observed": False}
    configuration: dict[str, object] = {
        "working_directory": canonical_cwd,
        "startup_output_timeout_seconds": startup_timeout,
        "stdout_idle_timeout_seconds": stdout_timeout,
        "process_shutdown_timeout_seconds": process_shutdown_timeout,
        "completed_process_group_shutdown_timeout_seconds": completed_shutdown_timeout,
        "max_resume_retries": max_resume_retries,
        "max_ouroboros_depth": max_depth,
        "max_stderr_lines": max_stderr_lines,
        "use_process_group": use_process_group,
        "child_session_env_keys": sorted(child_session_env_keys),
    }
    if runtime_type is CopilotCliRuntime:
        # Copilot translates the resolved runtime profile to an --agent flag.
        # Bind the exact stored value that command construction consumes.
        configuration["copilot"] = {
            "runtime_profile": copilot_runtime_profile,
            "resolved_agent": copilot_agent,
        }
    return _digest_descriptor(configuration, field="runtime execution configuration")


def runtime_authority_contract(
    adapter: object,
    *,
    force_process_local: bool = False,
    instance_nonce: str | None = None,
) -> dict[str, object]:
    """Return a process-local descriptor without invoking runtime behavior.

    Current CLI runtimes use dynamic instance helpers, handler caches,
    configuration readers, and launcher chains.  Calling their identity or
    capability providers while constructing an authority would itself re-open
    an unbounded behavior surface.  They remain executable, but a fresh nonce
    confines every capture to one live process until a separate sealed kernel
    supplies a reviewed finite execution path.
    """
    del adapter, force_process_local
    return {
        "version": 5,
        "stability": "process_local",
        "portable_identity_observed": False,
        "runtime_backend": {"observed": False},
        "llm_backend": {"observed": False},
        "permission_mode": {"observed": False},
        "execution_identity": {"observed": False},
        "capabilities": {"observed": False},
        "implementation": {"observed": False},
        "executable": {"observed": False},
        "configuration": {"observed": False},
        "self_governs_rate_limit": None,
        "instance_nonce": instance_nonce or uuid.uuid4().hex,
    }


def _valid_runtime_authority(value: object) -> bool:
    required = {
        "version",
        "stability",
        "runtime_backend",
        "llm_backend",
        "permission_mode",
        "execution_identity",
        "capabilities",
        "implementation",
        "executable",
        "configuration",
        "self_governs_rate_limit",
        "portable_identity_observed",
        "instance_nonce",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("version") != 5:
        return False
    if value.get("stability") not in {"durable", "process_local"}:
        return False
    if not all(
        _valid_digest_descriptor(value.get(name))
        for name in (
            "runtime_backend",
            "llm_backend",
            "permission_mode",
            "execution_identity",
            "capabilities",
            "implementation",
            "executable",
            "configuration",
        )
    ):
        return False
    self_governs_rate_limit = value.get("self_governs_rate_limit")
    portable_identity_observed = value.get("portable_identity_observed")
    if not isinstance(portable_identity_observed, bool):
        return False
    nonce = value.get("instance_nonce")
    return (
        value.get("stability") == "process_local"
        and not portable_identity_observed
        and self_governs_rate_limit is None
        and isinstance(nonce, str)
        and len(nonce) == 32
    )


def execution_policy_authority_contract(policy: Mapping[str, object]) -> dict[str, object]:
    """Represent the explicit static policy by a safe digest only."""
    descriptor = _digest_descriptor(dict(policy), field="execution policy")
    if descriptor["observed"] is not True:
        return {"version": 1, "stability": "process_local", "observed": False}
    return {
        "version": 1,
        "stability": "durable",
        "observed": True,
        "identity_digest": descriptor["digest"],
    }


def _valid_execution_policy_authority(value: object) -> bool:
    return _valid_workspace_authority(value)


def executor_authority_contract() -> dict[str, object]:
    """Return the closed Foundation A implementation component registry."""
    return {
        "version": 1,
        "stability": "durable",
        "components": dict(_EXECUTOR_COMPONENT_VERSIONS),
    }


def verifier_authority_contract(
    verifier: Verifier | None,
    *,
    instance_nonce: str | None = None,
) -> dict[str, object]:
    """Describe a verifier without inspecting arbitrary Python behavior."""
    if verifier is None:
        return {
            "version": 1,
            "mode": "runtime_transcript",
            "stability": "durable",
            "implementation": _BUILTIN_TRANSCRIPT_VERIFIER,
        }
    if (
        verifier is structural_atomic_verifier
        and getattr(verifier, "__code__", None) is _BUILTIN_STRUCTURAL_VERIFIER_CODE
    ):
        return {
            "version": 1,
            "mode": "structural_atomic",
            "stability": "durable",
            "implementation": _BUILTIN_STRUCTURAL_VERIFIER,
        }

    return {
        "version": 1,
        "mode": "custom",
        "stability": "process_local",
        "instance_nonce": instance_nonce or uuid.uuid4().hex,
        # A custom verifier can expose arbitrary dynamic behavior through a
        # descriptor method.  Do not execute that method merely to describe it.
        "configuration": {"observed": False},
    }


def _valid_verifier_authority(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return False
    mode = value.get("mode")
    if mode in {"runtime_transcript", "structural_atomic"}:
        return (
            value.get("stability") == "durable"
            and set(value) == {"version", "mode", "stability", "implementation"}
            and isinstance(value.get("implementation"), str)
        )
    if mode != "custom":
        return False
    nonce = value.get("instance_nonce")
    return (
        value.get("stability") == "process_local"
        and set(value) == {"version", "mode", "stability", "instance_nonce", "configuration"}
        and isinstance(nonce, str)
        and len(nonce) == 32
        and _valid_digest_descriptor(value.get("configuration"))
    )


def _contains_sensitive_authority_data(value: object) -> bool:
    if isinstance(value, str):
        return _looks_like_credential(value)
    if isinstance(value, Mapping):
        return any(
            _is_sensitive_identity_key(key) or _contains_sensitive_authority_data(item)
            for key, item in value.items()
            if isinstance(key, str)
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_authority_data(item) for item in value)
    return False


def _unwrap_static_callable(value: object) -> object | None:
    if isinstance(value, (staticmethod, classmethod)):
        value = value.__func__
    return value if callable(value) else None


def _static_callable_root(value: object | None, member_name: str | None) -> object | None:
    """Return one direct callable root without traversing its behavior graph."""
    if value is None:
        return None
    try:
        if member_name is not None:
            return _unwrap_static_callable(inspect.getattr_static(value, member_name))
        if inspect.isfunction(value) or inspect.isbuiltin(value) or inspect.ismethod(value):
            return value
        return _unwrap_static_callable(inspect.getattr_static(type(value), "__call__"))
    except (AttributeError, TypeError):
        return None


def _class_callable_root(value: object, member_name: str) -> object | None:
    """Return a callable declared on an instance type, never its instance dict."""
    try:
        return _unwrap_static_callable(inspect.getattr_static(type(value), member_name))
    except (AttributeError, TypeError):
        return None


def _callable_code_identity(value: object | None) -> object | None:
    """Return a direct Python-code root without traversing callable state."""
    if not inspect.isfunction(value):
        return None
    try:
        return value.__code__
    except AttributeError:
        return None


def _static_property_getter_root(value: object | None, member_name: str) -> object | None:
    """Return a direct property getter without invoking dynamic lookup."""
    if value is None:
        return None
    try:
        descriptor = inspect.getattr_static(type(value), member_name)
    except (AttributeError, TypeError):
        return None
    if not isinstance(descriptor, property):
        return None
    getter = descriptor.fget
    return getter if callable(getter) else None


def _is_finite_number(value: object) -> bool:
    """Accept the finite scalar knobs owned by the rate-gate algorithm."""
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _has_observable_runtime_dispatch_root(adapter: object) -> bool:
    """Return whether a runtime exposes one direct, non-dynamic dispatch root."""
    try:
        dispatch_root = _static_callable_root(adapter, "execute_task")
        return (
            dispatch_root is not None
            and dispatch_root is _class_callable_root(adapter, "execute_task")
            and _callable_code_identity(dispatch_root) is not None
            and _uses_default_instance_attribute_resolution(adapter)
        )
    except TypeError:
        return False


def _uses_default_instance_attribute_resolution(value: object) -> bool:
    """Return whether instances have the closed default attribute lookup root.

    A static method root alone is not sufficient when a class can redirect an
    instance attribute through ``__getattribute__``. This is intentionally a
    finite check of the direct effect-owner type, not inspection of arbitrary
    instance state or callable graphs.
    """
    target_type = value if isinstance(value, type) else type(value)
    try:
        return target_type.__getattribute__ is object.__getattribute__
    except (AttributeError, TypeError):
        return False


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityContract:
    """Immutable, versioned Foundation A baseline and fingerprint."""

    canonical_json: str

    def __post_init__(self) -> None:
        try:
            decoded = json.loads(self.canonical_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("execution authority contract is invalid JSON") from exc
        data = _canonical_object(decoded, field="execution authority contract")
        required = {
            "version",
            "boundary",
            "executor",
            "workspace",
            "runtime",
            "verifier",
            "execution_policy",
        }
        if data.get("version") != EXECUTION_AUTHORITY_VERSION or set(data) != required:
            raise ValueError("execution authority contract has an invalid shape")
        if data.get("boundary") != execution_authority_boundary_contract():
            raise ValueError("execution authority contract has an invalid boundary")
        if not executor_authority_contract() == data.get("executor"):
            raise ValueError("execution authority contract has an invalid executor")
        if not _valid_workspace_authority(data.get("workspace")):
            raise ValueError("execution authority contract has an invalid workspace")
        if not _valid_runtime_authority(data.get("runtime")):
            raise ValueError("execution authority contract has an invalid runtime")
        if not _valid_verifier_authority(data.get("verifier")):
            raise ValueError("execution authority contract has an invalid verifier")
        if not _valid_execution_policy_authority(data.get("execution_policy")):
            raise ValueError("execution authority contract has an invalid execution policy")
        if _contains_sensitive_authority_data(data):
            raise ValueError("execution authority contract contains sensitive data")
        if _canonical_json(data, field="execution authority contract") != self.canonical_json:
            raise ValueError("execution authority contract is not canonical")

    @classmethod
    def build(
        cls,
        *,
        adapter: object,
        verifier: Verifier | None,
        workspace: str | None,
        execution_policy: Mapping[str, object],
        verifier_instance_nonce: str | None = None,
        runtime_instance_nonce: str | None = None,
        force_runtime_process_local: bool = False,
    ) -> ExecutionAuthorityContract:
        data = {
            "version": EXECUTION_AUTHORITY_VERSION,
            "boundary": execution_authority_boundary_contract(),
            "executor": executor_authority_contract(),
            "workspace": canonical_workspace_authority(workspace),
            "runtime": runtime_authority_contract(
                adapter,
                force_process_local=force_runtime_process_local,
                instance_nonce=runtime_instance_nonce,
            ),
            "verifier": verifier_authority_contract(
                verifier,
                instance_nonce=verifier_instance_nonce,
            ),
            "execution_policy": execution_policy_authority_contract(execution_policy),
        }
        return cls(_canonical_json(data, field="execution authority contract"))

    @property
    def fingerprint(self) -> str:
        return _sha256(self.canonical_json)

    @property
    def data(self) -> dict[str, Any]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise ValueError("execution authority contract is not an object")
        return value

    @property
    def portable_across_processes(self) -> bool:
        """Return only an identity-stability property, never reuse authority."""
        data = self.data
        runtime = data.get("runtime")
        return (
            all(
                isinstance(component, Mapping) and component.get("stability") == "durable"
                for component in (
                    data.get("executor"),
                    data.get("workspace"),
                    data.get("runtime"),
                    data.get("verifier"),
                    data.get("execution_policy"),
                )
            )
            and isinstance(runtime, Mapping)
            and runtime.get("portable_identity_observed") is True
            and data.get("workspace", {}).get("observed") is True
            and data.get("execution_policy", {}).get("observed") is True
        )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ExecutionAuthorityLiveBinding:
    """The finite live roots that must remain identical before an effect."""

    contract: ExecutionAuthorityContract
    executor: object | None
    executor_attribute_resolution_observable: bool
    adapter: object
    runtime_instance_nonce: str | None
    verifier: Verifier | None
    session_signal_hub: object | None
    dispatcher_type: object
    dispatcher: object | None
    dispatcher_executor: object | None
    transcript_verifier: object
    adapter_dispatch_root: object | None
    adapter_dispatch_code: object | None
    adapter_attribute_resolution_observable: bool
    verifier_root: object | None
    verifier_code: object | None
    dispatcher_stream_root: object | None
    dispatcher_stream_code: object | None
    dispatcher_stream_callable: object | None
    dispatcher_attribute_resolution_observable: bool
    dispatcher_binding_observable: bool
    transcript_verifier_root: object | None
    transcript_verifier_code: object | None
    coordinator: object | None
    coordinator_review_callable: object | None
    coordinator_review_root: object | None
    coordinator_review_code: object | None
    coordinator_adapter: object | None
    coordinator_task_cwd: object | None
    coordinator_reasoning_effort: object | None
    coordinator_attribute_resolution_observable: bool
    coordinator_binding_observable: bool
    rate_gate: object
    rate_gate_acquire_root: object | None
    rate_gate_acquire_code: object | None
    rate_gate_acquire_callable: object | None
    rate_gate_attribute_resolution_observable: bool
    rate_gate_max_wait_seconds: object | None
    rate_gate_heartbeat_seconds: object | None
    rate_gate_sleep: object | None
    rate_gate_sleep_root: object | None
    rate_gate_sleep_code: object | None
    rate_gate_semantics_observable: bool
    rate_gate_bucket: object | None
    rate_gate_bucket_config: tuple[object, object, object, object] | None
    rate_gate_bucket_attribute_resolution_observable: bool
    rate_gate_bucket_time: object | None
    rate_gate_bucket_time_root: object | None
    rate_gate_bucket_time_code: object | None
    rate_gate_bucket_enabled_root: object | None
    rate_gate_bucket_enabled_code: object | None
    rate_gate_bucket_acquire_root: object | None
    rate_gate_bucket_acquire_code: object | None
    rate_gate_bucket_force_reserve_root: object | None
    rate_gate_bucket_force_reserve_code: object | None
    rate_gate_bucket_helper_roots: tuple[tuple[str, object | None, object | None], ...]
    rate_gate_bucket_binding_observable: bool
    verifier_instance_nonce: str | None
    force_runtime_process_local: bool

    @classmethod
    def capture(
        cls,
        *,
        adapter: object,
        verifier: Verifier | None,
        dispatcher_type: object,
        transcript_verifier: object,
        rate_gate: object,
        workspace: str | None,
        execution_policy: Mapping[str, object],
        executor: object | None = None,
        session_signal_hub: object | None = None,
        dispatcher: object | None = None,
        dispatcher_executor: object | None = None,
        dispatcher_stream_callable: object | None = None,
        rate_gate_acquire_callable: object | None = None,
        coordinator: object | None = None,
        coordinator_review_callable: object | None = None,
        expected_dispatcher_type: object | None = None,
        expected_dispatcher_stream_root: object | None = None,
        expected_dispatcher_stream_code: object | None = None,
        expected_transcript_verifier: object | None = None,
        expected_transcript_verifier_code: object | None = None,
        expected_rate_gate_acquire_root: object | None = None,
        expected_rate_gate_acquire_code: object | None = None,
        expected_rate_gate_type: type[object] | None = None,
        expected_rate_gate_sleep: object | None = None,
        expected_rate_gate_sleep_code: object | None = None,
        expected_rate_gate_bucket_type: type[object] | None = None,
        expected_rate_gate_bucket_time: object | None = None,
        expected_rate_gate_bucket_enabled_root: object | None = None,
        expected_rate_gate_bucket_enabled_code: object | None = None,
        expected_rate_gate_bucket_acquire_root: object | None = None,
        expected_rate_gate_bucket_acquire_code: object | None = None,
        expected_rate_gate_bucket_force_reserve_root: object | None = None,
        expected_rate_gate_bucket_force_reserve_code: object | None = None,
        expected_rate_gate_bucket_helper_roots: (
            tuple[tuple[str, object | None, object | None], ...] | None
        ) = None,
        expected_coordinator_type: type[object] | None = None,
        expected_coordinator_review_root: object | None = None,
        expected_coordinator_review_code: object | None = None,
        force_runtime_process_local: bool = False,
    ) -> ExecutionAuthorityLiveBinding:
        executor_attribute_resolution_observable = (
            executor is None or _uses_default_instance_attribute_resolution(executor)
        )
        adapter_dispatch_root = _static_callable_root(adapter, "execute_task")
        adapter_dispatch_code = _callable_code_identity(adapter_dispatch_root)
        adapter_attribute_resolution_observable = _uses_default_instance_attribute_resolution(
            adapter
        )
        verifier_root = _static_callable_root(verifier, None)
        verifier_code = _callable_code_identity(verifier_root)
        dispatcher_stream_root = _static_callable_root(dispatcher_type, "stream")
        dispatcher_stream_code = _callable_code_identity(dispatcher_stream_root)
        dispatcher_attribute_resolution_observable = _uses_default_instance_attribute_resolution(
            dispatcher_type
        )
        captured_dispatcher_executor: object | None = None
        dispatcher_binding_observable = dispatcher is None
        if dispatcher is not None:
            try:
                captured_dispatcher_executor = object.__getattribute__(dispatcher, "_executor")
                dispatcher_binding_observable = (
                    type(dispatcher) is dispatcher_type
                    and captured_dispatcher_executor is dispatcher_executor
                )
            except (AttributeError, TypeError):
                dispatcher_binding_observable = False
        transcript_verifier_root = _static_callable_root(transcript_verifier, None)
        transcript_verifier_code = _callable_code_identity(transcript_verifier_root)
        if coordinator_review_callable is None:
            try:
                candidate = coordinator.run_review if coordinator is not None else None
                coordinator_review_callable = candidate if callable(candidate) else None
            except (AttributeError, TypeError):
                coordinator_review_callable = None
        coordinator_review_root = _static_callable_root(coordinator, "run_review")
        coordinator_review_code = _callable_code_identity(coordinator_review_root)
        coordinator_adapter: object | None = None
        coordinator_task_cwd: object | None = None
        coordinator_reasoning_effort: object | None = None
        coordinator_attribute_resolution_observable = (
            coordinator is None or _uses_default_instance_attribute_resolution(coordinator)
        )
        coordinator_binding_observable = coordinator is None
        if coordinator is not None:
            try:
                coordinator_adapter = object.__getattribute__(coordinator, "_adapter")
                coordinator_task_cwd = object.__getattribute__(coordinator, "_task_cwd")
                coordinator_reasoning_effort = object.__getattribute__(
                    coordinator,
                    "_reasoning_effort",
                )
                coordinator_binding_observable = (
                    coordinator_adapter is adapter
                    and (coordinator_task_cwd is None or isinstance(coordinator_task_cwd, str))
                    and (
                        coordinator_reasoning_effort is None
                        or isinstance(coordinator_reasoning_effort, str)
                    )
                )
            except (AttributeError, TypeError):
                coordinator_binding_observable = False
        rate_gate_acquire_root = _static_callable_root(rate_gate, "acquire")
        rate_gate_acquire_code = _callable_code_identity(rate_gate_acquire_root)
        rate_gate_attribute_resolution_observable = _uses_default_instance_attribute_resolution(
            rate_gate
        )
        rate_gate_max_wait_seconds: object | None = None
        rate_gate_heartbeat_seconds: object | None = None
        rate_gate_sleep: object | None = None
        rate_gate_sleep_root: object | None = None
        rate_gate_sleep_code: object | None = None
        rate_gate_semantics_observable = False
        rate_gate_bucket: object | None = None
        rate_gate_bucket_config: tuple[object, object, object, object] | None = None
        rate_gate_bucket_attribute_resolution_observable = False
        rate_gate_bucket_time: object | None = None
        rate_gate_bucket_time_root: object | None = None
        rate_gate_bucket_time_code: object | None = None
        rate_gate_bucket_enabled_root: object | None = None
        rate_gate_bucket_enabled_code: object | None = None
        rate_gate_bucket_acquire_root: object | None = None
        rate_gate_bucket_acquire_code: object | None = None
        rate_gate_bucket_force_reserve_root: object | None = None
        rate_gate_bucket_force_reserve_code: object | None = None
        rate_gate_bucket_helper_roots: tuple[tuple[str, object | None, object | None], ...] = ()
        rate_gate_bucket_binding_observable = False
        try:
            rate_gate_max_wait_seconds = object.__getattribute__(rate_gate, "_max_wait_seconds")
            rate_gate_heartbeat_seconds = object.__getattribute__(rate_gate, "_heartbeat_seconds")
            rate_gate_sleep = object.__getattribute__(rate_gate, "_sleep")
            rate_gate_sleep_root = _static_callable_root(rate_gate_sleep, None)
            rate_gate_sleep_code = _callable_code_identity(rate_gate_sleep_root)
            rate_gate_bucket = object.__getattribute__(rate_gate, "_bucket")
            rate_gate_bucket_config = (
                object.__getattribute__(rate_gate_bucket, "_runtime_backend"),
                object.__getattribute__(rate_gate_bucket, "_request_limit"),
                object.__getattribute__(rate_gate_bucket, "_token_limit"),
                object.__getattribute__(rate_gate_bucket, "_window_seconds"),
            )
            rate_gate_bucket_attribute_resolution_observable = (
                _uses_default_instance_attribute_resolution(rate_gate_bucket)
            )
            rate_gate_bucket_time = object.__getattribute__(rate_gate_bucket, "_time")
            rate_gate_bucket_time_root = _static_callable_root(rate_gate_bucket_time, None)
            rate_gate_bucket_time_code = _callable_code_identity(rate_gate_bucket_time_root)
            rate_gate_bucket_enabled_root = _static_property_getter_root(
                rate_gate_bucket,
                "enabled",
            )
            rate_gate_bucket_enabled_code = _callable_code_identity(rate_gate_bucket_enabled_root)
            rate_gate_bucket_acquire_root = _static_callable_root(rate_gate_bucket, "acquire")
            rate_gate_bucket_acquire_code = _callable_code_identity(rate_gate_bucket_acquire_root)
            rate_gate_bucket_force_reserve_root = _static_callable_root(
                rate_gate_bucket,
                "force_reserve",
            )
            rate_gate_bucket_force_reserve_code = _callable_code_identity(
                rate_gate_bucket_force_reserve_root
            )
            bucket_helper_roots: list[tuple[str, object | None, object | None]] = []
            for name in _RATE_GATE_BUCKET_HELPER_NAMES:
                helper_root = _static_callable_root(rate_gate_bucket, name)
                bucket_helper_roots.append(
                    (name, helper_root, _callable_code_identity(helper_root))
                )
            rate_gate_bucket_helper_roots = tuple(bucket_helper_roots)
            rate_gate_bucket_binding_observable = (
                isinstance(rate_gate_bucket_config[0], str)
                and (
                    rate_gate_bucket_config[1] is None
                    or isinstance(rate_gate_bucket_config[1], int)
                    and not isinstance(rate_gate_bucket_config[1], bool)
                )
                and (
                    rate_gate_bucket_config[2] is None
                    or isinstance(rate_gate_bucket_config[2], int)
                    and not isinstance(rate_gate_bucket_config[2], bool)
                )
                and _is_finite_number(rate_gate_bucket_config[3])
            )
            rate_gate_semantics_observable = (
                _is_finite_number(rate_gate_max_wait_seconds)
                and _is_finite_number(rate_gate_heartbeat_seconds)
                and rate_gate_sleep_root is not None
                and rate_gate_sleep_code is not None
                and rate_gate_bucket_attribute_resolution_observable
                and rate_gate_bucket_time_root is not None
                and rate_gate_bucket_enabled_root is not None
                and rate_gate_bucket_enabled_code is not None
                and rate_gate_bucket_acquire_root is not None
                and rate_gate_bucket_acquire_code is not None
                and rate_gate_bucket_force_reserve_root is not None
                and rate_gate_bucket_force_reserve_code is not None
                and all(
                    helper_root is not None and helper_code is not None
                    for _, helper_root, helper_code in rate_gate_bucket_helper_roots
                )
            )
        except (AttributeError, TypeError):
            rate_gate_bucket = None
            rate_gate_bucket_config = None
        dispatcher_is_closed = (
            expected_dispatcher_type is not None
            and dispatcher_type is expected_dispatcher_type
            and expected_dispatcher_stream_root is not None
            and dispatcher_stream_root is expected_dispatcher_stream_root
            and expected_dispatcher_stream_code is not None
            and dispatcher_stream_code is expected_dispatcher_stream_code
            and dispatcher_stream_callable is dispatcher_stream_root
        )
        transcript_is_closed = (
            expected_transcript_verifier is not None
            and transcript_verifier is expected_transcript_verifier
            and transcript_verifier_root is expected_transcript_verifier
            and expected_transcript_verifier_code is not None
            and transcript_verifier_code is expected_transcript_verifier_code
        )
        rate_gate_is_closed = (
            expected_rate_gate_type is not None
            and type(rate_gate) is expected_rate_gate_type
            and expected_rate_gate_acquire_root is not None
            and rate_gate_acquire_root is expected_rate_gate_acquire_root
            and expected_rate_gate_acquire_code is not None
            and rate_gate_acquire_code is expected_rate_gate_acquire_code
            and rate_gate_acquire_callable is rate_gate_acquire_root
            and rate_gate_semantics_observable
            and expected_rate_gate_sleep is not None
            and rate_gate_sleep is expected_rate_gate_sleep
            and expected_rate_gate_sleep_code is not None
            and rate_gate_sleep_code is expected_rate_gate_sleep_code
            and expected_rate_gate_bucket_type is not None
            and type(rate_gate_bucket) is expected_rate_gate_bucket_type
            and expected_rate_gate_bucket_time is not None
            and rate_gate_bucket_time is expected_rate_gate_bucket_time
            and expected_rate_gate_bucket_enabled_root is not None
            and rate_gate_bucket_enabled_root is expected_rate_gate_bucket_enabled_root
            and expected_rate_gate_bucket_enabled_code is not None
            and rate_gate_bucket_enabled_code is expected_rate_gate_bucket_enabled_code
            and expected_rate_gate_bucket_acquire_root is not None
            and rate_gate_bucket_acquire_root is expected_rate_gate_bucket_acquire_root
            and expected_rate_gate_bucket_acquire_code is not None
            and rate_gate_bucket_acquire_code is expected_rate_gate_bucket_acquire_code
            and expected_rate_gate_bucket_force_reserve_root is not None
            and rate_gate_bucket_force_reserve_root is expected_rate_gate_bucket_force_reserve_root
            and expected_rate_gate_bucket_force_reserve_code is not None
            and rate_gate_bucket_force_reserve_code is expected_rate_gate_bucket_force_reserve_code
            and expected_rate_gate_bucket_helper_roots is not None
            and len(rate_gate_bucket_helper_roots) == len(expected_rate_gate_bucket_helper_roots)
            and all(
                name == expected_name and root is expected_root and code is expected_code
                for (name, root, code), (
                    expected_name,
                    expected_root,
                    expected_code,
                ) in zip(
                    rate_gate_bucket_helper_roots,
                    expected_rate_gate_bucket_helper_roots,
                    strict=True,
                )
            )
        )
        coordinator_is_closed = coordinator is None or (
            expected_coordinator_type is not None
            and type(coordinator) is expected_coordinator_type
            and expected_coordinator_review_root is not None
            and coordinator_review_root is expected_coordinator_review_root
            and expected_coordinator_review_code is not None
            and coordinator_review_code is expected_coordinator_review_code
        )
        # A dynamic attribute hook or a missing direct callable root has no
        # finite implementation identity. Keep execution working, but do not
        # upgrade that adapter to a portable authority claim.
        force_runtime_process_local = force_runtime_process_local or (
            session_signal_hub is not None
            or not executor_attribute_resolution_observable
            or adapter_dispatch_root is None
            or adapter_dispatch_code is None
            or not adapter_attribute_resolution_observable
            or not dispatcher_is_closed
            or not dispatcher_attribute_resolution_observable
            or not dispatcher_binding_observable
            or not transcript_is_closed
            or (
                coordinator is not None
                and (
                    coordinator_review_callable is None
                    or not coordinator_is_closed
                    or not coordinator_binding_observable
                    or not coordinator_attribute_resolution_observable
                )
            )
            or not rate_gate_is_closed
            or not rate_gate_attribute_resolution_observable
            or not rate_gate_semantics_observable
            or not rate_gate_bucket_binding_observable
        )
        # A pristine structural verifier is the one built-in verifier with a
        # durable implementation descriptor. If its code was already changed
        # before construction, keep it executable as a custom, process-local
        # verifier with one stable live-instance nonce instead of generating a
        # new nonce on every guard check.
        nonce = (
            None
            if verifier is None
            or (
                verifier is structural_atomic_verifier
                and verifier_code is _BUILTIN_STRUCTURAL_VERIFIER_CODE
            )
            else uuid.uuid4().hex
        )
        runtime_instance_nonce = uuid.uuid4().hex
        contract = ExecutionAuthorityContract.build(
            adapter=adapter,
            verifier=verifier,
            workspace=workspace,
            execution_policy=execution_policy,
            verifier_instance_nonce=nonce,
            runtime_instance_nonce=runtime_instance_nonce,
            force_runtime_process_local=force_runtime_process_local,
        )
        return cls(
            contract=contract,
            executor=executor,
            executor_attribute_resolution_observable=executor_attribute_resolution_observable,
            adapter=adapter,
            runtime_instance_nonce=runtime_instance_nonce,
            verifier=verifier,
            session_signal_hub=session_signal_hub,
            dispatcher_type=dispatcher_type,
            dispatcher=dispatcher,
            dispatcher_executor=captured_dispatcher_executor,
            transcript_verifier=transcript_verifier,
            adapter_dispatch_root=adapter_dispatch_root,
            adapter_dispatch_code=adapter_dispatch_code,
            adapter_attribute_resolution_observable=adapter_attribute_resolution_observable,
            verifier_root=verifier_root,
            verifier_code=verifier_code,
            dispatcher_stream_root=dispatcher_stream_root,
            dispatcher_stream_code=dispatcher_stream_code,
            dispatcher_stream_callable=dispatcher_stream_callable,
            dispatcher_attribute_resolution_observable=(dispatcher_attribute_resolution_observable),
            dispatcher_binding_observable=dispatcher_binding_observable,
            transcript_verifier_root=transcript_verifier_root,
            transcript_verifier_code=transcript_verifier_code,
            coordinator=coordinator,
            coordinator_review_callable=coordinator_review_callable,
            coordinator_review_root=coordinator_review_root,
            coordinator_review_code=coordinator_review_code,
            coordinator_adapter=coordinator_adapter,
            coordinator_task_cwd=coordinator_task_cwd,
            coordinator_reasoning_effort=coordinator_reasoning_effort,
            coordinator_attribute_resolution_observable=(
                coordinator_attribute_resolution_observable
            ),
            coordinator_binding_observable=coordinator_binding_observable,
            rate_gate=rate_gate,
            rate_gate_acquire_root=rate_gate_acquire_root,
            rate_gate_acquire_code=rate_gate_acquire_code,
            rate_gate_acquire_callable=rate_gate_acquire_callable,
            rate_gate_attribute_resolution_observable=rate_gate_attribute_resolution_observable,
            rate_gate_max_wait_seconds=rate_gate_max_wait_seconds,
            rate_gate_heartbeat_seconds=rate_gate_heartbeat_seconds,
            rate_gate_sleep=rate_gate_sleep,
            rate_gate_sleep_root=rate_gate_sleep_root,
            rate_gate_sleep_code=rate_gate_sleep_code,
            rate_gate_semantics_observable=rate_gate_semantics_observable,
            rate_gate_bucket=rate_gate_bucket,
            rate_gate_bucket_config=rate_gate_bucket_config,
            rate_gate_bucket_attribute_resolution_observable=(
                rate_gate_bucket_attribute_resolution_observable
            ),
            rate_gate_bucket_time=rate_gate_bucket_time,
            rate_gate_bucket_time_root=rate_gate_bucket_time_root,
            rate_gate_bucket_time_code=rate_gate_bucket_time_code,
            rate_gate_bucket_enabled_root=rate_gate_bucket_enabled_root,
            rate_gate_bucket_enabled_code=rate_gate_bucket_enabled_code,
            rate_gate_bucket_acquire_root=rate_gate_bucket_acquire_root,
            rate_gate_bucket_acquire_code=rate_gate_bucket_acquire_code,
            rate_gate_bucket_force_reserve_root=rate_gate_bucket_force_reserve_root,
            rate_gate_bucket_force_reserve_code=rate_gate_bucket_force_reserve_code,
            rate_gate_bucket_helper_roots=rate_gate_bucket_helper_roots,
            rate_gate_bucket_binding_observable=rate_gate_bucket_binding_observable,
            verifier_instance_nonce=nonce,
            force_runtime_process_local=force_runtime_process_local,
        )

    def is_intact(
        self,
        *,
        adapter: object,
        verifier: Verifier | None,
        dispatcher_type: object,
        transcript_verifier: object,
        rate_gate: object,
        workspace: str | None,
        execution_policy: Mapping[str, object],
        session_signal_hub: object | None = None,
        executor: object | None = None,
        coordinator: object | None = None,
        coordinator_review_callable: object | None = None,
        dispatcher: object | None = None,
        dispatcher_executor: object | None = None,
        dispatcher_stream_callable: object | None = None,
        rate_gate_acquire_callable: object | None = None,
    ) -> bool:
        if executor is not self.executor:
            return False
        if adapter is not self.adapter or verifier is not self.verifier:
            return False
        if session_signal_hub is not self.session_signal_hub:
            return False
        if dispatcher_type is not self.dispatcher_type:
            return False
        if dispatcher is not self.dispatcher or dispatcher_executor is not self.dispatcher_executor:
            return False
        if dispatcher_stream_callable is not self.dispatcher_stream_callable:
            return False
        if transcript_verifier is not self.transcript_verifier:
            return False
        if coordinator is not self.coordinator:
            return False
        if coordinator_review_callable is not self.coordinator_review_callable:
            return False
        if rate_gate is not self.rate_gate:
            return False
        if rate_gate_acquire_callable is not self.rate_gate_acquire_callable:
            return False
        if (
            self.executor_attribute_resolution_observable
            and executor is not None
            and not _uses_default_instance_attribute_resolution(executor)
        ):
            return False
        if (
            self.adapter_attribute_resolution_observable
            and not _uses_default_instance_attribute_resolution(adapter)
        ):
            return False
        if (
            self.dispatcher_attribute_resolution_observable
            and not _uses_default_instance_attribute_resolution(dispatcher_type)
        ):
            return False
        if (
            self.coordinator_attribute_resolution_observable
            and coordinator is not None
            and not _uses_default_instance_attribute_resolution(coordinator)
        ):
            return False
        if (
            self.rate_gate_attribute_resolution_observable
            and not _uses_default_instance_attribute_resolution(rate_gate)
        ):
            return False
        if (
            self.rate_gate_bucket_attribute_resolution_observable
            and self.rate_gate_bucket is not None
            and not _uses_default_instance_attribute_resolution(self.rate_gate_bucket)
        ):
            return False
        if self.rate_gate_semantics_observable:
            try:
                current_rate_gate_max_wait_seconds = object.__getattribute__(
                    rate_gate,
                    "_max_wait_seconds",
                )
                current_rate_gate_heartbeat_seconds = object.__getattribute__(
                    rate_gate,
                    "_heartbeat_seconds",
                )
                current_rate_gate_sleep = object.__getattribute__(rate_gate, "_sleep")
                current_rate_gate_bucket = object.__getattribute__(rate_gate, "_bucket")
                current_rate_gate_bucket_config = (
                    object.__getattribute__(current_rate_gate_bucket, "_runtime_backend"),
                    object.__getattribute__(current_rate_gate_bucket, "_request_limit"),
                    object.__getattribute__(current_rate_gate_bucket, "_token_limit"),
                    object.__getattribute__(current_rate_gate_bucket, "_window_seconds"),
                )
                current_rate_gate_bucket_time = object.__getattribute__(
                    current_rate_gate_bucket,
                    "_time",
                )
            except (AttributeError, TypeError):
                return False
            if (
                not _is_finite_number(current_rate_gate_max_wait_seconds)
                or not _is_finite_number(current_rate_gate_heartbeat_seconds)
                or current_rate_gate_max_wait_seconds != self.rate_gate_max_wait_seconds
                or current_rate_gate_heartbeat_seconds != self.rate_gate_heartbeat_seconds
                or current_rate_gate_sleep is not self.rate_gate_sleep
                or _static_callable_root(current_rate_gate_sleep, None)
                is not self.rate_gate_sleep_root
                or _callable_code_identity(_static_callable_root(current_rate_gate_sleep, None))
                is not self.rate_gate_sleep_code
                or current_rate_gate_bucket is not self.rate_gate_bucket
                or current_rate_gate_bucket_config != self.rate_gate_bucket_config
                or current_rate_gate_bucket_time is not self.rate_gate_bucket_time
                or _static_callable_root(current_rate_gate_bucket_time, None)
                is not self.rate_gate_bucket_time_root
                or _callable_code_identity(
                    _static_callable_root(current_rate_gate_bucket_time, None)
                )
                is not self.rate_gate_bucket_time_code
                or _static_property_getter_root(current_rate_gate_bucket, "enabled")
                is not self.rate_gate_bucket_enabled_root
                or _callable_code_identity(
                    _static_property_getter_root(current_rate_gate_bucket, "enabled")
                )
                is not self.rate_gate_bucket_enabled_code
                or _static_callable_root(current_rate_gate_bucket, "acquire")
                is not self.rate_gate_bucket_acquire_root
                or _callable_code_identity(
                    _static_callable_root(current_rate_gate_bucket, "acquire")
                )
                is not self.rate_gate_bucket_acquire_code
                or _static_callable_root(current_rate_gate_bucket, "force_reserve")
                is not self.rate_gate_bucket_force_reserve_root
                or _callable_code_identity(
                    _static_callable_root(current_rate_gate_bucket, "force_reserve")
                )
                is not self.rate_gate_bucket_force_reserve_code
            ):
                return False
            for name, root, code in self.rate_gate_bucket_helper_roots:
                current_root = _static_callable_root(current_rate_gate_bucket, name)
                if current_root is not root or _callable_code_identity(current_root) is not code:
                    return False
        if (
            self.adapter_dispatch_root is not None
            and _static_callable_root(adapter, "execute_task") is not self.adapter_dispatch_root
        ):
            return False
        if (
            self.adapter_dispatch_code is not None
            and _callable_code_identity(_static_callable_root(adapter, "execute_task"))
            is not self.adapter_dispatch_code
        ):
            return False
        if (
            self.verifier_root is not None
            and _static_callable_root(verifier, None) is not self.verifier_root
        ):
            return False
        if (
            self.verifier_code is not None
            and _callable_code_identity(_static_callable_root(verifier, None))
            is not self.verifier_code
        ):
            return False
        if (
            self.dispatcher_stream_root is not None
            and _static_callable_root(dispatcher_type, "stream") is not self.dispatcher_stream_root
        ):
            return False
        if (
            self.dispatcher_stream_code is not None
            and _callable_code_identity(_static_callable_root(dispatcher_type, "stream"))
            is not self.dispatcher_stream_code
        ):
            return False
        if self.dispatcher_binding_observable and dispatcher is not None:
            try:
                current_dispatcher_executor = object.__getattribute__(dispatcher, "_executor")
            except (AttributeError, TypeError):
                return False
            if current_dispatcher_executor is not self.dispatcher_executor:
                return False
        if (
            self.transcript_verifier_root is not None
            and _static_callable_root(transcript_verifier, None)
            is not self.transcript_verifier_root
        ):
            return False
        if (
            self.transcript_verifier_code is not None
            and _callable_code_identity(_static_callable_root(transcript_verifier, None))
            is not self.transcript_verifier_code
        ):
            return False
        if (
            self.coordinator_review_root is not None
            and _static_callable_root(coordinator, "run_review") is not self.coordinator_review_root
        ):
            return False
        if (
            self.coordinator_review_code is not None
            and _callable_code_identity(_static_callable_root(coordinator, "run_review"))
            is not self.coordinator_review_code
        ):
            return False
        if self.coordinator_binding_observable and coordinator is not None:
            try:
                current_coordinator_adapter = object.__getattribute__(coordinator, "_adapter")
                current_coordinator_task_cwd = object.__getattribute__(coordinator, "_task_cwd")
                current_coordinator_reasoning_effort = object.__getattribute__(
                    coordinator,
                    "_reasoning_effort",
                )
            except (AttributeError, TypeError):
                return False
            if (
                current_coordinator_adapter is not self.coordinator_adapter
                or current_coordinator_task_cwd != self.coordinator_task_cwd
                or current_coordinator_reasoning_effort != self.coordinator_reasoning_effort
            ):
                return False
        if (
            self.rate_gate_acquire_root is not None
            and _static_callable_root(rate_gate, "acquire") is not self.rate_gate_acquire_root
        ):
            return False
        if (
            self.rate_gate_acquire_code is not None
            and _callable_code_identity(_static_callable_root(rate_gate, "acquire"))
            is not self.rate_gate_acquire_code
        ):
            return False
        try:
            current = ExecutionAuthorityContract.build(
                adapter=adapter,
                verifier=verifier,
                workspace=workspace,
                execution_policy=execution_policy,
                verifier_instance_nonce=self.verifier_instance_nonce,
                runtime_instance_nonce=self.runtime_instance_nonce,
                force_runtime_process_local=self.force_runtime_process_local,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        return current.canonical_json == self.contract.canonical_json


__all__ = [
    "EXECUTION_AUTHORITY_VERSION",
    "ExecutionAuthorityContract",
    "ExecutionAuthorityLiveBinding",
    "canonical_workspace_authority",
    "constructor_model_contract",
    "execution_authority_boundary_contract",
    "execution_policy_authority_contract",
    "executor_authority_contract",
    "runtime_authority_contract",
    "runtime_execution_identity_contract",
    "runtime_execution_proves_effective_model",
    "valid_constructor_model_contract",
    "valid_process_local_authority_contract",
    "valid_runtime_execution_identity_contract",
    "verifier_authority_contract",
]
