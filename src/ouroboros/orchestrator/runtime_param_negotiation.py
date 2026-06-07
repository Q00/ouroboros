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

from ouroboros.orchestrator.adapter import ParamSupport, RuntimeCapabilities

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


def negotiate_execution_params(
    capabilities: RuntimeCapabilities,
    *,
    system_prompt: str | None,
    tools: list[str] | None,
    permission_mode: str | None,
) -> tuple[ParamDegradation, ...]:
    """Report execution parameters the runtime will not honor natively.

    Only parameters that were actually *requested* (non-empty) are considered —
    an absent parameter cannot be degraded. The result is purely informational;
    nothing here changes what the runtime receives.
    """
    requested: list[tuple[str, ParamSupport]] = []
    if system_prompt:
        requested.append(("system_prompt", capabilities.system_prompt_support))
    if tools:
        requested.append(("tools", capabilities.tool_restriction_support))
    if permission_mode:
        requested.append(("permission_mode", capabilities.permission_mode_support))

    degradations = (_degradation(name, support) for name, support in requested)
    return tuple(item for item in degradations if item is not None)


__all__ = [
    "ParamDegradation",
    "negotiate_execution_params",
]
