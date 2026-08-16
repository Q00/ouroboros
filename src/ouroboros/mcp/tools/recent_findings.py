"""Recent fan-out findings for one project, addressed by where they were written.

RFC Q00/ouroboros#2153: a lane may answer from a finding another lane produced
recently, whichever session produced it. What a lane reports changes on the
system's clock -- a commit lands, data flows -- not on the clock of whoever is
being interviewed, so the boundary that matters is recency, and the session was
a proxy for it that happened to be exact and expensive.

Nothing here is an index. A completed fan-out is published into the project's
artifact store, and the store is already organised the way a lane needs to read
it: per project, newest last, expiring on its own schedule. So "what has been
found recently here" is a listing of that directory, which is why this file
holds no state of its own and nothing has to be kept in step with the store.
"""

from __future__ import annotations

from pathlib import Path
import time

#: How far back a finding still counts as describing the system as it is now.
#:
#: A day, in the sense the RFC decided: long enough that a second session on the
#: same repository starts warm rather than cold, short enough that a policy claim
#: or an aggregate has not moved under it. Expressed as a rolling window rather
#: than a calendar day deliberately -- a calendar boundary empties mid-session at
#: midnight, and it would empty into exactly the state that means "nothing has
#: been found here", which is the one failure this whole mechanism keeps
#: producing. A rolling window has no such edge.
RECENT_FINDINGS_WINDOW_SECONDS = 24 * 60 * 60

#: How many paths a lane is handed. A bound on the prompt, not on the search:
#: the child reads this list, and a project that ran all day should not spend
#: its attention on the morning. Newest first, so what is cut is the oldest.
_RECENT_FINDINGS_MAX_PATHS = 20


def recent_finding_paths(
    findings_root: Path | str | None,
    *,
    now: float | None = None,
) -> list[str]:
    """Return this project's recently published finding files, newest first.

    Metadata only: the modified time decides, and no file is opened. What a
    finding says is the child's business, and reading them here to decide which
    are worth handing over would pay the cost of every file to save the child a
    choice it is better placed to make.

    An unreadable or absent root returns nothing. This is advisory -- a lane
    that is handed no paths investigates the way it always did, and raising here
    would cost the user their question to save the child a shortcut.
    """
    if findings_root is None:
        return []
    root = Path(findings_root)
    cutoff = (time.time() if now is None else now) - RECENT_FINDINGS_WINDOW_SECONDS
    found: list[tuple[float, str]] = []
    try:
        # Content-addressed bodies live one shard directory deep. ``contracts``
        # and ``bindings`` hold the store's own bookkeeping and are not findings,
        # so the shape of the walk is what excludes them rather than a list of
        # directory names this module would have to keep in step.
        for shard in root.iterdir():
            if not shard.is_dir() or len(shard.name) != 2:
                continue
            for path in shard.glob("*.json"):
                try:
                    written = path.stat().st_mtime
                except OSError:
                    continue
                if written >= cutoff:
                    found.append((written, str(path)))
    except OSError:
        return []
    # The path breaks a same-timestamp tie, so two findings written in one clock
    # tick order the same way on every call rather than by whatever order the
    # directory happened to be read in.
    found.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in found[:_RECENT_FINDINGS_MAX_PATHS]]


__all__ = ["RECENT_FINDINGS_WINDOW_SECONDS", "recent_finding_paths"]
