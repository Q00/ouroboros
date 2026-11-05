"""Unconfirmed interpretations for an opt-in interview opening."""

import hashlib
import json
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ouroboros.interview_adapters.first_question import _safe_single_line

OPENING_QUESTION = "What would you like to clarify or correct? You may reject both interpretations."
NEITHER_PROMPT = "To explicitly reject both proposed interpretations, reply exactly: NEITHER"


def confirmation_prompt(item: "StoredInterpretation") -> str:
    """Version-one prompt; hosts must show this exact text before recording a decision."""
    return f"To explicitly confirm this interpretation as your intent, reply exactly: YES\n{item.interpretation}"


OPENING_INSTRUCTION = (
    "Return only JSON with key interpretations containing exactly two distinct, "
    "competing interpretations of the user's request. Each object must contain "
    "interpretation, evidence, and uncertainty, all concise single-line strings. "
    "Use the user's language. Evidence must refer only to the provided context; "
    "mark any missing information as uncertainty. Do not recommend either one, "
    "claim user confirmation, invent requirements, or include other keys."
)


class InterpretationProposal(BaseModel):
    """A model proposal, never a user decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interpretation: str = Field(min_length=1, max_length=600)
    evidence: str = Field(min_length=1, max_length=600)
    uncertainty: str = Field(min_length=1, max_length=600)

    @field_validator("interpretation", "evidence", "uncertainty")
    @classmethod
    def safe_line(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or not _safe_single_line(value)
            or any(
                marker in value.casefold()
                for marker in (
                    "[from-user]",
                    "[from-code]",
                    "[from-assumption]",
                    "[from-inference]",
                )
            )
        ):
            raise ValueError("interpretation fields must be nonblank single lines")
        return value


class ProviderOpening(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    interpretations: tuple[InterpretationProposal, InterpretationProposal]

    @model_validator(mode="after")
    def distinct(self) -> "ProviderOpening":
        if (
            self.interpretations[0].interpretation.casefold()
            == self.interpretations[1].interpretation.casefold()
        ):
            raise ValueError("interpretations must be distinct")
        return self


class StoredInterpretation(InterpretationProposal):
    item_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    provenance: Literal["proposed"] = "proposed"


class ProposedOpening(BaseModel):
    """Server-owned proposals, kept separate from later confirmation events."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    interpretations: tuple[StoredInterpretation, StoredInterpretation]
    provenance: Literal["proposed"] = "proposed"

    @model_validator(mode="after")
    def unique_ids(self) -> "ProposedOpening":
        if self.interpretations[0].item_id == self.interpretations[1].item_id:
            raise ValueError("opening item IDs must be distinct")
        return self

    @classmethod
    def from_provider(cls, opening: ProviderOpening) -> "ProposedOpening":
        first, second = opening.interpretations
        return cls(
            interpretations=(
                StoredInterpretation(item_id=uuid4().hex, **first.model_dump()),
                StoredInterpretation(item_id=uuid4().hex, **second.model_dump()),
            )
        )

    def render(
        self, question: str = OPENING_QUESTION, decisions: "tuple[OpeningDecisionEvent, ...]" = ()
    ) -> str:
        confirmed = {item.item_id for item in confirmed_interpretations(self, decisions)}
        lines = [
            "Possible interpretations (model proposals; confirmation requires a separate explicit decision):",
            f"Opening revision: {opening_revision(self, decisions)}",
        ]
        for number, item in enumerate(self.interpretations, 1):
            status = (
                "confirmed by host-reported user decision"
                if item.item_id in confirmed
                else "proposed"
            )
            lines.extend(
                [
                    f"{number}. {item.interpretation} [item_id={item.item_id}; {status}]",
                    f"   Model-cited evidence (unverified): {item.evidence}",
                    f"   Uncertainty (proposal): {item.uncertainty}",
                    f"   Individual confirmation prompt (show exactly):\n{confirmation_prompt(item)}",
                ]
            )
        lines.extend(
            [
                "Neither interpretation may be correct.",
                f"Neither prompt (show exactly): {NEITHER_PROMPT}",
                "",
                question,
            ]
        )
        return "\n".join(lines)


class OpeningDecisionEvent(BaseModel):
    """An explicit host-reported user decision stored atomically with state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: Literal["confirm_item", "neither"]
    item_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    user_response: str = Field(min_length=1, max_length=2000)
    presented_prompt: str = Field(min_length=1, max_length=2000)
    opening_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: Literal["host_reported_explicit_user_decision"] = "host_reported_explicit_user_decision"

    @model_validator(mode="after")
    def valid_kind(self) -> "OpeningDecisionEvent":
        if self.user_response != ("NEITHER" if self.kind == "neither" else "YES"):
            raise ValueError("invalid explicit decision text")
        if (self.kind == "confirm_item") != (self.item_id is not None):
            raise ValueError("decision item does not match its kind")
        return self


def opening_digest(opening: ProposedOpening) -> str:
    payload = json.dumps(
        opening.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def opening_revision(opening: ProposedOpening, decisions: tuple[OpeningDecisionEvent, ...]) -> str:
    payload = json.dumps(
        {
            "opening": opening_digest(opening),
            "decisions": [item.model_dump(mode="json") for item in decisions],
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def confirmed_opening_events(
    opening: ProposedOpening | None, decisions: tuple[OpeningDecisionEvent, ...]
) -> tuple[tuple[StoredInterpretation, OpeningDecisionEvent], ...]:
    """Replay only digest-bound individual confirmations after the last neither."""
    if opening is None:
        if decisions:
            raise ValueError("opening decisions require an opening")
        return ()
    digest = opening_digest(opening)
    items = {item.item_id: item for item in opening.interpretations}
    confirmed: dict[str, tuple[StoredInterpretation, OpeningDecisionEvent]] = {}
    event_ids: set[str] = set()
    prefix: tuple[OpeningDecisionEvent, ...] = ()
    for event in decisions:
        if (
            event.user_response != ("NEITHER" if event.kind == "neither" else "YES")
            or event.event_id in event_ids
            or event.opening_digest != digest
            or event.previous_revision != opening_revision(opening, prefix)
        ):
            raise ValueError("invalid opening decision history")
        event_ids.add(event.event_id)
        if event.kind == "neither":
            if event.presented_prompt != NEITHER_PROMPT:
                raise ValueError("opening decision prompt does not match")
            confirmed.clear()
        elif event.item_id not in items:
            raise ValueError("opening decision references an unknown item")
        else:
            assert event.item_id is not None
            item = items[event.item_id]
            if event.presented_prompt != confirmation_prompt(item):
                raise ValueError("opening decision prompt does not match")
            confirmed[event.item_id] = (item, event)
        prefix += (event,)
    return tuple(confirmed.values())


def confirmed_interpretations(
    opening: ProposedOpening | None, decisions: tuple[OpeningDecisionEvent, ...]
) -> tuple[StoredInterpretation, ...]:
    return tuple(item for item, _event in confirmed_opening_events(opening, decisions))


def opening_fingerprint_payload(
    opening: ProposedOpening | None, decisions: tuple[OpeningDecisionEvent, ...]
) -> dict[str, object]:
    """Retain legacy fingerprints when no opening decisions exist."""
    if not decisions:
        return {}
    return {
        "confirmed_opening": [
            item.model_dump(mode="json") for item in confirmed_interpretations(opening, decisions)
        ]
    }
