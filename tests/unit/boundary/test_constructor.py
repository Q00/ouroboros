"""The check constructor: one read-only runtime call on a copy of the base."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ouroboros.boundary.constructor import (
    CHECK_DIR,
    CONSTRUCTOR_PERMISSION_MODE,
    CONSTRUCTOR_TOOLS,
    CheckConstructor,
    build_constructor_prompt,
    extract_json_object,
)
from ouroboros.boundary.package import CheckPackageError, seed_criterion_keys
from ouroboros.core.seed import AcceptanceCriterionSpec, OntologySchema, Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.orchestrator.adapter import TaskResult

SCRIPT = "import sys\nprint('OUROBOROS_CHECK_FAILED:repro_add')\nsys.exit(1)\n"


def _seed() -> Seed:
    return Seed(
        goal="add returns the sum",
        constraints=("keep the signature",),
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="add(2, 3) returns 5", verify_command="python -m pytest -q"
            ),
            "the code stays readable",
        ),
        ontology_schema=OntologySchema(name="calc", description="calculator"),
        metadata=SeedMetadata(seed_id="seed_constructor"),
    )


def _reply() -> str:
    body = {
        "checks": [
            {
                "check_id": "repro_add",
                "role": "reproduction",
                "argv": ["python3", f"{CHECK_DIR}/repro_add.py"],
                "failure_signature": "OUROBOROS_CHECK_FAILED:repro_add",
                "assertions": [{"criterion": 1, "locator": "add(2, 3) == 5"}],
            }
        ],
        "files": [{"path": f"{CHECK_DIR}/repro_add.py", "content": SCRIPT}],
        "uncovered": [{"criterion": 2, "reason": "readability is not mechanical"}],
    }
    return "Here is the package:\n```json\n" + json.dumps(body) + "\n```\n"


class FakeRuntime:
    def __init__(self, reply: str, *, delay: float = 0.0, write_to: Path | None = None) -> None:
        self.reply = reply
        self.delay = delay
        self.write_to = write_to
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None):
        self.calls.append({"prompt": prompt, "tools": tools, "system_prompt": system_prompt})
        if self.write_to is not None:
            (self.write_to / "calc.py").write_text("tampered\n")
        await asyncio.sleep(self.delay)
        return Result.ok(TaskResult(success=True, final_message=self.reply, messages=()))

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    return root


def _constructor(runtime: FakeRuntime, created: list[dict[str, Any]], **kwargs: Any):
    def factory(**factory_kwargs: Any) -> FakeRuntime:
        created.append(factory_kwargs)
        return runtime

    return CheckConstructor(
        runtime_backend="codex",
        model="gpt-test",
        runtime_factory=factory,
        system_prompt="SYSTEM",
        **kwargs,
    )


async def test_one_read_only_call_on_a_copy_of_the_base(base: Path) -> None:
    runtime = FakeRuntime(_reply())
    created: list[dict[str, Any]] = []

    outcome = await _constructor(runtime, created).construct(_seed(), base)

    assert outcome.failure_reason is None and outcome.package is not None
    (factory_kwargs,) = created
    assert factory_kwargs["permission_mode"] == CONSTRUCTOR_PERMISSION_MODE == "default"
    assert factory_kwargs["backend"] == "codex" and factory_kwargs["model"] == "gpt-test"
    assert Path(factory_kwargs["cwd"]).resolve() != base.resolve()
    assert not Path(factory_kwargs["cwd"]).exists()  # the copy is removed afterwards
    (call,) = runtime.calls
    assert tuple(call["tools"]) == CONSTRUCTOR_TOOLS
    assert call["system_prompt"] == "SYSTEM"
    assert "1. add(2, 3) returns 5" in call["prompt"]
    assert "declared verify_command: python -m pytest -q" in call["prompt"]
    assert runtime.closed
    keys = seed_criterion_keys(_seed())
    assert outcome.package.uncovered[0].criterion_key == keys[1]
    assert outcome.generator == "codex:gpt-test"
    assert not (base / CHECK_DIR).exists()


async def test_timeout_is_a_typed_construction_failure(base: Path) -> None:
    runtime = FakeRuntime(_reply(), delay=5)
    outcome = await _constructor(runtime, [], timeout_seconds=0.05).construct(_seed(), base)
    assert outcome.package is None and outcome.failure_reason == "constructor_timeout"


async def test_reply_without_json_is_a_typed_construction_failure(base: Path) -> None:
    outcome = await _constructor(FakeRuntime("I could not do it."), []).construct(_seed(), base)
    assert outcome.package is None
    assert outcome.failure_reason is not None
    assert outcome.failure_reason.startswith("constructor_reply_invalid:")
    assert outcome.reply_sha256 is not None


async def test_check_files_outside_the_check_dir_are_refused(base: Path) -> None:
    reply = _reply().replace(f"{CHECK_DIR}/repro_add.py", "tests/test_add.py")
    outcome = await _constructor(FakeRuntime(reply), []).construct(_seed(), base)
    assert outcome.package is None
    assert "must live under" in (outcome.failure_reason or "")


async def test_feedback_reaches_the_prompt() -> None:
    prompt = build_constructor_prompt(_seed(), ["package_rejected", "repro_add: passed on base"])
    assert "was not admitted" in prompt
    assert "- repro_add: passed on base" in prompt


def test_extract_json_accepts_raw_and_fenced_objects() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('text ```json\n{"b": 2}\n``` more') == {"b": 2}
    with pytest.raises(CheckPackageError):
        extract_json_object("no object here")


async def test_a_runtime_that_writes_to_the_base_fails_construction(base: Path) -> None:
    runtime = FakeRuntime(_reply(), write_to=base)
    outcome = await _constructor(runtime, []).construct(_seed(), base)
    assert outcome.package is None and outcome.failure_reason == "constructor_mutated_base"
