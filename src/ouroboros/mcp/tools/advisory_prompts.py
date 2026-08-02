"""Prompt text for interview advisory lanes.

The Output section a lane is given and the standing brief the ``data_context``
lane carries. They live here rather than beside the payload builder because they
are the words a child is judged by: the Output section must ask for exactly the
contract re-entry enforces, and the data brief states the boundaries the child
cannot be trusted to rediscover.

Extracted from ``mcp/tools/subagent.py`` (Q00/ouroboros#1754), which keeps
re-exports for its existing importers.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

#: A rendered answer contract is the largest thing in an advisory prompt, so it
#: is bounded rather than allowed to crowd out the question it is about.
_INTERVIEW_DATA_CONTRACT_MAX_JSON_CHARS = 8_000


def _bounded_json(value: Any, max_chars: int) -> str:
    """Render JSON for prompts without letting metadata dominate context.

    Shared with ``mcp/tools/subagent.py``, which imports it from here rather
    than keeping the second copy the extraction briefly created.
    """
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    except (TypeError, ValueError):
        rendered = json.dumps(str(value), ensure_ascii=False)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[:max_chars].rstrip() + "\n... [truncated]"


_GENERIC_ADVISORY_OUTPUT_SECTION = """## Output
Return a compact JSON object with:
- lane_id
- finding: the single most useful advisory finding
- evidence: short list of file paths, source URLs, or reasoning anchors
- suggested_options: up to 3 answer options or draft snippets
- unresolved_ambiguities: short list of what the human still must decide

Keep it brief. The parent session will synthesize multiple advisory lanes before
forwarding anything back to ouroboros_interview."""


def _advisory_output_section(answer_contract: Any) -> str:
    """Return the Output section for one advisory lane.

    A lane that ships an answer contract is validated against it at re-entry, so
    its output section must ask for that contract and nothing else. The generic
    shape below asks for ``finding`` / ``evidence`` / ``suggested_options`` —
    fields a closed contract forbids — while omitting the ones it requires, so
    emitting both told the child two incompatible things. A required lane that
    obeys the wrong one is rejected and pins the fan-out at ``partial`` forever.

    The branch is on the contract's presence rather than on the lane id so the
    shape is written once. A second hand-authored block would agree with the
    contract on the day it was written and drift the next time either moved.
    """
    if not isinstance(answer_contract, Mapping):
        return _GENERIC_ADVISORY_OUTPUT_SECTION
    contract_id = str(answer_contract.get("contract_id") or "the lane answer contract")
    return f"""## Output
Return one JSON object satisfying `{contract_id}`, rendered in full above. Where
it offers alternative shapes, satisfy exactly one of them — the fields of the
others are not available to borrow. Each shape is closed: every field it
requires must be present, and any field it does not name is rejected — the
generic advisory fields (`finding`, `evidence`, `suggested_options`) as much as
a value this prompt showed you as context. What the Session block tells you is
for your reasoning, not for your output.

Your output is validated against that contract when the parent submits it. An
answer in any other shape is discarded, and because this lane is required, the
parent cannot complete the consultation without it."""


def _data_context_lane_brief(answer_contract: Any) -> str:
    """Render the data lane's standing rules plus its answer contract.

    The rules are stated here rather than left to the child's judgment because
    every one of them is a boundary the child cannot be trusted to rediscover:
    what it may not do (execute), what it may not carry (a value it fetched, a
    row, an identifier), and what its output is for (the user's judgment, never
    the answer).
    """
    contract_json = _bounded_json(answer_contract, _INTERVIEW_DATA_CONTRACT_MAX_JSON_CHARS)
    return f"""You may discover which data tools exist so you can name them. You may not
call one. Every lookup you would run is returned as a read_request and the
parent session runs it only after the user confirms it.

Your output is material for the user's judgment, never the answer. Whatever a
number would show, the interview answer is the user's own words.

Only aggregates can be carried. If the honest answer is a row list, a name, an
identifier, or an error message, that is data_needed=false with one of the
listed reasons — not evidence. Grouping keys must be categorical; grouping by an
identifier is a row list wearing an aggregate's clothes.

metric and informs_decision are read by the user before they approve the read,
so write what you would say to them: name the thing measured and the decision it
serves. They are not a place to report a number, a row, an address, or a query —
nothing you write there is an answer, and a value put there is one the user was
never offered the chance to confirm.

## Answer Contract (data_evidence_answer.v1)
```json
{contract_json}
```"""


__all__ = [
    "_GENERIC_ADVISORY_OUTPUT_SECTION",
    "_advisory_output_section",
    "_data_context_lane_brief",
]
