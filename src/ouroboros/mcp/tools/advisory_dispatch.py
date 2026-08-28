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
_HOST_DECIDES_ACTION = "dispatch_subagents_if_supported"

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

#: The OpenCode bridge's opening. It is a *second* producer appending to the
#: visible question: on ``PLUGIN_PASSIVE`` the server renders nothing, and the
#: bridge then stamps its dispatch banner and response-shape JSON — including
#: ``question_advisory_fanout_id`` — onto the text a host sees and may echo back.
#:
#: The bridge declares itself with our marker and this opening rather than being
#: recognised by its prose, because its prose is not stable: ``[Ouroboros] ``
#: leads only the dispatched banner, while failed-only and skipped-only banners
#: begin with their own words. A gatekeeper reverse-engineering that would have
#: been wrong on arrival and wrong again on the next wording change.
#:
#: Duplicated in ``opencode/plugin/ouroboros-bridge.ts`` because the two cannot
#: import each other; a test pins them equal, which is the language-boundary
#: form of writing a shared constant once.
_BRIDGE_NOTICE_OPENING = "> **Bridge dispatch — plugin_subagent:** "

#: **A producer that appends dispatch state to the visible question declares it
#: with this grammar**, so the gatekeeper never has to reverse-engineer prose.
#: Two do: the host directive above and the bridge. The bridge went five rounds
#: undeclared, which is why the rule is written where the next one will read it.
#:
#: It is a rule about dispatch state, not about every append.
#: :func:`append_lateral_review_notice` also adds to the visible question and
#: does not declare, so an echo carrying only that notice is recorded verbatim.
#: That is a real residue and it is deliberately not fixed here: it predates this
#: lane, carries no identifiers, and belongs to whichever change needs it rather
#: than to the one that happened to notice it.
_DIRECTIVE_OPENINGS = (_HOST_DIRECTIVE_OPENING, _BRIDGE_NOTICE_OPENING)


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


def directive_was_appended(meta: dict[str, Any]) -> bool:
    """Return whether :func:`append_question_advisory_dispatch` added a directive.

    The renderer's own condition, named once so a reader of the response cannot
    disagree with the writer of it about whether there is anything to strip.
    ``PLUGIN_PASSIVE`` stamps no host action and leaves content unchanged, so
    this is false there and `auto` cuts nothing — the same reason
    ``_HOST_DIRECTIVE_OPENING`` is a shared constant rather than two copies.
    """
    payloads = meta.get(_PAYLOADS_KEY)
    return bool(meta.get(_HOST_ACTION_KEY)) and isinstance(payloads, list) and bool(payloads)


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
    if not directive_was_appended(meta):
        return response_text
    host_action = meta[_HOST_ACTION_KEY]
    payloads = meta[_PAYLOADS_KEY]

    correlation_key = str(meta.get(_CORRELATION_KEY) or _DEFAULT_CORRELATION_KEY)
    lanes = _lane_ids(payloads)
    required_lanes = _lane_ids(payloads, required_only=True)
    if host_action == _SEQUENTIAL_HOST_ACTION:
        directive = "process every payload below sequentially"
    elif host_action == _HOST_DECIDES_ACTION:
        directive = (
            "use your native parallel subagent primitive when available; "
            "otherwise process every payload below sequentially"
        )
    else:
        directive = "spawn one subagent per payload below with your native subagent primitive"

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


