"""The review-and-fix pass after a run whose Seed carries expected experience."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from ouroboros.core.errors import ProviderError
from ouroboros.core.seed import Seed, SourcedScope
from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator import completion_pass
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
)
from ouroboros.orchestrator.completion_pass import (
    REVIEWER_PERMISSION_MODE,
    REVIEWER_TOOLS,
    STAGE_EVENT,
    WORKER_TOOLS,
    build_recheck_prompt,
    build_review_prompt,
    complete_after_run,
    parse_defects,
)

GOAL = "간단한 할 일 관리 웹앱 만들어줘."
LINES = ("(core) Add, edit, complete and delete tasks.", "(common) Filters and a remaining count.")


def _seed_content(*, expected: tuple[str, ...] = LINES, sourced: bool = True) -> str:
    data: dict[str, Any] = {
        "goal": "Build a simple to-do web app",
        "acceptance_criteria": ["A user can add and complete tasks"],
        "ontology_schema": {"name": "Todo", "description": "d"},
        "metadata": {"ambiguity_score": 0.1},
    }
    if sourced:
        data["sourced_scope"] = SourcedScope(goal=GOAL, expected_experience=expected).model_dump(
            mode="json"
        )
    return yaml.safe_dump(Seed.from_dict(data).to_dict(), allow_unicode=True)


def _run_result(status: str = "completed", *, is_error: bool = False) -> MCPToolResult:
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="Seed Execution COMPLETED"),),
        is_error=is_error,
        meta={"status": status, "success": status == "completed"},
    )


@dataclass
class _Final:
    final_message: str


@dataclass
class _Runtime:
    reply: str | None
    calls: list[dict[str, Any]]
    hang: bool = False
    closed: bool = False

    async def execute_task_to_result(self, prompt: str, tools: list[str] | None = None) -> Any:
        self.calls.append({"prompt": prompt, "tools": tools})
        if self.hang:
            await asyncio.Event().wait()
        if self.reply is None:
            return Result.err(ProviderError("boom"))
        return Result.ok(_Final(self.reply))

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class _Factory:
    replies: list[str | None]
    hang: bool = False
    created: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    runtimes: list[_Runtime] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> _Runtime:
        self.created.append(kwargs)
        runtime = _Runtime(self.replies.pop(0), self.calls, hang=self.hang)
        self.runtimes.append(runtime)
        return runtime


@dataclass
class _Events:
    fail: bool = False
    appended: list[Any] = field(default_factory=list)

    async def append(self, event: Any) -> None:
        if self.fail:
            raise RuntimeError("database is locked")
        self.appended.append(event)

    def stages(self) -> list[tuple[str, float]]:
        return [(e.data["stage"], e.data["bound_seconds"]) for e in self.appended]


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch) -> None:
    monkeypatch.setattr(completion_pass, "RETRY_DELAY_SECONDS", 0.0)


async def _complete(factory: _Factory, **overrides: Any) -> MCPToolResult:
    kwargs: dict[str, Any] = {
        "seed_content": _seed_content(),
        "working_dir": Path("/work/app"),
        "runtime_backend": "codex",
        "runtime_factory": factory,
    }
    kwargs.update(overrides)
    run_result = kwargs.pop("run_result", _run_result())
    return await complete_after_run(run_result, **kwargs)


@pytest.mark.asyncio
async def test_defects_are_reviewed_read_only_then_fixed_by_a_worker() -> None:
    review = "- app.js:21 new tasks without a due date are hidden\n- (core) no edit\nnot a defect"
    factory = _Factory([review, "done", "NONE"])

    result = await _complete(factory)

    reviewer, worker, rechecker = factory.created
    assert reviewer["permission_mode"] == REVIEWER_PERMISSION_MODE == "default"
    assert reviewer["cwd"] == Path("/work/app")
    assert "permission_mode" not in worker  # the run's own permission mode
    assert worker["cwd"] == Path("/work/app")
    assert factory.calls[0]["tools"] == list(REVIEWER_TOOLS)
    assert factory.calls[1]["tools"] == list(WORKER_TOOLS)
    assert rechecker["permission_mode"] == "default"
    assert factory.calls[2]["tools"] == list(REVIEWER_TOOLS)
    assert factory.calls[2]["prompt"] == build_recheck_prompt(
        Seed.from_dict(yaml.safe_load(_seed_content()))
    )
    fix_prompt = factory.calls[1]["prompt"]
    assert "- app.js:21 new tasks without a due date are hidden\n- (core) no edit" in fix_prompt
    assert "not a defect" not in fix_prompt
    assert GOAL in fix_prompt
    assert all(runtime.closed for runtime in factory.runtimes)
    assert result.meta["completion_pass"] == {
        "status": "fixed",
        "defects": 2,
        "defect_list": ["app.js:21 new tasks without a due date are hidden", "(core) no edit"],
        "recheck": "clean",
    }
    assert result.meta["status"] == "completed"
    assert result.is_error is False
    assert "a reviewer found 2 defect(s); a worker fixed them." in result.text_content
    assert "A second review found nothing broken." in result.text_content


@pytest.mark.asyncio
async def test_what_the_fix_broke_is_found_again_and_repaired() -> None:
    regression = "- index.html: game.js reads #lapTotal, which the page does not have"
    factory = _Factory(["- (core) no lap count", "done", regression + "\nmissing minimap", "done"])

    result = await _complete(factory)

    assert len(factory.created) == 4
    assert factory.created[2]["permission_mode"] == "default"
    assert factory.calls[2]["tools"] == list(REVIEWER_TOOLS)
    assert factory.calls[3]["tools"] == list(WORKER_TOOLS)
    assert regression in factory.calls[3]["prompt"]
    assert "missing minimap" not in factory.calls[3]["prompt"]
    record = result.meta["completion_pass"]
    assert record["status"] == "fixed"
    assert record["recheck"] == "repaired"
    assert record["regressions"] == [regression[2:]]
    assert "A second review found 1 broken part(s); a worker repaired them." in result.text_content


def test_the_recheck_asks_only_for_what_stops_basic_use() -> None:
    prompt = build_recheck_prompt(Seed.from_dict(yaml.safe_load(_seed_content())))

    assert GOAL in prompt
    assert "Do not modify any file." in prompt
    assert "does not exist or is not loaded" in prompt
    assert "Do not list missing features." in prompt
    assert prompt.endswith("Reply with the list only, or the single word NONE.")


@pytest.mark.asyncio
async def test_a_clean_review_runs_no_worker() -> None:
    factory = _Factory(["NONE"])

    result = await _complete(factory)

    assert len(factory.created) == 1
    assert result.meta["completion_pass"] == {"status": "clean", "defects": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replies", "status"),
    [([None, None], "review_failed"), (["- broken", None, None, "NONE"], "fix_failed")],
)
async def test_a_session_that_fails_twice_leaves_the_run_verdict(replies, status) -> None:
    result = await _complete(_Factory(list(replies)))

    assert result.meta["completion_pass"]["status"] == status
    assert result.meta["status"] == "completed"
    assert result.meta["success"] is True
    assert result.is_error is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replies", "status"),
    [([None, "NONE"], "clean"), (["- broken", None, "done", "NONE"], "fixed")],
)
async def test_a_session_that_ends_in_an_error_is_tried_once_more(replies, status) -> None:
    factory = _Factory(list(replies))

    result = await _complete(factory)

    assert result.meta["completion_pass"]["status"] == status
    assert len(factory.created) == len(replies)
    assert all(runtime.closed for runtime in factory.runtimes)


@pytest.mark.asyncio
async def test_a_hung_session_is_bounded_and_not_tried_again() -> None:
    factory = _Factory(["x", "x"], hang=True)

    result = await asyncio.wait_for(_complete(factory, timeout_seconds=0.05), timeout=5)

    assert result.meta["completion_pass"]["status"] == "review_failed"
    assert len(factory.created) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"seed_content": _seed_content(sourced=False)},
        {"seed_content": _seed_content(expected=())},
        {"seed_content": "not: [a, seed"},
        {"run_result": _run_result("failed")},
        {"run_result": _run_result("paused")},
        {"run_result": _run_result(is_error=True)},
        {"runtime_backend": None},
    ],
)
async def test_nothing_runs_without_expected_experience_or_a_completed_run(overrides) -> None:
    factory = _Factory([])
    run_result = overrides.get("run_result", _run_result())

    result = await _complete(factory, **overrides)

    assert factory.created == []
    assert result is run_result or result.meta == run_result.meta
    assert "completion_pass" not in result.meta


@pytest.mark.asyncio
async def test_a_delegating_parent_bounds_both_sessions_as_it_bounded_the_run() -> None:
    factory = _Factory(["- broken", "done", "NONE"])
    parent = {
        DELEGATED_PARENT_PERMISSION_MODE_ARG: "default",
        DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG: ["Read", "Edit", "Grep", "WebFetch"],
    }

    await _complete(factory, run_arguments=parent)

    reviewer, worker, rechecker = factory.created
    assert reviewer["permission_mode"] == rechecker["permission_mode"] == "default"
    assert worker["permission_mode"] == "default"
    assert factory.calls[0]["tools"] == ["Read", "Grep"]
    assert factory.calls[1]["tools"] == ["Read", "Edit", "Grep"]


@pytest.mark.asyncio
async def test_nothing_runs_when_the_parent_allows_no_tool_that_writes() -> None:
    factory = _Factory([])
    parent = {DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG: ["Read", "Glob", "Grep"]}

    result = await _complete(factory, run_arguments=parent)

    assert factory.created == []
    assert "completion_pass" not in result.meta


@pytest.mark.asyncio
async def test_a_plugin_dispatched_backend_is_left_alone(monkeypatch) -> None:
    monkeypatch.setattr(
        "ouroboros.mcp.tools.subagent.should_dispatch_via_plugin", lambda *_args: True
    )
    factory = _Factory([])

    result = await _complete(factory, runtime_backend="opencode", opencode_mode="plugin")

    assert factory.created == []
    assert "completion_pass" not in result.meta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replies", "stages", "status"),
    [
        (["- broken", "done", "NONE"], ["review", "fix", "recheck", "done"], "fixed"),
        (
            ["- broken", "done", "- broke", "done"],
            ["review", "fix", "recheck", "repair", "done"],
            "fixed",
        ),
        (["NONE"], ["review", "done"], "clean"),
        ([None, "NONE"], ["review", "review", "done"], "clean"),
        ([None, None], ["review", "review", "done"], "review_failed"),
    ],
)
async def test_each_stage_is_announced_with_the_most_it_can_take(replies, stages, status) -> None:
    events = _Events()

    await _complete(_Factory(list(replies)), event_store=events, execution_id="exec_1")

    assert [e.type for e in events.appended] == [STAGE_EVENT] * len(stages)
    assert {e.aggregate_type for e in events.appended} == {"execution"}
    assert {e.aggregate_id for e in events.appended} == {"exec_1"}
    bound = completion_pass.SESSION_TIMEOUT_SECONDS
    assert events.stages() == [(stage, 0.0 if stage == "done" else bound) for stage in stages]
    assert events.appended[-1].data["status"] == status
    attempts = [e.data["attempt"] for e in events.appended if e.data["stage"] == "review"]
    assert attempts == list(range(1, len(attempts) + 1))


@pytest.mark.asyncio
async def test_a_skipped_pass_announces_nothing_and_a_failed_announce_changes_nothing() -> None:
    events = _Events()
    await _complete(
        _Factory([]), event_store=events, execution_id="x", run_result=_run_result("failed")
    )
    assert events.appended == []

    result = await _complete(
        _Factory(["- broken", "done", "NONE"]), event_store=_Events(fail=True), execution_id="x"
    )
    assert result.meta["completion_pass"]["status"] == "fixed"


def test_the_review_prompt_asks_for_basic_use_first_then_each_expected_line() -> None:
    seed = Seed.from_dict(yaml.safe_load(_seed_content()))

    prompt = build_review_prompt(seed)

    assert prompt.startswith(f"You review an app an AI built for this request: {GOAL}")
    assert "\n".join(f"- {line}" for line in LINES) in prompt
    assert prompt.index("request's basic use") < prompt.index("each expected line above")
    assert "Do not modify any file." in prompt
    assert prompt.endswith("Reply with the list only, or the single word NONE.")


def test_defects_are_bounded() -> None:
    reply = "\n".join(f"- {'x' * 500} {n}" for n in range(60))

    defects = parse_defects(reply)

    assert len(defects) == completion_pass.MAX_DEFECTS
    assert all(len(defect) == completion_pass.MAX_DEFECT_CHARS for defect in defects)
    assert parse_defects("NONE") == ()
    assert parse_defects("- \n-") == ()
