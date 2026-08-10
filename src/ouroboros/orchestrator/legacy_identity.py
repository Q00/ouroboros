"""Pre-anchor (v9) project-identity representations — transitional (#1799).

Sessions started before the Project Map anchor landed (2026-07-29, #1769)
carry no ``project_id`` on their durable ``orchestrator.session.started``
events. Rewriting their resume authority under the current resolver would
make resume disagree with the immutable start event, so those sessions —
and only those — keep the exact pre-anchor representation reproduced here.

This surface is transitional, not permanent:

- **Trigger.** The legacy path is taken only when the persisted start
  snapshot predates the anchor (``has_project_anchor`` is false).
- **Observability.** An activation emits the structured event
  ``project_map.legacy_identity_path`` only when a durable start-identity
  snapshot is present and still lacks the anchor; current prepared
  executions restore an intentionally anchorless contract-only snapshot and
  never count. The event is an advisory liveness signal only — the default
  log sink retains seven days and may be absent — so log absence carries no
  removal authority.
- **Removal criterion** (mirrored in ``docs/rfc/project-map-v1.md``):
  delete this module and the ``has_project_anchor`` legacy branch only
  after a fail-closed inventory preflight over the durable EventStore
  finds no persisted session whose start-identity snapshot still lacks the
  anchor. A session that cannot be enumerated or whose snapshot cannot be
  read counts as pre-anchor, so missing evidence blocks removal instead of
  authorizing it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from ouroboros.core.worktree import TaskWorkspace

log = structlog.get_logger(__name__)


def note_legacy_identity_path(entry_point: str, **context: Any) -> None:
    """Counter-style structured event marking one legacy-path activation."""
    log.info(
        "project_map.legacy_identity_path",
        entry_point=entry_point,
        **context,
    )


def legacy_task_workspace_identity(
    workspace: TaskWorkspace, canonical: Callable[[str], str]
) -> dict[str, str]:
    """Reproduce the pre-anchor managed-workspace representation exactly."""
    project_root = Path(canonical(workspace.repo_root))
    source_workspace = Path(canonical(workspace.original_cwd))
    try:
        workspace_path = source_workspace.relative_to(project_root).as_posix() or "."
    except ValueError as exc:
        # Deferred: OrchestratorError lives in runner, which imports this module.
        from ouroboros.orchestrator.runner import OrchestratorError

        raise OrchestratorError(
            message="Cannot resume from an invalid historical task workspace",
            details={"invalid": "legacy_task_workspace"},
        ) from exc
    return {
        "project_root": str(project_root),
        "workspace_path": workspace_path,
    }
