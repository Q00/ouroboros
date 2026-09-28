"""Per-check admission and coverage after exclusions: the library API.

``per_check_admission`` on a recorded admission, then the usual
``assign_tiers(admitted_tiers=...)`` and ``criterion_verdicts``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.admission import admit_check_package
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.per_check import (
    ALL_CHECKS_EXCLUDED,
    per_check_admission,
)
from ouroboros.boundary.receipts import PackageVerdict
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
        metadata=SeedMetadata(seed_id="seed_per_check", ambiguity_score=0.1),
    )


def _oracle(
    criterion: int,
    check_id: str,
    role: str,
    args: tuple[int, int, int],
    value: int,
    held: tuple[int, int, int, int] = (-3, -2, 4, -2),
) -> dict[str, Any]:
    value_, low, high = args
    held_value, held_low, held_high, held_expected = held
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
                "args": {"value": held_value, "low": held_low, "high": held_high},
                "expect": {"kind": "returns", "value": held_expected},
                "held_out": True,
            },
        ],
    }


# Base (BUGGY): clamp(15, 0, 10) = 15, clamp(5, 0, 10) = 5, clamp(-5, 0, 10) = 0.
# A reproduction oracle's held-out case must fail on the base too
# (clamp(99, 1, 7) = 99 there), or it discriminates nothing.
GOOD_REPRO_1 = _oracle(1, "oracle_1", "reproduction", (15, 0, 10), 10, held=(99, 1, 7, 7))
BAD_REPRO_2 = _oracle(2, "oracle_2", "reproduction", (5, 0, 10), 5)  # passes on base
GOOD_PRESERVE_3 = _oracle(3, "oracle_3", "preservation", (-5, 0, 10), 0)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


async def test_an_indeterminate_check_leaves_the_package_unadmitted(repo: Path) -> None:
    seed = _seed("clamp(15, 0, 10) returns 10", "clamp(5, 0, 10) returns 5")
    crash = _oracle(1, "oracle_1", "reproduction", (15, 0, 10), 10)
    crash["default_binding"] = {"symbol": "mathutils.clamp"}
    package = package_from_reply(
        {"oracles": [crash, BAD_REPRO_2]},
        seed,
        input_digest="1" * 64,
        generator="fake",
    )
    (repo / "mathutils.py").write_text("raise ImportError('broken at import')\n")
    admission = await admit_check_package(
        package,
        repo,
    )
    assert admission.verdict is PackageVerdict.INDETERMINATE
    assert admission.excluded_checks is None


async def test_excluding_every_check_is_no_admission(repo: Path) -> None:
    seed = _seed("clamp(5, 0, 10) returns 5")
    package = package_from_reply(
        {"oracles": [_oracle(1, "oracle_1", "reproduction", (5, 0, 10), 5)]},
        seed,
        input_digest="1" * 64,
        generator="fake",
    )
    raw = await admit_check_package(package, repo)
    applied = per_check_admission(raw)
    assert raw.verdict is applied.verdict is PackageVerdict.REJECTED
    assert applied.reasons == (*raw.reasons, ALL_CHECKS_EXCLUDED)
    assert applied.excluded_checks is None


async def test_an_admission_without_exclusions_keeps_its_bytes(repo: Path) -> None:
    seed = _seed("clamp(15, 0, 10) returns 10")
    package = package_from_reply(
        {"oracles": [GOOD_REPRO_1]},
        seed,
        input_digest="1" * 64,
        generator="fake",
    )
    admission = await admit_check_package(package, repo)
    assert admission.verdict is PackageVerdict.ADMITTED
    assert per_check_admission(admission) is admission
    assert "excluded_checks" not in admission.event_summary()


DJANGO_UNDER = (
    "When models use custom fields and mixins, generated migration files include the imports "
    "needed to resolve referenced names and do not raise a NameError for undefined names."
)
WILLING = "The user is willing to assist with debugging the issue."


def _dev_seed() -> Seed:
    return _seed(DJANGO_UNDER, WILLING)
