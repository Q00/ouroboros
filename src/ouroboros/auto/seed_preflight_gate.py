"""Pipeline-facing Seed preflight enforcement."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ouroboros.auto.seed_preflight import run_seed_preflight
from ouroboros.auto.state import AutoPipelineState
from ouroboros.core.seed import Seed


async def enforce_seed_preflight(
    seed: Seed,
    state: AutoPipelineState,
    emit: Callable[[str, str, dict[str, Any]], Awaitable[None]],
) -> str | None:
    """Return and persist a blocker description for unverifiable Seed claims."""
    workspace_root: Path | None = None
    if state.cwd:
        candidate = Path(state.cwd).expanduser()
        if candidate.is_dir():
            workspace_root = candidate
    report = run_seed_preflight(seed, workspace_root=workspace_root)
    if report.passed:
        return None
    questions = list(report.open_questions)[:8]
    await emit(
        "auto.seed_preflight.blocked",
        state.auto_session_id,
        {
            "schema_version": 1,
            "auto_session_id": state.auto_session_id,
            "seed_id": seed.metadata.seed_id,
            "codes": [finding.code for finding in report.blocking_findings][:8],
            "open_questions": questions,
        },
    )
    blocker = (
        f"Seed preflight found {len(report.blocking_findings)} unexecutable contract claim(s); "
        "answer these open questions, revise the Seed, and start a new session: "
        + " | ".join(questions)
    )
    state.mark_blocked(
        blocker, tool_name="seed_preflight", error_code="seed_preflight_unexecutable"
    )
    return blocker
