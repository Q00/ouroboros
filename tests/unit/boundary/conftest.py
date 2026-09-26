"""Shared fixtures: a tiny buggy checkout, its Seed, and package builders."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys

import pytest

from ouroboros.boundary import (
    AssertionLink,
    CheckPackage,
    CheckRole,
    CheckSpec,
    PackageFile,
    UncoveredObligation,
    seed_criterion_keys,
    seed_digest,
)
from ouroboros.core.seed import AcceptanceCriterionSpec, OntologySchema, Seed, SeedMetadata

PY = sys.executable
SIGNATURE = "BOUNDARY_ASSERT add_sums_operands"
INPUT_DIGEST = "1" * 64

REPRO_SCRIPT = f"""import sys
sys.path.insert(0, ".")
from calc import add
if add(2, 3) != 5:
    print("{SIGNATURE}: expected 5, got", add(2, 3))
    sys.exit(1)
"""

PRESERVE_SCRIPT = """import sys
sys.path.insert(0, ".")
from calc import add
assert add(0, 0) == 0
"""


def make_seed() -> Seed:
    return Seed(
        goal="add returns the sum of its operands",
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="add(a, b) returns a + b",
                verify_command="python probe/test_add.py",
            ),
            AcceptanceCriterionSpec(description="add(0, 0) keeps returning 0"),
            "the change keeps the public signature",
        ),
        ontology_schema=OntologySchema(name="calc", description="calculator"),
        metadata=SeedMetadata(
            ambiguity_score=0.05,
            seed_id="seed_fixed_0001",
            created_at=datetime(2026, 9, 25, tzinfo=UTC),
        ),
    )


@pytest.fixture
def seed() -> Seed:
    return make_seed()


@pytest.fixture
def base_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "README.md").write_text("calc\n")
    return root


@pytest.fixture
def fixed_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("calc\n")
    return root


def build_package(
    seed: Seed,
    *,
    repro_script: str = REPRO_SCRIPT,
    preserve_script: str = PRESERVE_SCRIPT,
    extra_files: tuple[PackageFile, ...] = (),
    scratch_paths: tuple[str, ...] = (),
    repro_argv: tuple[str, ...] | None = None,
    **overrides: object,
) -> CheckPackage:
    keys = seed_criterion_keys(seed)
    fields: dict[str, object] = {
        "seed_digest": seed_digest(seed),
        "criterion_keys": keys,
        "input_digest": INPUT_DIGEST,
        "generated_at": datetime(2026, 9, 26, 0, 0, tzinfo=UTC),
        "generator": "test-generator",
        "checks": (
            CheckSpec(
                check_id="repro-add",
                role=CheckRole.REPRODUCTION,
                argv=repro_argv or (PY, "probe/test_add.py"),
                assertions=(
                    AssertionLink(
                        assertion_id="add_sums_operands",
                        criterion_key=keys[0],
                        file="probe/test_add.py",
                    ),
                ),
                failure_signature=SIGNATURE,
            ),
            CheckSpec(
                check_id="preserve-zero",
                role=CheckRole.PRESERVATION,
                argv=(PY, "probe/test_zero.py"),
                assertions=(
                    AssertionLink(
                        assertion_id="zero_identity",
                        criterion_key=keys[1],
                        file="probe/test_zero.py",
                    ),
                ),
            ),
        ),
        "files": (
            PackageFile.from_content("probe/test_add.py", repro_script),
            PackageFile.from_content("probe/test_zero.py", preserve_script),
            *extra_files,
        ),
        "scratch_paths": scratch_paths,
        "uncovered": (
            UncoveredObligation(criterion_key=keys[2], reason="signature is not observable"),
        ),
    }
    fields.update(overrides)
    return CheckPackage.model_validate(fields)


@pytest.fixture
def package(seed: Seed) -> CheckPackage:
    return build_package(seed)
