"""Make ouroboros worker sub-agents VISIBLE in the Codex app (human observability).

Worker sessions spawned via ``codex mcp-server`` are persisted as rollout files
(``$CODEX_HOME/sessions/.../rollout-*.jsonl``) but are NOT added to
``$CODEX_HOME/session_index.jsonl`` — the index the Codex app's session picker
reads. Verified empirically: a worker rollout exists on disk yet never appears in
the picker, and the app does NOT rebuild the index from rollouts, so the entry
must be appended explicitly.

This module appends one index entry per worker session so a human can SEE each
sub-agent in the Codex app and ``codex resume`` it. It is:

- **schema-matched** to real entries: ``{"id", "thread_name", "updated_at"}``;
- **append-only** — it never rewrites or truncates the user's index;
- **best-effort** — any failure is swallowed (observability must never break a run);
- **identifiable** — labels are prefixed ``ooo:`` so the user can spot/clean them.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path

from ouroboros.observability.logging import get_logger

# ``derive_session_label`` is provider-neutral (Codex index + Claude --name share
# it); it lives in :mod:`subagent_label`. Re-exported here for back-compat.
from ouroboros.orchestrator.subagent_label import derive_session_label

log = get_logger(__name__)


def _resolve_codex_home(codex_home: str | Path | None = None) -> Path:
    if codex_home:
        return Path(codex_home).expanduser()
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def register_codex_session(
    thread_id: str | None,
    thread_name: str,
    *,
    codex_home: str | Path | None = None,
) -> bool:
    """Append a session-index entry so the Codex app lists this worker session.

    Returns True if an entry was written, False otherwise. Never raises — this is
    pure observability and must not affect execution.
    """
    try:
        if not thread_id:
            return False
        home = _resolve_codex_home(codex_home)
        if not home.is_dir():
            return False
        index = home / "session_index.jsonl"
        entry = {
            "id": thread_id,
            "thread_name": thread_name,
            # Match codex's microsecond-precision Z format exactly.
            "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }
        with index.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — observability is strictly best-effort
        log.debug("codex_session_index.register_failed", error=str(exc))
        return False


# ``derive_session_label`` is re-exported (imported above) for callers that have
# historically imported it from this module.
__all__ = ["derive_session_label", "register_codex_session"]
