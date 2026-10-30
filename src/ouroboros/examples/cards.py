"""Question cards: disagreement probes that split the candidates the same way.

The partition signature of a disagreement probe is the set partition of the
candidate trees induced by its groups, written as a sorted tuple of sorted
tuples of candidate ids. Probes with the same signature probably reflect the
same split in reading, so they form one card and one pick covers them all.

A card:

- ``card_id``: sha256 of the canonical JSON of the signature and every probe
  id on the card.
- ``probe_ids``: the probes shown, at most ``max_probes`` (default 3), the
  smallest probe ids first. The rest are kept in ``overflow_probe_ids``.
- ``options``: one per block of the partition, labelled ``A`` to ``Z``,
  then ``AA``, ``AB``, ... (``option_label``), so any number of blocks fits.
  The design labels options by the order of the canonical group bytes, but a
  card with several probes has different bytes on each probe, and a label
  must name the same block on every probe. So the order is fixed by the
  first shown probe: blocks are ordered by their group's canonical bytes on
  that probe, then by their candidate ids. Each option holds, per shown
  probe, the group's outcome and its relation to the base.

An option keeps the candidate ids of its block so the controller can trace
a pick back. They are internal: a requester must never see them.

Card order: more groups first, then a larger smallest group first (a lone
outlier is asked last), then ``card_id``. ``select_cards`` splits the
ordered cards into the ones asked (at most ``max_cards``, default 5) and the
ones only recorded.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ouroboros.examples.disagreement import BaseRelation, OutcomeGroup, ProbeComparison
from ouroboros.examples.record import canonical_json_bytes, sha256_hex

DEFAULT_MAX_PROBES_PER_CARD = 3
DEFAULT_MAX_CARDS = 5
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

PartitionSignature = tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class OptionOutcome:
    """One option's output on one shown probe."""

    probe_id: str
    outcome: Mapping[str, Any]
    outcome_bytes: bytes
    base_relation: BaseRelation


@dataclass(frozen=True, slots=True)
class CardOption:
    """One block of the partition: a label, its candidates, its outputs."""

    label: str
    candidate_ids: tuple[str, ...]
    outcomes: tuple[OptionOutcome, ...]


@dataclass(frozen=True, slots=True)
class QuestionCard:
    """Disagreement probes with one partition signature, as one question."""

    card_id: str
    partition: PartitionSignature
    probe_ids: tuple[str, ...]
    overflow_probe_ids: tuple[str, ...]
    options: tuple[CardOption, ...]

    @property
    def group_count(self) -> int:
        return len(self.partition)

    @property
    def smallest_group(self) -> int:
        return min(len(block) for block in self.partition)


def option_label(index: int) -> str:
    """Return the label of the option at ``index``: A..Z, then AA, AB, ..."""
    if index < 0:
        raise ValueError("index must not be negative")
    label = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, len(_LETTERS))
        label = _LETTERS[rest] + label
    return label


def partition_signature(comparison: ProbeComparison) -> PartitionSignature:
    """Return the set partition of candidate ids induced by the probe's groups."""
    return tuple(sorted(tuple(sorted(group.candidate_ids)) for group in comparison.groups))


def card_id_for(partition: PartitionSignature, probe_ids: Iterable[str]) -> str:
    """Return the card id for a signature and its probe ids (order-free)."""
    body = {"partition": [list(block) for block in partition], "probes": sorted(probe_ids)}
    return sha256_hex(canonical_json_bytes(body))


def _group_by_block(comparison: ProbeComparison) -> dict[tuple[str, ...], OutcomeGroup]:
    return {tuple(sorted(group.candidate_ids)): group for group in comparison.groups}


def _build_card(
    partition: PartitionSignature, comparisons: Sequence[ProbeComparison], max_probes: int
) -> QuestionCard:
    ordered = sorted(comparisons, key=lambda item: item.probe_id)
    shown, overflow = ordered[:max_probes], ordered[max_probes:]
    by_block = [_group_by_block(comparison) for comparison in shown]
    first = by_block[0]
    blocks = sorted(partition, key=lambda block: (first[block].outcome_bytes, block))
    options = tuple(
        CardOption(
            label=option_label(index),
            candidate_ids=block,
            outcomes=tuple(
                OptionOutcome(
                    probe_id=comparison.probe_id,
                    outcome=groups[block].outcome,
                    outcome_bytes=groups[block].outcome_bytes,
                    base_relation=groups[block].base_relation,
                )
                for comparison, groups in zip(shown, by_block, strict=True)
            ),
        )
        for index, block in enumerate(blocks)
    )
    return QuestionCard(
        card_id=card_id_for(partition, (item.probe_id for item in ordered)),
        partition=partition,
        probe_ids=tuple(item.probe_id for item in shown),
        overflow_probe_ids=tuple(item.probe_id for item in overflow),
        options=options,
    )


def card_order_key(card: QuestionCard) -> tuple[int, int, str]:
    """Return the sort key: more groups, then larger smallest group, then id."""
    return (-card.group_count, -card.smallest_group, card.card_id)


def build_cards(
    comparisons: Iterable[ProbeComparison],
    max_probes: int = DEFAULT_MAX_PROBES_PER_CARD,
) -> tuple[QuestionCard, ...]:
    """Group disagreement probes by partition signature into ordered cards.

    Only disagreements (two or more groups) can be on a card; any other
    comparison raises ``ValueError``. A probe id may appear once.
    """
    if max_probes < 1:
        raise ValueError("max_probes must be at least 1")
    by_signature: dict[PartitionSignature, list[ProbeComparison]] = {}
    seen: set[str] = set()
    for comparison in comparisons:
        if not comparison.is_disagreement:
            raise ValueError(f"probe {comparison.probe_id!r} is not a disagreement")
        if comparison.probe_id in seen:
            raise ValueError(f"probe {comparison.probe_id!r} appears twice")
        seen.add(comparison.probe_id)
        by_signature.setdefault(partition_signature(comparison), []).append(comparison)
    cards = [_build_card(signature, items, max_probes) for signature, items in by_signature.items()]
    return tuple(sorted(cards, key=card_order_key))


def select_cards(
    cards: Sequence[QuestionCard], max_cards: int = DEFAULT_MAX_CARDS
) -> tuple[tuple[QuestionCard, ...], tuple[QuestionCard, ...]]:
    """Split ordered cards into (asked, recorded only) under the card budget."""
    if max_cards < 0:
        raise ValueError("max_cards must not be negative")
    ordered = sorted(cards, key=card_order_key)
    return tuple(ordered[:max_cards]), tuple(ordered[max_cards:])
