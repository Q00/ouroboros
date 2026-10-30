"""Readings of a goal: one model call, kept only when they pass a structural check.

A reading names one verbatim span of the goal and says how it reads that span.
Each candidate solution is written under one reading, so candidates differ
where the goal can be read in more than one way.

The call gets the goal and the investigator's repository facts, nothing else.
Its reply must be one JSON object ``{"readings": [{"span": ..., "reading":
...}, ...]}``. A reading is kept only when:

- ``span`` and ``reading`` are strings;
- the span, with whitespace runs collapsed, has at least ``MIN_QUOTE_CHARS``
  characters and is a substring of the goal with whitespace runs collapsed
  (the containment rule of ``auto.evidence_check.check_span``);
- the reading text is not empty and at most ``MAX_READING_CHARS`` long.

Readings with identical text (compared on canonical bytes after stripping the
ends) are merged; the first one is kept. At most ``max_readings`` are kept, in
reply order. Nothing decides by pattern or keyword whether two readings
differ, and nothing here interprets what a reading means.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
from typing import Any

from ouroboros.auto.evidence_check import MIN_QUOTE_CHARS, normalize_whitespace
from ouroboros.examples.record import canonical_json_bytes, sha256_hex
from ouroboros.providers.base import CompletionConfig, LLMAdapter, Message, MessageRole

MAX_READING_CHARS = 2_000

READINGS_SYSTEM_PROMPT = """\
You state readings of a software change request (the goal).

A reading picks one span of the goal, copied verbatim, that can be understood
in more than one way, and says in one or two sentences how this reading
understands it: what the code would do for concrete inputs under this reading.
Different readings must lead to different observable behavior.

Reply with one JSON object and nothing else:
{"readings": [{"span": "<verbatim text from the goal>", "reading": "<how this reading understands it>"}]}

Rules:
- Copy each span exactly from the goal.
- Give at most the number of readings requested; fewer is fine.
- Do not write tests, expected values, or code.
"""


@dataclass(frozen=True, slots=True)
class Reading:
    """One reading: the goal span it interprets and how it reads that span."""

    reading_id: str
    span: str
    text: str

    def as_event_data(self) -> dict[str, str]:
        return {"reading_id": self.reading_id, "span": self.span, "text": self.text}


@dataclass(frozen=True, slots=True)
class ReadingsResult:
    """The kept readings, how many reply items were dropped, and a call failure."""

    readings: tuple[Reading, ...]
    dropped: int = 0
    call_failed: str | None = None


def reading_id_for(text: str) -> str:
    """The sha256 of the canonical bytes of a reading's text."""
    return sha256_hex(canonical_json_bytes(text))


def _kept(item: Any, goal_text: str) -> tuple[str, str] | None:
    if not isinstance(item, dict):
        return None
    span, text = item.get("span"), item.get("reading")
    if not isinstance(span, str) or not isinstance(text, str):
        return None
    quote = normalize_whitespace(span)
    if len(quote) < MIN_QUOTE_CHARS or quote not in goal_text:
        return None
    text = text.strip()
    if not text or len(text) > MAX_READING_CHARS:
        return None
    return span, text


def parse_readings(reply: str, goal: str, max_readings: int) -> ReadingsResult:
    """Keep the readings of ``reply`` that pass the structural check (module docstring)."""
    if max_readings < 1:
        raise ValueError("max_readings must be at least 1")
    try:
        payload = json.loads(reply)
    except (json.JSONDecodeError, RecursionError):
        return ReadingsResult((), 0, "reply_not_json")
    items = payload.get("readings") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return ReadingsResult((), 0, "reply_without_readings")
    goal_text = normalize_whitespace(goal)
    kept: list[Reading] = []
    seen: set[str] = set()
    dropped = 0
    for item in items:
        pair = _kept(item, goal_text)
        if pair is None:
            dropped += 1
            continue
        span, text = pair
        reading_id = reading_id_for(text)
        if reading_id in seen or len(kept) >= max_readings:
            dropped += 1
            continue
        seen.add(reading_id)
        kept.append(Reading(reading_id, span, text))
    return ReadingsResult(tuple(kept), dropped)


def readings_prompt(goal: str, repo_facts: Sequence[str], max_readings: int) -> str:
    """The user message of the readings call: the goal and the repository facts."""
    facts = "\n".join(f"- {fact}" for fact in repo_facts) or "(none)"
    return (
        f"Goal:\n{goal}\n\n"
        f"Repository facts found by the investigator:\n{facts}\n\n"
        f"State at most {max_readings} readings."
    )


async def state_readings(
    llm: LLMAdapter,
    config: CompletionConfig,
    *,
    goal: str,
    repo_facts: Sequence[str],
    max_readings: int,
) -> ReadingsResult:
    """Make the one readings call and keep what passes ``parse_readings``.

    A failed call or an exception keeps no reading; the step then writes
    candidates with the goal alone (``call_failed`` says why).
    """
    messages = [
        Message(role=MessageRole.SYSTEM, content=READINGS_SYSTEM_PROMPT),
        Message(role=MessageRole.USER, content=readings_prompt(goal, repo_facts, max_readings)),
    ]
    try:
        result = await llm.complete(messages, config)
    except Exception as exc:  # noqa: BLE001 - a failed call keeps no reading
        return ReadingsResult((), 0, f"call_raised:{type(exc).__name__}")
    if result.is_err:
        return ReadingsResult((), 0, f"call_failed:{type(result.error).__name__}")
    return parse_readings(result.value.content, goal, max_readings)


__all__ = [
    "MAX_READING_CHARS",
    "READINGS_SYSTEM_PROMPT",
    "Reading",
    "ReadingsResult",
    "parse_readings",
    "reading_id_for",
    "readings_prompt",
    "state_readings",
]