def _directive_at(text: str, openings: tuple[str, ...] = _DIRECTIVE_OPENINGS) -> int:
    """Return the offset of a declared append by one of *openings*, or -1.

    A directive is the marker *and* an opening that follows it, never the marker
    alone. Both readers of a response ask this same question — the echo path to
    decide whether an echo is a repair, the parser to decide where a response
    body ends — so they ask it in one place and cannot answer it differently.

    They do not ask about the same producers, though, and *openings* is where
    they differ. Two producers now declare, and when both append the server's
    directive comes first and the bridge's notice after it. A reader that simply
    took the last marker would answer for whichever producer wrote last: the
    parser, gated on the server having appended, would cut at the bridge's
    marker and leave the server's own directive inside the question — the exact
    failure that gate exists to prevent. So the scan walks markers from the end
    and returns the last one belonging to a producer the caller asked about.

    From the end, because a declared append always follows the question: an
    earlier marker matching the same grammar is text the question carried, and
    the caller's own gate is what makes preferring the later one safe.
    """
    end = len(text)
    while True:
        cut = text.rfind(QUESTION_ADVISORY_DISPATCH_MARKER, 0, end)
        if cut < 0:
            return -1
        suffix = text[cut + len(QUESTION_ADVISORY_DISPATCH_MARKER) :].lstrip("\n")
        if suffix.startswith(openings):
            return cut
        end = cut


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

    **The marker alone is not the test, and the false positive is not free.**
    For one round it was the test, and that broke the repair this exists to
    allow: a question quoting the marker with no directive under it was read as
    an echo, so the stored question won — and where the stored question is the
    damaged one ``last_question`` exists to replace, the damage is what reached
    the transcript. The argument for the bare marker was that the worst case is
    keeping the right question. That holds only while the stored question is
    right, which is precisely the case this parameter is not for.

    So it asks for a directive, not a mention. What remains is narrower and
    stated: a repair that reproduces a whole directive is not distinguishable
    from an echo, and preferring the record there is the safe reading.

    A non-string is not an echo; the caller's own validation rejects it.
    """
    return isinstance(echoed, str) and _directive_at(echoed) >= 0


def split_appended_dispatch(text: str) -> str:
    """Return *text* up to the directive this module appends to a response.

    Parsing, not provenance. The caller is reading a response the server
    produced in the same call (``auto/adapters.py`` extracting the question it
    must answer), so there is no second copy to compare against and the shape is
    the only evidence there is.

    **Only call this when the server appended a directive**, which
    :func:`directive_was_appended` answers from the same response. With none
    present the last marker in the text belongs to the question, and cutting
    there destroys it — on ``PLUGIN_PASSIVE``, where content is left unchanged
    by design, that would be every turn.

    The gate is also what retires the residual an earlier version of this
    docstring accepted. A directive is always appended last, so once one exists
    the last marker is ours and a question quoting the sentinel survives in
    front of the cut.

    The shape question itself is asked in one place, :func:`_directive_at`, and
    both readers of a response ask it. What differs is the warrant each side has
    for the answer: this one removes text, and does so only after
    ``directive_was_appended`` has said the server wrote some; the echo side
    removes nothing and only prefers a record it already holds. Sharing the
    question keeps them from disagreeing about where a response ends; keeping
    the warrants apart is what stops the weaker one licensing a cut.

    It also asks only about ``_HOST_DIRECTIVE_OPENING``, because the gate it
    obeys is a statement about the server's own directive. The bridge declares
    with the same marker and appends *after* the server, so accepting its
    opening here would cut at the bridge's notice and leave the server's
    directive standing in the question.

    That earlier version also claimed the residual reached durable state only
    through the echo path, where it would be refused. It was wrong. A truncation
    here removes the marker, so the echo arrives looking like an ordinary
    question and is recorded as one — a guarantee stated where nothing made it
    true, which is the shape this branch keeps finding in its own comments.
    """
    cut = _directive_at(text, (_HOST_DIRECTIVE_OPENING,))
    return text if cut < 0 else text[:cut].strip()


def strip_bridge_notice(text: Any) -> Any:
    """Return *text* without the bridge's declared append.

    For the one branch that holds no issued question. Plugin mode persists no
    question-only round — the child asks and the server only records answers —
    so its answer branch has nothing to prefer, and refusing the echo would
    store a placeholder instead of what the user was asked.

    Scoped to the bridge's opening on purpose. This module appends nothing on
    ``PLUGIN_PASSIVE``, so the bridge is the only producer that can have
    written there; asking for the host opening as well would cut a question
    that merely quotes a directive, which is the damage the echo path exists to
    avoid. What remains is a question reproducing a whole *bridge* notice,
    which is narrower than the banner and fan-out id this otherwise records
    verbatim.
    """
    if not isinstance(text, str):
        return text
    cut = _directive_at(text, (_BRIDGE_NOTICE_OPENING,))
    return text if cut < 0 else text[:cut].strip()


__all__ = [
    "QUESTION_ADVISORY_DISPATCH_MARKER",
    "append_lateral_review_notice",
    "append_question_advisory_dispatch",
    "directive_was_appended",
    "echo_carries_dispatch",
    "split_appended_dispatch",
    "strip_bridge_notice",
]
