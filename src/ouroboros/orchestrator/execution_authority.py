"""Canonical portable executor baseline shared by later runtime layers.

This module owns *baseline* identity only.  It does not authorize dispatch,
persist checkpoints, select a resume handle, or declare acceptance.  The
contract has an explicit boundary: a later attempt capsule owns AC semantics,
prompt/tool data, runtime-handle selection, handle-manager state, and recovery;
event delivery and live signal state remain volatile.  That prevents a baseline
fingerprint from pretending to be a complete fingerprint of an active attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import inspect
import json
import marshal
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading
from typing import Any
import weakref

from ouroboros.core.security import is_sensitive_field, is_sensitive_value
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.model_routing import (
    ModelRouter,
    deserialize_model_router,
    serialize_model_router,
)
from ouroboros.orchestrator.profile_loader import ExecutionProfile
from ouroboros.orchestrator.runtime_param_negotiation import runtime_capabilities_for
from ouroboros.orchestrator.verifier import Verifier

EXECUTION_AUTHORITY_VERSION = 5
EXECUTION_AUTHORITY_BOUNDARY_VERSION = 2
_MAX_IDENTITY_DEPTH = 8
_MAX_IDENTITY_ITEMS = 256
_MAX_IDENTITY_SCALAR_CHARS = 8_192
_MAX_IDENTITY_JSON_CHARS = 64_000
_MAX_IDENTITY_FILE_BYTES = 16 * 1024 * 1024
_MAX_CALLABLE_DEPENDENCY_DEPTH = 16
_RESOLVED_RUNTIME_AUTHORITY_TOKEN = object()
_RESOLVED_RUNTIME_LABEL_BINDING_KEY = secrets.token_bytes(32)
_RESOLVED_RUNTIME_EXECUTION_BINDING_KEY = secrets.token_bytes(32)
_WORKSPACE_AUTHORITY_BINDING_KEY = secrets.token_bytes(32)
_SHA256_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTITY_UNAVAILABLE_REASON = "identity_unavailable"
_PROCESS_LOCAL_RUNTIME_NONCES: dict[int, tuple[weakref.ReferenceType[object], str]] = {}
_PROCESS_LOCAL_RUNTIME_NONCES_LOCK = threading.Lock()
_WORKSPACE_PATH_IDENTITY_FIELDS = frozenset(
    {
        "effective_cwd",
        "original_cwd",
        "repo_root",
        "worktree_path",
        "lock_path",
    }
)
_EXECUTION_POLICY_VERSION = 1
_EXECUTION_POLICY_KEYS = {
    "version",
    "decomposition_mode",
    "max_decomposition_depth",
    "max_concurrent",
    "execution_profile",
    "fat_harness_mode",
    "run_verify_commands",
    "verify_command_timeout_seconds",
    "ac_retry_attempts",
    "reasoning_effort",
    "model_routing",
    "cross_harness_redispatch",
    "shadow_replay_enabled",
    "dispatch_rate",
}

# This is a deliberately finite authority boundary.  Do not infer more
# collaborators by traversing the live object graph: additions belong in the
# named later layer, together with that layer's own replay/acceptance tests.
_PORTABLE_BASELINE_BOUNDARY = (
    "executor_implementation",
    "leaf_dispatcher_implementation",
    "dispatch_token_estimator_implementation",
    "runtime_adapter_configuration",
    "workspace_generation",
    "verifier_implementation",
    "execution_policy",
)
_PER_ATTEMPT_CAPSULE_BOUNDARY = (
    "ac_semantics",
    "prompt_and_tool_catalog",
    "selected_runtime_handle",
    "ac_runtime_handle_manager",
    "checkpoint_and_resume_state",
    "level_coordinator_behavior_and_session_state",
    "selected_ac_reasoning_effort",
)
_VOLATILE_BOUNDARY = (
    "session_signal_hub",
    "event_store_handle",
    "execution_event_emitter",
    "locks_and_live_pools",
)
_PORTABLE_EXECUTOR_COMPONENTS = frozenset({"leaf_dispatcher", "dispatch_token_estimator"})
_UNBOUND_EXECUTOR_NONCE_OWNER = object()
# These module bindings are named explicitly in the Foundation-A boundary as
# per-attempt or volatile collaborators. Their implementation/state belongs to
# later capsules, so following them through an executor method would quietly
# reintroduce excluded authority through a callable-global graph.
_PORTABLE_CALLABLE_GLOBAL_EXCLUSIONS = frozenset(
    {
        ("ouroboros.orchestrator.parallel_executor", "LevelCoordinator"),
        ("ouroboros.orchestrator.parallel_executor", "ACRuntimeHandleManager"),
        ("ouroboros.orchestrator.parallel_executor", "ExecutionEventEmitter"),
    }
)


def execution_authority_boundary_contract() -> dict[str, object]:
    """Return the versioned Foundation-A inclusion/exclusion matrix.

    The matrix is part of canonical authority JSON so a caller cannot confuse a
    portable baseline with the later per-attempt or event/recovery contracts.
    """
    return {
        "version": EXECUTION_AUTHORITY_BOUNDARY_VERSION,
        "portable_baseline": list(_PORTABLE_BASELINE_BOUNDARY),
        "per_attempt_capsule": list(_PER_ATTEMPT_CAPSULE_BOUNDARY),
        "volatile": list(_VOLATILE_BOUNDARY),
    }


def _valid_execution_authority_boundary(value: object) -> bool:
    return value == execution_authority_boundary_contract()


def _process_local_runtime_nonce(*identity_inputs: object) -> str:
    """Return a stable-in-process opaque nonce without retaining collaborators.

    A process-local baseline still needs repeatable snapshots for one live
    object: runner routing compares its pre-dispatch snapshot with the snapshot
    taken during construction.  A strong object-keyed cache would keep clients,
    pools, and possibly credentials alive forever, however.  An identity-keyed
    weak-reference side table gives each live object one random nonce and
    removes the entry on collection; no adapter state is mutated or retained.
    """
    if not identity_inputs:
        raise ValueError("a process-local identity needs at least one input")
    primary = identity_inputs[0]
    object_id = id(primary)
    try:
        with _PROCESS_LOCAL_RUNTIME_NONCES_LOCK:
            cached = _PROCESS_LOCAL_RUNTIME_NONCES.get(object_id)
            if cached is not None and cached[0]() is primary:
                return cached[1]

            nonce = secrets.token_hex(32)

            def discard(
                dead_reference: weakref.ReferenceType[object], *, key: int = object_id
            ) -> None:
                with _PROCESS_LOCAL_RUNTIME_NONCES_LOCK:
                    current = _PROCESS_LOCAL_RUNTIME_NONCES.get(key)
                    if current is not None and current[0] is dead_reference:
                        _PROCESS_LOCAL_RUNTIME_NONCES.pop(key, None)

            _PROCESS_LOCAL_RUNTIME_NONCES[object_id] = (weakref.ref(primary, discard), nonce)
            return nonce
    except TypeError:
        # ``__slots__``-only and foreign extension objects can be non-weakrefable.
        # Retaining an ``id()`` table for them would reintroduce both a strong
        # reference leak and object-id reuse risk.  Make that narrow fallback
        # explicitly unstable instead: it can never claim cross-snapshot or
        # cross-process portability, but it also cannot accidentally collide
        # with a later object that reuses the same address.
        return secrets.token_hex(32)


def _unobserved_runtime_execution_identity(
    adapter: object,
    *,
    process_local: bool,
) -> dict[str, object]:
    identity: dict[str, object] = {"version": 1, "observed": False}
    if process_local:
        identity["instance_nonce"] = _process_local_runtime_nonce(adapter)
    return identity


def _safe_runtime_attribute(adapter: object, name: str, default: object = None) -> object:
    """Read provider state without allowing provider exception text to escape."""
    try:
        return getattr(adapter, name, default)
    except Exception:
        return default


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


def _workspace_authority_contains_sensitive_data(value: object) -> bool:
    """Return whether a workspace identity would disclose sensitive data."""
    try:
        _reject_sensitive_identity_fields(value)
    except Exception:
        return True
    return False


def _resolved_workspace_path(value: object) -> str | None:
    """Resolve one workspace path without reflecting invalid provider input."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, ValueError):
        return None


def _canonical_workspace_identity_paths(
    identity: Mapping[str, object],
) -> dict[str, object] | None:
    """Resolve path-bearing workspace fields before identity serialization.

    A benign-looking symlink can resolve to a credential-shaped directory name.
    Canonical workspace identity must inspect the same resolved path that
    execution uses; otherwise that target enters durable authority JSON.
    """
    canonical = dict(identity)
    for field_name in _WORKSPACE_PATH_IDENTITY_FIELDS:
        if field_name not in canonical:
            continue
        resolved = _resolved_workspace_path(canonical[field_name])
        if resolved is None:
            return None
        canonical[field_name] = resolved
    return canonical


def _process_local_workspace_authority(*identity_inputs: object) -> dict[str, object]:
    """Return a non-portable workspace marker without serializing its paths.

    Workspace paths, branch names, and caller-supplied identity fields can be
    provider-controlled strings. When one contains a credential-shaped value,
    preserve a same-process distinction with a keyed digest while omitting every
    raw path/value from the baseline. The secret key is process-local, so this
    marker can never claim cross-process portability or reusable authority.
    """
    digest = hashlib.blake2b(
        key=_WORKSPACE_AUTHORITY_BINDING_KEY,
        digest_size=32,
    )
    for index, value in enumerate(identity_inputs):
        try:
            encoded = _canonical_json(value, field="workspace identity")
        except Exception:
            encoded = f"unavailable:{id(value)}:{id(type(value))}"
        encoded_bytes = encoded.encode("utf-8", errors="surrogatepass")
        digest.update(index.to_bytes(2, byteorder="big", signed=False))
        digest.update(len(encoded_bytes).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded_bytes)
    return {
        "version": 1,
        "observed": False,
        "identity": {
            "mode": "process_local",
            "binding_digest": "blake2b:" + digest.hexdigest(),
        },
        "generation": {"observed": False},
    }


