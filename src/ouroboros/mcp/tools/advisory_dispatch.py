"""Interview advisory fan-out rendered onto the channel the host reads.

Companion to ``mcp/tools/advisory_prompts.py``. That module holds the words a
*child* is judged by; this one holds the words the *host* is instructed by. The
split is the audience, not the format: a child prompt may be rewritten freely,
while this text is a contract the host is expected to act on verbatim.

``_attach_question_assist_requests`` stamps the payloads, the correlation key,
and the host action onto the response ``meta``. On a ``PLUGIN_PASSIVE`` runtime
a bridge process sits between the server and the host model and reads that
metadata out of band, so nothing further is needed. ``HOST_DRIVEN`` and
``SEQUENTIAL`` runtimes have no such reader: the host *model* is the only
consumer, and the only channel it reads is response content. A contract
delivered solely through ``meta`` is therefore unobservable to exactly the
runtimes it addresses — the lanes are built, registered, and stamped, and the
host never learns a fan-out exists.

That is not a guess about one client. ``lateral_think`` already emits a visible
banner for this case "so meta-dropping transports still get a deterministic
cue" (#1517); the same commit added ``host_action`` to the interview advisory
and left the cue out, so that fan-out kept a ``MUST`` whose trigger no host
could observe.

The gap was silent in a way the fan-out's own vocabulary is not. A lane that
runs and finds nothing returns its no-op answer; a lane that is skipped comes
back ``partial``; a lane that could not be spawned is declarable
``undispatched``. A host that never learned the lanes existed produces none of
these — only an unredeemed registry record, and an interview that reads as
though no advice was ever due.
"""

from __future__ import annotations

import json
from typing import Any

#: Written precisely when the host must act, and omitted for ``PLUGIN_PASSIVE``
#: (see ``stamp_fanout_meta``). Reading it is what keeps this renderer from
#: re-deriving a dispatch mode that was already resolved upstream.
_HOST_ACTION_KEY = "question_advisory_host_action"
_PAYLOADS_KEY = "question_advisory_subagents"
_CORRELATION_KEY = "question_advisory_result_correlation_key"
_FANOUT_ID_KEY = "question_advisory_fanout_id"
_DEFAULT_CORRELATION_KEY = "context.lane_id"
_SEQUENTIAL_HOST_ACTION = "process_payloads_sequentially"

#: Boundary between the question and the host directive that follows it.
#:
#: The response text is read by two different consumers. A host model reads it
#: as prose and needs the directive; the auto driver reads it programmatically
#: and takes everything after the session envelope to be the question
#: (``auto/adapters.py::_extract_interview_question``). Without a boundary the
#: driver would answer a question with a fan-out directive stapled to it.
#:
#: An HTML comment, matching the ``ouroboros-lateral-inline-dispatch-v1`` marker
#: ``lateral_think`` already uses: invisible where the text is rendered, exact
#: where it is parsed.
QUESTION_ADVISORY_DISPATCH_MARKER = "<!-- ouroboros-question-advisory-dispatch-v1 -->"

#: The first words of the directive that always follows the marker. Recognising
#: the pair is what lets the strip tell its own output from a question that
#: merely quotes the marker; written once here so the emitter and the stripper
#: cannot drift into disagreeing about what a directive looks like.
_HOST_DIRECTIVE_OPENING = "> **Host action — "


def _lane_ids(payloads: list[Any], *, required_only: bool = False) -> list[str]:
    """Return lane ids in payload order, skipping anything malformed."""
    lanes: list[str] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        context = payload.get("context")
        if not isinstance(context, dict):
            continue
        if required_only and not bool(context.get("required")):
            continue
        lane_id = str(context.get("lane_id") or "")
        if lane_id:
            lanes.append(lane_id)
    return lanes


