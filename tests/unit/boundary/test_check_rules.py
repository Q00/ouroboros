"""Prose-only checks are rejected at admission (boundary/check_rules.py).

Regression for a smoke run ("S4, first attempt"): for the criterion "the
README documents clamp", the constructor wrote a reproduction check that
regex-matched README prose. The worker's README was correct but worded
differently, so the authoritative package failed a correct run. The script
below is that package's check, verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.authority import CheckPackageAuthority
from ouroboros.boundary.check_rules import is_prose_only_script, prose_only_checks
from ouroboros.boundary.constructor import (
    ALL_CRITERIA_UNCOVERED,
    CHECK_DIR,
    ConstructionOutcome,
    package_from_reply,
)
from ouroboros.boundary.events import (
    ADMISSION_COMPLETED,
    BOUNDARY_AGGREGATE_TYPE,
    CONSTRUCTION_FAILED,
    PACKAGE_FROZEN,
    SUPERSEDED,
)
from ouroboros.boundary.run_wiring import CheckPackageSettings, prepare_check_package
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
)
from ouroboros.persistence.event_store import EventStore

from .test_constructor import FakeRuntime, _constructor
from .test_run_wiring import BUGFIX_SCRIPT, FakeConstructor

S4_FIRST_ATTEMPT_SCRIPT = r"""from pathlib import Path
import re
import sys

readme = Path("README.md").read_text(encoding="utf-8").lower()
normalized = re.sub(r"\s+", " ", readme)

has_signature = bool(re.search(r"clamp\s*\(\s*value\s*,\s*low\s*,\s*high\s*\)", normalized))

# Accept either direct descriptions of the three cases or the concrete examples.
has_high = bool(
    re.search(r"clamp\s*\(\s*15\s*,\s*0\s*,\s*10\s*\)\s*(?:==|->|=)\s*10", normalized)
    or re.search(r"returns?\s+high\s+(?:when|if)\s+value\s*>\s*high", normalized)
    or re.search(r"(?:when|if)\s+value\s*>\s*high.{0,80}returns?\s+high", normalized)
)
has_low = bool(
    re.search(r"clamp\s*\(\s*-?3\s*,\s*0\s*,\s*10\s*\)\s*(?:==|->|=)\s*0", normalized)
    or re.search(r"returns?\s+low\s+(?:when|if)\s+value\s*<\s*low", normalized)
    or re.search(r"(?:when|if)\s+value\s*<\s*low.{0,80}returns?\s+low", normalized)
)
has_unchanged = bool(
    re.search(r"clamp\s*\(\s*7\s*,\s*0\s*,\s*10\s*\)\s*(?:==|->|=)\s*7", normalized)
    or re.search(r"otherwise[, ]+(?:it\s+)?returns?\s+(?:the\s+)?(?:original\s+)?value", normalized)
    or re.search(r"returns?\s+(?:the\s+)?(?:original\s+)?value\s+otherwise", normalized)
    or re.search(r"value\s+otherwise", normalized)
)

try:
    assert has_signature and has_high and has_low and has_unchanged, (
        "Expected README.md to document clamp(value, low, high): return high above high, "
        "low below low, and the input value otherwise (or show the specified examples). "
        f"Observed: signature={has_signature}, high case={has_high}, "
        f"low case={has_low}, unchanged case={has_unchanged}."
    )
except AssertionError as exc:
    print("OUROBOROS_CHECK_FAILED:repro_1")
    print(exc)
    sys.exit(1)
