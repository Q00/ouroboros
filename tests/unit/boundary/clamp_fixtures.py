"""Clamp oracles and Seeds shared by the coverage and replacement tests (data only)."""

from __future__ import annotations

from typing import Any

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

BUGGY = "def clamp(value, low, high):\n    if value > high:\n        return value\n    return max(low, value)\n"


FIXED = "def clamp(value, low, high):\n    return max(low, min(high, value))\n"


def _seed(*criteria: str) -> Seed:
    return Seed(
        goal="clamp helper",
        acceptance_criteria=criteria
        or (
            "clamp(15, 0, 10) returns 10",
            "clamp(5, 0, 10) returns 5",
            "clamp(-5, 0, 10) returns 0",
        ),
        ontology_schema=OntologySchema(name="mathutils", description="math helpers"),
        metadata=SeedMetadata(seed_id="seed_u_reduction", ambiguity_score=0.1),
    )


def _oracle(
    criterion: int, check_id: str, role: str, args: tuple[int, int, int], value: int
) -> dict[str, Any]:
    value_, low, high = args
    return {
        "criterion": criterion,
        "check_id": check_id,
        "role": role,
        "call_kind": "function",
        "params": ["value", "low", "high"],
        "default_binding": {"symbol": "mathutils.clamp"},
        "target_named_in_criterion": False,
        "cases": [
            {
                "case_id": "stated",
                "held_out": False,
                "args": {"value": value_, "low": low, "high": high},
                "expect": {"kind": "returns", "value": value},
            },
            # A case the Seed does not state: only a held-out pass verifies.
            {
                "case_id": "held",
                "args": {"value": -3, "low": -2, "high": 4},
                "expect": {"kind": "returns", "value": -2},
                "held_out": True,
            },
        ],
    }


# Base (BUGGY): clamp(15, 0, 10) = 15, clamp(5, 0, 10) = 5, clamp(-5, 0, 10) = 0.
GOOD_REPRO_1 = _oracle(1, "oracle_1", "reproduction", (15, 0, 10), 10)


BAD_REPRO_2 = _oracle(2, "oracle_2", "reproduction", (5, 0, 10), 5)  # passes on base


BAD_PRESERVE_3 = _oracle(3, "oracle_3", "preservation", (-5, 0, 10), 1)  # fails on base


GOOD_PRESERVE_3 = _oracle(3, "oracle_3", "preservation", (-5, 0, 10), 0)


WILLING = "The user is willing to assist with debugging the issue."


# What an older prompt produced: a kind label and the reason it implied.
STALE_REASON = "non_behavioral"
