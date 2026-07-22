"""Finite execution-authority identity for the AC runtime.

Foundation A deliberately identifies a small, explicit component boundary. It
does not attempt to derive authority from an arbitrary Python callable graph:
closures, globals, descriptors, module monkeypatches, caches, and runtime
handles are volatile. A custom verifier can therefore be bound only to its
exact live object for this process; it is never made portable by introspection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
from typing import Any
import uuid

from ouroboros.orchestrator.runtime_param_negotiation import runtime_capabilities_for
from ouroboros.orchestrator.verifier import Verifier, structural_atomic_verifier

EXECUTION_AUTHORITY_VERSION = 1
EXECUTION_AUTHORITY_BOUNDARY_VERSION = 1

_MAX_IDENTITY_DEPTH = 8
_MAX_IDENTITY_ITEMS = 256
_MAX_IDENTITY_SCALAR_CHARS = 8_192
_MAX_IDENTITY_JSON_CHARS = 64_000

_EXECUTOR_COMPONENT_VERSIONS = {
    "parallel_ac_executor": "parallel-ac-executor/v1",
    "leaf_dispatcher": "leaf-dispatcher/v1",
    "level_coordinator": "level-coordinator/v1",
    "rate_limit_gate": "rate-limit-gate/v1",
}
_BUILTIN_TRANSCRIPT_VERIFIER = "runtime-transcript-verifier/v1"
_BUILTIN_STRUCTURAL_VERIFIER = "structural-atomic-verifier/v1"


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
    return any(
        marker in compact
        for marker in (
            "apikey",
            "credential",
            "password",
            "authorization",
            "bearer",
            "privatekey",
            "clientsecret",
        )
    ) or compact.endswith(("token", "tokenvalue", "keyvalue", "secret"))


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
            "runtime_descriptor",
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


def runtime_authority_contract(
    adapter: object,
    *,
    force_process_local: bool = False,
) -> dict[str, object]:
    """Return a finite, digest-only runtime descriptor.

    The runner's resume identity remains a separate API. This authority payload
    never serializes provider-controlled identity values or capabilities.
    """
    # This public descriptor builder must retain the finite live-root rule used
    # by ``ExecutionAuthorityLiveBinding``. A runtime with only dynamic
    # ``execute_task`` lookup remains executable, but cannot claim portability.
    if not force_process_local:
        force_process_local = not _has_observable_runtime_dispatch_root(adapter)
    try:
        execution = runtime_execution_identity_contract(adapter)
        execution_descriptor = (
            _digest_descriptor(execution["identity"], field="runtime execution identity")
            if execution.get("observed") is True
            else {"observed": False}
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        execution_descriptor = {"observed": False}
    capabilities = _runtime_capabilities_descriptor(adapter)
    runtime_backend = _runtime_label_descriptor(adapter, "runtime_backend")
    llm_backend = _runtime_label_descriptor(adapter, "llm_backend")
    permission_mode = _runtime_label_descriptor(adapter, "permission_mode")
    try:
        self_governs_rate_limit = object.__getattribute__(adapter, "self_governs_rate_limit")
    except (AttributeError, TypeError):
        self_governs_rate_limit = False
    stable = (
        runtime_backend["observed"] is True
        and execution_descriptor["observed"] is True
        and capabilities["observed"] is True
        and isinstance(self_governs_rate_limit, bool)
        and not force_process_local
    )
    return {
        "version": 1,
        "stability": "durable" if stable else "process_local",
        "runtime_backend": runtime_backend,
        "llm_backend": llm_backend,
        "permission_mode": permission_mode,
        "execution_identity": execution_descriptor,
        "capabilities": capabilities,
        "self_governs_rate_limit": (
            self_governs_rate_limit if isinstance(self_governs_rate_limit, bool) else None
        ),
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
        "self_governs_rate_limit",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("version") != 1:
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
        )
    ):
        return False
    return value.get("self_governs_rate_limit") is None or isinstance(
        value.get("self_governs_rate_limit"), bool
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
    if verifier is structural_atomic_verifier:
        return {
            "version": 1,
            "mode": "structural_atomic",
            "stability": "durable",
            "implementation": _BUILTIN_STRUCTURAL_VERIFIER,
        }

    configuration: dict[str, object] = {"observed": False}
    try:
        descriptor = inspect.getattr_static(verifier, "verification_identity_contract", None)
        if descriptor is not None:
            provider = object.__getattribute__(verifier, "verification_identity_contract")
            if callable(provider):
                value = provider()
                if isinstance(value, Mapping):
                    configuration = _digest_descriptor(
                        dict(value),
                        field="custom verifier identity",
                    )
    except Exception:
        configuration = {"observed": False}
    return {
        "version": 1,
        "mode": "custom",
        "stability": "process_local",
        "instance_nonce": instance_nonce or uuid.uuid4().hex,
        "configuration": configuration,
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
            and data.get("workspace", {}).get("observed") is True
            and data.get("execution_policy", {}).get("observed") is True
        )

    @property
    def reusable_across_processes(self) -> bool:
        """Backward-compatible alias for the identity-only portability flag."""
        return self.portable_across_processes


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityLiveBinding:
    """The finite live roots that must remain identical before an effect."""

    contract: ExecutionAuthorityContract
    executor: object | None
    executor_attribute_resolution_observable: bool
    adapter: object
    verifier: Verifier | None
    dispatcher_type: object
    dispatcher: object | None
    dispatcher_executor: object | None
    transcript_verifier: object
    adapter_dispatch_root: object | None
    adapter_dispatch_code: object | None
    adapter_attribute_resolution_observable: bool
    verifier_root: object | None
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
    rate_gate_bucket: object | None
    rate_gate_bucket_config: tuple[object, object, object, object] | None
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
        rate_gate_bucket: object | None = None
        rate_gate_bucket_config: tuple[object, object, object, object] | None = None
        rate_gate_bucket_binding_observable = False
        try:
            rate_gate_bucket = object.__getattribute__(rate_gate, "_bucket")
            rate_gate_bucket_config = (
                object.__getattribute__(rate_gate_bucket, "_runtime_backend"),
                object.__getattribute__(rate_gate_bucket, "_request_limit"),
                object.__getattribute__(rate_gate_bucket, "_token_limit"),
                object.__getattribute__(rate_gate_bucket, "_window_seconds"),
            )
            rate_gate_bucket_binding_observable = (
                isinstance(rate_gate_bucket_config[0], str)
                and (
                    rate_gate_bucket_config[1] is None
                    or isinstance(rate_gate_bucket_config[1], int)
                )
                and (
                    rate_gate_bucket_config[2] is None
                    or isinstance(rate_gate_bucket_config[2], int)
                )
                and isinstance(rate_gate_bucket_config[3], float)
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
            expected_rate_gate_acquire_root is not None
            and rate_gate_acquire_root is expected_rate_gate_acquire_root
            and expected_rate_gate_acquire_code is not None
            and rate_gate_acquire_code is expected_rate_gate_acquire_code
            and rate_gate_acquire_callable is rate_gate_acquire_root
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
            not executor_attribute_resolution_observable
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
            or not rate_gate_bucket_binding_observable
        )
        nonce = (
            None if verifier is None or verifier is structural_atomic_verifier else uuid.uuid4().hex
        )
        contract = ExecutionAuthorityContract.build(
            adapter=adapter,
            verifier=verifier,
            workspace=workspace,
            execution_policy=execution_policy,
            verifier_instance_nonce=nonce,
            force_runtime_process_local=force_runtime_process_local,
        )
        return cls(
            contract=contract,
            executor=executor,
            executor_attribute_resolution_observable=executor_attribute_resolution_observable,
            adapter=adapter,
            verifier=verifier,
            dispatcher_type=dispatcher_type,
            dispatcher=dispatcher,
            dispatcher_executor=captured_dispatcher_executor,
            transcript_verifier=transcript_verifier,
            adapter_dispatch_root=adapter_dispatch_root,
            adapter_dispatch_code=adapter_dispatch_code,
            adapter_attribute_resolution_observable=adapter_attribute_resolution_observable,
            verifier_root=verifier_root,
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
            rate_gate_bucket=rate_gate_bucket,
            rate_gate_bucket_config=rate_gate_bucket_config,
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
        if self.rate_gate_bucket_binding_observable:
            try:
                current_rate_gate_bucket = object.__getattribute__(rate_gate, "_bucket")
                current_rate_gate_bucket_config = (
                    object.__getattribute__(current_rate_gate_bucket, "_runtime_backend"),
                    object.__getattribute__(current_rate_gate_bucket, "_request_limit"),
                    object.__getattribute__(current_rate_gate_bucket, "_token_limit"),
                    object.__getattribute__(current_rate_gate_bucket, "_window_seconds"),
                )
            except (AttributeError, TypeError):
                return False
            if (
                current_rate_gate_bucket is not self.rate_gate_bucket
                or current_rate_gate_bucket_config != self.rate_gate_bucket_config
            ):
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
    "valid_runtime_execution_identity_contract",
    "verifier_authority_contract",
]
