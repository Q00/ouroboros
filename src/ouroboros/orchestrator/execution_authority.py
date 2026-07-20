"""Canonical execution authority shared by routing, recovery, and verification.

This module owns identity only. It does not authorize a dispatch, persist a
checkpoint, or declare acceptance. Later runtime layers can depend on one stable
contract instead of deriving narrower fingerprints independently.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import inspect
import json
import marshal
import math
from pathlib import Path
from typing import Any
import uuid

from ouroboros.orchestrator.runtime_param_negotiation import runtime_capabilities_for
from ouroboros.orchestrator.verifier import Verifier

EXECUTION_AUTHORITY_VERSION = 1
_MAX_IDENTITY_DEPTH = 8
_MAX_IDENTITY_ITEMS = 256
_MAX_IDENTITY_SCALAR_CHARS = 8_192
_MAX_IDENTITY_JSON_CHARS = 64_000


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


def canonical_workspace_authority(workspace: str | None) -> dict[str, object]:
    """Return the exact checkout path that owns effects for this executor."""
    if not isinstance(workspace, str) or not workspace.strip():
        return {"version": 1, "observed": False}
    canonical = str(Path(workspace).expanduser().resolve(strict=False))
    return {
        "version": 1,
        "observed": True,
        "mode": "direct",
        "effective_cwd": canonical,
    }


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
    """Return backend-specific resolved identity without backend logic here."""
    provider_descriptor = inspect.getattr_static(
        type(adapter),
        "execution_identity_contract",
        None,
    )
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
    if (
        set(value) != {"version", "observed", "identity"}
        or not isinstance(identity, Mapping)
        or not identity
    ):
        return False
    try:
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


def runtime_authority_contract(adapter: object) -> dict[str, object]:
    """Combine generic capabilities with backend-owned resolved identity."""
    runtime_backend = getattr(adapter, "runtime_backend", None)
    llm_backend = getattr(adapter, "llm_backend", None)
    permission_mode = getattr(adapter, "permission_mode", None)
    return {
        "version": 1,
        "runtime_backend": (
            runtime_backend.strip()
            if isinstance(runtime_backend, str) and runtime_backend.strip()
            else None
        ),
        "llm_backend": (
            llm_backend.strip() if isinstance(llm_backend, str) and llm_backend.strip() else None
        ),
        "permission_mode": (
            {"observed": True, "mode": permission_mode.strip()}
            if isinstance(permission_mode, str) and permission_mode.strip()
            else {"observed": False}
        ),
        "constructor_model": constructor_model_contract(adapter),
        "execution_identity": runtime_execution_identity_contract(adapter),
        "capabilities": _runtime_capabilities_contract(adapter),
    }


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
    if inspect.ismethod(verifier):
        target = verifier.__func__
    elif inspect.isfunction(verifier) or inspect.isbuiltin(verifier):
        target = verifier
    else:
        target = type(verifier).__call__

    module = getattr(target, "__module__", type(verifier).__module__)
    qualname = getattr(target, "__qualname__", type(verifier).__qualname__)
    try:
        source_digest = _sha256(inspect.getsource(target))
    except (OSError, TypeError):
        source_digest = None
    code = getattr(target, "__code__", None)
    code_digest = (
        "sha256:" + hashlib.sha256(marshal.dumps(code)).hexdigest() if code is not None else None
    )
    return {
        "module": str(module),
        "qualname": str(qualname),
        "source_digest": source_digest,
        "code_digest": code_digest,
    }


def verifier_authority_contract(verifier: Verifier | None) -> dict[str, object]:
    """Return durable verifier identity only when it is explicitly declared."""
    if verifier is None:
        return {"version": 1, "mode": "runtime_transcript"}

    implementation = _verifier_implementation_contract(verifier)
    identity_descriptor = inspect.getattr_static(
        verifier,
        "verification_identity_contract",
        None,
    )
    if identity_descriptor is None:
        behavioral_state: dict[str, object] = {
            "stability": "process_local",
            "instance_nonce": uuid.uuid4().hex,
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
            projected = _project_explicit_identity(
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
        except (AttributeError, TypeError, ValueError) as exc:
            behavioral_state = {
                "stability": "process_local",
                "instance_nonce": uuid.uuid4().hex,
                "reason": str(exc),
            }
    return {
        "version": 1,
        "mode": "custom",
        "implementation": implementation,
        "behavioral_state": behavioral_state,
    }


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityContract:
    """Immutable canonical JSON contract and its stable fingerprint."""

    canonical_json: str

    @classmethod
    def build(
        cls,
        *,
        adapter: object,
        verifier: Verifier | None,
        workspace: str | None,
        execution_policy: Mapping[str, object],
    ) -> ExecutionAuthorityContract:
        data = {
            "version": EXECUTION_AUTHORITY_VERSION,
            "workspace": canonical_workspace_authority(workspace),
            "runtime": runtime_authority_contract(adapter),
            "verifier": verifier_authority_contract(verifier),
            "execution_policy": _canonical_object(
                dict(execution_policy),
                field="execution policy",
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
    def reusable_across_processes(self) -> bool:
        verifier = self.data.get("verifier")
        if not isinstance(verifier, Mapping) or verifier.get("mode") != "custom":
            return True
        behavioral_state = verifier.get("behavioral_state")
        return (
            isinstance(behavioral_state, Mapping) and behavioral_state.get("stability") == "durable"
        )


__all__ = [
    "EXECUTION_AUTHORITY_VERSION",
    "ExecutionAuthorityContract",
    "canonical_workspace_authority",
    "constructor_model_contract",
    "runtime_authority_contract",
    "runtime_execution_identity_contract",
    "runtime_execution_proves_effective_model",
    "valid_constructor_model_contract",
    "valid_runtime_execution_identity_contract",
    "verifier_authority_contract",
]
