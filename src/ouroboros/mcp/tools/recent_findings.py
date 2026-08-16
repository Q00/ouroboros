"""Recent fan-out findings for one project, addressed by where they were written.

RFC Q00/ouroboros#2153: a lane may answer from a finding another lane produced
recently, whichever session produced it. What these lanes report changes on the
system's clock -- a commit lands, data flows -- not on the clock of whoever is
being interviewed, so the boundary that matters is recency, and the session was
a proxy for it that happened to be exact and expensive.

Nothing here is an index. A completed fan-out is published into the project's
artifact store, and the store is already organised the way a lane needs to read
it: per project, newest last, expiring on its own schedule. So "what has been
found recently here" is a listing of that directory, which is why this file
holds no state of its own and nothing has to be kept in step with the store.

**A listing is not authority, so what a listing offers is checked.** The store's
bodies share one namespace with every other fan-out kind and with artifacts that
are not fan-outs at all, and the directory is inside a workspace this process
does not own. Two things follow, and both are checks on the way out rather than
rules the reading child is asked to remember:

* An offered path is a regular file inside this root whose name is the digest of
  its own bytes. Content addressing is the store's own naming rule, so a file
  that is not what its name says is not one of its files -- a symlink out of the
  project and a planted body are both excluded by the same test, and neither
  becomes a condition to detect further downstream.
* An offered finding is one this decision made eligible. The RFC closes the list
  at ``code_context`` and ``data_context``: those report on the system, and a
  lane that challenges a question or drafts answers for it produces reasoning
  about one question rather than a fact that keeps. Since a single body can hold
  both -- an interview turn runs six lanes into one submission -- eligibility is
  read per lane, and a body carrying no eligible lane is not offered at all.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

#: How far back a finding still counts as describing the system as it is now.
#:
#: A day, in the sense the RFC decided: the window is where two forces balance.
#: Repetition wants it wide -- the same person asks about the same subsystems all
#: day, and the wider it is the more of that is reachable. Drift wants it narrow
#: -- code and data keep moving, and every hour a finding is a slightly worse
#: statement about now.
#:
#: Rolling rather than calendar, deliberately: a calendar boundary empties at
#: midnight, and it empties into exactly the state that means "nothing has been
#: found here", which is the one failure this mechanism keeps producing.
RECENT_FINDINGS_WINDOW_SECONDS = 24 * 60 * 60

#: The kind of fan-out whose results these lanes may read. Persona panels and
#: code investigations publish into the same namespace and are not this.
_ELIGIBLE_FANOUT_KIND = "question_advisory"

#: The lanes the RFC leaves eligible, and the list is closed. A web lane is
#: excluded rather than pending: the window above was derived from how a
#: repository drifts, and nothing about this project bounds how fast an external
#: fact turns over.
_ELIGIBLE_LANE_IDS = frozenset({"code_context", "data_context"})

#: A content-addressed body is a SHA-256 digest of its own bytes.
_ARTIFACT_NAME_RE = re.compile(r"^[0-9a-f]{64}$")

#: How many paths a lane is handed. A bound on the prompt, not on the search:
#: the child reads this list, and a project that ran all day should not spend
#: its attention on the morning. Newest first, so what is cut is the oldest.
_RECENT_FINDINGS_MAX_PATHS = 20


def _eligible_body(raw: bytes) -> bool:
    """Return whether these bytes are a fan-out result this decision admits."""
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(body, dict) or body.get("kind") != _ELIGIBLE_FANOUT_KIND:
        return False
    outputs = body.get("result", {}).get("aggregated_outputs")
    if not isinstance(outputs, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("lane_id") in _ELIGIBLE_LANE_IDS for entry in outputs
    )


def _offerable(path: Path) -> bool:
    """Return whether this file is one of the store's and one a lane may read.

    The name is checked against the bytes rather than the path against a list of
    forbidden shapes. A symlink pointing out of the project fails because what it
    resolves to does not hash to the name it was found under, and so does a body
    someone dropped in by hand -- one test, and neither case needs recognising.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if not _ARTIFACT_NAME_RE.fullmatch(path.stem):
            return False
        raw = path.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(raw).hexdigest() != path.stem:
        return False
    return _eligible_body(raw)


def recent_finding_paths(
    findings_root: Path | str | None,
    *,
    now: float | None = None,
) -> list[str]:
    """Return this project's recently published eligible findings, newest first.

    Recency is decided from metadata, so a body outside the window is never
    read. Inside it, a body is read to establish that it is the store's and that
    it carries a lane this decision admits -- but what it *says* stays the
    child's to weigh, and none of it travels back with the path.

    An unreadable or absent root returns nothing. This is advisory: a lane that
    is handed no paths investigates the way it always did, and raising here
    would cost the user their question to save the child a shortcut.
    """
    if findings_root is None:
        return []
    root = Path(findings_root)
    cutoff = (time.time() if now is None else now) - RECENT_FINDINGS_WINDOW_SECONDS
    recent: list[tuple[float, Path]] = []
    try:
        # Content-addressed bodies live one shard directory deep. ``contracts``
        # and ``bindings`` hold the store's own bookkeeping and are not findings,
        # so the shape of the walk is what excludes them rather than a list of
        # directory names this module would have to keep in step.
        for shard in root.iterdir():
            if shard.is_symlink() or not shard.is_dir() or len(shard.name) != 2:
                continue
            for path in shard.glob("*.json"):
                try:
                    written = path.stat().st_mtime
                except OSError:
                    continue
                if written >= cutoff:
                    recent.append((written, path))
    except OSError:
        return []
    # The path breaks a same-timestamp tie, so two findings written in one clock
    # tick order the same way on every call rather than by whatever order the
    # directory happened to be read in.
    recent.sort(key=lambda item: (-item[0], str(item[1])))
    offered: list[str] = []
    for _, path in recent:
        if len(offered) >= _RECENT_FINDINGS_MAX_PATHS:
            break
        if _offerable(path):
            offered.append(str(path))
    return offered


__all__ = [
    "RECENT_FINDINGS_WINDOW_SECONDS",
    "recent_finding_paths",
]