def append_question_advisory_dispatch(response_text: str, meta: dict[str, Any]) -> str:
    """Append the fan-out directive and its payloads to a question response.

    The question stays first. ``question_advisory_preserve_content`` requires
    it, and a directive that displaced the question would trade one silent
    failure for another.

    Payloads are emitted whole. The host is told not to reconstruct prompts
    from prose, which is only honourable if the prompts are present; a cue
    naming the lanes without carrying them would leave the host knowing it must
    fan out and unable to. No base64 dispatch block is written: ``lateral_think``
    carries one for machine consumers on a response shape it shares with its
    bridge path, whereas this text is reached only when the sole reader is the
    host model, for which a second encoded copy is cost without a consumer.

    Returns ``response_text`` unchanged when there is nothing for a host to do:
    a ``PLUGIN_PASSIVE`` runtime (no host action stamped), or a turn that
    attached no advisory at all (the length-guard path).
    """
    host_action = meta.get(_HOST_ACTION_KEY)
    payloads = meta.get(_PAYLOADS_KEY)
    if not host_action or not isinstance(payloads, list) or not payloads:
        return response_text

    correlation_key = str(meta.get(_CORRELATION_KEY) or _DEFAULT_CORRELATION_KEY)
    lanes = _lane_ids(payloads)
    required_lanes = _lane_ids(payloads, required_only=True)
    directive = (
        "process every payload below sequentially"
        if host_action == _SEQUENTIAL_HOST_ACTION
        else "spawn one subagent per payload below with your native subagent primitive"
    )

    lines = [
        response_text,
        "",
        QUESTION_ADVISORY_DISPATCH_MARKER,
        "",
        f"{_HOST_DIRECTIVE_OPENING}{host_action}:** keep the question above visible, then "
        f"{directive}. Dispatch the payloads as issued rather than rewriting them "
        f"from this prose. Correlate results by `{correlation_key}`.",
        "",
        f"Lanes ({len(lanes)}): {', '.join(lanes)}. "
        f"Required to complete: {', '.join(required_lanes) or 'none'}. "
        "A lane that ran and found nothing still submits its output; a lane you "
        'could not spawn at all is submitted as `{"key": <lane>, "undispatched": true}`. '
        "Never invent output for a lane you did not run.",
    ]

    fanout_id = meta.get(_FANOUT_ID_KEY)
    if fanout_id:
        lines += [
            "",
            "Submit results with `ouroboros_submit_fanout_results` "
            f"(`fanout_id`: `{fanout_id}`, `correlation_key`: `{correlation_key}`).",
        ]

    lines += ["", "```json", json.dumps(payloads, ensure_ascii=False), "```"]
    return "\n".join(lines)


def append_lateral_review_notice(
    response_text: str,
    lateral_review_meta: dict[str, Any] | None,
) -> str:
    """Surface a short user-visible cue without hiding the question first."""
    if lateral_review_meta is None:
        return response_text
    personas = ", ".join(str(p) for p in lateral_review_meta["lateral_review_personas"])
    milestone = lateral_review_meta["lateral_review_milestone"]
    return (
        f"{response_text}\n\nLateral review queued: running "
        f"{personas} before this interview turn "
        f"(milestone: {milestone})."
    )


def echo_carries_dispatch(echoed: Any) -> bool:
    """Return whether an echoed question carries a directive this module wrote.

    A host is asked to echo back "the exact question you asked", and the text it
    was shown ends with the fan-out directive. That value becomes the recorded
    round's question, which requirement extraction reads and the Seed inherits,
    so an echo copied wholesale would write server-authored instructions into
    durable state.

    **The marker is recognised here, and that is safe in this direction only.**
    Three rounds were spent learning that an in-band sentinel cannot separate our
    output from a question quoting our output: each longer prefix was a longer
    thing to quote, and each round truncated a question someone could legitimately
    ask. What made that unsafe was not the recognition — it was that recognition
    licensed *cutting*, so a false positive destroyed the user's text.

    Here it licenses nothing but a preference for the record we already hold. A
    false positive costs a repair we did not need to make, because the stored
    question of an unanswered round is the question the server issued and asked
    about. So the worst case is keeping the right question, and no echo of any
    shape — the question alone, the ambiguity-prefixed form, the whole response
    body, a paraphrase with the directive still attached — can carry a directive
    past it. Enumerating those forms is what the previous approach had to do, and
    the list only grows.

    A non-string is not an echo; the caller's own validation rejects it.
    """
    return isinstance(echoed, str) and QUESTION_ADVISORY_DISPATCH_MARKER in echoed


def split_appended_dispatch(text: str) -> str:
    """Return *text* up to the directive this module appends to a response.

    Parsing, not provenance. The caller is reading a response the server
    produced in the same call (``auto/adapters.py`` extracting the question it
    must answer), so there is no second copy to compare against and the shape is
    the only evidence there is. Kept apart from :func:`echo_carries_dispatch`
    for that reason: this one removes text on the strength of a shape, and that
    one only prefers a record we already hold. A single function doing both
    would let the weaker warrant license the destructive act.

    The residual risk is bounded and stated: a question quoting the marker and
    the directive's opening is cut short here too. It reaches durable state only
    through the echo path, where the comparison above refuses it.
    """
    cut = text.rfind(QUESTION_ADVISORY_DISPATCH_MARKER)
    if cut < 0:
        return text
    suffix = text[cut + len(QUESTION_ADVISORY_DISPATCH_MARKER) :]
    if not suffix.lstrip("\n").startswith(_HOST_DIRECTIVE_OPENING):
        return text
    return text[:cut].strip()


__all__ = [
    "QUESTION_ADVISORY_DISPATCH_MARKER",
    "append_lateral_review_notice",
    "append_question_advisory_dispatch",
    "echo_carries_dispatch",
    "split_appended_dispatch",
]
