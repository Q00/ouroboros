"""Canonical executor-baseline authority shared by later runtime layers.

This module owns identity only. It does not authorize a dispatch, persist a
checkpoint, or declare acceptance. Per-attempt inputs such as the AC, prompt,
tool envelope, and a checkpoint-selected resume handle belong to the later
attempt capsule. That capsule can compose this stable baseline instead of
deriving narrower runtime, verifier, workspace, and policy fingerprints again.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import marshal
import math
import os
from pathlib import Path
import re
import shutil
from typing import Any
import uuid

from ouroboros.core.security import is_sensitive_field
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

EXECUTION_AUTHORITY_VERSION = 2
_MAX_IDENTITY_DEPTH = 8
_MAX_IDENTITY_ITEMS = 256
_MAX_IDENTITY_SCALAR_CHARS = 8_192
_MAX_IDENTITY_JSON_CHARS = 64_000
_RESOLVED_RUNTIME_AUTHORITY_TOKEN = object()
_SHA256_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
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


def canonical_workspace_authority(
    workspace: str | None,
    *,
    identity: Mapping[str, object] | None = None,
    generation: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return the checkout owner plus an optional immutable generation identity."""
    if identity is not None:
        owner = _canonical_object(dict(identity), field="workspace identity")
        resolved_cwd = owner.get("effective_cwd")
        if (
            not isinstance(workspace, str)
            or not workspace.strip()
            or not isinstance(resolved_cwd, str)
            or str(Path(workspace).expanduser().resolve(strict=False))
            != str(Path(resolved_cwd).expanduser().resolve(strict=False))
        ):
            raise ValueError("workspace identity disagrees with the effective workspace")
    elif isinstance(workspace, str) and workspace.strip():
        owner = {
            "mode": "direct",
            "effective_cwd": str(Path(workspace).expanduser().resolve(strict=False)),
        }
    else:
        return {
            "version": 1,
            "observed": False,
            "generation": {"observed": False},
        }
    generation_contract: dict[str, object] = {"observed": False}
    if generation is not None:
        generation_identity = _canonical_object(
            dict(generation),
            field="workspace generation",
        )
        if generation_identity:
            if not _valid_workspace_generation_identity(generation_identity):
                raise ValueError("workspace generation identity is invalid")
            generation_contract = {
                "observed": True,
                "identity": generation_identity,
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


def runtime_permission_mode_contract(adapter: object) -> dict[str, object]:
    """Return the normalized permission mode that the runtime actually executes."""
    permission_mode: object = None
    private_descriptor = inspect.getattr_static(adapter, "_permission_mode", None)
    if private_descriptor is not None:
        permission_mode = object.__getattribute__(adapter, "_permission_mode")
    if not isinstance(permission_mode, str) or not permission_mode.strip():
        permission_mode = getattr(adapter, "permission_mode", None)
    return (
        {"observed": True, "mode": permission_mode.strip()}
        if isinstance(permission_mode, str) and permission_mode.strip()
        else {"observed": False}
    )


def _active_resolved_runtime_fields(adapter: object) -> dict[str, object]:
    runtime_backend = getattr(adapter, "runtime_backend", None)
    llm_backend = getattr(adapter, "llm_backend", None)
    return {
        "runtime_backend": (
            runtime_backend.strip()
            if isinstance(runtime_backend, str) and runtime_backend.strip()
            else None
        ),
        "llm_backend": (
            llm_backend.strip() if isinstance(llm_backend, str) and llm_backend.strip() else None
        ),
        "permission_mode": runtime_permission_mode_contract(adapter),
        "constructor_model": constructor_model_contract(adapter),
        "runtime_execution": runtime_execution_identity_contract(adapter),
    }


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeAuthority:
    """Runner-resolved runtime identity validated against the active adapter."""

    canonical_json: str
    _binding_token: object = field(repr=False, compare=False)
    _adapter: object = field(repr=False, compare=False)

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


def _callable_implementation_contract(target: object) -> dict[str, object]:
    """Identify executable behavior without persisting source or code objects."""
    module = getattr(target, "__module__", type(target).__module__)
    qualname = getattr(target, "__qualname__", type(target).__qualname__)
    try:
        source_digest = _sha256(inspect.getsource(target))
    except (OSError, TypeError):
        source_digest = None
    code = getattr(target, "__code__", None)
    code_digest = (
        "sha256:" + hashlib.sha256(marshal.dumps(code)).hexdigest()
        if source_digest is None and code is not None
        else None
    )
    if source_digest is None and code_digest is None:
        return {
            "stability": "process_local",
            "instance_nonce": uuid.uuid4().hex,
            "module": str(module),
            "qualname": str(qualname),
        }
    return {
        "stability": "durable",
        "module": str(module),
        "qualname": str(qualname),
        "source_digest": source_digest,
        "code_digest": code_digest,
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
            contract["instance_nonce"] = uuid.uuid4().hex
        return contract
    return _callable_leaf_implementation_contract(target)


def _qualified_type(value: object) -> str:
    return f"{type(value).__module__}.{type(value).__qualname__}"


def _file_content_digest(path: str | os.PathLike[str]) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source_file:
            while chunk := source_file.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


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
    if realpath is not None:
        try:
            digest = hashlib.sha256()
            with Path(realpath).open("rb") as executable_file:
                stat = os.fstat(executable_file.fileno())
                while chunk := executable_file.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            pass
        else:
            generation = {
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
            content_digest = "sha256:" + digest.hexdigest()
    return {
        "path": value,
        "realpath": realpath,
        "generation": generation,
        "content_digest": content_digest,
    }


def _runtime_executable_contract(adapter: object) -> dict[str, object]:
    """Bind subprocess executables, launchers, and delegated command policy."""
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
        executable = _resolved_executable(declared.get("executable"), cwd=cwd)
        launcher = _resolved_executable(declared.get("launcher"), cwd=cwd)
        raw_policy = declared.get("command_policy")
        if raw_policy is not None:
            command_policy = _canonical_object(raw_policy, field="runtime command policy")
    else:
        executable = _resolved_executable(
            getattr(adapter, "cli_path", None) or getattr(adapter, "_cli_path", None),
            cwd=cwd,
        )
        launcher = _resolved_executable(getattr(adapter, "_electron_node_path", None), cwd=cwd)
    required = executable is not None or launcher is not None or command_policy is not None
    observed = not required or all(
        item is None
        or item.get("realpath") is not None
        and item.get("generation") is not None
        and item.get("content_digest") is not None
        for item in (executable, launcher)
    )
    return {
        "required": required,
        "observed": observed,
        "executable": executable,
        "launcher": launcher,
        "command_policy": command_policy,
    }


def _runtime_watchdog_contract(adapter: object) -> dict[str, object]:
    provider_descriptor = inspect.getattr_static(
        type(adapter),
        "watchdog_identity_contract",
        None,
    )
    if provider_descriptor is not None:
        provider = object.__getattribute__(adapter, "watchdog_identity_contract")
        value = provider()
        if not isinstance(value, Mapping):
            raise ValueError("runtime watchdog identity is not a mapping")
        return {"observed": True, "identity": _canonical_object(value, field="watchdog identity")}
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
    missing = object()
    values: dict[str, object] = {}
    for field_name in fields:
        value = inspect.getattr_static(adapter, field_name, missing)
        if value is not missing:
            values[field_name.removeprefix("_")] = object.__getattribute__(
                adapter,
                field_name,
            )
    return {
        "observed": bool(values),
        "identity": _canonical_object(values, field="watchdog identity") if values else None,
    }


def _runtime_skill_dispatcher_contract(adapter: object) -> dict[str, object]:
    interceptor_descriptor = inspect.getattr_static(adapter, "_interceptor", None)
    if interceptor_descriptor is not None:
        try:
            interceptor = object.__getattribute__(adapter, "_interceptor")
            component = _declared_component_contract(interceptor)
        except Exception as exc:
            return {
                "mode": "delegated",
                "stability": "process_local",
                "instance_nonce": uuid.uuid4().hex,
                "reason": f"{type(exc).__module__}.{type(exc).__qualname__}",
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
            "instance_nonce": uuid.uuid4().hex,
            "skills_dir": str(getattr(adapter, "_skills_dir", None)),
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
    except Exception as exc:
        return {
            "mode": "custom",
            "stability": "process_local",
            "instance_nonce": uuid.uuid4().hex,
            "reason": f"{type(exc).__module__}.{type(exc).__qualname__}",
            "implementation": implementation,
        }
    if callable(identity_provider):
        try:
            identity = identity_provider()
            if not isinstance(identity, Mapping):
                raise ValueError("skill dispatcher identity is not a mapping")
            encoded = _canonical_json(dict(identity), field="skill dispatcher identity")
        except Exception as exc:
            return {
                "mode": "custom",
                "stability": "process_local",
                "instance_nonce": uuid.uuid4().hex,
                "reason": f"{type(exc).__module__}.{type(exc).__qualname__}",
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
        "instance_nonce": uuid.uuid4().hex,
        "implementation": implementation,
    }


def _class_implementation_contract(adapter: object) -> dict[str, object]:
    """Bind one object's class hierarchy without inspecting its instance state."""
    classes: list[str] = []
    members: dict[str, object] = {}
    modules: dict[str, object] = {}
    durable = True
    for runtime_class in type(adapter).__mro__:
        if runtime_class is object:
            continue
        qualified_name = f"{runtime_class.__module__}.{runtime_class.__qualname__}"
        classes.append(qualified_name)
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
                member_key = f"{member_name}:{index}"
                member_contract = _callable_implementation_contract(target)
                class_members[member_key] = member_contract
                durable = durable and member_contract.get("stability") == "durable"
        members[qualified_name] = {
            "observed": bool(class_members),
            "content_digest": _sha256(
                _canonical_json(
                    class_members,
                    field=f"runtime class members {qualified_name}",
                )
            ),
        }
        try:
            source_path = inspect.getsourcefile(runtime_class)
        except (OSError, TypeError):
            source_path = None
        if source_path is None:
            durable = False
            modules[qualified_name] = {"observed": False}
            continue
        try:
            realpath = str(Path(source_path).resolve(strict=False))
        except (OSError, RuntimeError):
            durable = False
            modules[qualified_name] = {"observed": False}
            continue
        digest = _file_content_digest(realpath)
        if digest is None:
            durable = False
            modules[realpath] = {"observed": False}
            continue
        modules[realpath] = {
            "observed": True,
            "content_digest": digest,
        }
    return {
        "stability": "durable" if durable and classes else "process_local",
        "classes": classes,
        "members": members,
        "modules": modules,
    }


def _safe_class_implementation_contract(value: object) -> dict[str, object]:
    try:
        return _class_implementation_contract(value)
    except Exception as exc:
        return {
            "stability": "process_local",
            "observed": False,
            "reason": f"{type(exc).__module__}.{type(exc).__qualname__}",
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


def _executor_implementation_contract(executor: object | None) -> dict[str, object]:
    """Bind the concrete effect-owning executor or fail closed when absent."""
    if executor is None:
        return {
            "version": 1,
            "mode": "unbound",
            "stability": "process_local",
            "instance_nonce": uuid.uuid4().hex,
        }
    implementation = _safe_class_implementation_contract(executor)
    instance_overrides = _instance_declared_method_overrides(executor)
    durable = implementation.get("stability") == "durable" and not instance_overrides
    contract: dict[str, object] = {
        "version": 1,
        "mode": "bound",
        "type": _qualified_type(executor),
        "stability": "durable" if durable else "process_local",
        "implementation": implementation,
        "instance_overrides": instance_overrides,
    }
    if not durable:
        contract["instance_nonce"] = uuid.uuid4().hex
    return contract


def _reject_sensitive_identity_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if is_sensitive_field(key):
                raise ValueError("component identity contains a sensitive field")
            _reject_sensitive_identity_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_identity_fields(item)


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
    except Exception as exc:
        executable = {
            "required": True,
            "observed": False,
            "reason": f"{type(exc).__module__}.{type(exc).__qualname__}",
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
            "instance_nonce": uuid.uuid4().hex,
            "reason": "identity_not_declared",
            "implementation": implementation,
            "executable": executable,
        }
    try:
        provider = object.__getattribute__(value, "execution_identity_contract")
        identity = provider()
        if not isinstance(identity, Mapping):
            raise ValueError("component identity is not a mapping")
        projected = _project_explicit_identity(
            dict(identity),
            field="component execution identity",
        )
        _reject_sensitive_identity_fields(projected)
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
        if len(encoded) > _MAX_IDENTITY_JSON_CHARS:
            raise ValueError("component identity exceeds its budget")
    except Exception as exc:
        return {
            "mode": "process_local",
            "stability": "process_local",
            "type": _qualified_type(value),
            "instance_nonce": uuid.uuid4().hex,
            "reason": f"identity_error:{type(exc).__module__}.{type(exc).__qualname__}",
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
        contract["instance_nonce"] = uuid.uuid4().hex
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
        if not all(isinstance(name, str) and name for name in components):
            raise ValueError("runtime execution component names are invalid")
        contracts = {
            name: _declared_component_contract(component) for name, component in components.items()
        }
    except Exception as exc:
        return {
            "version": 1,
            "mode": "declared",
            "stability": "process_local",
            "instance_nonce": uuid.uuid4().hex,
            "reason": f"{type(exc).__module__}.{type(exc).__qualname__}",
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
        result["instance_nonce"] = uuid.uuid4().hex
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
        contract["instance_nonce"] = uuid.uuid4().hex
    return contract


def _runtime_handle_selector_contract(
    adapter: object,
    runtime_handle: RuntimeHandle | None,
) -> dict[str, object]:
    if runtime_handle is None:
        return {"version": 1, "mode": "none"}
    provider_descriptor = inspect.getattr_static(
        type(adapter),
        "resume_handle_execution_identity_contract",
        None,
    )
    if provider_descriptor is None:
        return {"version": 1, "mode": "present", "observed": False}
    provider = object.__getattribute__(
        adapter,
        "resume_handle_execution_identity_contract",
    )
    identity = provider(runtime_handle)
    if not isinstance(identity, Mapping):
        raise ValueError("runtime handle selector identity is not a mapping")
    return {
        "version": 1,
        "mode": "present",
        "observed": True,
        "identity": _canonical_object(dict(identity), field="runtime handle selector identity"),
    }


def runtime_authority_contract(
    adapter: object,
    *,
    resolved_routing: ResolvedRuntimeAuthority | None = None,
    runtime_handle: RuntimeHandle | None = None,
) -> dict[str, object]:
    """Combine generic capabilities with one already-resolved runtime identity."""
    if resolved_routing is None:
        runtime_backend = getattr(adapter, "runtime_backend", None)
        llm_backend = getattr(adapter, "llm_backend", None)
        constructor_model = constructor_model_contract(adapter)
        execution_identity = runtime_execution_identity_contract(adapter)
        permission_contract = runtime_permission_mode_contract(adapter)
    else:
        resolved_routing.require_adapter(adapter)
        resolved_data = resolved_routing.data
        runtime_backend = resolved_data.get("runtime_backend")
        llm_backend = resolved_data.get("llm_backend")
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
    return {
        "version": 1,
        "runtime_backend": (
            runtime_backend.strip()
            if isinstance(runtime_backend, str) and runtime_backend.strip()
            else None
        ),
        "self_governs_rate_limit": bool(getattr(adapter, "self_governs_rate_limit", False)),
        "llm_backend": (
            llm_backend.strip() if isinstance(llm_backend, str) and llm_backend.strip() else None
        ),
        "permission_mode": permission_contract,
        "constructor_model": constructor_model,
        "execution_identity": execution_identity,
        "capabilities": _runtime_capabilities_contract(adapter),
        "implementation": _runtime_implementation_contract(adapter),
        "executable": _runtime_executable_contract(adapter),
        "watchdog": _runtime_watchdog_contract(adapter),
        "skill_dispatcher": _runtime_skill_dispatcher_contract(adapter),
        "handle_selector": _runtime_handle_selector_contract(adapter, runtime_handle),
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
    return _callable_entrypoint_contract(verifier)


def verifier_authority_contract(
    verifier: Verifier | None,
    *,
    runtime_transcript_verifier: object | None = None,
) -> dict[str, object]:
    """Return durable verifier identity only when it is explicitly declared."""
    transcript_implementation = _callable_implementation_contract(
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
        except Exception as exc:
            behavioral_state = {
                "stability": "process_local",
                "instance_nonce": uuid.uuid4().hex,
                "reason": f"{type(exc).__module__}.{type(exc).__qualname__}",
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
    if not isinstance(value, Mapping) or set(value) != {
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
    }:
        return False
    version = value.get("version")
    token_version = value.get("token_estimation_version")
    backend = value.get("backend")
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
        or not isinstance(backend, str)
        or not backend.strip()
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


def valid_execution_policy_contract(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _EXECUTION_POLICY_KEYS:
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
    """Return the one canonical parallel-execution policy payload."""
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
            "executor",
            "workspace",
            "runtime",
            "verifier",
            "execution_policy",
        }:
            raise ValueError("execution authority contract has an invalid shape")
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
        expected_backend = (
            runtime_backend if isinstance(runtime_backend, str) and runtime_backend else "unknown"
        )
        if dispatch_backend != expected_backend:
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
        workspace_identity: Mapping[str, object] | None = None,
        workspace_generation: Mapping[str, object] | None = None,
        resolved_routing: ResolvedRuntimeAuthority | None = None,
        runtime_handle: RuntimeHandle | None = None,
        runtime_transcript_verifier: object | None = None,
    ) -> ExecutionAuthorityContract:
        data = {
            "version": EXECUTION_AUTHORITY_VERSION,
            "executor": _executor_implementation_contract(executor),
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
    def portable_across_processes(self) -> bool:
        """Return whether this baseline can be composed into a capsule elsewhere.

        Portability is an identity-stability property only. It never authorizes
        reuse of an attempt, result, checkpoint, trust verdict, or acceptance;
        those require a complete per-attempt capsule with the omitted inputs.
        """
        data = self.data
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

        runtime = data.get("runtime")
        if not isinstance(runtime, Mapping):
            return False
        runtime_backend = runtime.get("runtime_backend")
        if not isinstance(runtime_backend, str) or not runtime_backend:
            return False
        for field_name in ("permission_mode", "constructor_model", "execution_identity"):
            value = runtime.get(field_name)
            if not isinstance(value, Mapping) or value.get("observed") is not True:
                return False
        execution_identity = runtime.get("execution_identity")
        resolved_identity = (
            execution_identity.get("identity") if isinstance(execution_identity, Mapping) else None
        )
        if (
            not isinstance(resolved_identity, Mapping)
            or resolved_identity.get("effective_model_observed") is not True
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
        if executable.get("required") is True and (
            not isinstance(watchdog, Mapping) or watchdog.get("observed") is not True
        ):
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
        if handle_selector.get("mode") != "none" and handle_selector.get("observed") is not True:
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
    "ExecutionAuthorityContract",
    "ResolvedRuntimeAuthority",
    "build_execution_policy_contract",
    "canonical_workspace_authority",
    "constructor_model_contract",
    "runtime_authority_contract",
    "runtime_execution_identity_contract",
    "runtime_execution_proves_effective_model",
    "valid_constructor_model_contract",
    "valid_execution_policy_contract",
    "valid_runtime_execution_identity_contract",
    "verifier_authority_contract",
]
