"""The calc checkout, Seed and script-check packages that the wiring tests share (no execution)."""

from __future__ import annotations

from typing import Any

from ouroboros.boundary.binding import CHECK_DIR
from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

INPUT_DIGEST = "2" * 64


BUGGY = "def add(a, b):\n    return a - b\n"


FIXED = "def add(a, b):\n    return a + b\n"


BUGFIX_SCRIPT = """import sys
sys.path.insert(0, ".")
SIG = "OUROBOROS_CHECK_FAILED:repro_add"
from calc import add
if add(2, 3) != 5:
    print(SIG)
    print("expected 5, observed", add(2, 3))
    sys.exit(1)
"""


PASSES_ON_BASE_SCRIPT = """print("nothing asserted")
"""


FEATURE_GUARDED_SCRIPT = """import sys
sys.path.insert(0, ".")
SIG = "OUROBOROS_CHECK_FAILED:repro_multiply"
try:
    from calc import multiply
except ImportError:
    print(SIG)
    print("expected calc.multiply to exist; observed: missing")
    sys.exit(1)
if multiply(3, 4) != 12:
    print(SIG)
    print("expected 12, observed", multiply(3, 4))
    sys.exit(1)
"""


FEATURE_UNGUARDED_SCRIPT = """import sys
sys.path.insert(0, ".")
from calc import multiply
if multiply(3, 4) != 12:
    print("OUROBOROS_CHECK_FAILED:repro_multiply")
    sys.exit(1)
"""


def _seed(*criteria: str, score: float | None = 0.05) -> Seed:
    return Seed(
        goal="calc works",
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(name="calc", description="calculator"),
        metadata=SeedMetadata(seed_id="seed_wiring", ambiguity_score=score),
    )


def _reply(check_id: str, script: str, *, criterion: int = 1, uncovered: tuple = ()) -> dict:
    path = f"{CHECK_DIR}/{check_id}.py"
    return {
        "checks": [
            {
                "check_id": check_id,
                "role": "reproduction",
                "argv": ["python3", path],
                "target_named_in_criterion": False,
                "cwd": ".",
                "failure_signature": f"OUROBOROS_CHECK_FAILED:{check_id}",
                "assertions": [{"criterion": criterion, "locator": "main assertion"}],
            }
        ],
        "files": [{"path": path, "content": script}],
        "uncovered": [{"criterion": c, "reason": "not mechanical"} for c in uncovered],
    }


def _package(seed: Seed, check_id: str, script: str, **kwargs: Any):
    return package_from_reply(
        _reply(check_id, script, **kwargs), seed, input_digest=INPUT_DIGEST, generator="fake"
    )
