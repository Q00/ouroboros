"""Recent fan-out findings for one project, narrowed to what a lane may reuse.

RFC Q00/ouroboros#2153: a lane may answer from a finding another lane produced
recently, whichever session produced it. What these lanes report changes on the
system's clock -- a commit lands, data flows -- not on the clock of whoever is
being interviewed, so the boundary that matters is recency, and the session was
a proxy for it that happened to be exact and expensive.

**The store is asked for when and what kind.** A completed fan-out is published
into the project's artifact store, and the store keeps a record of having
published it. ``published_contracts`` answers from that record because
publication time exists nowhere else, and its ``kind`` filter reads the same
field of the body this module used to open every candidate to check.

**Nothing here reads storage directly.** Publication times come from the store's
own query and bodies from its ``fetch``. That is not a boundary against a
project -- RFC #2153 puts the project workspace inside the trust boundary, and
anything able to write there can more easily edit the source a lane would cite.
It is ownership: this module once knew the on-disk shape of a contract record,
and the store that stopped being a directory would have broken it silently.

What is left for this module is the one question the store has no opinion about:
which publications this decision admits. The RFC closes that at ``code_context``
and ``data_context`` -- the lanes reporting on the system -- and since one
submission can carry six lanes, eligibility is read per lane and only the
eligible lanes of a body travel onward.

What travels is where a finding is -- a contract id, the lane that produced it,
and a publication time -- and a lane fetches its own by passing both values
back. Bodies travelled once and could not: the same block was copied into every
lane of the turn, the tool result outgrew what a host takes inline, and the
turn lost its fan-out entirely.

The narrowing is the store's (RFC Q00/ouroboros#2167): the fetch takes the lane
beside the contract and returns that lane's output alone, so there is no
selecting left for the child and no rule for it to carry out. That rule once
cost a lane its findings: told to select from a body it had to open, it
selected against the wrong shape, found nothing, and re-investigated -- silently,
since a lane with nothing to reuse looks exactly like a project with nothing to
reuse. Which is why the count travels beside the ids: a lane that cannot reach
the fetch tool still knows something is there, and can say so instead of
reporting emptiness.

Freshness is the window and nothing else. Nothing here re-establishes at read
time that a finding still holds: drift inside the window is not a gap left open,
it is what the RFC decided.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ouroboros.persistence.artifact_errors import ArtifactStoreError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.persistence.artifact_store import ArtifactStore

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

#: The kind of fan-out whose results these lanes may read. Persona panels and
#: code investigations publish through the same store and are not this. The
#: store's query filters on it, so ineligible kinds are never even fetched.
_ELIGIBLE_FANOUT_KIND = "question_advisory"

#: The lanes the RFC admits, and the list is closed. A web lane is excluded
#: rather than pending: the window was derived from how a repository drifts, and
#: nothing about this project bounds how fast an external fact turns over.
_ELIGIBLE_LANE_IDS = frozenset({"code_context", "data_context"})

#: How many findings a lane is offered. A bound on attention, not on the
#: search: the child chooses among these, and a project that ran all day should
#: not spend its attention on the morning. Newest first, so what is cut is the
#: oldest.
#:
#: It is also the whole bound on the prompt now, which it could not be while
#: bodies travelled -- a count says nothing about how much twenty of something
#: is, and twenty findings inlined outgrew what a host takes in one response.
#: An id costs the same whatever the finding says, so counting them is counting
#: characters.
_RECENT_FINDINGS_MAX_ENTRIES = 20
_INTERVIEW_BASELINE_LANE_IDS = frozenset({"code_context", "web_context"})


def interview_baseline_by_lane(
    findings_store: ArtifactStore | None,
    *,
    session_id: str,
    now: datetime | None = None,
) -> dict[str, dict]:
    """Return this interview's newest fresh start-turn factual snapshot.

    The server-authored synthesis provenance supplies session and phase identity;
    child prose never grants reuse authority. A missing, stale, malformed, or
    incomplete artifact simply falls back to normal factual fan-out.
    """
    if findings_store is None or not session_id:
        return {}
    effective_now = now or datetime.now(UTC)
    try:
        published = findings_store.published_contracts(
            since=effective_now - RECENT_FINDINGS_WINDOW,
            until=effective_now,
            kind=_ELIGIBLE_FANOUT_KIND,
        )
    except (ArtifactStoreError, OSError):
        return {}
    for candidate in published:
        try:
            fetched = findings_store.fetch(candidate.contract_id)
        except Exception:
            continue
        body = fetched.body
        if not isinstance(body, dict):
            continue
        provenance = body.get("provenance")
        if not isinstance(provenance, dict):
            continue
        if provenance.get("session_id") != session_id or provenance.get("phase") != "start":
            continue
        result = body.get("result")
        outputs = result.get("aggregated_outputs") if isinstance(result, dict) else None
        if not isinstance(outputs, list):
            continue
        carried = {
            entry["lane_id"]
            for entry in outputs
            if isinstance(entry, dict) and entry.get("lane_id") in _INTERVIEW_BASELINE_LANE_IDS
        }
        lanes = carried & _INTERVIEW_BASELINE_LANE_IDS
        if not lanes:
            continue
        return {
            lane_id: {
                "contract_id": candidate.contract_id,
                "lane_id": lane_id,
                "published_at": candidate.published_at.isoformat(),
            }
            for lane_id in sorted(lanes)
        }
    return {}


def _eligible_lane_ids(body: Any) -> set[str]:
    """Return which eligible lanes one published body carries.

    The fan-out kind was already decided by the store's query; what is judged
    here is the lanes. Empty for a body whose lanes are all ineligible -- an
    interview turn runs six lanes, and a contrarian's challenge reused as
    evidence answers a question nobody asked.

    Only the lane ids are read. What a lane *wrote* stays in the store and is
    reached by the lane itself, so nothing here carries a body onward.
    """
    if not isinstance(body, dict):
        return set()
    result = body.get("result")
    if not isinstance(result, dict):
        return set()
    outputs = result.get("aggregated_outputs")
    if not isinstance(outputs, list):
        return set()
    return {
        entry["lane_id"]
        for entry in outputs
        if isinstance(entry, dict) and entry.get("lane_id") in _ELIGIBLE_LANE_IDS
    }


def recent_findings_by_lane(
    findings_store: ArtifactStore | None,
    *,
    lanes: Collection[str] | None = None,
    now: datetime | None = None,
) -> dict[str, list[dict]]:
    """Return, per eligible lane, the recent findings that lane itself published.

    Keyed by lane id, and a lane with none is absent rather than empty. What
    each entry carries is ``contract_id``, ``lane_id`` and ``published_at`` --
    where the finding is and when it was made, never what it said. A lane reads
    its own with ``ouroboros_fetch_artifact``, passing both back.

    **The lane is a second value, not a suffix on the first.** A fan-out
    publishes one artifact carrying every lane it dispatched, so a contract id
    names the turn and fetching one returned every sibling's output. Folding the
    lane into that id narrowed what came back but left a string two readings
    could claim: contract ids are bounded by length and nothing else, so an
    ordinary id containing the separator was taken apart and its artifact went
    missing. Two values travel as two values.

    **Bodies do not travel, and that is the whole of it.** They did once: every
    lane of the turn received every eligible finding inline, so one turn carried
    six copies of the same text and the tool result outgrew what a host will
    take inline. Written to a file instead, it stopped being a fan-out at all --
    the host spent the turn reading its own output rather than dispatching. Ids
    cost the same whatever a lane wrote.

    **A lane is offered only its own** (RFC Q00/ouroboros#2167). The four
    reasoning lanes are absent from this mapping: a lane that produces no fact
    that keeps consumes none either, and handing one a code fact is a new
    capability rather than a cache hit.

    ``lanes`` narrows it further to the lanes a particular tool declares.
    Eligibility follows what a lane produces, never the shape of its answer:
    a contracted lane is offered its own findings exactly as a prose lane is
    (#2223) -- what differs is only the offer text it is handed, decided where
    the prompt is rendered. Absent, every eligible lane is returned.

    A store that cannot be read returns nothing, and so does a single record
    that cannot be read: the rest of the store still answers. This is advisory
    -- a lane offered no findings investigates the way it always did, and
    raising here would cost the user their question to save the child a
    shortcut.
    """
    if findings_store is None:
        return {}
    readable = _ELIGIBLE_LANE_IDS if lanes is None else _ELIGIBLE_LANE_IDS & set(lanes)
    if not readable:
        return {}
    effective_now = now or datetime.now(UTC)
    try:
        published_contracts = findings_store.published_contracts(
            since=effective_now - RECENT_FINDINGS_WINDOW,
            until=effective_now,
            kind=_ELIGIBLE_FANOUT_KIND,
        )
    except (ArtifactStoreError, OSError):
        return {}
    by_lane: dict[str, list[dict]] = {}
    for published in published_contracts:
        if by_lane.keys() >= readable and all(
            len(found) >= _RECENT_FINDINGS_MAX_ENTRIES for found in by_lane.values()
        ):
            break
        try:
            fetched = findings_store.fetch(published.contract_id)
            carried = _eligible_lane_ids(fetched.body)
        except Exception:
            # Reading the record's shape is inside the boundary, not after it.
            # A body is whatever was published, so the shapes it can take are
            # not a list this module can finish writing -- and one that ends up
            # outside the boundary costs every later finding in the window, not
            # just its own.
            continue
        for lane_id in sorted(carried & readable):
            found = by_lane.setdefault(lane_id, [])
            if len(found) >= _RECENT_FINDINGS_MAX_ENTRIES:
                continue
            found.append(
                {
                    "contract_id": published.contract_id,
                    "lane_id": lane_id,
                    "published_at": published.published_at.isoformat(),
                }
            )
    return by_lane


__all__ = [
    "RECENT_FINDINGS_WINDOW",
    "interview_baseline_by_lane",
    "recent_findings_by_lane",
]
