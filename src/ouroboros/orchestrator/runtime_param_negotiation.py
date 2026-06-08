"""Parameter-level capability negotiation (observability).

The orchestrator builds execution parameters — ``system_prompt``, a ``tools``
allow-list, ``permission_mode`` — and hands them to whichever runtime is active.
Runtimes do not all honor those parameters in the form they are supplied: some
embed the system prompt into the user message, map a permission mode onto
coarser CLI flags, or drop a parameter entirely. Historically this degradation
was silent.

This module turns that silence into an explicit, surfaceable signal. It compares
the *requested* parameters against the runtime's declared
:class:`~ouroboros.orchestrator.adapter.RuntimeCapabilities` and reports any that
are not honored natively. It is pure and side-effect free — it never alters what
is passed to the runtime; callers decide how to surface the result (log, console
notice, event).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import FULL_CAPABILITIES, ParamSupport, RuntimeCapabilities

log = get_logger(__name__)

_DEGRADATION_DETAIL = {
    ParamSupport.TRANSLATED: "honored via lossy translation, not in the form supplied",
    ParamSupport.IGNORED: "not honored by this runtime; it is silently dropped",
}


@dataclass(frozen=True, slots=True)
class ParamDegradation:
    """One requested execution parameter the runtime does not honor natively.

    Attributes:
        parameter: The execution parameter name (``"system_prompt"``,
            ``"tools"``, ``"permission_mode"``).
        support: How the runtime handles it (``TRANSLATED`` or ``IGNORED``).
        detail: Human-readable explanation suitable for a log/console notice.
    """

    parameter: str
    support: ParamSupport
    detail: str


def _degradation(parameter: str, support: ParamSupport) -> ParamDegradation | None:
    """Return a degradation record when ``support`` is non-native, else ``None``."""
    if support == ParamSupport.NATIVE:
        return None
    return ParamDegradation(
        parameter=parameter,
        support=support,
        detail=_DEGRADATION_DETAIL[support],
    )


def _tool_restriction_support_for_request(
    capabilities: RuntimeCapabilities,
    tools: list[str],
) -> ParamSupport:
    """Return truthful support for this concrete tools allow-list request."""
    support = capabilities.tool_restriction_support
    if tools == [] and support == ParamSupport.TRANSLATED:
        return ParamSupport.IGNORED
    return support


def negotiate_execution_params(
    capabilities: RuntimeCapabilities,
    *,
    system_prompt: str | None,
    tools: list[str] | None,
    permission_mode: str | None,
) -> tuple[ParamDegradation, ...]:
    """Report execution parameters the runtime will not honor natively.

    Only parameters that were actually *requested* are considered — an absent
    parameter cannot be degraded. For ``tools``, an explicit empty list is a
    requested no-tools allow-list, so ``[]`` is distinct from ``None``.
    """
    requested: list[tuple[str, ParamSupport]] = []
    if system_prompt:
        requested.append(("system_prompt", capabilities.system_prompt_support))
    if tools is not None:
        requested.append(("tools", _tool_restriction_support_for_request(capabilities, tools)))
    if permission_mode:
        requested.append(("permission_mode", capabilities.permission_mode_support))

    degradations = (_degradation(name, support) for name, support in requested)
    return tuple(item for item in degradations if item is not None)


def runtime_capabilities_for(adapter: object) -> RuntimeCapabilities:
    """Return an adapter's declared capabilities, falling back to all-native."""
    declared_capabilities = getattr(adapter, "capabilities", FULL_CAPABILITIES)
    return (
        declared_capabilities
        if isinstance(declared_capabilities, RuntimeCapabilities)
        else FULL_CAPABILITIES
    )


def adapter_requested_permission_mode(adapter: object) -> str | None:
    """Return the permission mode only when the adapter marks it caller-requested."""
    requested = (
        getattr(adapter, "permission_mode_requested", False) is True
        or getattr(adapter, "_permission_mode_requested", False) is True
    )
    if not requested:
        return None
    permission_mode = getattr(adapter, "permission_mode", None)
    return permission_mode if isinstance(permission_mode, str) and permission_mode else None


def announce_execution_param_degradations(
    adapter: object,
    *,
    system_prompt: str | None,
    tools: list[str] | None,
    announced: set[tuple[str, str]] | None = None,
    console: Any | None = None,
    log_event: str = "orchestrator.runtime_params.param_degraded",
) -> None:
    """Log and optionally print requested execution params degraded by a runtime."""
    degradations = negotiate_execution_params(
        runtime_capabilities_for(adapter),
        system_prompt=system_prompt,
        tools=tools,
        permission_mode=adapter_requested_permission_mode(adapter),
    )
    backend = getattr(adapter, "runtime_backend", "unknown")
    for degradation in degradations:
        key = (degradation.parameter, degradation.support.value)
        if announced is not None:
            if key in announced:
                continue
            announced.add(key)
        log.info(
            log_event,
            runtime_backend=backend,
            parameter=degradation.parameter,
            support=degradation.support.value,
            detail=degradation.detail,
        )
        if console is not None:
            console.print(
                f"[yellow]Note:[/yellow] runtime '{backend}' does not natively honor "
                f"'{degradation.parameter}' ({degradation.support.value}): "
                f"{degradation.detail}."
            )


__all__ = [
    "ParamDegradation",
    "adapter_requested_permission_mode",
    "announce_execution_param_degradations",
    "negotiate_execution_params",
    "runtime_capabilities_for",
]
