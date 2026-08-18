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

What travels is the finding itself, one flat entry per eligible lane. The
narrowing happens here, where the eligible-lane rule already lives, so the child
is handed evidence rather than a place to look and a rule for looking. Handing
over the place cost a lane its findings once: told to select from a body it had
to open, it selected against the wrong shape, found nothing, and re-investigated
-- silently, since a lane with nothing to reuse looks exactly like a project
with nothing to reuse.

Freshness is the window and nothing else. Nothing here re-establishes at read
time that a finding still holds: drift inside the window is not a gap left open,
it is what the RFC decided.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
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

#: How many findings a lane is handed. A bound on attention, not on the search:
#: the child reads these, and a project that ran all day should not spend its
#: attention on the morning. Newest first, so what is cut is the oldest. What
#: bounds the prompt is the budget below -- twenty entries is a count, and a
#: count says nothing about how much twenty of something is.
_RECENT_FINDINGS_MAX_ENTRIES = 20

#: How large the rendered block may be. Counting entries does not bound a
#: prompt -- a finding is whatever a lane wrote, and the store admits a body of
#: 1 MiB, so twenty of them is twenty megabytes into every lane of every turn.
#: The number matches ``_INTERVIEW_DATA_CONTRACT_MAX_JSON_CHARS``, the largest
#: block an advisory prompt already carries; findings are evidence and may be
#: the largest thing in it, but not by an order of magnitude.
_RECENT_FINDINGS_MAX_CHARS = 24_000

#: What a shortened finding says about itself, spelled the way every other
#: bounded block in an advisory prompt spells it.
_TRUNCATION_MARKER = "... [truncated]"


def render_recent_findings(entries: list[dict]) -> str:
    """Render the entries exactly as a prompt carries them.

    The budget below and the prompt that receives it call this same function,
    so there is no second rendering for a measurement to be taken against and
    be wrong about. Encoded rather than written out: a finding may contain a
    newline or a line that reads as a heading, and written plainly the tail of
    one would become structure in whatever prompt holds it.
    """
    return json.dumps(entries, ensure_ascii=False, indent=2)


def _within_prompt_budget(entries: list[dict]) -> list[dict]:
    """Take the newest entries whose rendering fits, and never take none.

    The budget is a ceiling with nothing added on top of it, so what a lane
    receives is bounded by one number rather than by one number plus whatever
    the largest single finding happened to be.

    Which forces the last entry to be shortened rather than dropped, because a
    budget that can return nothing would hand a lane the one state this whole
    mechanism exists to prevent: silence that reads as "this project has
    nothing" while it has something (RFC Q00/ouroboros#2168, carried from
    #2167). A shortened finding is not that -- it says what it says up to the
    cut and says that it was cut, and a lane investigates the rest the way it
    would have anyway.
    """
    admitted: list[dict] = []
    for entry in entries:
        candidate = [*admitted, entry]
        if len(render_recent_findings(candidate)) > _RECENT_FINDINGS_MAX_CHARS:
            return admitted or [_shortened_to_fit(entry)]
        admitted = candidate
    return admitted


def _shortened_to_fit(entry: dict) -> dict:
    """Fit one finding that does not fit, by shortening what it reported.

    The other fields stay whole: a contract id and a publication time cost
    nothing and are what makes the entry legible as a finding at all. Only
    ``output`` is shortened, and it becomes rendered text rather than staying
    a structure, because half a structure is not one.

    The loop rather than one slice: escaping expands, so the length of a
    rendering is not the length of what was sliced. Each pass removes at least
    the overflow it measured, and removing a source character never adds a
    rendered one, so it terminates.
    """
    text = json.dumps(entry["output"], ensure_ascii=False)
    keep = len(text)
    while keep > 0:
        candidate = {**entry, "output": text[:keep] + _TRUNCATION_MARKER}
        overflow = len(render_recent_findings([candidate])) - _RECENT_FINDINGS_MAX_CHARS
        if overflow <= 0:
            return candidate
        keep -= max(overflow, 1)
    return {**entry, "output": _TRUNCATION_MARKER}


def _eligible_lane_outputs(body: Any) -> list[tuple[str, Any]]:
    """Return ``(lane_id, output)`` for the lanes of one body this decision admits.

    The fan-out kind was already decided by the store's query; what is judged
    here is the lanes. Empty for a body whose lanes are all ineligible -- an
    interview turn runs six lanes, and a contrarian's challenge reused as
    evidence answers a question nobody asked.
    """
    if not isinstance(body, dict):
        return []
    result = body.get("result")
    if not isinstance(result, dict):
        return []
    outputs = result.get("aggregated_outputs")
    if not isinstance(outputs, list):
        return []
    return [
        (entry["lane_id"], entry.get("output"))
        for entry in outputs
        if isinstance(entry, dict) and entry.get("lane_id") in _ELIGIBLE_LANE_IDS
    ]


def recent_findings_entries(
    findings_store: ArtifactStore | None,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Return this project's recently published eligible findings, newest first.

    One entry per eligible lane -- ``contract_id``, ``published_at``, ``lane_id``
    and ``output`` -- so what reaches the child is the finding and not a rule for
    extracting it.

    Bounded by how much it renders to, not only by how many entries it is: this
    is the retrieval interface, and how much comes back at once is its decision
    to make.

    A store that cannot be read returns nothing, and so does a single record that
    cannot be read: the rest of the store still answers. This is advisory -- a
    lane that is handed no findings investigates the way it always did, and
    raising here would cost the user their question to save the child a shortcut.
    """
    if findings_store is None:
        return []
    effective_now = now or datetime.now(UTC)
    try:
        published_contracts = findings_store.published_contracts(
            since=effective_now - RECENT_FINDINGS_WINDOW,
            until=effective_now,
            kind=_ELIGIBLE_FANOUT_KIND,
        )
    except (ArtifactStoreError, OSError):
        return []
    entries: list[dict] = []
    for published in published_contracts:
        if len(entries) >= _RECENT_FINDINGS_MAX_ENTRIES:
            break
        try:
            fetched = findings_store.fetch(published.contract_id)
            lanes = _eligible_lane_outputs(fetched.body)
        except Exception:
            # Reading the record's shape is inside the boundary, not after it.
            # A body is whatever was published, so the shapes it can take are
            # not a list this module can finish writing -- and one that ends up
            # outside the boundary costs every later finding in the window, not
            # just its own.
            continue
        for lane_id, output in lanes:
            entries.append(
                {
                    "contract_id": published.contract_id,
                    "published_at": published.published_at.isoformat(),
                    "lane_id": lane_id,
                    "output": output,
                }
            )
    return _within_prompt_budget(entries[:_RECENT_FINDINGS_MAX_ENTRIES])


__all__ = [
    "RECENT_FINDINGS_WINDOW",
    "recent_findings_entries",
    "render_recent_findings",
]
