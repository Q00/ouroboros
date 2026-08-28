"""Codex-side mapping for the orchestrator-level ``runtime_profile``.

The ``OrchestratorConfig.runtime_profile`` setting names a profile in the
orchestrator's own vocabulary (e.g. ``"worker"``). The Codex backend
translates that name to its own ``--profile`` identifier and applies it
at command-build time. Both the orchestrator runtime
(``ouroboros.orchestrator.codex_cli_runtime``) and the LLM provider
adapter (``ouroboros.providers.codex_cli_adapter``) share this module so
the mapping is single-sourced.

The module is intentionally Codex-local. Future Agent OS phases that add
OpenCode, Hermes, Claude Code, or LiteLLM mappings should each provide
their own backend-local mapping module rather than expanding this one —
the orchestrator surface only owns the *name*, not the per-backend
translation.

It also lives outside of ``ouroboros.orchestrator`` to avoid a circular
import: ``ouroboros.orchestrator.__init__`` pulls in the runner, which
pulls in the providers package, which is what imports this module from
the Codex LLM adapter.
"""

from __future__ import annotations

from collections.abc import Callable
import subprocess
from typing import Any

# Maps the orchestrator-level ``runtime_profile`` value to the Codex-side
# ``--profile`` name. Phase 1 only ships ``worker``; new entries should land
# alongside the matching ``[profiles.<name>]`` section written by setup so
# operators always have a managed home for the per-profile overrides.
RUNTIME_PROFILE_TO_CODEX_PROFILE: dict[str, str] = {
    "worker": "ouroboros-worker",
}

# Grace period for the ``codex --help`` contract probe. A slow or wedged CLI
# must not stall command building indefinitely; exceeding it is uncertainty
# (``None``), never evidence of a legacy CLI.
HELP_PROBE_TIMEOUT_SECONDS = 5.0


def codex_uses_profile_v2(
    codex_path: str | None,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool | None:
    """Detect whether ``--profile`` selects ``<name>.config.toml`` files.

    ``True`` means the unified profile-v2 selector, ``False`` means the
    legacy split selector, and ``None`` means the CLI contract could not be
    determined.  In particular, a help timeout or OS error is uncertainty,
    not evidence that the CLI is legacy.
    """
    if codex_path is None:
        return None

    try:
        result = run_command(
            [codex_path, "--help"],
            capture_output=True,
            text=True,
            timeout=HELP_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    help_text = f"{result.stdout}\n{result.stderr}"
    lines = help_text.splitlines()
    for index, line in enumerate(lines):
        if "--profile-v2" in line:
            continue
        if "--profile" not in line:
            continue

        description_lines: list[str] = []
        for description_line in lines[index + 1 :]:
            stripped = description_line.strip()
            if stripped.startswith("-") or stripped.startswith("--"):
                break
            if stripped:
                description_lines.append(stripped)
        if any(
            "Layer $CODEX_HOME/<name>.config.toml on top of the base user config"
            in description_line
            for description_line in description_lines
        ):
            return True
        if "<CONFIG_PROFILE>" in line or any(
            "Configuration profile from config.toml" in description_line
            for description_line in description_lines
        ):
            return False
        return None

    return None


def resolve_codex_profile(
    runtime_profile: str | None,
    *,
    logger: Any,
    log_namespace: str,
) -> str | None:
    """Translate an orchestrator runtime_profile to a Codex ``--profile`` name.

    Args:
        runtime_profile: The orchestrator-level profile name. ``None`` or an
            empty string means "no profile requested" — every Codex code path
            then preserves its current default user-config behaviour.
        logger: The caller's structured logger (the same object the call site
            uses for its own warnings). Passing it through keeps the warning
            attributable to that namespace and keeps existing
            ``patch("module.log.warning")`` test seams intact.
        log_namespace: Caller's structured-log namespace (e.g.
            ``"codex_cli_runtime"`` or ``"codex_cli_adapter"``). Used as the
            event prefix for the ``runtime_profile_unmapped`` warning so the
            event name carries the call site even though the function lives
            in a shared module.

    Returns:
        The Codex profile name when the orchestrator profile maps to one,
        otherwise ``None``. An unmapped non-empty value emits a structured
        warning via the caller's logger so the existing fallback path runs
        without surprises.
    """
    if not runtime_profile:
        return None
    mapped = RUNTIME_PROFILE_TO_CODEX_PROFILE.get(runtime_profile)
    if mapped is None:
        logger.warning(
            f"{log_namespace}.runtime_profile_unmapped",
            runtime_profile=runtime_profile,
            hint="No Codex backend mapping; running without --profile.",
        )
    return mapped


__all__ = [
    "RUNTIME_PROFILE_TO_CODEX_PROFILE",
    "codex_uses_profile_v2",
    "resolve_codex_profile",
]
