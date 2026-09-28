"""Incremental construction: each criterion's oracle is kept as it is produced."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
from typing import Any

import pytest

from ouroboros.boundary.constructor import CheckConstructor
from ouroboros.boundary.coverage import why_excluded
from ouroboros.boundary.incremental import CONSTRUCTION_TIMEOUT, restrict_reply
from ouroboros.boundary.oracle_build import DECLARED_NOT_EXECUTABLE
from ouroboros.boundary.package import package_record_bytes, seal_package, seed_criterion_keys
from ouroboros.boundary.per_check import (
    REPRO_PASSES_ON_BASE,
)
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult

from .clamp_fixtures import BUGGY, GOOD_REPRO_1, STALE_REASON, WILLING
from .clamp_fixtures import _oracle as _oracle_u_reduction
from .clamp_fixtures import _seed as _seed_u_reduction


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
        "target_named_in_criterion": False,
        "cases": [
            {
                "case_id": "stated",
                "held_out": False,
                "args": {"value": value, "low": 0, "high": 10},
                "expect": {"kind": "returns", "value": expected},
            },
            {
                "case_id": "held",
                "held_out": True,
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
    # A Codex-like runtime: the constructor switches it to ``--ephemeral``.
    _runtime_backend = "codex"
    _exec_session_flags: tuple[str, ...] = ()

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


async def test_one_call_per_criterion_each_kept_as_produced(tmp_path: Path) -> None:
    runtime = _Runtime({})
    constructor = _constructor(runtime, 5)
    outcome = await constructor.construct(_seed(), _base(tmp_path))
    assert outcome.failure_reason is None and outcome.package is not None
    assert len(runtime.prompts) == 3
    assert [spec.check_id for spec in outcome.package.oracles] == ["oracle_1", "oracle_2"]
    keys = seed_criterion_keys(_seed())
    assert {item.criterion_key: item.reason for item in outcome.package.uncovered} == {
        keys[2]: "declared_not_executable"
    }


async def test_timeout_keeps_completed_checks_and_marks_the_rest_uncovered(tmp_path: Path) -> None:
    runtime = _Runtime({2: 30.0, 3: 30.0})
    constructor = _constructor(runtime, 0.5)
    outcome = await constructor.construct(_seed(), _base(tmp_path))
    assert outcome.package is not None
    assert [spec.check_id for spec in outcome.package.oracles] == ["oracle_1"]
    keys = seed_criterion_keys(_seed())
    reasons = {item.criterion_key: item.reason for item in outcome.package.uncovered}
    assert reasons == {keys[1]: CONSTRUCTION_TIMEOUT, keys[2]: CONSTRUCTION_TIMEOUT}


async def test_nothing_produced_in_time_is_a_construction_failure(tmp_path: Path) -> None:
    runtime = _Runtime({1: 30.0, 2: 30.0, 3: 30.0})
    outcome = await _constructor(runtime, 0.3).construct(_seed(), _base(tmp_path))
    assert outcome.package is None and outcome.failure_reason == "constructor_timeout"


async def test_the_reference_reaches_the_outcome_but_never_its_repr(tmp_path: Path) -> None:
    """The reference computes every expected value: memory only (reference_check.py)."""
    marker = "reference_marker_71c2"
    source = f"# {marker}\ndef clamp(value, low, high):\n    return max(low, min(value, high))\n"
    with_reference = {
        **FULL,
        "oracles": [
            {**item, "reference": {"source": source, "symbol": "clamp"}} for item in FULL["oracles"]
        ],
    }

    class _ReferenceRuntime(_Runtime):
        async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None):
            reply = "```json\n" + json.dumps(with_reference) + "\n```"
            return Result.ok(TaskResult(success=True, final_message=reply, messages=()))

    constructor = _constructor(_ReferenceRuntime({}), 5)
    outcome = await constructor.construct(_seed(), _base(tmp_path))
    assert outcome.references is not None
    assert sorted(outcome.references) == ["oracle_1", "oracle_2"]
    assert all(item is not None and marker in item.source for item in outcome.references.values())
    assert marker not in repr(outcome)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mathutils.py").write_text(BUGGY)
    return root


async def test_the_replacement_call_is_one_single_call_for_the_targets_only(
    repo: Path,
) -> None:
    from ouroboros.boundary.binding import CHECK_DIR

    class _Runtime:
        _runtime_backend = "codex"
        _exec_session_flags: tuple[str, ...] = ()

        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None):
            self.prompts.append(prompt)
            reply = {
                "oracles": [
                    _oracle_u_reduction(1, "r1_other", "reproduction", (15, 0, 10), 10),
                    _oracle_u_reduction(2, "r2_repro", "reproduction", (20, 0, 10), 10),
                ]
            }
            return Result.ok(TaskResult(success=True, final_message=json.dumps(reply), messages=()))

    runtime = _Runtime()
    constructor = CheckConstructor(
        runtime_backend="codex",
        model="gpt-test",
        runtime_factory=lambda **_kwargs: runtime,
        system_prompt="SYSTEM",
    )  # per_criterion by default: the replacement is still one call
    seed = _seed_u_reduction()
    why = why_excluded(REPRO_PASSES_ON_BASE)
    outcome = await constructor.construct_replacements(seed, repo, targets={2: why})
    assert len(runtime.prompts) == 1
    assert f"- criterion 2: {why}" in runtime.prompts[0]
    assert "5, 0, 10" not in runtime.prompts[0].split("no admitted check")[1]
    assert outcome.package is not None
    assert [check.check_id for check in outcome.package.checks] == ["oracle_2"]
    assert not (repo / CHECK_DIR).exists()


async def test_the_incremental_constructor_asks_for_every_criterion(repo: Path) -> None:
    """One call per criterion; a ``labels`` entry in a reply is ignored."""
    from ouroboros.boundary.constructor import load_constructor_system_prompt

    seed = _seed_u_reduction("clamp(15, 0, 10) returns 10", WILLING)

    class _Runtime:
        _runtime_backend = "codex"
        _exec_session_flags: tuple[str, ...] = ()
        calls = 0

        async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None):
            type(self).calls += 1
            reply = {
                "oracles": [GOOD_REPRO_1],
                "uncovered": [{"criterion": 2, "reason": STALE_REASON}],
                "labels": [{"criterion": 1, "kind": "context", "evidence_span": "returns 10"}],
            }
            return Result.ok(TaskResult(success=True, final_message=json.dumps(reply), messages=()))

    constructor = CheckConstructor(
        runtime_backend="codex",
        model="gpt-test",
        runtime_factory=lambda **_kwargs: _Runtime(),
        system_prompt="SYSTEM",
    )
    outcome = await constructor.construct(seed, repo)
    assert _Runtime.calls == 2  # every criterion gets its call
    assert outcome.package is not None
    assert [check.check_id for check in outcome.package.checks] == ["oracle_1"]
    keys = seed_criterion_keys(seed)
    assert [(item.criterion_key, item.reason) for item in outcome.package.uncovered] == [
        (keys[1], DECLARED_NOT_EXECUTABLE)
    ]
    assert not hasattr(outcome, "labels")
    prompt = load_constructor_system_prompt()
    assert "Attempt an oracle or a script check for every criterion." in prompt
    assert "evidence_span" not in prompt and "## Labels" not in prompt


class _PerCriterionRuntime(_Runtime):
    """Each criterion's call gets its own reply object."""

    def __init__(self, replies: dict[int, object]) -> None:
        super().__init__({})
        self.replies = replies

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None):
        self.prompts.append(prompt)
        number = int(re.search(r"for criterion (\d+) only", prompt).group(1))
        reply = "```json\n" + json.dumps(self.replies[number]) + "\n```"
        return Result.ok(TaskResult(success=True, final_message=reply, messages=()))


