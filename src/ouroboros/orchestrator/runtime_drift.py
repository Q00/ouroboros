"""Mid-run drift of frozen runtime authority inputs: observe, don't kill.

A CLI runtime freezes its authority inputs at initialization — the CLI's own
config, the executable, packaged dispatch registries, Ouroboros profile
routing — and used to fail every later AC once any of them changed ("start a
new execution session"). Observed live: a concurrent session's trust write, a
``codex`` upgrade, or an ``ouroboros`` upgrade mid-run killed every remaining
AC in 0.12s while the product was correct.

What those guards protected is narrower than what they enforced: a native
thread created under the old inputs must never be *resumed* under the new
ones. This ledger keeps exactly that. Every observation bumps an epoch;
runtime handles are stamped with the epoch they were created under, and a
handle from an earlier epoch is not resumed — the next turn starts a fresh
thread on the re-baselined inputs. The run itself continues.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from ouroboros import telemetry as usage_telemetry
from ouroboros.orchestrator.adapter import RuntimeHandle

log = get_logger(__name__)

# Closed vocabulary for drift observations. SSOT pairing with
# telemetry._RUNTIME_DRIFT_KINDS — edit both together.
RUNTIME_DRIFT_KINDS = frozenset(
    {
        "codex_config",
        "cli_executable",
        "skill_dispatcher",
        "mcp_handler_registry",
        "skill_dispatch_registry",
        "profile_routing",
        "baseline_unavailable",
    }
)
DRIFT_EPOCH_METADATA_KEY = "ouroboros_runtime_drift_epoch"


class RuntimeDriftLedger:
    """Epoch counter shared by one runtime's drift checks and resume decisions."""

    def __init__(self, *, runtime_backend: str) -> None:
        self._runtime_backend = runtime_backend
        self.epoch = 0

    def observe(self, kind: str, detail: str) -> None:
        """Record one changed input: log, count, and retire existing threads."""
        self.epoch += 1
        log.warning(
            "runtime.drift_observed",
            kind=kind,
            detail=detail,
            drift_epoch=self.epoch,
            runtime_backend=self._runtime_backend,
        )
        usage_telemetry.capture_runtime_drift(kind if kind in RUNTIME_DRIFT_KINDS else "unknown")

    def stamp(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return handle metadata carrying the current epoch."""
        return {**(metadata or {}), DRIFT_EPOCH_METADATA_KEY: self.epoch}

    def handle_predates_drift(self, runtime_handle: RuntimeHandle | None) -> bool:
        """Return True when the handle's thread was created before a drift."""
        if runtime_handle is None or self.epoch == 0:
            return False
        stamped = runtime_handle.metadata.get(DRIFT_EPOCH_METADATA_KEY)
        return not isinstance(stamped, int) or stamped != self.epoch


__all__ = ["DRIFT_EPOCH_METADATA_KEY", "RUNTIME_DRIFT_KINDS", "RuntimeDriftLedger"]