def canonical_workspace_authority(
    workspace: str | None,
    *,
    identity: Mapping[str, object] | None = None,
    generation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return the checkout owner plus an optional immutable generation identity."""
    normalized_identity: dict[str, object] | None = None
    normalized_generation: dict[str, object] | None = None
    resolved_workspace = _resolved_workspace_path(workspace)
    if identity is not None:
        normalized_identity = _canonical_object(dict(identity), field="workspace identity")
        resolved_identity = _canonical_workspace_identity_paths(normalized_identity)
        if (
            resolved_identity is None
            or _workspace_authority_contains_sensitive_data(normalized_identity)
            or _workspace_authority_contains_sensitive_data(resolved_identity)
            or is_sensitive_value(workspace)
            or is_sensitive_value(resolved_workspace)
        ):
            return _process_local_workspace_authority(
                workspace,
                normalized_identity,
                generation,
            )
        owner = resolved_identity
        resolved_cwd = owner.get("effective_cwd")
        if (
            resolved_workspace is None
            or not isinstance(resolved_cwd, str)
            or resolved_workspace != resolved_cwd
        ):
            raise ValueError("workspace identity disagrees with the effective workspace")
    elif isinstance(workspace, str) and workspace.strip():
        if (
            resolved_workspace is None
            or is_sensitive_value(workspace)
            or is_sensitive_value(resolved_workspace)
        ):
            return _process_local_workspace_authority(workspace, generation)
        owner = {
            "mode": "direct",
            "effective_cwd": resolved_workspace,
        }
    else:
        return {
            "version": 1,
            "observed": False,
            "generation": {"observed": False},
        }
    generation_contract: dict[str, object] = {"observed": False}
    if generation is not None:
        normalized_generation = _canonical_object(
            dict(generation),
            field="workspace generation",
        )
        if _workspace_authority_contains_sensitive_data(normalized_generation):
            return _process_local_workspace_authority(
                workspace,
                normalized_identity,
                normalized_generation,
            )
        if normalized_generation:
            if not _valid_workspace_generation_identity(normalized_generation):
                raise ValueError("workspace generation identity is invalid")
            generation_contract = {
                "observed": True,
                "identity": normalized_generation,
            }
    return {
        "version": 1,
        "observed": True,
        "identity": owner,
        "generation": generation_contract,
    }


def _valid_workspace_generation_identity(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"version", "kind", "digest"}:
        return False
    version = value.get("version")
    kind = value.get("kind")
    digest = value.get("digest")
    return (
        not isinstance(version, bool)
        and version == 1
        and isinstance(kind, str)
        and bool(kind.strip())
        and isinstance(digest, str)
        and _SHA256_DIGEST_PATTERN.fullmatch(digest) is not None
    )


def constructor_model_contract(adapter: object) -> dict[str, object]:
    """Return the normalized constructor-level model pin, when observable."""
    try:
        raw_model = inspect.getattr_static(adapter, "_model")
    except AttributeError:
        return {"observed": False}
    if raw_model is None:
        return {"observed": True, "model": None}
    if not isinstance(raw_model, str) or is_sensitive_value(raw_model):
        return {
            "observed": False,
            "instance_nonce": _process_local_runtime_nonce(adapter),
        }

    normalized_model: object = raw_model.strip() or None
    normalizer_descriptor = inspect.getattr_static(type(adapter), "_normalize_model", None)
    if normalizer_descriptor is not None:
        try:
            normalizer = object.__getattribute__(adapter, "_normalize_model")
            normalized_model = normalizer(raw_model)
        except Exception:
            return {
                "observed": False,
                "instance_nonce": _process_local_runtime_nonce(adapter),
            }
    if normalized_model is None:
        return {"observed": True, "model": None}
    if (
        not isinstance(normalized_model, str)
        or not normalized_model.strip()
        or is_sensitive_value(normalized_model)
    ):
        return {
            "observed": False,
            "instance_nonce": _process_local_runtime_nonce(adapter),
        }
    return {"observed": True, "model": normalized_model.strip()}


def valid_constructor_model_contract(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("observed") is not True:
        return False
    model = value.get("model")
    return set(value) == {"observed", "model"} and (
        model is None
        or isinstance(model, str)
        and bool(model.strip())
        and not is_sensitive_value(model)
    )


def runtime_execution_identity_contract(
    adapter: object,
    *,
    include_process_local_nonce: bool = True,
) -> dict[str, object]:
    """Return backend-specific resolved identity without backend logic here.

    A runner's durable routing payload needs to preserve the legacy
    ``observed=False`` shape so it can still validate an otherwise observable
    runtime configuration on a later process. The Foundation-A baseline, in
    contrast, must distinguish live instances that cannot provide an identity.
    Callers building a baseline therefore retain the default nonce, while the
    runner and its pre-dispatch binding use ``False`` and add the nonce only to
    the constructed baseline.
    """
    provider_descriptor = inspect.getattr_static(
        type(adapter),
        "execution_identity_contract",
        None,
    )
    if provider_descriptor is None:
        # Missing provider identity is a collision risk even when the concrete
        # class happens to be known: private runtime state can still select
        # materially different behavior.  Keep same-instance snapshots stable
        # but never let two live instances share this non-portable identity.
        return _unobserved_runtime_execution_identity(
            adapter,
            process_local=include_process_local_nonce,
        )

    try:
        provider = object.__getattribute__(adapter, "execution_identity_contract")
        identity = provider()
    except Exception:
        # A declared provider that cannot report its identity is a construction
        # failure, not an optional missing declaration. Do not dispatch through
        # that ambiguity, and do not surface provider-controlled error text.
        raise ValueError("runtime execution identity provider failed") from None
    try:
        if not isinstance(identity, Mapping):
            raise ValueError("runtime execution identity contract is not a mapping")
        normalized = _canonical_explicit_identity(
            dict(identity),
            field="runtime execution identity contract",
        )
    except Exception:
        # Provider-owned credential-bearing identity must never reach the public
        # authority payload. Mark it unobserved so portability fails closed.
        return _unobserved_runtime_execution_identity(
            adapter,
            process_local=include_process_local_nonce,
        )
    if not normalized:
        return _unobserved_runtime_execution_identity(
            adapter,
            process_local=include_process_local_nonce,
        )
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
        nonce = value.get("instance_nonce")
        return set(value) == {"version", "observed"} or (
            set(value) == {"version", "observed", "instance_nonce"}
            and isinstance(nonce, str)
            and bool(nonce)
        )
    identity = value.get("identity")
    if (
        set(value) != {"version", "observed", "identity"}
        or not isinstance(identity, Mapping)
        or not identity
    ):
        return False
    try:
        _reject_sensitive_identity_fields(identity)
        _canonical_json(dict(identity), field="runtime execution identity contract")
    except ValueError:
        return False
    return True


def runtime_execution_proves_effective_model(value: object) -> bool:
    if not valid_runtime_execution_identity_contract(value):
        return False
    if not isinstance(value, Mapping) or value.get("observed") is not True:
        return False
    identity = value.get("identity")
    return isinstance(identity, Mapping) and identity.get("effective_model_observed") is True


def _identity_digest(value: Mapping[str, object], *, field: str) -> str:
    """Return an opaque digest without persisting provider-controlled values."""
    return _sha256(_canonical_json(dict(value), field=field))


def _authority_runtime_execution_identity(
    value: object,
    *,
    adapter: object,
) -> dict[str, object]:
    """Project a runner identity into safe baseline authority data.

    Runner resume contracts may need provider-specific fields to validate an
    active run.  Foundation A never serializes those literal values: it records
    only their deterministic digest and the one model-observation bit the
    baseline needs.  Known credential-bearing values still fail closed before
    this point; opaque provider values cannot egress through authority JSON.
    """
    if not valid_runtime_execution_identity_contract(value):
        raise ValueError("runtime execution identity contract is invalid")
    assert isinstance(value, Mapping)  # narrowed by the validator above
    if value.get("observed") is not True:
        result: dict[str, object] = {"version": 1, "observed": False}
        nonce = value.get("instance_nonce")
        result["instance_nonce"] = (
            nonce if isinstance(nonce, str) and nonce else _process_local_runtime_nonce(adapter)
        )
        return result
    identity = value.get("identity")
    assert isinstance(identity, Mapping)  # narrowed by the validator above
    return {
        "version": 1,
        "observed": True,
        "effective_model_observed": identity.get("effective_model_observed") is True,
        "identity_digest": _identity_digest(
            identity,
            field="runtime execution identity contract",
        ),
    }


def _valid_authority_runtime_execution_identity(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return False
    observed = value.get("observed")
    if observed is False:
        nonce = value.get("instance_nonce")
        return set(value) == {"version", "observed"} or (
            set(value) == {"version", "observed", "instance_nonce"}
            and isinstance(nonce, str)
            and bool(nonce)
        )
    return (
        observed is True
        and set(value) == {"version", "observed", "effective_model_observed", "identity_digest"}
        and isinstance(value.get("effective_model_observed"), bool)
        and isinstance(value.get("identity_digest"), str)
        and _SHA256_DIGEST_PATTERN.fullmatch(value["identity_digest"]) is not None
    )


def runtime_permission_mode_contract(adapter: object) -> dict[str, object]:
    """Return the normalized permission mode that the runtime actually executes."""
    permission_mode: object = None
    missing = object()
    private_descriptor = inspect.getattr_static(adapter, "_permission_mode", missing)
    if private_descriptor is not missing:
        try:
            permission_mode = object.__getattribute__(adapter, "_permission_mode")
        except Exception:
            return {
                "observed": False,
                "instance_nonce": _process_local_runtime_nonce(adapter),
            }
        if permission_mode is None:
            # A declared private ``None`` is an explicit configuration choice,
            # not an unobservable provider value.  This matters for generic
            # worker runtimes whose transport policy is otherwise fully
            # authority-bound, and avoids spuriously tying a baseline to a
            # newly-created wrapper instance.
            return {"observed": True, "mode": None}
    if not isinstance(permission_mode, str) or not permission_mode.strip():
        permission_mode = _safe_runtime_attribute(adapter, "permission_mode")
    if (
        isinstance(permission_mode, str)
        and permission_mode.strip()
        and not is_sensitive_value(permission_mode)
    ):
        return {"observed": True, "mode": permission_mode.strip()}
    return {
        "observed": False,
        "instance_nonce": _process_local_runtime_nonce(adapter),
    }


def _runtime_label(adapter: object, attribute: str) -> tuple[str | None, bool]:
    """Read a provider label only when it is safe to serialize verbatim."""
    value = _safe_runtime_attribute(adapter, attribute)
    if not isinstance(value, str):
        return None, False
    normalized = value.strip()
    if normalized and not is_sensitive_value(normalized):
        return normalized, True
    return None, False


def runtime_routing_labels_contract(adapter: object) -> dict[str, object]:
    """Return runner-routing labels with explicit observation state.

    The runner persists these values before binding them back to the same live
    adapter for parallel execution. Keep the unobserved markers beside their
    ``None`` values so a missing, fabricated, or sensitive label cannot be
    mistaken for an observed empty selection during that bind.
    """
    runtime_backend, runtime_backend_observed = _runtime_label(adapter, "runtime_backend")
    llm_backend, llm_backend_observed = _runtime_label(adapter, "llm_backend")
    result: dict[str, object] = {
        "runtime_backend": runtime_backend,
        "llm_backend": llm_backend,
    }
    if not runtime_backend_observed:
        result["runtime_backend_unobserved"] = True
    if not llm_backend_observed:
        result["llm_backend_unobserved"] = True
    return result


def _active_resolved_runtime_fields(adapter: object) -> dict[str, object]:
    result = {
        **runtime_routing_labels_contract(adapter),
        "permission_mode": runtime_permission_mode_contract(adapter),
        "constructor_model": constructor_model_contract(adapter),
        "runtime_execution": runtime_execution_identity_contract(
            adapter,
            include_process_local_nonce=False,
        ),
    }
    return result


def _unobserved_runtime_label_binding(
    adapter: object,
    labels: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    """Return a process-local, non-serializable guard for hidden labels.

    A credential-shaped label must never enter the durable routing payload, but
    a same-process mutation of that label can still change dispatch behavior.
    Capture a keyed digest solely on :class:`ResolvedRuntimeAuthority`; its
    secret key and output never enter the canonical authority contract.  This
    lets the original live adapter remain usable while rejecting a changed
    hidden value before executor construction.
    """
    bindings: list[tuple[str, str]] = []
    for field_name in ("runtime_backend", "llm_backend"):
        if labels.get(f"{field_name}_unobserved") is not True:
            continue
        value = _safe_runtime_attribute(adapter, field_name)
        digest = hashlib.blake2b(
            key=_RESOLVED_RUNTIME_LABEL_BINDING_KEY,
            digest_size=32,
        )
        digest.update(field_name.encode("utf-8"))
        if isinstance(value, str):
            encoded = value.encode("utf-8", errors="surrogatepass")
            digest.update(b"str")
            digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
            digest.update(encoded)
        elif value is None:
            digest.update(b"none")
        else:
            # Non-string backend labels are malformed and therefore
            # non-portable. Their exact representation may itself be hostile
            # or sensitive, so guard their process-local object identity only.
            digest.update(b"object")
            digest.update(str(id(type(value))).encode("ascii"))
            digest.update(str(id(value)).encode("ascii"))
        bindings.append((field_name, digest.hexdigest()))
    return tuple(bindings)


def _unobserved_runtime_execution_binding(
    adapter: object,
    active: Mapping[str, object],
) -> str | None:
    """Bind an unobservable provider identity without persisting its values.

    ``runtime_execution_identity_contract`` intentionally reduces a malformed
    or credential-bearing provider identity to ``observed=False`` before it
    reaches a durable routing contract.  That public shape cannot distinguish
    two secret values on the same adapter, however.  Capture a keyed,
    process-local digest of the raw provider response for the live binding so
    a planning-to-dispatch change is rejected without serializing any provider
    value.  If the raw response cannot be safely normalized for this private
    comparison, use fresh entropy: the next binding check will fail closed.
    """
    execution_identity = active.get("runtime_execution")
    if (
        not isinstance(execution_identity, Mapping)
        or execution_identity.get("observed") is not False
    ):
        return None

    digest = hashlib.blake2b(
        key=_RESOLVED_RUNTIME_EXECUTION_BINDING_KEY,
        digest_size=32,
    )
    digest.update(b"runtime_execution")
    provider_descriptor = inspect.getattr_static(
        type(adapter),
        "execution_identity_contract",
        None,
    )
    if provider_descriptor is None:
        # No provider exists to mutate.  The adapter-local nonce makes this
        # private binding distinct without placing it in the routing payload.
        digest.update(b"missing-provider")
        digest.update(_process_local_runtime_nonce(adapter).encode("ascii"))
        return digest.hexdigest()

    try:
        provider = object.__getattribute__(adapter, "execution_identity_contract")
        raw_identity = provider()
        if not isinstance(raw_identity, Mapping):
            raise ValueError("runtime execution identity is not a mapping")
        encoded = _canonical_json(
            _project_explicit_identity(
                dict(raw_identity),
                field="unobserved runtime execution identity",
            ),
            field="unobserved runtime execution identity",
        )
    except Exception:
        # An invalid opaque identity cannot be compared reliably.  A different
        # value on every call deliberately prevents it from crossing the
        # planning-to-dispatch boundary.
        digest.update(b"unavailable")
        digest.update(secrets.token_bytes(32))
        return digest.hexdigest()

    encoded_bytes = encoded.encode("utf-8", errors="surrogatepass")
    digest.update(b"provider-response")
    digest.update(len(encoded_bytes).to_bytes(8, byteorder="big", signed=False))
    digest.update(encoded_bytes)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeAuthority:
    """Runner-resolved runtime identity validated against the active adapter."""

    canonical_json: str
    _binding_token: object = field(repr=False, compare=False)
    _adapter: object = field(repr=False, compare=False)
    _unobserved_label_binding: tuple[tuple[str, str], ...] = field(
        repr=False,
        compare=False,
    )
    _unobserved_execution_binding: str | None = field(
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self._binding_token is not _RESOLVED_RUNTIME_AUTHORITY_TOKEN:
            raise ValueError("resolved runtime authority was not bound to an adapter")
        canonical = _canonical_json(
            json.loads(self.canonical_json),
            field="resolved runtime authority",
        )
        if canonical != self.canonical_json:
            raise ValueError("resolved runtime authority is not canonical")

    @classmethod
    def bind(
        cls,
        adapter: object,
        resolved_routing: Mapping[str, object],
    ) -> ResolvedRuntimeAuthority:
        active = _active_resolved_runtime_fields(adapter)
        for field_name, active_value in active.items():
            if resolved_routing.get(field_name) != active_value:
                raise ValueError(f"resolved runtime authority disagrees with active {field_name}")
        return cls(
            canonical_json=_canonical_json(
                dict(resolved_routing),
                field="resolved runtime authority",
            ),
            _binding_token=_RESOLVED_RUNTIME_AUTHORITY_TOKEN,
            _adapter=adapter,
            _unobserved_label_binding=_unobserved_runtime_label_binding(adapter, active),
            _unobserved_execution_binding=_unobserved_runtime_execution_binding(
                adapter,
                active,
            ),
        )

    def require_adapter(self, adapter: object) -> None:
        """Reject reuse of a routing identity bound against another runtime instance."""
        if self._adapter is not adapter:
            raise ValueError("resolved runtime authority is bound to a different adapter")
        active = _active_resolved_runtime_fields(adapter)
        resolved = self.data
        for field_name, active_value in active.items():
            if resolved.get(field_name) != active_value:
                raise ValueError(f"resolved runtime authority drifted from active {field_name}")
        if self._unobserved_label_binding != _unobserved_runtime_label_binding(adapter, active):
            raise ValueError("resolved runtime authority drifted from an unobserved backend label")
        if self._unobserved_execution_binding != _unobserved_runtime_execution_binding(
            adapter,
            active,
        ):
            raise ValueError(
                "resolved runtime authority drifted from an unobserved execution identity"
            )

    @property
    def data(self) -> dict[str, Any]:
        return _canonical_object(
            json.loads(self.canonical_json), field="resolved runtime authority"
        )


def _runtime_capabilities_contract(adapter: object) -> dict[str, object]:
    capabilities = runtime_capabilities_for(adapter)
    return {
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


def _opaque_type_identity_digest(value: object) -> str:
    """Digest a type label without exposing a dynamic module or class name."""
    runtime_type = value if isinstance(value, type) else type(value)
    module = getattr(runtime_type, "__module__", None)
    qualname = getattr(runtime_type, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(qualname, str):
        raise ValueError("type identity is unavailable")
    return _sha256(
        _canonical_json(
            {"module": module, "qualname": qualname},
            field="opaque type identity",
        )
    )


def _opaque_callable_identity_digest(target: object) -> str:
    """Digest callable display metadata rather than serializing it verbatim."""
    module = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(qualname, str):
        return _opaque_type_identity_digest(target)
    return _sha256(
        _canonical_json(
            {"module": module, "qualname": qualname},
            field="opaque callable identity",
        )
    )


def _opaque_callable_data_digest(value: object, *, field: str) -> str:
    """Digest bounded callable data without retaining argument names or values."""
    projected = _project_explicit_identity(value, field=field)
    return _sha256(_canonical_json(projected, field=field))


def _callable_behavior_digest(target: object) -> str:
    """Digest code, defaults, closures, and directly resolved behavior.

    The payload is kept only while hashing.  It is deliberately a list rather
    than a name-keyed mapping so dynamic argument/global names cannot egress
    through authority JSON.
    """
    active: set[int] = set()
    function_count = 0

    def opaque_value_projection(
        value: object,
        *,
        depth: int = 0,
        seen: set[int] | None = None,
    ) -> object:
        """Project mutable helper data into hash-only, non-egressing state."""
        if depth > _MAX_IDENTITY_DEPTH:
            raise ValueError("callable data dependency graph exceeds its depth budget")
        # ``StrEnum`` is also a ``str``, so test enums before scalar strings.
        if isinstance(value, Enum):
            return {
                "kind": "enum",
                "type": _opaque_type_identity_digest(value),
                "name": value.name,
                "value": opaque_value_projection(value.value, depth=depth + 1, seen=seen),
            }
        if value is None or isinstance(value, (bool, int, str)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                return {"kind": "non_finite_float", "type": _opaque_type_identity_digest(value)}
            return value
        if isinstance(value, bytes):
            return {
                "kind": "bytes",
                "digest": "sha256:" + hashlib.sha256(value).hexdigest(),
            }
        if isinstance(value, type):
            return {"kind": "type", "identity_digest": _opaque_type_identity_digest(value)}
        if inspect.ismethod(value) or inspect.isfunction(value) or inspect.isbuiltin(value):
            return {
                "kind": "callable",
                "identity_digest": _opaque_callable_identity_digest(value),
            }
        if inspect.ismodule(value):
            module_name = getattr(value, "__name__", None)
            source_path = getattr(value, "__file__", None)
            if not isinstance(module_name, str) or not module_name:
                raise ValueError("callable data module lacks an identity")
            return {
                "kind": "module",
                "identity_digest": _sha256(
                    _canonical_json(
                        {
                            "name": module_name,
                            "content_digest": (
                                _file_content_digest(source_path)
                                if isinstance(source_path, str)
                                else None
                            ),
                        },
                        field="callable data module",
                    )
                ),
            }

        seen = set() if seen is None else seen
        value_id = id(value)
        if value_id in seen:
            raise ValueError("callable data dependency graph contains a cycle")
        seen.add(value_id)
        try:
            if isinstance(value, Mapping):
                if len(value) > _MAX_IDENTITY_ITEMS:
                    raise ValueError("callable data mapping is oversized")
                items = [
                    [
                        opaque_value_projection(key, depth=depth + 1, seen=seen),
                        opaque_value_projection(item, depth=depth + 1, seen=seen),
                    ]
                    for key, item in value.items()
                ]
                return {
                    "kind": "mapping",
                    "items": sorted(
                        items,
                        key=lambda item: _canonical_json(item, field="callable data mapping item"),
                    ),
                }
            if isinstance(value, (list, tuple)):
                if len(value) > _MAX_IDENTITY_ITEMS:
                    raise ValueError("callable data sequence is oversized")
                return {
                    "kind": type(value).__name__,
                    "items": [
                        opaque_value_projection(item, depth=depth + 1, seen=seen) for item in value
                    ],
                }
            if isinstance(value, (set, frozenset)):
                if len(value) > _MAX_IDENTITY_ITEMS:
                    raise ValueError("callable data set is oversized")
                items = [
                    opaque_value_projection(item, depth=depth + 1, seen=seen) for item in value
                ]
                return {
                    "kind": type(value).__name__,
                    "items": sorted(
                        items,
                        key=lambda item: _canonical_json(item, field="callable data set item"),
                    ),
                }
        finally:
            seen.remove(value_id)
        # Opaque service/logging objects are intentionally represented only by
        # type. They are not executable authority dependencies; treating them
        # as serializable state would risk credential egress.
        return {"kind": "opaque", "type": _opaque_type_identity_digest(value)}

    def opaque_value_digest(value: object, *, field: str) -> str:
        return _sha256(_canonical_json(opaque_value_projection(value), field=field))

    def direct_global_identity(value: object, *, depth: int) -> dict[str, object]:
        """Bind a direct global and recursively bind Ouroboros helper graphs."""
        if inspect.ismethod(value) or inspect.isfunction(value):
            function = value.__func__ if inspect.ismethod(value) else value
            if not inspect.isfunction(function):  # pragma: no cover - narrowed above
                raise ValueError("callable global is not a Python function")
            module = getattr(function, "__module__", "")
            if isinstance(module, str) and module.startswith("ouroboros."):
                return {"kind": "function", "identity": collect(function, depth=depth + 1)}
            return {
                "kind": "function_leaf",
                "identity_digest": _opaque_callable_identity_digest(function),
                "code_digest": "sha256:"
                + hashlib.sha256(marshal.dumps(function.__code__)).hexdigest(),
                "defaults_digest": opaque_value_digest(
                    [tuple(function.__defaults__ or ()), dict(function.__kwdefaults__ or {})],
                    field="callable global defaults",
                ),
            }
        if inspect.isbuiltin(value):
            return {
                "kind": "builtin",
                "identity_digest": _opaque_callable_identity_digest(value),
            }
        if isinstance(value, type):
            return {
                "kind": "type",
                "identity_digest": _opaque_type_identity_digest(value),
            }
        if inspect.ismodule(value):
            return opaque_value_projection(value)  # type: ignore[return-value]
        return {
            "kind": "data",
            "identity_digest": opaque_value_digest(value, field="callable global data"),
        }

    def collect(function_value: object, *, depth: int) -> dict[str, object]:
        nonlocal function_count
        if depth > _MAX_CALLABLE_DEPENDENCY_DEPTH:
            raise ValueError("callable dependency graph exceeds its depth budget")
        function = function_value.__func__ if inspect.ismethod(function_value) else function_value
        if not inspect.isfunction(function):
            raise ValueError("callable implementation is not a Python function")
        code = function.__code__
        code_digest = "sha256:" + hashlib.sha256(marshal.dumps(code)).hexdigest()
        function_id = id(function)
        if function_id in active:
            return {"mode": "recursive", "code_digest": code_digest}
        function_count += 1
        if function_count > _MAX_IDENTITY_ITEMS:
            raise ValueError("callable dependency graph has too many functions")
        active.add(function_id)
        try:
            defaults_digest = opaque_value_digest(
                [tuple(function.__defaults__ or ()), dict(function.__kwdefaults__ or {})],
                field="callable implementation defaults",
            )
            closure_values: list[dict[str, object]] = []
            for cell in function.__closure__ or ():
                try:
                    value = cell.cell_contents
                except ValueError as exc:
                    raise ValueError("callable closure cell is empty") from exc
                if inspect.ismethod(value) or inspect.isfunction(value):
                    closure_values.append(
                        {"kind": "function", "identity": collect(value, depth=depth + 1)}
                    )
                elif isinstance(value, type):
                    closure_values.append(
                        {"kind": "type", "identity_digest": _opaque_type_identity_digest(value)}
                    )
                else:
                    closure_values.append(
                        {
                            "kind": "data",
                            "identity_digest": opaque_value_digest(
                                value,
                                field="callable closure data",
                            ),
                        }
                    )

            closure_vars = inspect.getclosurevars(function)
            global_dependencies = [
                direct_global_identity(dependency, depth=depth)
                for global_name, dependency in sorted(closure_vars.globals.items())
                if (function.__module__, global_name) not in _PORTABLE_CALLABLE_GLOBAL_EXCLUSIONS
            ]
            builtin_dependencies: list[dict[str, object]] = []
            for _name, dependency in sorted(closure_vars.builtins.items()):
                if inspect.ismethod(dependency) or inspect.isfunction(dependency):
                    builtin_dependencies.append(
                        {"kind": "function", "identity": collect(dependency, depth=depth + 1)}
                    )
                elif inspect.isbuiltin(dependency):
                    builtin_dependencies.append(
                        {
                            "kind": "builtin",
                            "identity_digest": _opaque_callable_identity_digest(dependency),
                        }
                    )
                elif isinstance(dependency, type):
                    # Normal Python methods routinely resolve constructors and
                    # exception classes (``str``, ``dict``, ``ValueError``, …)
                    # through ``__builtins__``.  Those are static executable
                    # dependencies, not opaque live state.  Keep their labels
                    # out of canonical authority JSON while still detecting a
                    # replacement via their opaque identity digest.
                    builtin_dependencies.append(
                        {
                            "kind": "type",
                            "identity_digest": _opaque_type_identity_digest(dependency),
                        }
                    )
                elif dependency is None or isinstance(dependency, (bool, int, float, str)):
                    builtin_dependencies.append(
                        {
                            "kind": "data",
                            "identity_digest": opaque_value_digest(
                                dependency,
                                field="callable builtin data",
                            ),
                        }
                    )
                else:
                    # Replacing a builtin with an opaque callable/object changes
                    # execution semantics but cannot be made portable safely.
                    raise ValueError("callable builtin dependency is not inspectable")
            return {
                "code_digest": code_digest,
                "defaults_digest": defaults_digest,
                "closure": closure_values,
                "global_dependencies": global_dependencies,
                "builtin_dependencies": builtin_dependencies,
            }
        finally:
            active.remove(function_id)

    return _sha256(
        _canonical_json(
            collect(target, depth=0),
            field="callable behavior",
        )
    )


def _callable_implementation_contract(target: object) -> dict[str, object]:
    """Identify executable behavior without persisting source or raw metadata.

    Source text alone misses in-place code/default drift.  Conversely, treating
    every closure as opaque makes ordinary generated methods and zero-argument
    ``super()`` process-local.  The bounded behavior digest captures safe
    closure cells and builtins; authority-critical helper functions are bound
    explicitly at their owning component boundary. Unsupported live state fails
    closed to a stable per-instance process-local nonce.
    """
    try:
        source_digest = _sha256(inspect.getsource(target))
    except (OSError, TypeError):
        source_digest = None
    code = getattr(target, "__code__", None)
    if code is None:
        return {
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(target),
        }
    try:
        code_digest = "sha256:" + hashlib.sha256(marshal.dumps(code)).hexdigest()
        behavior_digest = _callable_behavior_digest(target)
    except Exception:
        return {
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(target),
        }
    return {
        "stability": "durable",
        "identity_digest": _opaque_callable_identity_digest(target),
        "source_digest": source_digest,
        "code_digest": code_digest,
        "behavior_digest": behavior_digest,
    }


def _callable_leaf_implementation_contract(target: object) -> dict[str, object]:
    """Fingerprint one callable entrypoint without following nested callable state."""
    if inspect.ismethod(target):
        implementation = target.__func__
    elif inspect.isfunction(target) or inspect.isbuiltin(target):
        implementation = target
    else:
        implementation = type(target).__call__
    return _callable_implementation_contract(implementation)


def _callable_entrypoint_contract(target: object) -> dict[str, object]:
    """Fingerprint the code actually invoked for functions, methods, and callables."""
    if not (inspect.ismethod(target) or inspect.isfunction(target) or inspect.isbuiltin(target)):
        class_contract = _safe_class_implementation_contract(target)
        instance_overrides = _instance_executable_overrides(target)
        durable = class_contract.get("stability") == "durable" and not instance_overrides
        contract: dict[str, object] = {
            **class_contract,
            "stability": "durable" if durable else "process_local",
            "entrypoint": _callable_leaf_implementation_contract(target),
            "instance_overrides": instance_overrides,
        }
        if not durable:
            contract["instance_nonce"] = _process_local_runtime_nonce(target)
        return contract
    return _callable_leaf_implementation_contract(target)


def _callable_dependency_implementation_contract(target: object) -> dict[str, object]:
    """Bind a transcript verifier and the Python helpers it actually invokes.

    A wrapper's own source digest is not enough for an acceptance-relevant
    verifier: its imported helpers may be replaced or evolve independently.
    Follow direct Python-function globals only. Closures, unsupported callable
    globals, or uninspectable dependencies make the whole verifier
    process-local instead of claiming a durable identity. Recursive helpers are
    represented by a bounded source-digest reference rather than unrolling an
    infinite graph.
    """

    active: set[int] = set()
    function_count = 0

    def global_contract(value: object) -> dict[str, object]:
        """Return a safe identity for a non-function global used by a verifier."""
        if isinstance(value, type):
            # A transcript verifier often uses standard-library types such as
            # ``pathlib.Path``.  Requiring a full member-by-member class graph
            # for those types turns an otherwise durable verifier process-local
            # simply because a standard-library base is implemented in C or
            # exposes a descriptor we cannot walk.  The type identity itself is
            # sufficient for this boundary: replacing ``VerifierVerdict`` (or
            # any other imported type) changes the opaque digest, while raw
            # module/class labels never enter authority JSON.
            return {"mode": "type", "identity_digest": _opaque_type_identity_digest(value)}
        if isinstance(value, re.Pattern):
            return {
                "mode": "regex",
                "identity_digest": _sha256(f"{value.pattern}\x00{value.flags}"),
            }
        if inspect.ismodule(value):
            module_name = getattr(value, "__name__", None)
            source_path = getattr(value, "__file__", None)
            digest = _file_content_digest(source_path) if isinstance(source_path, str) else None
            if not isinstance(module_name, str) or not module_name or digest is None:
                raise ValueError("runtime transcript verifier module is not durable")
            return {
                "mode": "module",
                "identity_digest": _sha256(
                    _canonical_json(
                        {"name": module_name, "content_digest": digest},
                        field="runtime transcript verifier module",
                    )
                ),
            }

        if isinstance(value, (set, frozenset)):
            if len(value) > _MAX_IDENTITY_ITEMS:
                raise ValueError("runtime transcript verifier global is oversized")
            projected_items = [
                _project_explicit_identity(
                    item,
                    field="runtime transcript verifier global item",
                )
                for item in value
            ]
            projected: object = {
                "items": sorted(
                    projected_items,
                    key=lambda item: _canonical_json(
                        item,
                        field="runtime transcript verifier global item",
                    ),
                )
            }
        else:
            projected = {"value": value}
        return {
            "mode": "data",
            "identity_digest": _opaque_callable_data_digest(
                projected,
                field="runtime transcript verifier global",
            ),
        }

    def collect(value: object, *, depth: int) -> dict[str, object]:
        nonlocal function_count
        if depth > _MAX_IDENTITY_DEPTH:
            raise ValueError("runtime transcript verifier exceeds dependency depth")
        if inspect.ismethod(value):
            function = value.__func__
        elif inspect.isfunction(value):
            function = value
        else:
            raise ValueError("runtime transcript verifier has a non-Python entrypoint")
        function_id = id(function)
        if function_id in active:
            implementation = _callable_implementation_contract(function)
            if implementation.get("stability") != "durable":
                raise ValueError("runtime transcript verifier implementation is not durable")
            return {
                "mode": "recursive_reference",
                "implementation": implementation,
            }
        function_count += 1
        if function_count > _MAX_IDENTITY_ITEMS:
            raise ValueError("runtime transcript verifier has too many dependencies")
        defaults_digest = _opaque_callable_data_digest(
            [
                list(function.__defaults__ or ()),
                list((function.__kwdefaults__ or {}).values()),
            ],
            field="runtime transcript verifier defaults",
        )

        active.add(function_id)
        try:
            implementation = _callable_implementation_contract(function)
            if implementation.get("stability") != "durable":
                raise ValueError("runtime transcript verifier implementation is not durable")
            dependencies: list[dict[str, object]] = []
            missing = object()
            for global_name in sorted(set(function.__code__.co_names)):
                dependency = function.__globals__.get(global_name, missing)
                if dependency is missing:
                    continue
                if inspect.ismethod(dependency) or inspect.isfunction(dependency):
                    dependencies.append(collect(dependency, depth=depth + 1))
                else:
                    dependencies.append(global_contract(dependency))
            return {
                "implementation": implementation,
                "defaults_digest": defaults_digest,
                "dependencies": dependencies,
            }
        finally:
            active.remove(function_id)

    try:
        return {
            "stability": "durable",
            "graph": collect(target, depth=0),
        }
    except Exception:
        return {
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(target),
            "reason": _IDENTITY_UNAVAILABLE_REASON,
        }


def _qualified_type(value: object) -> str:
    """Return an opaque type identity suitable for canonical authority JSON."""
    return _opaque_type_identity_digest(value)


def _bounded_regular_file_identity(
    path: str | os.PathLike[str],
) -> tuple[str, os.stat_result] | None:
    """Hash a bounded regular file without opening pipes or device nodes."""
    try:
        initial = os.stat(path)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > _MAX_IDENTITY_FILE_BYTES:
            return None
        digest = hashlib.sha256()
        total = 0
        with Path(path).open("rb") as source_file:
            opened = os.fstat(source_file.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > _MAX_IDENTITY_FILE_BYTES
                or (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino)
            ):
                return None
            while chunk := source_file.read(min(1024 * 1024, _MAX_IDENTITY_FILE_BYTES - total + 1)):
                total += len(chunk)
                if total > _MAX_IDENTITY_FILE_BYTES:
                    return None
                digest.update(chunk)
            final = os.fstat(source_file.fileno())
            if (
                final.st_dev,
                final.st_ino,
                final.st_size,
                final.st_mtime_ns,
            ) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ):
                return None
    except OSError:
        return None
    return "sha256:" + digest.hexdigest(), opened


def _file_content_digest(path: str | os.PathLike[str]) -> str | None:
    identity = _bounded_regular_file_identity(path)
    return identity[0] if identity is not None else None


def _resolved_executable(value: object, *, cwd: str | None) -> dict[str, object] | None:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value:
        return None
    expanded = os.path.expanduser(value)
    has_separator = os.sep in expanded or os.altsep is not None and os.altsep in expanded
    if not has_separator:
        search_path = os.environ.get("PATH", os.defpath)
        if cwd is not None:
            search_path = os.pathsep.join(
                entry if os.path.isabs(entry) else os.path.join(cwd, entry)
                for entry in search_path.split(os.pathsep)
            )
        resolved = shutil.which(expanded, path=search_path)
        realpath = os.path.realpath(resolved) if resolved is not None else None
    else:
        if not os.path.isabs(expanded) and cwd is not None:
            expanded = os.path.join(cwd, expanded)
        realpath = os.path.realpath(expanded)
    generation: dict[str, int] | None = None
    content_digest: str | None = None
    executable: bool | None = None
    if realpath is not None:
        identity = _bounded_regular_file_identity(realpath)
        if identity is not None:
            content_digest, file_stat = identity
            generation = {
                "device": file_stat.st_dev,
                "inode": file_stat.st_ino,
                "size": file_stat.st_size,
                "mtime_ns": file_stat.st_mtime_ns,
                "mode": stat.S_IMODE(file_stat.st_mode),
            }
            executable = bool(generation["mode"] & 0o111) and os.access(realpath, os.X_OK)
    return {
        # Paths can be provider-configured and may themselves contain a token
        # (for example a signed download location).  They identify executable
        # selection through stable digests without becoming authority payload.
        "path_digest": _sha256(value),
        "realpath_digest": _sha256(realpath) if realpath is not None else None,
        "generation": generation,
        "content_digest": content_digest,
        "executable": executable,
    }


def _runtime_executable_contract(adapter: object) -> dict[str, object]:
    """Bind subprocess executables, launchers, and delegated command policy."""
    try:
        cwd_value = getattr(adapter, "working_directory", None) or getattr(adapter, "_cwd", None)
        cwd = cwd_value if isinstance(cwd_value, str) and cwd_value else None
        declared_descriptor = inspect.getattr_static(
            type(adapter),
            "executable_identity_contract",
            None,
        )
        command_policy: dict[str, Any] | None = None
        executable: dict[str, object] | None = None
        launcher: dict[str, object] | None = None
        if declared_descriptor is not None:
            declared_provider = object.__getattribute__(adapter, "executable_identity_contract")
            declared = declared_provider()
            if not isinstance(declared, Mapping):
                raise ValueError("runtime executable identity is not a mapping")
            declared_identity = _canonical_explicit_identity(
                dict(declared),
                field="runtime executable identity",
            )
            if not set(declared_identity) <= {"executable", "launcher", "command_policy"}:
                raise ValueError("runtime executable identity contains unknown fields")
            executable = _resolved_executable(declared_identity.get("executable"), cwd=cwd)
            launcher = _resolved_executable(declared_identity.get("launcher"), cwd=cwd)
            raw_policy = declared_identity.get("command_policy")
            if raw_policy is not None:
                command_policy = _canonical_explicit_identity(
                    raw_policy,
                    field="runtime command policy",
                )
        else:
            executable = _resolved_executable(
                getattr(adapter, "cli_path", None) or getattr(adapter, "_cli_path", None),
                cwd=cwd,
            )
            launcher = _resolved_executable(
                getattr(adapter, "_electron_node_path", None),
                cwd=cwd,
            )
        required = executable is not None or launcher is not None or command_policy is not None
        observed = not required or all(
            item is None
            or item.get("realpath_digest") is not None
            and item.get("generation") is not None
            and item.get("content_digest") is not None
            and item.get("executable") is True
            for item in (executable, launcher)
        )
        return {
            "required": required,
            "observed": observed,
            "executable": executable,
            "launcher": launcher,
            "command_policy_digest": (
                _identity_digest(command_policy, field="runtime command policy")
                if command_policy is not None
                else None
            ),
        }
    except Exception:
        # Attribute/property failures are provider-controlled input. Make the
        # baseline non-portable without exposing an error message *or* a custom
        # exception type/qualname in canonical authority data.
        return {
            "required": True,
            "observed": False,
            "executable": None,
            "launcher": None,
            "command_policy_digest": None,
            "instance_nonce": _process_local_runtime_nonce(adapter),
            "reason": _IDENTITY_UNAVAILABLE_REASON,
        }


def _runtime_watchdog_contract(adapter: object) -> dict[str, object]:
    provider_descriptor = inspect.getattr_static(
        type(adapter),
        "watchdog_identity_contract",
        None,
    )
    if provider_descriptor is not None:
        try:
            provider = object.__getattribute__(adapter, "watchdog_identity_contract")
            value = provider()
            if not isinstance(value, Mapping):
                raise ValueError("runtime watchdog identity is not a mapping")
            identity = _canonical_explicit_identity(value, field="watchdog identity")
            if not identity:
                raise ValueError("runtime watchdog identity is empty")
        except Exception:
            return {
                "required": True,
                "observed": False,
                "instance_nonce": _process_local_runtime_nonce(adapter),
                "reason": _IDENTITY_UNAVAILABLE_REASON,
            }
        return {
            "required": True,
            "observed": True,
            "identity_digest": _identity_digest(identity, field="watchdog identity"),
        }
    fields = (
        "_startup_output_timeout_seconds",
        "_stdout_idle_timeout_seconds",
        "_process_shutdown_timeout_seconds",
        "_completed_process_group_shutdown_timeout_seconds",
        "_max_resume_retries",
        "_max_stderr_lines",
        "_use_process_group",
        "_child_session_env_keys",
    )
    try:
        missing = object()
        values: dict[str, object] = {}
        for field_name in fields:
            value = inspect.getattr_static(adapter, field_name, missing)
            if value is not missing:
                # This records environment variable *names*, not their values. Do
                # not use a structural key containing ``key``: generic sensitive
                # field screening intentionally treats that word as credential-like.
                identity_name = (
                    "child_session_environment_names"
                    if field_name == "_child_session_env_keys"
                    else field_name.removeprefix("_")
                )
                values[identity_name] = object.__getattribute__(
                    adapter,
                    field_name,
                )
        if not values:
            return {"required": False, "observed": False, "identity_digest": None}
        identity = _canonical_explicit_identity(values, field="watchdog identity")
    except Exception:
        return {
            "required": True,
            "observed": False,
            "instance_nonce": _process_local_runtime_nonce(adapter),
            "reason": _IDENTITY_UNAVAILABLE_REASON,
        }
    return {
        "required": True,
        "observed": True,
        "identity_digest": _identity_digest(identity, field="watchdog identity"),
    }


def _runtime_skill_dispatcher_contract(adapter: object) -> dict[str, object]:
    """Return skill-dispatch identity without surfacing provider getter errors."""
    try:
        return _runtime_skill_dispatcher_contract_impl(adapter)
    except Exception:
        return {
            "mode": "unobserved",
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(adapter),
            "reason": _IDENTITY_UNAVAILABLE_REASON,
        }


def _runtime_skill_dispatcher_contract_impl(adapter: object) -> dict[str, object]:
    interceptor_descriptor = inspect.getattr_static(adapter, "_interceptor", None)
    if interceptor_descriptor is not None:
        try:
            interceptor = object.__getattribute__(adapter, "_interceptor")
            component = _declared_component_contract(interceptor)
        except Exception:
            return {
                "mode": "delegated",
                "stability": "process_local",
                "instance_nonce": _process_local_runtime_nonce(interceptor),
                "reason": _IDENTITY_UNAVAILABLE_REASON,
            }
        return {
            "mode": "delegated",
            "stability": component.get("stability", "process_local"),
            "component": component,
        }

    dispatcher_descriptor = inspect.getattr_static(adapter, "_skill_dispatcher", None)
    dispatcher = (
        object.__getattribute__(adapter, "_skill_dispatcher")
        if dispatcher_descriptor is not None
        else None
    )
    local_dispatch_descriptor = inspect.getattr_static(
        type(adapter),
        "_maybe_dispatch_skill_intercept",
        None,
    )
    if dispatcher is None and local_dispatch_descriptor is None:
        return {"mode": "none", "stability": "durable"}

    if dispatcher is None:
        local_entrypoint = inspect.getattr_static(
            type(adapter),
            "_dispatch_skill_intercept_locally",
            None,
        )
        return {
            "mode": "local_fallback",
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(adapter),
            "resolver_implementation": _callable_implementation_contract(local_dispatch_descriptor),
            "dispatcher_implementation": (
                _callable_implementation_contract(local_entrypoint)
                if local_entrypoint is not None
                else None
            ),
        }

    implementation = _callable_entrypoint_contract(dispatcher)
    try:
        identity_provider = getattr(dispatcher, "execution_identity_contract", None)
    except Exception:
        return {
            "mode": "custom",
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(dispatcher),
            "reason": _IDENTITY_UNAVAILABLE_REASON,
            "implementation": implementation,
        }
    if callable(identity_provider):
        try:
            identity = identity_provider()
            if not isinstance(identity, Mapping):
                raise ValueError("skill dispatcher identity is not a mapping")
            normalized = _canonical_explicit_identity(
                dict(identity),
                field="skill dispatcher identity",
            )
            encoded = _canonical_json(normalized, field="skill dispatcher identity")
        except Exception:
            return {
                "mode": "custom",
                "stability": "process_local",
                "instance_nonce": _process_local_runtime_nonce(dispatcher),
                "reason": _IDENTITY_UNAVAILABLE_REASON,
                "implementation": implementation,
            }
        if implementation.get("stability") == "durable" and local_dispatch_descriptor is None:
            return {
                "mode": "custom",
                "stability": "durable",
                "identity_digest": _sha256(encoded),
                "implementation": implementation,
            }
    return {
        "mode": "custom",
        "stability": "process_local",
        "instance_nonce": _process_local_runtime_nonce(dispatcher),
        "implementation": implementation,
    }


def _class_implementation_contract_for_type(runtime_type: type[object]) -> dict[str, object]:
    """Bind one class hierarchy without inspecting mutable instance state."""
    classes: list[str] = []
    members: dict[str, object] = {}
    modules: dict[str, object] = {}
    durable = True
    for runtime_class in runtime_type.__mro__:
        if runtime_class is object:
            continue
        class_identity = _opaque_type_identity_digest(runtime_class)
        classes.append(class_identity)
        if "<locals>" in runtime_class.__qualname__:
            durable = False
        class_members: dict[str, object] = {}
        for member_name, raw_member in vars(runtime_class).items():
            targets: tuple[object, ...]
            if isinstance(raw_member, (classmethod, staticmethod)):
                targets = (raw_member.__func__,)
            elif isinstance(raw_member, property):
                targets = tuple(
                    target
                    for target in (raw_member.fget, raw_member.fset, raw_member.fdel)
                    if target is not None
                )
            elif inspect.isfunction(raw_member):
                targets = (raw_member,)
            else:
                continue
            for index, target in enumerate(targets):
                member_key = _sha256(
                    _canonical_json(
                        [member_name, index],
                        field="runtime class member identity",
                    )
                )
                member_contract = _callable_implementation_contract(target)
                class_members[member_key] = member_contract
                durable = durable and member_contract.get("stability") == "durable"
        members[class_identity] = {
            "observed": bool(class_members),
            "content_digest": _sha256(
                _canonical_json(
                    class_members,
                    field="runtime class members",
                )
            ),
        }
        try:
            source_path = inspect.getsourcefile(runtime_class)
        except (OSError, TypeError):
            source_path = None
        if source_path is None:
            durable = False
            modules[class_identity] = {"observed": False}
            continue
        try:
            realpath = str(Path(source_path).resolve(strict=False))
        except (OSError, RuntimeError):
            durable = False
            modules[class_identity] = {"observed": False}
            continue
        digest = _file_content_digest(realpath)
        if digest is None:
            durable = False
            modules[class_identity] = {"observed": False}
            continue
        modules[class_identity] = {
            "observed": True,
            "path_digest": _sha256(realpath),
            "content_digest": digest,
        }
    return {
        "stability": "durable" if durable and classes else "process_local",
        "classes": classes,
        "members": members,
        "modules": modules,
    }


def _class_implementation_contract(adapter: object) -> dict[str, object]:
    """Bind one object's class hierarchy without inspecting its instance state."""
    return _class_implementation_contract_for_type(type(adapter))


def _safe_class_implementation_contract(value: object) -> dict[str, object]:
    try:
        return _class_implementation_contract(value)
    except Exception:
        return {
            "stability": "process_local",
            "observed": False,
            "reason": _IDENTITY_UNAVAILABLE_REASON,
        }


def _instance_declared_method_overrides(value: object) -> dict[str, object]:
    """Bind only instance state that shadows executable class members."""
    try:
        instance_state = vars(value)
    except TypeError:
        return {}
    missing = object()
    overrides: dict[str, object] = {}
    for name, item in instance_state.items():
        if not isinstance(name, str) or not name:
            continue
        class_member = inspect.getattr_static(type(value), name, missing)
        if class_member is missing or not (
            callable(class_member) or isinstance(class_member, (classmethod, staticmethod))
        ):
            continue
        if callable(item):
            overrides[name] = {
                "mode": "callable",
                "implementation": _callable_leaf_implementation_contract(item),
            }
        else:
            overrides[name] = {"mode": "non_callable", "type": _qualified_type(item)}
    return overrides


def _static_executor_component_contract(component: type[object]) -> dict[str, object]:
    """Bind a class-selected execution component without constructing it."""
    try:
        implementation = _class_implementation_contract_for_type(component)
    except Exception:
        return {
            "mode": "static_type",
            "stability": "process_local",
            "type": _opaque_type_identity_digest(component),
            "instance_nonce": _process_local_runtime_nonce(component),
            "reason": _IDENTITY_UNAVAILABLE_REASON,
        }
    durable = implementation.get("stability") == "durable"
    contract: dict[str, object] = {
        "mode": "static_type",
        "stability": "durable" if durable else "process_local",
        "type": _opaque_type_identity_digest(component),
        "implementation": implementation,
    }
    if not durable:
        contract["instance_nonce"] = _process_local_runtime_nonce(component)
    return contract


def _static_executor_callable_contract(component: object) -> dict[str, object]:
    """Bind a captured static helper used directly by executor behavior."""
    implementation = _callable_implementation_contract(component)
    durable = implementation.get("stability") == "durable"
    contract: dict[str, object] = {
        "mode": "static_callable",
        "stability": "durable" if durable else "process_local",
        "implementation": implementation,
    }
    if not durable:
        contract["instance_nonce"] = _process_local_runtime_nonce(component)
    return contract


def _out_of_boundary_executor_component_contract(
    component: object,
    *,
    name: str,
) -> dict[str, object]:
    """Fail closed for a collaborator outside Foundation A's allowlist.

    ``execution_identity_contract()`` on a live object must never upgrade it
    into a portable component.  In particular, signal hubs, session caches, and
    recovery managers have mutable state whose authority belongs to later
    capsule/event layers.
    """
    category = (
        "volatile"
        if name in {"session_signal_hub", "execution_event_emitter", "event_store"}
        else "per_attempt_capsule"
    )
    return {
        "mode": "out_of_boundary",
        "boundary": category,
        "stability": "process_local",
        "type": _qualified_type(component) if component is not None else None,
        "instance_nonce": _process_local_runtime_nonce(component)
        if component is not None
        else _process_local_runtime_nonce(_UNBOUND_EXECUTOR_NONCE_OWNER),
    }


def _executor_component_contract(name: str, component: object) -> dict[str, object]:
    """Describe one explicitly allowed portable executor component.

    Foundation A intentionally has one executor subcomponent: the static leaf
    dispatcher implementation.  The coordinator, runtime-handle manager,
    signal hub, event emitter, stores, queues, and live handles are named in
    :func:`execution_authority_boundary_contract` and are not inferred here.
    """
    if name not in _PORTABLE_EXECUTOR_COMPONENTS:
        return _out_of_boundary_executor_component_contract(component, name=name)
    if component is None:
        return {"mode": "none", "stability": "durable"}
    if isinstance(component, type):
        return _static_executor_component_contract(component)
    if inspect.ismethod(component) or inspect.isfunction(component) or inspect.isbuiltin(component):
        return _static_executor_callable_contract(component)
    return {
        "mode": "invalid_portable_component",
        "stability": "process_local",
        "type": _qualified_type(component),
        "instance_nonce": _process_local_runtime_nonce(component),
    }


def _executor_component_contracts(
    components: Mapping[str, object] | None,
) -> dict[str, object]:
    if components is None:
        return {}
    try:
        if len(components) > _MAX_IDENTITY_ITEMS or not all(
            isinstance(name, str) and name for name in components
        ):
            raise ValueError("executor components are invalid")
        return {
            name: _executor_component_contract(name, component)
            for name, component in components.items()
        }
    except Exception:
        return {
            "unobserved": {
                "mode": "process_local",
                "stability": "process_local",
                "instance_nonce": _process_local_runtime_nonce(components),
                "reason": _IDENTITY_UNAVAILABLE_REASON,
            }
        }


def _executor_implementation_contract(
    executor: object | None,
    *,
    components: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Bind the concrete effect-owning executor or fail closed when absent."""
    component_contracts = _executor_component_contracts(components)
    if executor is None:
        return {
            "version": 3,
            "mode": "unbound",
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(_UNBOUND_EXECUTOR_NONCE_OWNER),
            "components": component_contracts,
        }
    implementation = _safe_class_implementation_contract(executor)
    instance_overrides = _instance_declared_method_overrides(executor)
    durable = (
        implementation.get("stability") == "durable"
        and not instance_overrides
        and all(
            isinstance(component, Mapping) and component.get("stability") == "durable"
            for component in component_contracts.values()
        )
    )
    contract: dict[str, object] = {
        "version": 3,
        "mode": "bound",
        "type": _qualified_type(executor),
        "stability": "durable" if durable else "process_local",
        "implementation": implementation,
        "instance_overrides": instance_overrides,
        "components": component_contracts,
    }
    if not durable:
        contract["instance_nonce"] = _process_local_runtime_nonce(executor)
    return contract


def _reject_sensitive_identity_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if is_sensitive_field(key):
                raise ValueError("component identity contains sensitive data")
            _reject_sensitive_identity_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_identity_fields(item)
    elif is_sensitive_value(value):
        raise ValueError("component identity contains sensitive data")


def _canonical_explicit_identity(
    value: object,
    *,
    field: str,
) -> dict[str, Any]:
    """Bound, sanitize, and canonicalize one provider-supplied identity mapping."""
    projected = _project_explicit_identity(value, field=field)
    _reject_sensitive_identity_fields(projected)
    normalized = _canonical_object(projected, field=field)
    encoded = _canonical_json(normalized, field=field)
    if len(encoded) > _MAX_IDENTITY_JSON_CHARS:
        raise ValueError(f"{field} exceeds its budget")
    return normalized


def _instance_executable_overrides(value: object) -> dict[str, object]:
    try:
        instance_state = vars(value)
    except TypeError:
        return {}
    missing = object()
    overrides: dict[str, object] = {}
    for name, item in instance_state.items():
        if not isinstance(name, str) or not name:
            continue
        if callable(item):
            overrides[name] = {
                "mode": "callable",
                "implementation": _callable_leaf_implementation_contract(item),
            }
            continue
        class_member = inspect.getattr_static(type(value), name, missing)
        if class_member is missing or not (
            callable(class_member) or isinstance(class_member, (classmethod, staticmethod))
        ):
            continue
        overrides[name] = {"mode": "non_callable", "type": _qualified_type(item)}
    return overrides


def _declared_component_contract(value: object) -> dict[str, object]:
    implementation = _safe_class_implementation_contract(value)
    try:
        executable = _runtime_executable_contract(value)
    except Exception:
        executable = {
            "required": True,
            "observed": False,
            "reason": _IDENTITY_UNAVAILABLE_REASON,
        }
    provider_descriptor = inspect.getattr_static(
        type(value),
        "execution_identity_contract",
        None,
    )
    instance_overrides = _instance_executable_overrides(value)
    if provider_descriptor is None:
        return {
            "mode": "process_local",
            "stability": "process_local",
            "type": _qualified_type(value),
            "instance_nonce": _process_local_runtime_nonce(value),
            "reason": "identity_not_declared",
            "implementation": implementation,
            "executable": executable,
        }
    try:
        provider = object.__getattribute__(value, "execution_identity_contract")
        identity = provider()
        if not isinstance(identity, Mapping):
            raise ValueError("component identity is not a mapping")
        projected = _canonical_explicit_identity(
            dict(identity),
            field="component execution identity",
        )
        if not isinstance(projected, dict) or set(projected) != {"version", "configuration"}:
            raise ValueError("component identity has an invalid envelope")
        version = projected.get("version")
        configuration = projected.get("configuration")
        if (
            isinstance(version, bool)
            or version != 1
            or not isinstance(configuration, dict)
            or not configuration
        ):
            raise ValueError("component identity has an invalid version or configuration")
        encoded = _canonical_json(projected, field="component execution identity")
    except Exception:
        return {
            "mode": "process_local",
            "stability": "process_local",
            "type": _qualified_type(value),
            "instance_nonce": _process_local_runtime_nonce(value),
            "reason": _IDENTITY_UNAVAILABLE_REASON,
            "implementation": implementation,
            "executable": executable,
        }
    durable = (
        not instance_overrides
        and implementation.get("stability") == "durable"
        and (executable.get("required") is not True or executable.get("observed") is True)
    )
    contract: dict[str, object] = {
        "mode": "declared",
        "stability": "durable" if durable else "process_local",
        "type": _qualified_type(value),
        "identity_digest": _sha256(encoded),
        "instance_overrides": instance_overrides,
        "implementation": implementation,
        "executable": executable,
    }
    if not durable:
        contract["instance_nonce"] = _process_local_runtime_nonce(value)
    return contract


def _runtime_composition_contract(adapter: object) -> dict[str, object]:
    provider_descriptor = inspect.getattr_static(
        type(adapter),
        "execution_components",
        None,
    )
    if provider_descriptor is None:
        return {"version": 1, "mode": "none", "stability": "durable", "components": {}}
    try:
        provider = object.__getattribute__(adapter, "execution_components")
        components = provider()
        if not isinstance(components, Mapping) or len(components) > _MAX_IDENTITY_ITEMS:
            raise ValueError("runtime execution components are invalid")
        if not all(
            isinstance(name, str) and name and not is_sensitive_value(name) for name in components
        ):
            raise ValueError("runtime execution component names are invalid")
        contracts = {
            name: _declared_component_contract(component) for name, component in components.items()
        }
    except Exception:
        return {
            "version": 1,
            "mode": "declared",
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(adapter),
            "reason": _IDENTITY_UNAVAILABLE_REASON,
            "components": {},
        }
    durable = all(contract.get("stability") == "durable" for contract in contracts.values())
    result: dict[str, object] = {
        "version": 1,
        "mode": "declared",
        "stability": "durable" if durable else "process_local",
        "components": contracts,
    }
    if not durable:
        result["instance_nonce"] = _process_local_runtime_nonce(adapter)
    return result


def _runtime_implementation_contract(adapter: object) -> dict[str, object]:
    """Bind runtime code plus explicitly owned execution components."""
    class_contract = _class_implementation_contract(adapter)
    instance_overrides = _instance_executable_overrides(adapter)
    durable = class_contract.get("stability") == "durable"
    try:
        vars(adapter)
    except TypeError:
        durable = False
        instance_state_observed = False
    else:
        instance_state_observed = True
    if instance_overrides:
        durable = False
    composition = _runtime_composition_contract(adapter)
    durable = durable and composition.get("stability") == "durable"
    contract: dict[str, object] = {
        **class_contract,
        "stability": "durable" if durable else "process_local",
        "instance_overrides": instance_overrides,
        "instance_state_observed": instance_state_observed,
        "composition": composition,
    }
    if contract["stability"] == "process_local":
        contract["instance_nonce"] = _process_local_runtime_nonce(adapter)
    return contract


def _runtime_handle_selector_contract(
    adapter: object,
    runtime_handle: RuntimeHandle | None,
) -> dict[str, object]:
    """Bind selector *implementation*, never a selected attempt handle.

    A concrete ``RuntimeHandle`` is capsule/recovery state.  Foundation A may
    describe the adapter method that will select one, but must not fold a
    checkpoint-selected handle or its metadata into the portable baseline.
    """
    del runtime_handle
    provider_descriptor = inspect.getattr_static(
        type(adapter),
        "resume_handle_execution_identity_contract",
        None,
    )
    if provider_descriptor is None:
        return {
            "version": 1,
            "mode": "none",
            "stability": "durable",
        }
    try:
        provider = object.__getattribute__(
            adapter,
            "resume_handle_execution_identity_contract",
        )
        if not callable(provider):
            raise ValueError("runtime handle selector identity is not callable")
        implementation = _callable_entrypoint_contract(provider)
    except Exception:
        return {
            "version": 1,
            "mode": "declared",
            "stability": "process_local",
            "instance_nonce": _process_local_runtime_nonce(adapter),
        }
    result: dict[str, object] = {
        "version": 1,
        "mode": "declared",
        "stability": (
            "durable" if implementation.get("stability") == "durable" else "process_local"
        ),
        "implementation": implementation,
    }
    if result["stability"] != "durable":
        result["instance_nonce"] = _process_local_runtime_nonce(adapter)
    return result


def runtime_authority_contract(
    adapter: object,
    *,
    resolved_routing: ResolvedRuntimeAuthority | None = None,
    runtime_handle: RuntimeHandle | None = None,
) -> dict[str, object]:
    """Combine generic capabilities with one already-resolved runtime identity."""
    if resolved_routing is None:
        runtime_backend, runtime_backend_observed = _runtime_label(adapter, "runtime_backend")
        llm_backend, llm_backend_observed = _runtime_label(adapter, "llm_backend")
        constructor_model = constructor_model_contract(adapter)
        execution_identity = runtime_execution_identity_contract(adapter)
        permission_contract = runtime_permission_mode_contract(adapter)
    else:
        resolved_routing.require_adapter(adapter)
        resolved_data = resolved_routing.data
        runtime_backend = resolved_data.get("runtime_backend")
        llm_backend = resolved_data.get("llm_backend")
        runtime_backend_observed = resolved_data.get("runtime_backend_unobserved") is not True
        llm_backend_observed = resolved_data.get("llm_backend_unobserved") is not True
        constructor_model = _canonical_object(
            resolved_data.get("constructor_model"),
            field="resolved constructor model",
        )
        execution_identity = _canonical_object(
            resolved_data.get("runtime_execution"),
            field="resolved runtime execution identity",
        )
        permission_contract = _canonical_object(
            resolved_data.get("permission_mode"),
            field="resolved permission mode",
        )
    result: dict[str, object] = {
        "version": 1,
        "runtime_backend": runtime_backend if isinstance(runtime_backend, str) else None,
        "self_governs_rate_limit": bool(
            _safe_runtime_attribute(adapter, "self_governs_rate_limit", False)
        ),
        "llm_backend": llm_backend if isinstance(llm_backend, str) else None,
        "permission_mode": permission_contract,
        "constructor_model": constructor_model,
        "execution_identity": _authority_runtime_execution_identity(
            execution_identity,
            adapter=adapter,
        ),
        "capabilities": _runtime_capabilities_contract(adapter),
        "implementation": _runtime_implementation_contract(adapter),
        "executable": _runtime_executable_contract(adapter),
        "watchdog": _runtime_watchdog_contract(adapter),
        "skill_dispatcher": _runtime_skill_dispatcher_contract(adapter),
        "handle_selector": _runtime_handle_selector_contract(adapter, runtime_handle),
    }
    if not runtime_backend_observed:
        result["runtime_backend_unobserved"] = True
    if not llm_backend_observed:
        result["llm_backend_unobserved"] = True
    return result


def _project_explicit_identity(
    value: object,
    *,
    field: str,
    depth: int = 0,
    seen: set[int] | None = None,
) -> object:
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


def _verifier_implementation_contract(verifier: Verifier) -> dict[str, object]:
    return _callable_entrypoint_contract(verifier)


def verifier_authority_contract(
    verifier: Verifier | None,
    *,
    runtime_transcript_verifier: object | None = None,
) -> dict[str, object]:
    """Return durable verifier identity only when it is explicitly declared."""
    transcript_implementation = _callable_dependency_implementation_contract(
        runtime_transcript_verifier or _verify_atomic_evidence_against_runtime_messages
    )
    if verifier is None:
        return {
            "version": 1,
            "mode": "runtime_transcript",
            "implementation": transcript_implementation,
            "behavioral_state": {"stability": "durable", "protocol_version": 1},
        }

    implementation = _verifier_implementation_contract(verifier)
    identity_descriptor = inspect.getattr_static(
        verifier,
        "verification_identity_contract",
        None,
    )
    if identity_descriptor is None:
        behavioral_state: dict[str, object] = {
            "stability": "process_local",
            # An undeclared custom verifier does not expose the state needed to
            # compare two independently-built baselines.  Deliberately issue a
            # fresh nonce for each contract rather than treating two snapshots
            # of the same callable object as reusable authority.
            "instance_nonce": secrets.token_hex(32),
            "reason": "verification_identity_contract is not declared",
        }
    else:
        try:
            identity_provider = object.__getattribute__(
                verifier,
                "verification_identity_contract",
            )
            if not callable(identity_provider):
                raise ValueError("verification_identity_contract is not callable")
            identity = identity_provider()
            if not isinstance(identity, Mapping):
                raise ValueError("verification_identity_contract is not a mapping")
            projected = _canonical_explicit_identity(
                dict(identity),
                field="verification identity contract",
            )
            encoded = _canonical_json(projected, field="verification identity contract")
            if len(encoded) > _MAX_IDENTITY_JSON_CHARS:
                raise ValueError("verification identity contract exceeds its budget")
            behavioral_state = {
                "stability": "durable",
                "identity_digest": _sha256(encoded),
            }
        except Exception:
            behavioral_state = {
                "stability": "process_local",
                "instance_nonce": _process_local_runtime_nonce(verifier),
                "reason": _IDENTITY_UNAVAILABLE_REASON,
            }
    return {
        "version": 1,
        "mode": "custom",
        "implementation": implementation,
        "runtime_transcript_implementation": transcript_implementation,
        "behavioral_state": behavioral_state,
    }


def _positive_int_or_none(value: object) -> bool:
    return value is None or (not isinstance(value, bool) and isinstance(value, int) and value > 0)


def _positive_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _valid_dispatch_rate_contract(value: object) -> bool:
    required_keys = {
        "version",
        "backend",
        "owner",
        "observed",
        "self_governs_rate_limit",
        "requests_per_minute",
        "tokens_per_minute",
        "gate_enabled",
        "window_seconds",
        "heartbeat_seconds",
        "max_wait_seconds",
        "token_estimation_version",
        "gate_algorithm_version",
        "gate_algorithm",
        "token_estimator",
    }
    if not isinstance(value, Mapping) or (
        set(value) != required_keys and set(value) != required_keys | {"backend_observed"}
    ):
        return False
    version = value.get("version")
    token_version = value.get("token_estimation_version")
    gate_algorithm_version = value.get("gate_algorithm_version")
    gate_algorithm = value.get("gate_algorithm")
    token_estimator = value.get("token_estimator")
    backend = value.get("backend")
    backend_observed = value.get("backend_observed", True)
    owner = value.get("owner")
    observed = value.get("observed")
    self_governs = value.get("self_governs_rate_limit")
    request_limit = value.get("requests_per_minute")
    token_limit = value.get("tokens_per_minute")
    gate_enabled = value.get("gate_enabled")
    if (
        isinstance(version, bool)
        or version != 1
        or isinstance(token_version, bool)
        or token_version != 1
        or isinstance(gate_algorithm_version, bool)
        or gate_algorithm_version != 1
        or not isinstance(backend, str)
        or not backend.strip()
        or not isinstance(backend_observed, bool)
        or owner not in {"ouroboros", "runtime"}
        or not isinstance(observed, bool)
        or not isinstance(self_governs, bool)
        or not isinstance(gate_enabled, bool)
        or not _positive_int_or_none(request_limit)
        or not _positive_int_or_none(token_limit)
        or not _positive_number(value.get("window_seconds"))
        or not _positive_number(value.get("heartbeat_seconds"))
        or not _positive_number(value.get("max_wait_seconds"))
    ):
        return False
    if not _valid_rate_helper_contract(gate_algorithm) or not _valid_rate_helper_contract(
        token_estimator
    ):
        return False
    if owner == "runtime":
        return (
            self_governs
            and not observed
            and request_limit is None
            and token_limit is None
            and not gate_enabled
        )
    return (
        not self_governs
        and observed
        and gate_enabled == (request_limit is not None or token_limit is not None)
    )


def _valid_rate_helper_contract(value: object) -> bool:
    """Validate an opaque captured gate/estimator implementation snapshot."""
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return False
    if value.get("observed") is True:
        return (
            set(value) == {"version", "observed", "identity_digest", "digest"}
            and isinstance(value.get("identity_digest"), str)
            and _SHA256_DIGEST_PATTERN.fullmatch(value["identity_digest"]) is not None
            and isinstance(value.get("digest"), str)
            and _SHA256_DIGEST_PATTERN.fullmatch(value["digest"]) is not None
        )
    if value.get("observed") is False:
        return (
            set(value) == {"version", "observed", "instance_nonce"}
            and isinstance(value.get("instance_nonce"), str)
            and bool(value["instance_nonce"])
        )
    return False


def _redacted_authority_policy_value(value: object) -> object:
    """Return an authority-only copy with credential-shaped values opaque.

    Execution policy continues to drive the live executor unchanged.  This
    projection exists solely for the immutable authority payload, where a model
    alias or free-form effort label must never become a credential egress path.
    The digest preserves a deterministic distinction between two secrets while
    keeping the surrounding schema (including model-routing strings) valid.
    """
    if isinstance(value, str):
        if is_sensitive_value(value):
            return "redacted:sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        return value
    if isinstance(value, Mapping):
        return {key: _redacted_authority_policy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted_authority_policy_value(item) for item in value]
    return value


def _authority_execution_policy_contract(
    execution_policy: Mapping[str, object],
    *,
    adapter: object,
) -> dict[str, object]:
    """Project a safe authority snapshot without changing live dispatch policy."""
    policy = _canonical_object(dict(execution_policy), field="execution policy")
    dispatch_rate = policy.get("dispatch_rate")
    if isinstance(dispatch_rate, Mapping):
        normalized_rate = dict(dispatch_rate)
        policy_backend = normalized_rate.get("backend")
        runtime_backend = _safe_runtime_attribute(adapter, "runtime_backend")
        if is_sensitive_value(policy_backend) or is_sensitive_value(runtime_backend):
            normalized_rate["backend"] = "unknown"
            normalized_rate["backend_observed"] = False
        policy["dispatch_rate"] = normalized_rate
    redacted = _redacted_authority_policy_value(policy)
    if not isinstance(redacted, dict):  # pragma: no cover - canonical object invariant
        raise ValueError("execution policy redaction did not produce an object")
    return redacted


def _contains_sensitive_policy_value(value: object) -> bool:
    """Return whether a would-be authority policy still carries a credential."""
    if isinstance(value, str):
        return is_sensitive_value(value)
    if isinstance(value, Mapping):
        return any(_contains_sensitive_policy_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_sensitive_policy_value(item) for item in value)
    return False


def valid_execution_policy_contract(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _EXECUTION_POLICY_KEYS:
        return False
    if _contains_sensitive_policy_value(value):
        return False
    version = value.get("version")
    depth = value.get("max_decomposition_depth")
    concurrency = value.get("max_concurrent")
    timeout = value.get("verify_command_timeout_seconds")
    retries = value.get("ac_retry_attempts")
    if (
        isinstance(version, bool)
        or version != _EXECUTION_POLICY_VERSION
        or value.get("decomposition_mode") not in {"preflight", "bounce_only", "off"}
        or isinstance(depth, bool)
        or not isinstance(depth, int)
        or depth < 0
        or isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency < 1
        or isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout < 1
        or isinstance(retries, bool)
        or not isinstance(retries, int)
        or retries < 0
    ):
        return False
    for flag_name in (
        "fat_harness_mode",
        "run_verify_commands",
        "cross_harness_redispatch",
        "shadow_replay_enabled",
    ):
        if not isinstance(value.get(flag_name), bool):
            return False
    reasoning_effort = value.get("reasoning_effort")
    if reasoning_effort is not None and (
        not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
    ):
        return False

    raw_profile = value.get("execution_profile")
    if raw_profile is not None:
        if not isinstance(raw_profile, Mapping):
            return False
        try:
            profile = ExecutionProfile.model_validate(dict(raw_profile))
        except ValueError:
            return False
        if profile.model_dump(mode="json") != dict(raw_profile):
            return False

    raw_routing = value.get("model_routing")
    recognized, router = deserialize_model_router(raw_routing)
    if not recognized or serialize_model_router(router) != raw_routing:
        return False
    return _valid_dispatch_rate_contract(value.get("dispatch_rate"))


def build_execution_policy_contract(
    *,
    decomposition_mode: str,
    max_decomposition_depth: int,
    max_concurrent: int,
    execution_profile: ExecutionProfile | None,
    fat_harness_mode: bool,
    run_verify_commands: bool,
    verify_command_timeout_seconds: int,
    ac_retry_attempts: int,
    reasoning_effort: str | None,
    model_router: ModelRouter | None,
    cross_harness_redispatch: bool,
    shadow_replay_enabled: bool,
    dispatch_rate_policy: Mapping[str, object],
) -> dict[str, object]:
    """Return the canonical *static* parallel-execution policy payload.

    ``reasoning_effort`` is the configured base policy for this executor.  The
    actual effort selected for an AC can vary with its investment assessment,
    decomposition role, and retry attempt; that selected value belongs to the
    Foundation-C attempt capsule and is intentionally absent from this baseline.
    """
    return {
        "version": _EXECUTION_POLICY_VERSION,
        "decomposition_mode": decomposition_mode,
        "max_decomposition_depth": max_decomposition_depth,
        "max_concurrent": max_concurrent,
        "execution_profile": (
            execution_profile.model_dump(mode="json") if execution_profile is not None else None
        ),
        "fat_harness_mode": fat_harness_mode,
        "run_verify_commands": run_verify_commands,
        "verify_command_timeout_seconds": verify_command_timeout_seconds,
        "ac_retry_attempts": ac_retry_attempts,
        "reasoning_effort": reasoning_effort,
        "model_routing": serialize_model_router(model_router),
        "cross_harness_redispatch": cross_harness_redispatch,
        "shadow_replay_enabled": shadow_replay_enabled,
        "dispatch_rate": _canonical_object(
            dict(dispatch_rate_policy),
            field="dispatch rate policy",
        ),
    }


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityContract:
    """Immutable executor-baseline contract and its stable fingerprint.

    This excludes per-attempt AC/prompt/tool inputs and a runtime handle selected
    later by checkpoint recovery. A later attempt capsule must compose those
    values with this baseline before granting reusable trust or acceptance.
    """

    canonical_json: str

    def __post_init__(self) -> None:
        data = _canonical_object(
            self.canonical_json and json.loads(self.canonical_json),
            field="execution authority contract",
        )
        if data.get("version") != EXECUTION_AUTHORITY_VERSION or set(data) != {
            "version",
            "boundary",
            "executor",
            "workspace",
            "runtime",
            "verifier",
            "execution_policy",
        }:
            raise ValueError("execution authority contract has an invalid shape")
        if not _valid_execution_authority_boundary(data.get("boundary")):
            raise ValueError("execution authority contract has an invalid boundary")
        if not valid_execution_policy_contract(data.get("execution_policy")):
            raise ValueError("execution authority contract has an invalid execution policy")
        runtime = data.get("runtime")
        execution_policy = data.get("execution_policy")
        runtime_backend = runtime.get("runtime_backend") if isinstance(runtime, Mapping) else None
        dispatch_rate = (
            execution_policy.get("dispatch_rate") if isinstance(execution_policy, Mapping) else None
        )
        dispatch_backend = (
            dispatch_rate.get("backend") if isinstance(dispatch_rate, Mapping) else None
        )
        dispatch_self_governs = (
            dispatch_rate.get("self_governs_rate_limit")
            if isinstance(dispatch_rate, Mapping)
            else None
        )
        runtime_self_governs = (
            runtime.get("self_governs_rate_limit") if isinstance(runtime, Mapping) else None
        )
        runtime_backend_unobserved = (
            isinstance(runtime, Mapping) and runtime.get("runtime_backend_unobserved") is True
        )
        dispatch_backend_observed = (
            isinstance(dispatch_rate, Mapping)
            and dispatch_rate.get("backend_observed", True) is True
        )
        expected_backend = (
            runtime_backend if isinstance(runtime_backend, str) and runtime_backend else "unknown"
        )
        if (
            not runtime_backend_unobserved
            and dispatch_backend_observed
            and dispatch_backend != expected_backend
        ):
            raise ValueError("dispatch rate policy disagrees with the runtime backend")
        if dispatch_self_governs is not runtime_self_governs:
            raise ValueError("dispatch rate policy disagrees with runtime self-governance")
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
        executor: object | None = None,
        executor_components: Mapping[str, object] | None = None,
        workspace_identity: Mapping[str, object] | None = None,
        workspace_generation: Mapping[str, object] | None = None,
        resolved_routing: ResolvedRuntimeAuthority | None = None,
        runtime_handle: RuntimeHandle | None = None,
        runtime_transcript_verifier: object | None = None,
    ) -> ExecutionAuthorityContract:
        data = {
            "version": EXECUTION_AUTHORITY_VERSION,
            "boundary": execution_authority_boundary_contract(),
            "executor": _executor_implementation_contract(
                executor,
                components=executor_components,
            ),
            "workspace": canonical_workspace_authority(
                workspace,
                identity=workspace_identity,
                generation=workspace_generation,
            ),
            "runtime": runtime_authority_contract(
                adapter,
                resolved_routing=resolved_routing,
                runtime_handle=runtime_handle,
            ),
            "verifier": verifier_authority_contract(
                verifier,
                runtime_transcript_verifier=runtime_transcript_verifier,
            ),
            "execution_policy": _authority_execution_policy_contract(
                execution_policy,
                adapter=adapter,
            ),
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
        """Return whether this baseline can be composed into a capsule elsewhere.

        Portability is an identity-stability property only. It never authorizes
        reuse of an attempt, result, checkpoint, trust verdict, or acceptance;
        those require a complete per-attempt capsule with the omitted inputs.
        """
        data = self.data
        if not _valid_execution_authority_boundary(data.get("boundary")):
            return False
        executor = data.get("executor")
        if not isinstance(executor, Mapping) or executor.get("stability") != "durable":
            return False
        workspace = data.get("workspace")
        if not isinstance(workspace, Mapping) or workspace.get("observed") is not True:
            return False
        generation = workspace.get("generation")
        if not isinstance(generation, Mapping) or generation.get("observed") is not True:
            return False
        if not _valid_workspace_generation_identity(generation.get("identity")):
            return False

        execution_policy = data.get("execution_policy")
        if not valid_execution_policy_contract(execution_policy):
            return False
        dispatch_rate = (
            execution_policy.get("dispatch_rate") if isinstance(execution_policy, Mapping) else None
        )
        if not isinstance(dispatch_rate, Mapping) or dispatch_rate.get("observed") is not True:
            return False
        if dispatch_rate.get("backend_observed", True) is not True:
            return False
        gate_algorithm = dispatch_rate.get("gate_algorithm")
        token_estimator = dispatch_rate.get("token_estimator")
        if (
            not isinstance(gate_algorithm, Mapping)
            or gate_algorithm.get("observed") is not True
            or not isinstance(token_estimator, Mapping)
            or token_estimator.get("observed") is not True
        ):
            return False

        runtime = data.get("runtime")
        if not isinstance(runtime, Mapping):
            return False
        runtime_backend = runtime.get("runtime_backend")
        if (
            runtime.get("runtime_backend_unobserved") is True
            or not isinstance(runtime_backend, str)
            or not runtime_backend
        ):
            return False
        if runtime.get("llm_backend_unobserved") is True:
            return False
        for field_name in ("permission_mode", "constructor_model", "execution_identity"):
            value = runtime.get(field_name)
            if not isinstance(value, Mapping) or value.get("observed") is not True:
                return False
        execution_identity = runtime.get("execution_identity")
        if (
            not _valid_authority_runtime_execution_identity(execution_identity)
            or not isinstance(execution_identity, Mapping)
            or execution_identity.get("effective_model_observed") is not True
        ):
            return False
        implementation = runtime.get("implementation")
        if not isinstance(implementation, Mapping) or implementation.get("stability") != "durable":
            return False
        executable = runtime.get("executable")
        if not isinstance(executable, Mapping):
            return False
        if executable.get("required") is True and executable.get("observed") is not True:
            return False
        watchdog = runtime.get("watchdog")
        if (
            not isinstance(watchdog, Mapping)
            or watchdog.get("required") is True
            and watchdog.get("observed") is not True
        ):
            return False
        if executable.get("required") is True and (watchdog.get("observed") is not True):
            return False
        skill_dispatcher = runtime.get("skill_dispatcher")
        if (
            not isinstance(skill_dispatcher, Mapping)
            or skill_dispatcher.get("stability") != "durable"
        ):
            return False
        handle_selector = runtime.get("handle_selector")
        if not isinstance(handle_selector, Mapping):
            return False
        if (
            handle_selector.get("mode") not in {"none", "declared"}
            or handle_selector.get("stability") != "durable"
        ):
            return False

        verifier = data.get("verifier")
        if not isinstance(verifier, Mapping):
            return False
        verifier_implementation = verifier.get("implementation")
        transcript_implementation = (
            verifier_implementation
            if verifier.get("mode") == "runtime_transcript"
            else verifier.get("runtime_transcript_implementation")
        )
        behavioral_state = verifier.get("behavioral_state")
        return (
            isinstance(verifier_implementation, Mapping)
            and verifier_implementation.get("stability") == "durable"
            and isinstance(transcript_implementation, Mapping)
            and transcript_implementation.get("stability") == "durable"
            and isinstance(behavioral_state, Mapping)
            and behavioral_state.get("stability") == "durable"
        )


__all__ = [
    "EXECUTION_AUTHORITY_VERSION",
    "EXECUTION_AUTHORITY_BOUNDARY_VERSION",
    "ExecutionAuthorityContract",
    "ResolvedRuntimeAuthority",
    "build_execution_policy_contract",
    "canonical_workspace_authority",
    "constructor_model_contract",
    "execution_authority_boundary_contract",
    "runtime_authority_contract",
    "runtime_execution_identity_contract",
    "runtime_execution_proves_effective_model",
    "runtime_routing_labels_contract",
    "valid_constructor_model_contract",
    "valid_execution_policy_contract",
    "valid_runtime_execution_identity_contract",
    "verifier_authority_contract",
]
