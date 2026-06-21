"""Human-readable labels for ouroboros worker sub-agents (provider-neutral).

A worker sub-agent is far easier for a human to find/manage when it carries a
recognizable label instead of an opaque session id. The SAME label drives every
provider's observability surface:

- Codex: the ``thread_name`` written into ``$CODEX_HOME/session_index.jsonl`` so
  the Codex app session picker lists it (see :mod:`codex_session_index`).
- Claude: the ``--name`` passed to ``claude -p`` so the session shows up in the
  ``/resume`` picker and as the agent name (verified: ``--name`` persists a
  ``custom-title`` + ``agent-name`` record even in ``-p`` mode).

Labels are prefixed ``ooo:`` so a human can instantly spot — and clean up —
ouroboros-spawned sessions among their own.
"""

from __future__ import annotations

_LABEL_PREFIX = "ooo: "
_MAX_LABEL = 56
# Lines that are structure, not the human-meaningful task text.
_SKIP_LABELS = frozenset({"task", "deliverable", "scope", "verify"})


def derive_session_label(prompt: str) -> str:
    """Build a concise, human-readable label from a worker prompt.

    Picks the first real content line (skipping ``<assignment>`` fences, markdown
    headings, code fences, and the TASK/DELIVERABLE/SCOPE/VERIFY section labels)
    and prefixes it with ``ooo:`` so the entry is recognizable to a human.
    """
    for raw in (prompt or "").splitlines():
        line = raw.strip().lstrip("#").strip()
        if not line or line.startswith("<") or line.startswith("```"):
            continue
        if line.lower().rstrip(":") in _SKIP_LABELS:
            continue
        label = line[:_MAX_LABEL].rstrip()
        return f"{_LABEL_PREFIX}{label}" if label else f"{_LABEL_PREFIX}worker"
    return f"{_LABEL_PREFIX}worker"


__all__ = ["derive_session_label"]
