"""Recent fan-out findings for one project, as the store recorded publishing them.

RFC Q00/ouroboros#2153: a lane may answer from a finding another lane produced
recently, whichever session produced it. What these lanes report changes on the
system's clock -- a commit lands, data flows -- not on the clock of whoever is
being interviewed, so the boundary that matters is recency, and the session was
a proxy for it that happened to be exact and expensive.

**The store's record is asked, not the directory.** A completed fan-out is
published into the project's artifact store, and the store keeps a manifest of
having published it. Reading those manifests rather than listing the blob
directory is what makes three questions stop being questions --

* *When was this found?* The manifest's publication time, not the body's
  modified time. In a content-addressed store those differ: an identical result
  republished under a new contract reuses the existing body and leaves its
  timestamp untouched, and the modified time is also the one a project can
  rewrite.
* *Is this ours?* A body is reachable because a manifest says this store wrote
  it. Something placed in the blob directory by hand is not excluded by being
  recognised -- nothing refers to it, so it never enters. Content addressing
  alone could not answer this: a chosen body can always be named by its own
  digest.
* *How is it read?* Through the store's own ``fetch``, which is bounded,
  containment-checked and verifies the body against its address. Nothing here
  opens a blob.

The manifest reading uses the same public helpers the store parses its own
manifests with, so this module holds no second copy of what a manifest means.
It lives here rather than on the store because the store is one line under the
module-size cap that #1797 keeps; if it ever has room, this belongs there.

What is left for this module is the one question the store has no opinion about:
which publications this decision admits. The RFC closes that at ``code_context``
and ``data_context`` -- the lanes reporting on the system -- and since one
submission can carry six lanes, eligibility is read per lane and a body carrying
none of them is not offered.

Freshness is the window and nothing else. Nothing here re-establishes at read
time that a finding still holds: drift inside the window is not a gap left open,
it is what the RFC decided.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.persistence.artifact_schema import digest_from_ref
from ouroboros.persistence.artifact_validation import (
    event_artifact_ref,
    latest_artifact_event,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.persistence.artifact_store import ContentAddressedArtifactStore

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
RECENT_FINDINGS_WINDOW = timedelta(days=1)

#: Where the store keeps what it published, and what each record is called.
#: Named here because this module reads them; the store owns writing them.
_CONTRACTS_DIRNAME = "contracts"
_MANIFEST_FILENAME = "events.json"

#: A manifest is a small JSON record. Sized before it is opened so a planted
#: file cannot make a question turn read something arbitrarily large.
_MANIFEST_MAX_BYTES = 1 << 20

#: The kind of fan-out whose results these lanes may read. Persona panels and
#: code investigations publish through the same store and are not this.
_ELIGIBLE_FANOUT_KIND = "question_advisory"

#: The lanes the RFC admits, and the list is closed. A web lane is excluded
#: rather than pending: the window was derived from how a repository drifts, and
#: nothing about this project bounds how fast an external fact turns over.
_ELIGIBLE_LANE_IDS = frozenset({"code_context", "data_context"})

#: How many paths a lane is handed. A bound on the prompt, not on the search:
#: the child reads this list, and a project that ran all day should not spend
#: its attention on the morning. Newest first, so what is cut is the oldest.
_RECENT_FINDINGS_MAX_PATHS = 20


def _published_since(root: Path, since: datetime) -> list[tuple[datetime, str, str]]:
    """Return ``(published_at, contract_id, artifact_ref)`` for recent publications.

    Newest first, and nothing outside the window is looked at further. A
    manifest that cannot be read is skipped rather than raised on: this is a
    reader building a shortlist, and one unreadable record should not cost the
    user their question.
    """
    found: list[tuple[datetime, str, str]] = []
    try:
        contract_dirs = list((root / _CONTRACTS_DIRNAME).iterdir())
    except OSError:
        return []
    for contract_dir in contract_dirs:
        path = contract_dir / _MANIFEST_FILENAME
        # One try around the whole record, because every step below reads a file
        # this process does not own. Naming the exceptions one clause at a time
        # is how a malformed manifest reached the caller: the parse was guarded
        # and the parsing of what it produced was not, and the helpers that read
        # a manifest are written for manifests this store wrote.
        try:
            if path.is_symlink() or path.stat().st_size > _MANIFEST_MAX_BYTES:
                continue
            manifest = json.loads(path.read_bytes())
            if not isinstance(manifest, dict):
                continue
            contract_id = str(manifest.get("contract_id") or "")
            latest = latest_artifact_event(manifest)
            if not contract_id or latest is None or latest.get("type") != "artifact.referenced":
                continue
            published_at = datetime.fromisoformat(str(latest.get("timestamp")))
            artifact_ref = event_artifact_ref(latest, contract_id=contract_id)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        # A record the store wrote carries an offset; one that does not cannot be
        # compared against an aware cutoff at all, so it is read as no answer
        # rather than as an answer at an unknown offset.
        if published_at.tzinfo is None:
            continue
        if published_at >= since:
            found.append((published_at, contract_id, artifact_ref))
    # The reference breaks a same-timestamp tie, so two publications written in
    # one clock tick order the same way on every call rather than by whatever
    # order the directory happened to be read in.
    found.sort(key=lambda item: (item[0], item[2]), reverse=True)
    return found


def _carries_an_eligible_lane(body: Any) -> bool:
    """Return whether this published result holds a lane this decision admits."""
    if not isinstance(body, dict) or body.get("kind") != _ELIGIBLE_FANOUT_KIND:
        return False
    outputs = body.get("result", {}).get("aggregated_outputs")
    if not isinstance(outputs, list):
        return False
    return any(
        isinstance(entry, dict) and entry.get("lane_id") in _ELIGIBLE_LANE_IDS for entry in outputs
    )


def recent_finding_paths(
    findings_store: ContentAddressedArtifactStore | None,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Return this project's recently published eligible findings, newest first.

    A publication is read only to answer whether it carries an eligible lane;
    what it *says* stays the child's to weigh, and none of it travels back with
    the path.

    A store that cannot be read returns nothing. This is advisory: a lane that
    is handed no paths investigates the way it always did, and raising here
    would cost the user their question to save the child a shortcut.
    """
    if findings_store is None:
        return []
    since = (now or datetime.now(UTC)) - RECENT_FINDINGS_WINDOW
    root = Path(findings_store.root)
    offered: list[str] = []
    for _published_at, contract_id, artifact_ref in _published_since(root, since):
        if len(offered) >= _RECENT_FINDINGS_MAX_PATHS:
            break
        try:
            body = findings_store.fetch(contract_id).body
            digest = digest_from_ref(artifact_ref)
        except Exception:
            continue
        if _carries_an_eligible_lane(body):
            offered.append(str(root / digest[:2] / f"{digest}.json"))
    return offered


__all__ = [
    "RECENT_FINDINGS_WINDOW",
    "recent_finding_paths",
]