def _probe_seed() -> Seed:
    return Seed(
        goal="math helpers",
        acceptance_criteria=("clamp(15, 0, 10) returns 10", "clamp(-5, 0, 10) returns 0"),
        ontology_schema=OntologySchema(name="m", description="math"),
        metadata=SeedMetadata(seed_id="seed_incremental_probe", ambiguity_score=0.1),
    )


async def test_a_malformed_reply_for_one_criterion_keeps_the_others(tmp_path: Path) -> None:
    # #2464 review probe: criterion 2 answers {"checks": 1}. Construction must
    # not raise; criterion 1 is kept and criterion 2 is uncovered with a closed code.
    runtime = _PerCriterionRuntime({1: {"oracles": [_oracle(1, 15, 10)]}, 2: {"checks": 1}})
    outcome = await _constructor(runtime, 5).construct(_probe_seed(), _base(tmp_path))
    assert outcome.failure_reason is None and outcome.package is not None
    assert [spec.check_id for spec in outcome.package.oracles] == ["oracle_1"]
    keys = seed_criterion_keys(_probe_seed())
    assert {item.criterion_key: item.reason for item in outcome.package.uncovered} == {
        keys[1]: "constructor_failed:constructor_reply_invalid:section_not_list"
    }


async def test_a_refused_reply_leaves_no_trace_of_its_values(tmp_path: Path) -> None:
    # #2464 review probe: an invalid field carrying a value (the check id
    # itself is now always the product's). Neither the event-safe manifest nor
    # the stored record may contain it.
    bad = _oracle(2, -5, 0)
    bad["check_id"] = "c2_6173!"
    bad["role"] = "r6173"
    runtime = _PerCriterionRuntime({1: {"oracles": [_oracle(1, 15, 10)]}, 2: {"oracles": [bad]}})
    outcome = await _constructor(runtime, 5).construct(_probe_seed(), _base(tmp_path))
    assert outcome.package is not None
    keys = seed_criterion_keys(_probe_seed())
    reasons = {item.criterion_key: item.reason for item in outcome.package.uncovered}
    assert reasons == {keys[1]: "constructor_failed:constructor_reply_invalid:role_invalid"}
    sealed = seal_package(outcome.package)
    assert "6173" not in json.dumps(sealed.manifest_summary())
    assert b"6173" not in package_record_bytes(sealed)


async def test_a_single_call_refusal_is_a_closed_code(tmp_path: Path) -> None:
    # The whole-reply path (and the replacement call, which uses it) refuses
    # with the same closed codes and no reply text.
    bad = _oracle(1, 15, 10)
    bad["role"] = "r6173"
    runtime = _PerCriterionRuntime({})

    async def single(prompt: str, tools=None, system_prompt=None):
        reply = "```json\n" + json.dumps({"oracles": [bad]}) + "\n```"
        return Result.ok(TaskResult(success=True, final_message=reply, messages=()))

    runtime.execute_task_to_result = single  # type: ignore[method-assign]
    constructor = _constructor(runtime, 5)
    replaced = await constructor.construct_replacements(
        _probe_seed(), _base(tmp_path), targets={1: "no admitted check"}
    )
    assert replaced.package is None
    assert replaced.failure_reason == "constructor_reply_invalid:role_invalid"