"""

DOC_CRITERION = (
    "README.md documents clamp(value, low, high): it returns high when value > high, "
    "low when value < low, and value otherwise, with the example clamp(15, 0, 10) == 10."
)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (S4_FIRST_ATTEMPT_SCRIPT, True),
        ("import re\nre.search('x', open('docs/usage.rst').read())\n", True),
        ("print(open('CHANGELOG').read().count('clamp'))\n", True),
        (BUGFIX_SCRIPT, False),  # imports project code
        (
            "import sys\nsys.path.insert(0, '.')\nfrom mathutils import clamp\n"
            "assert 'clamp' in open('README.md').read()\nassert clamp(15, 0, 10) == 10\n",
            False,
        ),
        ("import subprocess\nsubprocess.run(['grep', 'x', 'README.md'], check=True)\n", False),
        ("from os import system\nsystem('cat README.md')\n", False),
        ("import json\njson.load(open('config.json'))\n", False),  # not prose
        ("import math\nassert math.isclose(1.0, 1.0)\n", False),  # names no prose file
        ("def broken(:\n    'README.md'\n", False),  # unparseable: admission runs it
    ],
)
def test_prose_only_scripts(script: str, expected: bool) -> None:
    assert is_prose_only_script(script) is expected


def _seed() -> Seed:
    return Seed(
        goal="Document clamp in the README only",
        acceptance_criteria=(DOC_CRITERION,),
        ontology_schema=OntologySchema(name="mathutils", description="math helpers"),
        metadata=SeedMetadata(seed_id="seed_s4_first", ambiguity_score=None),
    )


def _s4_package(seed: Seed):
    path = f"{CHECK_DIR}/repro_1.py"
    reply = {
        "checks": [
            {
                "check_id": "repro_1",
                "role": "reproduction",
                "argv": ["python3", path],
                "cwd": ".",
                "failure_signature": "OUROBOROS_CHECK_FAILED:repro_1",
                "assertions": [{"criterion": 1, "locator": "README.md documents clamp"}],
            }
        ],
        "files": [{"path": path, "content": S4_FIRST_ATTEMPT_SCRIPT}],
        "uncovered": [],
    }
    return package_from_reply(reply, seed, input_digest="3" * 64, generator="fake")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(
        "def clamp(value, low, high):\n    return max(low, min(value, high))\n"
    )
    (root / "README.md").write_text("# mathutils\n")
    return root


async def test_s4_first_attempt_is_rejected_and_the_criterion_goes_to_the_legacy_verifier(
    repo: Path, tmp_path: Path
) -> None:
    seed = _seed()
    package = _s4_package(seed)
    assert prose_only_checks(package) == ("repro_1",)

    constructor = FakeConstructor(
        ConstructionOutcome(package, None, package.input_digest, "fake"),
        # The regenerated reply follows the prompt: the criterion is uncovered.
        ConstructionOutcome(None, ALL_CRITERIA_UNCOVERED, package.input_digest, "fake"),
    )
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        settings = CheckPackageSettings(enabled=True, max_construction_attempts=3)
        state = await prepare_check_package(
            seed,
            event_store=store,
            constructor=constructor,
            execution_id="exec_s4",
            base_checkout=repo,
            worker_workspace=repo,
            runtime_label="codex",
            settings=settings,
            store_dir=tmp_path / "store",
        )
        v1 = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_s4/check_package/v1")
        v2 = await store.replay(BOUNDARY_AGGREGATE_TYPE, "exec_s4/check_package/v2")
        assert [event.type for event in v1] == [PACKAGE_FROZEN, ADMISSION_COMPLETED, SUPERSEDED]
        admission = v1[1].data
        assert admission["verdict"] == "rejected"
        assert admission["reasons"] == ["prose_only_check:repro_1"]
        assert admission["checks"] == []  # rejected before any check ran
        assert CONSTRUCTION_FAILED in [event.type for event in v2]
        # The regeneration was told why, and stopped once every criterion was uncovered.
        assert len(constructor.calls) == 2
        assert "prose_only_check:repro_1" in constructor.calls[1]
        assert state.admitted is False and state.failure_reason == ALL_CRITERIA_UNCOVERED

        # After the worker, the legacy verifier decides: the run result is untouched.
        authority = CheckPackageAuthority(
            state, settings, event_store=store, candidate_checkout=repo
        )
        legacy = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=DOC_CRITERION,
                    success=True,
                    outcome=ACExecutionOutcome.SUCCEEDED,
                ),
            ),
            success_count=1,
            failure_count=0,
        )
        assert await authority(seed=seed, execution_id="exec_s4", parallel_result=legacy) is legacy
        assert authority.outcome is not None and authority.outcome.package_decided is False
    finally:
        await store.close()


async def test_constructor_reply_with_every_criterion_uncovered_is_typed(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    reply: dict[str, Any] = {
        "checks": [],
        "files": [],
        "uncovered": [{"criterion": 1, "reason": "not executable"}],
    }
    outcome = await _constructor(FakeRuntime(json.dumps(reply)), []).construct(_seed(), base)
    assert outcome.package is None
    assert outcome.failure_reason == ALL_CRITERIA_UNCOVERED


def test_the_constructor_prompt_forbids_prose_checks() -> None:
    from ouroboros.boundary.constructor import load_constructor_system_prompt

    prompt = load_constructor_system_prompt()
    assert "prose_only_check" in prompt
    assert "not executable" in prompt
