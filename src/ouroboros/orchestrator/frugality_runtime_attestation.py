"""Stable auxiliary-runtime attestations for paired frugality evidence.

The execution/resume authority contracts intentionally include process-local
state.  Frugality compares independent arms, so its runtime contract must bind
the same effect-bearing settings without nonces, object ids, or secrets.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
import hashlib
import hmac
import json
from pathlib import Path
import re
from typing import Any

from ouroboros.codex.cli_policy import build_codex_child_env
from ouroboros.core.project_identity import ProjectIdentityError, resolve_project_identity
from ouroboros.providers.credential_authority import (
    INSTALLATION_AUTHORITY_KEY_BYTES,
    installation_authority_key,
)

_MAX_CHILD_ENVIRONMENT_ENTRIES = 4_096
_MAX_CHILD_ENVIRONMENT_KEY_BYTES = 1_024
_MAX_CHILD_ENVIRONMENT_VALUE_BYTES = 128 * 1_024
_MAX_CHILD_ENVIRONMENT_TOTAL_BYTES = 2 * 1_024 * 1_024
_CHILD_ENVIRONMENT_ENTRY_CONTEXT = b"ouroboros-codex-child-environment-authority-v2"
_CHILD_ENVIRONMENT_AUTHORITY_SCHEME = "hmac-sha256-installation-key-v2"
_CODEX_CONFIG_AUTHORITY_CONTEXT = b"ouroboros-codex-config-authority-v1"
_SENSITIVE_ENVIRONMENT_KEY_RE = re.compile(
    r"(?:^|_)(?:"
    r"access_?token|api_?key|apikey|auth(?:entication|orization)?|bearer|"
    r"client_?secret|cookie|credential(?:s)?|password|passwd|private_?key|"
    r"refresh_?token|secret|session_?token|token"
    r")(?:$|_)",
    re.IGNORECASE,
)
_ATTESTED_CHILD_ENVIRONMENT_ATTR = "_frugality_attested_child_environment"


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _digest(value: object) -> str | None:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _stable_dispatcher_identity(dispatcher: object) -> str | None:
    if dispatcher is None:
        return "packaged"
    owner = getattr(dispatcher, "__self__", None) or dispatcher
    stable_identity = getattr(owner, "stable_identity_contract", None)
    if not callable(stable_identity):
        return None
    try:
        identity = stable_identity()
    except Exception:  # noqa: BLE001 - opaque custom authority fails closed
        return None
    if not isinstance(identity, Mapping):
        return None
    return _digest(
        {
            "module": getattr(dispatcher, "__module__", type(owner).__module__),
            "qualname": getattr(dispatcher, "__qualname__", type(owner).__qualname__),
            "stable_identity": dict(identity),
        }
    )


def _resolved_path(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(Path(value).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _environment_entry_authority(
    key: str,
    value: str,
    *,
    installation_key: bytes,
) -> str:
    """Return an installation-keyed entry identity without retaining its value."""
    message = b"\x00".join(
        (_CHILD_ENVIRONMENT_ENTRY_CONTEXT, key.encode("utf-8"), value.encode("utf-8"))
    )
    digest = hmac.new(
        installation_key,
        message,
        hashlib.sha256,
    ).hexdigest()
    return digest


def _codex_config_authority(
    fingerprint: object,
    *,
    installation_key: bytes,
) -> str | None:
    """Protect a config-content fingerprint from offline candidate checks."""
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        return None
    message = b"\x00".join((_CODEX_CONFIG_AUTHORITY_CONTEXT, fingerprint.encode("ascii")))
    return hmac.new(installation_key, message, hashlib.sha256).hexdigest()


def codex_child_environment_attestation(
    environment: Mapping[str, str],
    *,
    secret_authority_key: bytes | None = None,
) -> dict[str, object] | None:
    """Fingerprint the complete inherited Codex child environment safely.

    Every key/value that reaches the subprocess is reduced through installation-
    keyed authority before persistence. Name classification is retained only as
    diagnostic cardinality; it is not the confidentiality boundary. Explicit
    size ceilings keep malformed or hostile mappings proof-ineligible.
    """
    if not isinstance(environment, Mapping) or not environment:
        return None
    try:
        items = list(environment.items())
    except Exception:  # noqa: BLE001 - opaque environment authority fails closed
        return None
    if len(items) > _MAX_CHILD_ENVIRONMENT_ENTRIES:
        return None
    if (
        not isinstance(secret_authority_key, bytes)
        or len(secret_authority_key) != INSTALLATION_AUTHORITY_KEY_BYTES
    ):
        return None

    entry_authorities: list[str] = []
    sensitive_entry_count = 0
    total_bytes = 0
    for key, value in items:
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        try:
            key_bytes = key.encode("utf-8")
            value_bytes = value.encode("utf-8")
        except UnicodeError:
            return None
        if (
            not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
            or len(key_bytes) > _MAX_CHILD_ENVIRONMENT_KEY_BYTES
            or len(value_bytes) > _MAX_CHILD_ENVIRONMENT_VALUE_BYTES
        ):
            return None
        total_bytes += len(key_bytes) + len(value_bytes)
        if total_bytes > _MAX_CHILD_ENVIRONMENT_TOTAL_BYTES:
            return None
        if _SENSITIVE_ENVIRONMENT_KEY_RE.search(key):
            sensitive_entry_count += 1
        entry_authorities.append(
            _environment_entry_authority(
                key,
                value,
                installation_key=secret_authority_key,
            )
        )

    entry_authorities.sort()
    authority_fingerprint = _digest(entry_authorities)
    if authority_fingerprint is None:
        return None
    return {
        "authority_fingerprint": authority_fingerprint,
        "entry_count": len(items),
        "sensitive_entry_count": sensitive_entry_count,
    }


def _semantic_cwd(runtime: Any) -> str | None:
    """Return the canonical repository-relative path used by Codex ``-C``."""
    cwd = _text(getattr(runtime, "_cwd", None))
    if cwd is None:
        return None
    try:
        return resolve_project_identity(cwd).workspace_path
    except ProjectIdentityError:
        return None


def build_codex_runtime_child_environment(runtime: Any) -> dict[str, str]:
    """Build the child environment from the exact runtime-owned policy."""
    return build_codex_child_env(
        max_depth=runtime._max_ouroboros_depth,
        child_session_env_keys=runtime._child_session_env_keys,
        depth_error_factory=lambda _depth, max_depth: RuntimeError(
            f"Maximum Ouroboros nesting depth ({max_depth}) exceeded"
        ),
    )


def _attested_child_environment_context(
    runtime: Any,
) -> ContextVar[Mapping[str, str] | None]:
    context = getattr(runtime, _ATTESTED_CHILD_ENVIRONMENT_ATTR, None)
    if isinstance(context, ContextVar):
        return context
    context = ContextVar(
        "ouroboros_codex_frugality_attested_child_environment",
        default=None,
    )
    setattr(runtime, _ATTESTED_CHILD_ENVIRONMENT_ATTR, context)
    return context


def attested_codex_child_environment(runtime: Any) -> dict[str, str] | None:
    """Return the task-local environment sealed for every spawn in one call."""
    context = getattr(runtime, _ATTESTED_CHILD_ENVIRONMENT_ATTR, None)
    if not isinstance(context, ContextVar):
        return None
    environment = context.get()
    return dict(environment) if environment is not None else None


def clear_attested_codex_child_environment(runtime: Any) -> None:
    """Release the task-local environment after the tracked call terminates."""
    context = getattr(runtime, _ATTESTED_CHILD_ENVIRONMENT_ATTR, None)
    if isinstance(context, ContextVar):
        context.set(None)


def codex_cli_runtime_attestation(runtime: Any) -> dict[str, object]:
    """Return Codex CLI settings that can alter auxiliary provider work.

    Every executable identity is the constructor-time snapshot.  The runtime
    separately rejects drift before dispatch; using the snapshot here keeps two
    isolated arms comparable without re-reading mutable global state.
    """
    implementation = f"{type(runtime).__module__}.{type(runtime).__qualname__}"
    skills_dir = getattr(runtime, "_skills_dir", None)
    skills_source = "directory" if skills_dir is not None else "package"
    skills_location = (
        _resolved_path(skills_dir)
        if skills_dir is not None
        else _text(getattr(runtime, "_skills_package_uri", None))
    )
    trust_baseline = getattr(runtime, "_codex_project_trust_baseline", None)
    trust_paths = (
        tuple(sorted(str(path) for path in trust_baseline))
        if isinstance(trust_baseline, (set, frozenset, dict))
        else None
    )
    authority_key = installation_authority_key()
    child_environment_candidate = build_codex_runtime_child_environment(runtime)
    child_environment = codex_child_environment_attestation(
        child_environment_candidate,
        secret_authority_key=authority_key,
    )
    codex_config_authority = (
        _codex_config_authority(
            getattr(runtime, "_codex_config_fingerprint", None),
            installation_key=authority_key,
        )
        if isinstance(authority_key, bytes)
        else None
    )
    if child_environment is not None:
        _attested_child_environment_context(runtime).set(child_environment_candidate)
    if not isinstance(child_environment, Mapping):
        child_environment = {}
    return {
        "schema_version": 4,
        "schema_id": "ouroboros.codex_cli_runtime.v4",
        "implementation": implementation,
        "runtime_backend": getattr(runtime, "_runtime_backend", None),
        "runtime_handle_backend": getattr(runtime, "_runtime_handle_backend", None),
        "settings": {
            "provider_name": getattr(runtime, "_provider_name", None),
            "llm_backend": getattr(runtime, "_llm_backend", None),
            "permission_mode": getattr(runtime, "_permission_mode", None),
            "constructor_model": _text(getattr(runtime, "_model", None)),
            "runtime_profile": _text(getattr(runtime, "_runtime_profile", None)),
            "codex_profile": _text(getattr(runtime, "_codex_profile", None)),
            "resolved_model": _text(getattr(runtime, "_resolved_fallback_model", None)),
            "resolved_profile": _text(getattr(runtime, "_resolved_fallback_profile", None)),
            "resolved_agent": _text(getattr(runtime, "_copilot_agent", None)),
            "resolved_reasoning_effort": _text(
                getattr(runtime, "_resolved_fallback_reasoning_effort", None)
            ),
            "semantic_cwd": _semantic_cwd(runtime),
            "cli_path": _text(getattr(runtime, "_cli_path", None)),
            "cli_executable_path": getattr(runtime, "_cli_executable_path_identity", None),
            "cli_executable_content_sha256": getattr(
                runtime, "_cli_executable_content_identity_snapshot", None
            ),
            "cli_executable_version_identity": getattr(
                runtime, "_cli_executable_version_identity_snapshot", None
            ),
            "skills_source": skills_source,
            "skills_location": skills_location,
            "skill_dispatcher_kind": (
                "custom" if getattr(runtime, "_skill_dispatcher", None) is not None else "packaged"
            ),
            "skill_dispatcher_identity": _stable_dispatcher_identity(
                getattr(runtime, "_skill_dispatcher", None)
            ),
            "skill_dispatch_registry_fingerprint": getattr(
                runtime, "_skill_dispatch_registry_fingerprint", None
            ),
            "builtin_mcp_handler_registry_fingerprint": getattr(
                runtime, "_builtin_mcp_handler_registry_fingerprint", None
            ),
            "startup_output_timeout_seconds": getattr(
                runtime, "_startup_output_timeout_seconds", None
            ),
            "stdout_idle_timeout_seconds": getattr(runtime, "_stdout_idle_timeout_seconds", None),
            "process_shutdown_timeout_seconds": getattr(
                runtime, "_process_shutdown_timeout_seconds", None
            ),
            "completed_process_group_shutdown_timeout_seconds": getattr(
                runtime, "_completed_process_group_shutdown_timeout_seconds", None
            ),
            "max_resume_retries": getattr(runtime, "_max_resume_retries", None),
            "max_ouroboros_depth": getattr(runtime, "_max_ouroboros_depth", None),
            "max_stderr_lines": getattr(runtime, "_max_stderr_lines", None),
            "use_process_group": getattr(runtime, "_use_process_group", None),
            "child_session_env_keys": tuple(
                sorted(str(key) for key in getattr(runtime, "_child_session_env_keys", ()))
            ),
            "child_environment_authority_fingerprint": child_environment.get(
                "authority_fingerprint"
            ),
            "child_environment_authority_scheme": _CHILD_ENVIRONMENT_AUTHORITY_SCHEME,
            "child_environment_entry_count": child_environment.get("entry_count"),
            "child_environment_sensitive_entry_count": child_environment.get(
                "sensitive_entry_count"
            ),
            "profile_resolution_fingerprint": getattr(
                runtime, "_profile_resolution_fingerprint", None
            ),
            "codex_config_authority_fingerprint": codex_config_authority,
            "codex_config_authority_scheme": _CHILD_ENVIRONMENT_AUTHORITY_SCHEME,
            "codex_profile_v2_names": tuple(
                sorted(str(name) for name in getattr(runtime, "_codex_profile_v2_names", ()))
            ),
            "codex_project_trust_paths": trust_paths,
        },
    }


__all__ = [
    "attested_codex_child_environment",
    "build_codex_runtime_child_environment",
    "clear_attested_codex_child_environment",
    "codex_child_environment_attestation",
    "codex_cli_runtime_attestation",
]
