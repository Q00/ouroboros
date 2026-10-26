"""Tests for partition signatures, card building, labels and order."""

from __future__ import annotations

import random

import pytest

from ouroboros.examples.cards import (
    build_cards,
    card_id_for,
    partition_signature,
    select_cards,
)
from ouroboros.examples.disagreement import BaseRelation, find_disagreements
from ouroboros.examples.record import OutcomeRecord

from .helpers import probe

CANDIDATES = ("c1", "c2", "c3", "c4")


def _values(probe_id: str, base: int, values: dict[str, object]) -> list[OutcomeRecord]:
    return probe(probe_id, {"value": base}, {c: {"value": v} for c, v in values.items()})


def _pool() -> list[OutcomeRecord]:
    # p1, p2, p5, p6: split {c1,c2} | {c3} | {c4}. p3: split {c1,c2,c3} | {c4}.
    # p4: split {c1,c2} | {c3,c4}. p7: all agree (not a card).
    return (
        _values("p1", 0, {"c1": 5400, "c2": 5400, "c3": 90, "c4": 0})
        + _values("p2", 0, {"c1": 7200, "c2": 7200, "c3": 120, "c4": 0})
        + _values("p5", 0, {"c1": 1, "c2": 1, "c3": 2, "c4": 3})
        + _values("p6", 0, {"c1": 9, "c2": 9, "c3": 8, "c4": 7})
        + _values("p3", 0, {"c1": 1, "c2": 1, "c3": 1, "c4": 2})
        + _values("p4", 0, {"c1": 1, "c2": 1, "c3": 2, "c4": 2})
        + _values("p7", 0, {"c1": 1, "c2": 1, "c3": 1, "c4": 1})
    )


def _cards():
    return build_cards(find_disagreements(_pool(), CANDIDATES).disagreements)


def test_partition_signature_is_sorted_blocks() -> None:
    report = find_disagreements(_pool(), CANDIDATES)
    by_id = {c.probe_id: partition_signature(c) for c in report.disagreements}
    assert by_id["p1"] == (("c1", "c2"), ("c3",), ("c4",))
    assert by_id["p1"] == by_id["p2"] == by_id["p5"] == by_id["p6"]
    assert by_id["p3"] == (("c1", "c2", "c3"), ("c4",))
    assert by_id["p4"] == (("c1", "c2"), ("c3", "c4"))


def test_probes_with_one_signature_form_one_card() -> None:
    cards = _cards()
    assert len(cards) == 3
    three_way = cards[0]
    assert three_way.partition == (("c1", "c2"), ("c3",), ("c4",))
    assert three_way.probe_ids == ("p1", "p2", "p5")
    assert three_way.overflow_probe_ids == ("p6",)
    assert three_way.card_id == card_id_for(three_way.partition, ["p6", "p5", "p2", "p1"])


def test_card_order_groups_then_smallest_group_then_id() -> None:
    cards = _cards()
    # Three groups first; then the even 2|2 split before the 3|1 outlier.
    assert [c.partition for c in cards] == [
        (("c1", "c2"), ("c3",), ("c4",)),
        (("c1", "c2"), ("c3", "c4")),
        (("c1", "c2", "c3"), ("c4",)),
    ]
    assert [(c.group_count, c.smallest_group) for c in cards] == [(3, 1), (2, 2), (2, 1)]


def test_ties_are_broken_by_card_id_only() -> None:
    records = _values("q1", 0, {"c1": 1, "c2": 2}) + _values("q2", 0, {"c3": 1, "c4": 2})
    # Two separate candidate sets in one pool are not possible, so build two
    # reports and merge their disagreements.
    left = find_disagreements(records[:6], ["c1", "c2"]).disagreements
    right = find_disagreements(records[6:], ["c3", "c4"]).disagreements
    cards = build_cards(left + right)
    assert [(c.group_count, c.smallest_group) for c in cards] == [(2, 1), (2, 1)]
    assert [c.card_id for c in cards] == sorted(c.card_id for c in cards)


def test_labels_follow_group_bytes_on_the_first_shown_probe() -> None:
    card = _cards()[0]
    # On p1 the values are 0 (c4), 5400 (c1,c2), 90 (c3): bytes order 0 < 5400 < 90.
    assert [(o.label, o.candidate_ids) for o in card.options] == [
        ("A", ("c4",)),
        ("B", ("c1", "c2")),
        ("C", ("c3",)),
    ]
    # A label names the same block on every shown probe.
    option_b = card.options[1]
    assert [(o.probe_id, o.outcome["value"]) for o in option_b.outcomes] == [
        ("p1", 5400),
        ("p2", 7200),
        ("p5", 1),
    ]
    assert [o.base_relation for o in card.options[0].outcomes] == [
        BaseRelation.SAME_AS_BASE,
        BaseRelation.SAME_AS_BASE,
        BaseRelation.CHANGED_FROM_BASE,
    ]


def test_cards_are_independent_of_input_order() -> None:
    disagreements = list(find_disagreements(_pool(), CANDIDATES).disagreements)
    expected = build_cards(disagreements)
    rng = random.Random(18)
    for _ in range(25):
        rng.shuffle(disagreements)
        assert build_cards(disagreements) == expected


def test_max_probes_per_card() -> None:
    disagreements = find_disagreements(_pool(), CANDIDATES).disagreements
    card = build_cards(disagreements, max_probes=1)[0]
    assert card.probe_ids == ("p1",)
    assert card.overflow_probe_ids == ("p2", "p5", "p6")
    assert all(len(o.outcomes) == 1 for o in card.options)
    with pytest.raises(ValueError, match="max_probes"):
        build_cards(disagreements, max_probes=0)


def test_select_cards_applies_the_budget_in_order() -> None:
    cards = _cards()
    asked, recorded = select_cards(cards, max_cards=2)
    assert asked == cards[:2] and recorded == cards[2:]
    assert select_cards(list(reversed(cards)), max_cards=5) == (cards, ())
    assert select_cards(cards, max_cards=0) == ((), cards)
    with pytest.raises(ValueError, match="negative"):
        select_cards(cards, max_cards=-1)


def test_non_disagreements_and_repeats_are_refused() -> None:
    report = find_disagreements(_pool(), CANDIDATES)
    with pytest.raises(ValueError, match="not a disagreement"):
        build_cards(report.consensus_changes)
    with pytest.raises(ValueError, match="appears twice"):
        build_cards(report.disagreements + report.disagreements[:1])


def test_no_disagreements_gives_no_cards() -> None:
    assert build_cards([]) == ()
    records = _values("p1", 0, {"c1": 1})
    assert build_cards(find_disagreements(records, ["c1"]).disagreements) == ()
