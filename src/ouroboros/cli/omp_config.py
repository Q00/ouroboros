"""Best-effort configuration for Oh My Pi's MCP tool-call deadline."""

from __future__ import annotations

import shutil
import subprocess

OMP_TOOL_CALL_TIMEOUT_MS = 60_000


def configure_omp_tool_call_timeout(*, dry_run: bool = False) -> bool:
    """Set OMP's active extension-tool deadline when OMP is installed.

    OMP is an optional host. Missing OMP is therefore a successful no-op, while
    a present-but-unwritable configuration is reported to the caller so setup
    and update can surface the remediation without failing Ouroboros itself.
    """
    omp = shutil.which("omp")
    if omp is None:
        return True

    try:
        current = subprocess.run(
            [omp, "config", "get", "extensionHandlers.toolCallTimeoutMs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if current.returncode == 0 and int(current.stdout.strip()) >= OMP_TOOL_CALL_TIMEOUT_MS:
            return True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    if dry_run:
        print(
            "Would run: "
            f"{omp} config set extensionHandlers.toolCallTimeoutMs {OMP_TOOL_CALL_TIMEOUT_MS}"
        )
        return True

    try:
        result = subprocess.run(
            [
                omp,
                "config",
                "set",
                "extensionHandlers.toolCallTimeoutMs",
                str(OMP_TOOL_CALL_TIMEOUT_MS),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


__all__ = ["OMP_TOOL_CALL_TIMEOUT_MS", "configure_omp_tool_call_timeout"]
