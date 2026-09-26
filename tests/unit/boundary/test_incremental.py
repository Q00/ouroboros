"""Incremental construction: each criterion's oracle is kept as it is produced."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from typing import Any

from ouroboros.boundary.constructor import CheckConstructor
from ouroboros.boundary.incremental import CONSTRUCTION_TIMEOUT, restrict_reply
from ouroboros.boundary.package import seed_criterion_keys
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult


def _seed() -> Seed:
    return Seed(
        goal="math helpers",
        acceptance_criteria=(
            "clamp(15, 0, 10) returns 10",
            "clamp(-5, 0, 10) returns 0",
            "the README documents clamp",
        ),
        ontology_schema=OntologySchema(name="m", description="math"),
        metadata=SeedMetadata(seed_id="seed_incremental", ambiguity_score=0.1),
    )


def _oracle(number: int, value: int, expected: int) -> dict[str, Any]:
    return {
        "criterion": number,
        "check_id": f"c{number}_oracle",
        "role": "reproduction",
        "call_kind": "function",
        "params": ["value", "low", "high"],
        "default_binding": {"symbol": "mathutils.clamp"},
        "cases": [
            {
                "case_id": "stated",
                "args": {"value": value, "low": 0, "high": 10},
                "expect": {"kind": "returns", "value": expected},
            },
            {
                "case_id": "held",
                "args": {"value": 42, "low": 3, "high": 8},
                "expect": {"kind": "returns", "value": 8},
            },
        ],
    }


FULL = {
    "oracles": [_oracle(1, 15, 10), _oracle(2, -5, 0)],
    "uncovered": [{"criterion": 3, "reason": "not executable"}],
}


class _Runtime:
    def __init__(self, delays: dict[int, float]) -> None:
        self.delays = delays
        self.prompts: list[str] = []

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None):
        self.prompts.append(prompt)
        number = int(re.search(r"for criterion (\d+) only", prompt).group(1))
        await asyncio.sleep(self.delays.get(number, 0.0))
        reply = "```json\n" + json.dumps(FULL) + "\n```"
        return Result.ok(TaskResult(success=True, final_message=reply, messages=()))

    async def aclose(self) -> None:
        return None


def _constructor(runtime: _Runtime, timeout: float) -> CheckConstructor:
    return CheckConstructor(
        runtime_backend="codex",
        model="gpt-test",
        runtime_factory=lambda **_: runtime,
        system_prompt="SYSTEM",
        timeout_seconds=timeout,  # type: ignore[arg-type]
    )


def _base(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    root.mkdir()
    (root / "mathutils.py").write_text("def clamp(value, low, high):\n    return value\n")
    return root


def test_restrict_reply_keeps_one_criterion() -> None:
    piece = restrict_reply(FULL, 2)
    assert [item["criterion"] for item in piece["oracles"]] == [2]
    assert piece["uncovered"] == [] and piece["checks"] == []


async def test_one_call_per_criterion_each_persisted_as_produced(tmp_path: Path) -> None:
    runtime = _Runtime({})
    constructor = _constructor(runtime, 5)
    partial = tmp_path / "partial"
    constructor.persist_partials_to(partial)
    outcome = await constructor.construct(_seed(), _base(tmp_path))
    assert outcome.failure_reason is None and outcome.package is not None
    assert len(runtime.prompts) == 3
    assert sorted(path.name for path in partial.iterdir()) == [
        "criterion-001.json",
        "criterion-002.json",
        "criterion-003.json",
    ]
    stored = json.loads((partial / "criterion-001.json").read_text())
    assert stored["status"] == "ok" and stored["reply"]["oracles"][0]["check_id"] == "c1_oracle"
    assert [spec.check_id for spec in outcome.package.oracles] == ["c1_oracle", "c2_oracle"]
    keys = seed_criterion_keys(_seed())
    assert {item.criterion_key: item.reason for item in outcome.package.uncovered} == {
        keys[2]: "not executable"
    }


async def test_timeout_keeps_completed_checks_and_marks_the_rest_uncovered(tmp_path: Path) -> None:
    runtime = _Runtime({2: 30.0, 3: 30.0})
    constructor = _constructor(runtime, 0.5)
    partial = tmp_path / "partial"
    constructor.persist_partials_to(partial)
    outcome = await constructor.construct(_seed(), _base(tmp_path))
    assert outcome.package is not None
    assert [spec.check_id for spec in outcome.package.oracles] == ["c1_oracle"]
    keys = seed_criterion_keys(_seed())
    reasons = {item.criterion_key: item.reason for item in outcome.package.uncovered}
    assert reasons == {keys[1]: CONSTRUCTION_TIMEOUT, keys[2]: CONSTRUCTION_TIMEOUT}
    assert [path.name for path in partial.iterdir()] == ["criterion-001.json"]


async def test_nothing_produced_in_time_is_a_construction_failure(tmp_path: Path) -> None:
    runtime = _Runtime({1: 30.0, 2: 30.0, 3: 30.0})
    outcome = await _constructor(runtime, 0.3).construct(_seed(), _base(tmp_path))
    assert outcome.package is None and outcome.failure_reason == "constructor_timeout"
