"""The review-and-fix pass after a run whose Seed carries expected experience."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

import pytest
import yaml

from ouroboros.core.errors import ProviderError
from ouroboros.core.seed import Seed, SourcedScope
from ouroboros.core.types import Result
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator import completion_pass, playtest
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
)
from ouroboros.orchestrator.completion_pass import (
    CHECK_DIR,
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
    action: Callable[[], Awaitable[None]] | None = None

    async def execute_task_to_result(self, prompt: str, tools: list[str] | None = None) -> Any:
        self.calls.append({"prompt": prompt, "tools": tools})
        if self.action is not None:
            await self.action()
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
    # what the session with this index does while it runs
    actions: dict[int, Callable[[], Awaitable[None]]] = field(default_factory=dict)

    def __call__(self, **kwargs: Any) -> _Runtime:
        self.created.append(kwargs)
        action = self.actions.get(len(self.created) - 1)
        runtime = _Runtime(self.replies.pop(0), self.calls, hang=self.hang, action=action)
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
        "playtest": "no_browser",
        "visible_defects": [],
        "recheck": "clean",
        "regressions": [],
    }
    assert result.meta["status"] == "completed"
    assert result.is_error is False
    assert (
        "Completion pass: a code review found 2 defect(s). It was not played: no"
        " Chromium-family browser was found. A worker fixed them. A second code review found"
        " nothing broken."
    ) in result.text_content


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
    assert record["recheck"] == "found"
    assert record["regressions"] == [regression[2:]]
    assert record["repair"] == "repaired"
    assert "A second code review found 1 broken part(s). A worker repaired them." in (
        result.text_content
    )


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
    assert result.meta["completion_pass"] == {
        "status": "clean",
        "defects": 0,
        "playtest": "no_browser",
        "visible_defects": [],
    }


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


PLAN = (
    'I read game.js. {"page": "index.html", "steps": [{"shot": "menu"}, '
    '{"click_text": "Start race"}, {"key": "ArrowUp", "hold": 2000}, {"shot": "drive"}]}'
)


@dataclass
class _Player:
    shots: int = 2
    errors: tuple[str, ...] = ("TypeError: lap is undefined",)
    fail: bool = False
    # A page that never ends: shots keep coming until the play is told to stop.
    endless: bool = False
    calls: list[tuple[Path, Any, Path, str]] = field(default_factory=list)
    stopped: list[float] = field(default_factory=list)

    def __call__(
        self,
        site: Path,
        script: Any,
        out_dir: Path,
        browser: str,
        stop: threading.Event | None = None,
    ) -> Any:
        from ouroboros.orchestrator.playtest import PlayReport

        self.calls.append((site, script, out_dir, browser))
        if self.fail:
            raise RuntimeError("browser closed the pipe")
        out_dir.mkdir(parents=True, exist_ok=True)
        shots = []
        n = 0
        while n < self.shots or self.endless:
            if stop is not None and stop.is_set():
                self.stopped.append(time.monotonic())
                break
            shot = out_dir / f"{n + 1:02d}-shot.png"
            shot.write_bytes(b"png")
            shots.append(shot)
            n += 1
            if self.endless:
                time.sleep(0.01)
        return PlayReport(shots=shots, errors=list(self.errors))


def _app(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<canvas></canvas>")
    return tmp_path


@pytest.mark.asyncio
async def test_the_app_is_played_before_the_fix_and_again_after_it(tmp_path) -> None:
    app = _app(tmp_path)
    player = _Player()
    visible = "- game.js: the GO! banner stays on screen during the race"
    factory = _Factory(["- (core) no lap count", PLAN, visible, "done", "NONE", "NONE", "0"])

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=player
    )

    reviewer, planner, looker, fixer, rechecker, relooker, picker = factory.created
    for session in (reviewer, planner, looker, rechecker, relooker, picker):
        assert session["permission_mode"] == "default"
    assert [call["tools"] for call in factory.calls] == [
        list(REVIEWER_TOOLS),
        list(REVIEWER_TOOLS),
        list(REVIEWER_TOOLS),
        list(WORKER_TOOLS),
        list(REVIEWER_TOOLS),
        list(REVIEWER_TOOLS),
        list(REVIEWER_TOOLS),
    ]
    assert "permission_mode" not in fixer
    assert factory.calls[1]["prompt"] == completion_pass.build_play_plan_prompt(
        Seed.from_dict(yaml.safe_load(_seed_content()))
    )
    (site, script, out_dir, browser), replay = player.calls
    assert (site, out_dir, browser) == (app, app / ".ouroboros/playtest/round-1", "/bin/chrome")
    assert script.page == "index.html"
    assert [next(iter(step)) for step in script.steps] == ["shot", "click_text", "key", "shot"]
    assert replay[1] is script and replay[2] == app / ".ouroboros/playtest/round-2"
    look = factory.calls[2]["prompt"]
    assert ".ouroboros/playtest/round-1/01-shot.png, .ouroboros/playtest/round-1/02-shot.png" in (
        look
    )
    assert "- TypeError: lap is undefined" in look
    assert '2. click_text "Start race"' in look
    assert "Open and look at every screenshot. Do not modify any file." in look
    fix_prompt = factory.calls[3]["prompt"]
    assert "- (core) no lap count\n" + visible in fix_prompt  # code defects, then the screen's
    assert "the screenshots are .ouroboros/playtest/round-1/01-shot.png" in fix_prompt
    assert f"run `{completion_pass.check_command()}` in this directory" in fix_prompt
    assert "never make the page detect that browser or behave differently in it" in fix_prompt
    saved = json.loads((app / ".ouroboros/playtest/script.json").read_text())
    assert saved == {"page": "index.html", "steps": [dict(step) for step in script.steps]}
    relook = factory.calls[5]["prompt"]
    assert ".ouroboros/playtest/round-2/01-shot.png" in relook
    assert (
        "with these screenshots, in the same order: .ouroboros/playtest/round-1/01-shot.png,"
        " .ouroboros/playtest/round-1/02-shot.png. Open them too"
    ) in relook
    assert "look unfinished" in look and "look unfinished" not in relook
    assert "played before the last change" not in look
    assert result.meta["completion_pass"] == {
        "status": "fixed",
        "defects": 1,
        "defect_list": ["(core) no lap count"],
        "playtest": "found",
        "visible_defects": [visible[2:]],
        "recheck": "clean",
        "regressions": [],
        "replay_2": "clean",
        "replay_2_defects": [],
        "kept_round": 2,
        "last_round": 2,
    }
    pick = factory.calls[6]["prompt"]
    assert (
        "Version 1, its screenshots in order: .ouroboros/playtest/round-2/01-shot.png,"
        " .ouroboros/playtest/round-2/02-shot.png\nIts page logged or met these problems:\n"
        "- TypeError: lap is undefined\nVersion 2, its screenshots in order:"
        " .ouroboros/playtest/round-1/01-shot.png"
    ) in pick
    assert (app / "index.html").read_text() == "<canvas></canvas>"
    assert (
        "a code review found 1 defect(s). Playing it in a browser showed 1 visible problem(s)."
        " A worker fixed them. A second code review found nothing broken. Played again, it"
        " showed nothing broken."
    ) in result.text_content


def test_only_the_first_lines_marked_unfinished_are_kept() -> None:
    defects = (
        "unfinished: the track fills a third of the window",
        "game.js: ArrowUp does nothing",
        "Unfinished: the scene is sparse",
        "unfinished: the HUD crowds the road",
        "unfinished: the sky is flat",
        "index.html: the start button is cut off",
        "unfinished: the karts are boxes",
    )

    assert completion_pass.bound_unfinished(defects) == defects[:4] + defects[5:6]
    assert completion_pass.bound_unfinished(defects[1:2]) == defects[1:2]
    assert completion_pass.bound_unfinished(()) == ()


@pytest.mark.asyncio
async def test_a_look_that_marks_more_than_three_unfinished_lines_passes_on_three(
    tmp_path,
) -> None:
    app = _app(tmp_path)
    look = "\n".join(
        [
            "- game.js: the GO! banner stays on screen during the race",
            *(f"- unfinished: problem {n}" for n in range(1, 6)),
        ]
    )
    factory = _Factory(["NONE", PLAN, look, "done", "NONE", "NONE", "0"])

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=_Player()
    )

    kept = [
        "game.js: the GO! banner stays on screen during the race",
        "unfinished: problem 1",
        "unfinished: problem 2",
        "unfinished: problem 3",
    ]
    assert result.meta["completion_pass"]["visible_defects"] == kept
    fix_prompt = factory.calls[3]["prompt"]
    assert "- unfinished: problem 3\n" in fix_prompt
    assert "problem 4" not in fix_prompt and "problem 5" not in fix_prompt
    assert "Playing it in a browser showed 4 visible problem(s)." in result.text_content


def test_the_check_command_asks_with_this_python() -> None:
    command = completion_pass.check_command()

    assert command.endswith(" -m ouroboros.orchestrator.playtest .ouroboros/playtest/check")
    assert sys.executable in command


@pytest.mark.asyncio
async def test_the_host_plays_the_page_whenever_the_worker_asks_while_it_fixes(tmp_path) -> None:
    app = _app(tmp_path)
    player = _Player()
    answers: list[Any] = []

    async def worker_checks() -> None:  # what the worker's check command does, in its shell
        answers.append(await asyncio.to_thread(playtest.request_play, app / CHECK_DIR, 5.0))

    factory = _Factory(
        ["NONE", PLAN, "- app.js: not drawn", "done", "NONE", "NONE"], actions={3: worker_checks}
    )

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=player
    )

    first, check, replay = player.calls
    assert check[1] is first[1]  # the saved script, never one the worker names
    assert check[2].parent == app / CHECK_DIR
    (answer,) = answers
    assert answer["shots"] == [
        f".ouroboros/playtest/check/{check[2].name}/01-shot.png",
        f".ouroboros/playtest/check/{check[2].name}/02-shot.png",
    ]
    assert answer["errors"] == ["TypeError: lap is undefined"]
    assert result.meta["completion_pass"]["checks"] == 1
    assert "While they worked, the page was played 1 more time(s)." in result.text_content
    # Once the worker is done, nobody answers.
    assert await asyncio.to_thread(playtest.request_play, app / CHECK_DIR, 0.6) is None


@pytest.mark.asyncio
async def test_a_planted_link_never_carries_the_saved_script_outside_the_app(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    app = _app(tmp_path / "app")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    saved = app / completion_pass.PLAYTEST_DIR / "script.json"
    saved.parent.mkdir(parents=True)
    saved.symlink_to(outside)
    factory = _Factory(["NONE", PLAN, "- app.js: not drawn", "done", "NONE", "NONE"])

    await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=_Player()
    )

    assert outside.read_text() == "keep"
    assert not saved.is_symlink()
    assert json.loads(saved.read_text())["page"] == "index.html"


@pytest.mark.asyncio
async def test_a_playtest_directory_that_leads_outside_the_app_is_never_written(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    app = _app(tmp_path / "app")
    outside = tmp_path / "outside"
    outside.mkdir()
    (app / ".ouroboros").mkdir()
    (app / completion_pass.PLAYTEST_DIR).symlink_to(outside)
    player = _Player()
    factory = _Factory(["- app.js: no lap count", PLAN, "done", "NONE"])

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=player
    )

    assert list(outside.iterdir()) == [] and player.calls == []
    assert result.meta["completion_pass"]["playtest"] == "play_failed"
    # no replay command without a saved script
    assert completion_pass.check_command() not in factory.calls[2]["prompt"]


def _files(path: Path) -> set[Path]:
    return {p for p in path.rglob("*") if p.is_file()}


@pytest.mark.asyncio
async def test_a_play_past_its_bound_is_stopped_before_the_pass_goes_on(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(completion_pass, "PLAY_BOUND_SECONDS", 0.2)
    app = _app(tmp_path)
    player = _Player(endless=True)
    factory = _Factory(["- app.js: no lap count", PLAN, "done", "NONE"])

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=player
    )

    assert result.meta["completion_pass"]["playtest"] == "play_failed"
    assert len(player.stopped) == 2  # both plays, each stopped at its bound
    written = _files(app / completion_pass.PLAYTEST_DIR)
    await asyncio.sleep(0.1)
    assert _files(app / completion_pass.PLAYTEST_DIR) == written  # nothing after


@pytest.mark.asyncio
async def test_a_replay_is_stopped_when_its_server_is_cancelled(tmp_path) -> None:
    app = _app(tmp_path)
    player = _Player(endless=True)
    task, _ = await _serve(app, player)
    asking = asyncio.create_task(asyncio.to_thread(playtest.request_play, app / CHECK_DIR, 1.0))
    while not player.calls:
        await asyncio.sleep(0.01)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(player.stopped) == 1
    written = _files(app / CHECK_DIR)
    await asyncio.sleep(0.1)
    assert _files(app / CHECK_DIR) == written
    assert await asyncio.wait_for(asking, 5) is None  # no answer after the worker's turn


async def _serve(app: Path, player: _Player, **kwargs: Any) -> tuple[asyncio.Task, list[str]]:
    served: list[str] = []
    script = playtest.parse_play_script(PLAN, app)
    assert script is not None
    task = asyncio.create_task(
        completion_pass.serve_checks(
            app, script, "/bin/chrome", player, served, poll_seconds=0.01, **kwargs
        )
    )
    return task, served


@pytest.mark.asyncio
async def test_a_worker_gets_at_most_the_limit_of_plays(tmp_path) -> None:
    app = _app(tmp_path)
    (app / CHECK_DIR).mkdir(parents=True)
    player = _Player()
    task, served = await _serve(app, player, limit=1)
    try:
        first = await asyncio.to_thread(playtest.request_play, app / CHECK_DIR, 5.0)
        second = await asyncio.to_thread(playtest.request_play, app / CHECK_DIR, 5.0)
    finally:
        task.cancel()
    assert first is not None and len(first["shots"]) == 2
    assert second == {"error": "the page was already played 1 times in this session"}
    assert len(player.calls) == len(served) == 1


@pytest.mark.asyncio
async def test_a_play_that_fails_is_reported_to_the_worker(tmp_path) -> None:
    app = _app(tmp_path)
    task, _ = await _serve(app, _Player(fail=True))
    try:
        answer = await asyncio.to_thread(playtest.request_play, app / CHECK_DIR, 5.0)
    finally:
        task.cancel()
    assert answer == {"error": "the play did not finish: browser closed the pipe"}


@pytest.mark.asyncio
async def test_a_check_directory_that_leads_outside_the_app_is_never_used(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    app = _app(tmp_path / "app")
    outside = tmp_path / "outside"
    outside.mkdir()
    (app / CHECK_DIR).parent.mkdir(parents=True)
    (app / CHECK_DIR).symlink_to(outside)
    player = _Player()
    task, _ = await _serve(app, player)
    try:
        answer = await asyncio.to_thread(playtest.request_play, app / CHECK_DIR, 0.5)
    finally:
        task.cancel()
    assert answer is None and player.calls == []
    assert [p.name for p in outside.iterdir()][0].startswith("request-")  # left unanswered


@pytest.mark.asyncio
async def test_a_planted_play_directory_is_refused_and_odd_requests_are_dropped(tmp_path) -> None:
    (tmp_path / "app").mkdir()
    app = _app(tmp_path / "app")
    outside = tmp_path / "outside"
    outside.mkdir()
    check_dir = app / CHECK_DIR
    check_dir.mkdir(parents=True)
    nonce = "a" * 32
    (check_dir / nonce).symlink_to(outside)
    (check_dir / f"request-{nonce}").write_text("")
    (check_dir / "request-not-a-nonce").write_text("")
    player = _Player()
    task, _ = await _serve(app, player)
    try:
        for _ in range(200):
            if (check_dir / f"result-{nonce}.json").exists():
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
    answer = json.loads((check_dir / f"result-{nonce}.json").read_text())
    assert answer["error"].startswith("the play did not finish")
    assert player.calls == [] and list(outside.iterdir()) == []
    assert not (check_dir / "request-not-a-nonce").exists()
    assert not (check_dir / "result-not-a-nonce.json").exists()


@pytest.mark.asyncio
async def test_a_worker_without_a_shell_gets_no_check_command(tmp_path) -> None:
    factory = _Factory(["NONE", PLAN, "- app.js: not drawn", "done", "NONE", "NONE"])

    await _complete(
        factory,
        working_dir=_app(tmp_path),
        browser_finder=lambda: "/bin/chrome",
        player=_Player(),
        run_arguments={DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG: ["Read", "Edit", "Glob", "Grep"]},
    )

    fix_prompt = factory.calls[3]["prompt"]
    assert "the screenshots are" in fix_prompt
    assert "To see the page after your changes" not in fix_prompt


@pytest.mark.asyncio
async def test_what_the_recheck_and_the_replay_find_go_to_one_repair(tmp_path) -> None:
    regression = "- index.html: game.js reads #lapTotal, which the page does not have"
    still = "- app.js: the galaxy is still a white blob"
    factory = _Factory(
        ["NONE", PLAN, "- app.js: not drawn", "done", regression, still, "done", "0", "1"]
    )
    player = _Player()

    result = await _complete(
        factory, working_dir=_app(tmp_path), browser_finder=lambda: "/bin/chrome", player=player
    )

    assert len(factory.created) == 9
    repair_prompt = factory.calls[6]["prompt"]
    assert factory.calls[6]["tools"] == list(WORKER_TOOLS)
    assert f"{regression}\n{still}" in repair_prompt
    assert ".ouroboros/playtest/round-2/01-shot.png" in repair_prompt
    assert "To see the page after your changes" in repair_prompt
    record = result.meta["completion_pass"]
    assert (record["recheck"], record["replay_2"], record["repair"]) == (
        "found",
        "found",
        "repaired",
    )
    assert record["replay_2_defects"] == [still[2:]]
    # The repair is played too, and compared with each version before it.
    assert player.calls[2][2] == tmp_path / ".ouroboros/playtest/round-3"
    assert (
        "Version 1, its screenshots in order: .ouroboros/playtest/round-3/01-shot.png"
        in (factory.calls[7]["prompt"])
    )
    assert (
        "Version 2, its screenshots in order: .ouroboros/playtest/round-2/01-shot.png"
        in (factory.calls[7]["prompt"])
    )
    assert (
        "Version 2, its screenshots in order: .ouroboros/playtest/round-1/01-shot.png"
        in (factory.calls[8]["prompt"])
    )
    assert (record["replay_3"], record["kept_round"], record["last_round"]) == ("played", 3, 3)
    assert (
        "Played again, it still showed 1 problem(s). A worker repaired them."
        " It was played once more." in result.text_content
    )


@pytest.mark.asyncio
async def test_a_clean_review_and_a_clean_play_run_no_worker(tmp_path) -> None:
    player = _Player(errors=())
    factory = _Factory(["NONE", PLAN, "NONE"])

    result = await _complete(
        factory, working_dir=_app(tmp_path), browser_finder=lambda: "/bin/chrome", player=player
    )

    assert len(factory.created) == 3
    assert len(player.calls) == 1
    assert "The page logged no errors." in factory.calls[2]["prompt"]
    assert result.meta["completion_pass"] == {
        "status": "clean",
        "defects": 0,
        "playtest": "clean",
        "visible_defects": [],
    }
    assert "Playing it in a browser showed nothing broken." in result.text_content


@pytest.mark.asyncio
async def test_what_only_the_screen_shows_is_fixed(tmp_path) -> None:
    factory = _Factory(
        ["NONE", PLAN, "- app.js: the galaxy is not drawn", "done", "NONE", "NONE", "0"]
    )

    result = await _complete(
        factory, working_dir=_app(tmp_path), browser_finder=lambda: "/bin/chrome", player=_Player()
    )

    assert "- app.js: the galaxy is not drawn" in factory.calls[3]["prompt"]
    assert result.meta["completion_pass"]["status"] == "fixed"
    assert result.meta["completion_pass"]["defects"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "player", "status", "sessions"),
    [
        ("NONE", _Player(), "not_planned", 2),
        ('{"page": "../secret.html", "steps": [{"shot": "a"}]}', _Player(), "not_planned", 2),
        (PLAN, _Player(fail=True), "play_failed", 2),
        (PLAN, _Player(shots=0), "play_failed", 2),
    ],
)
async def test_an_app_that_cannot_be_played_is_left_as_it_is(
    tmp_path, plan, player, status, sessions
) -> None:
    factory = _Factory(["NONE", plan])

    result = await _complete(
        factory, working_dir=_app(tmp_path), browser_finder=lambda: "/bin/chrome", player=player
    )

    assert len(factory.created) == sessions
    assert result.meta["completion_pass"]["playtest"] == status
    assert result.meta["completion_pass"]["status"] == "clean"
    assert result.meta["status"] == "completed"


@pytest.mark.asyncio
async def test_no_browser_means_no_play_and_no_extra_session(tmp_path) -> None:
    factory = _Factory(["NONE"])

    result = await _complete(factory, working_dir=_app(tmp_path), browser_finder=lambda: None)

    assert len(factory.created) == 1
    assert result.meta["completion_pass"]["playtest"] == "no_browser"
    assert "It was not played: no Chromium-family browser was found." in result.text_content


@pytest.mark.asyncio
async def test_the_play_stages_are_announced_with_their_bounds(tmp_path) -> None:
    events = _Events()
    factory = _Factory(["NONE", PLAN, "- game.js: banner stays", "done", "NONE", "NONE", "0"])

    await _complete(
        factory,
        working_dir=_app(tmp_path),
        browser_finder=lambda: "/bin/chrome",
        player=_Player(),
        event_store=events,
        execution_id="exec_1",
    )

    bound, play = completion_pass.SESSION_TIMEOUT_SECONDS, completion_pass.PLAY_BOUND_SECONDS
    assert events.stages() == [
        ("review", bound),
        ("play_plan", bound),
        ("play", play),
        ("play_review", bound),
        ("fix", bound),
        ("recheck", bound),
        ("play_2", play),
        ("play_review_2", bound),
        ("pick_1", bound),
        ("done", 0.0),
    ]


def test_the_play_prompts_say_what_to_write_and_what_to_look_for() -> None:
    seed = Seed.from_dict(yaml.safe_load(_seed_content()))
    plan = completion_pass.build_play_plan_prompt(seed)

    assert GOAL in plan
    assert "Do not modify any file." in plan
    assert "the controls the code really listens to" in plan
    assert "submit it by pressing Enter, as most people do" in plan
    assert (
        "click a text field before typing into it, with click_text naming its placeholder" in plan
    )
    assert '{"click_text": "visible label or a field\'s placeholder"}' in plan
    assert "at most 8, and name each" in plan
    assert '{"page": "<page path in this directory>", "steps": [...]}' in plan
    assert plan.endswith("If the app does not run that way, reply with the single word NONE.")
    assert "name each for what the user has just done" in plan
    assert "not for what the app should show by then" in plan
    assert "Do not list missing features." in completion_pass.PLAY_LOOK_PROMPT
    assert "text or overlays that stay" in completion_pass.PLAY_LOOK_PROMPT
    assert "reload the page once near the end and take a screenshot after it" in plan
    assert (
        "text a drag or click left selected, what the user made that a reload lost"
        in completion_pass.PLAY_LOOK_PROMPT
    )
    assert (
        "that something a real user needs longer to reach did not happen in it is not a defect, "
        "and nothing should happen faster than it would for a real user."
    ) in completion_pass.PLAY_LOOK_PROMPT
    assert (
        "A step the notes say missed its target tells nothing about the app."
        in completion_pass.PLAY_LOOK_PROMPT
    )


def test_only_the_first_look_asks_what_looks_unfinished(tmp_path: Path) -> None:
    seed = Seed.from_dict(yaml.safe_load(_seed_content()))
    script = playtest.PlayScript(page="index.html", steps=({"shot": "start"},))
    report = playtest.PlayReport(shots=[tmp_path / "round-1" / "01-start.png"])

    first = completion_pass.build_play_look_prompt(seed, script, report, tmp_path, 1)
    second = completion_pass.build_play_look_prompt(seed, script, report, tmp_path, 2)

    finish = completion_pass.PLAY_LOOK_FINISH.strip()
    assert finish.startswith("Then, at most three, what makes the screen look unfinished")
    assert 'the most noticeable first, each on a line starting with "- unfinished: "' in finish
    assert "a main view that fills only part of the window and leaves the rest empty" in finish
    assert finish in first
    assert "look unfinished" not in second
    assert first.endswith("Reply with the list only, or the single word NONE.")
    assert second.endswith("Reply with the list only, or the single word NONE.")
    assert completion_pass.build_play_look_prompt(seed, script, report, tmp_path) == first


def test_the_second_look_compares_with_the_first_play(tmp_path: Path) -> None:
    seed = Seed.from_dict(yaml.safe_load(_seed_content()))
    script = playtest.PlayScript(page="index.html", steps=({"shot": "start"},))
    report = playtest.PlayReport(shots=[tmp_path / ".ouroboros/playtest/round-2/01-start.png"])
    before = (".ouroboros/playtest/round-1/01-start.png",)

    second = completion_pass.build_play_look_prompt(seed, script, report, tmp_path, 2, before)
    alone = completion_pass.build_play_look_prompt(seed, script, report, tmp_path, 2)

    assert (
        "The same steps were played before the last change, with these screenshots, in the same"
        " order: .ouroboros/playtest/round-1/01-start.png. Open them too, and also list what looks"
        " worse now than in them: a control that responded and no longer does"
    ) in second
    assert "played before the last change" not in alone
    assert second.endswith("Reply with the list only, or the single word NONE.")


def test_the_plan_uses_every_control_and_the_fix_keeps_what_a_rewrite_had() -> None:
    seed = Seed.from_dict(yaml.safe_load(_seed_content()))
    plan = completion_pass.build_play_plan_prompt(seed)
    fix = completion_pass.build_fix_prompt(seed, ("the kart does not move",))

    assert (
        "When the code takes more than one control for the same action (arrow keys and letter"
        " keys, say), use each of them in some step: a later change can drop one"
    ) in plan
    assert (
        "Keep everything that already works: before you rewrite a whole file, note every"
        " control, screen and feature it has, and check that the new file still has each."
    ) in fix


def _rewrites(app: Path, **files: str | None) -> Callable[[], Awaitable[None]]:
    """What a worker session does while it runs: write (or, for ``None``, delete) files."""

    async def act() -> None:
        for name, text in files.items():
            path = app / name.replace("__", ".")
            if text is None:
                path.unlink()
            else:
                path.write_text(text)

    return act


FIXED = ["NONE", PLAN, "- app.js: the galaxy is not drawn", "done", "NONE", "NONE"]


@pytest.mark.asyncio
async def test_an_earlier_version_both_picks_prefer_is_put_back(tmp_path) -> None:
    app = _app(tmp_path)
    (app / "styles.css").write_text("body{}")
    (app / "run.sh").write_text("#!/bin/sh\n")
    (app / "run.sh").chmod(0o755)
    factory = _Factory([*FIXED, "2", "1"])
    factory.actions[3] = _rewrites(
        app, index__html="<p>flat</p>", styles__css=None, run__sh="echo", extra__js="x"
    )

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=_Player()
    )

    assert (app / "index.html").read_text() == "<canvas></canvas>"
    assert (app / "styles.css").read_text() == "body{}"
    assert (app / "run.sh").read_text() == "#!/bin/sh\n"
    assert (app / "run.sh").stat().st_mode & 0o777 == 0o755
    assert (app / "extra.js").read_text() == "x"  # a file the fix made stays: nothing is deleted
    first, second = factory.calls[6]["prompt"], factory.calls[7]["prompt"]
    assert "Version 1, its screenshots in order: .ouroboros/playtest/round-2/01-shot.png" in first
    assert "Version 1, its screenshots in order: .ouroboros/playtest/round-1/01-shot.png" in second
    record = result.meta["completion_pass"]
    assert (record["kept_round"], record["last_round"]) == (1, 2)
    assert (
        "The version played in round 1 looked better than the last one, so it was put back."
        in result.text_content
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("picks", [["2", "2"], ["1"], ["0"], ["2", "0"], [None, None]])
async def test_the_latest_version_stays_unless_both_picks_prefer_an_earlier_one(
    tmp_path, picks
) -> None:
    app = _app(tmp_path)
    factory = _Factory([*FIXED, *picks])
    factory.actions[3] = _rewrites(app, index__html="<p>new</p>")

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=_Player()
    )

    assert (app / "index.html").read_text() == "<p>new</p>"
    assert len(factory.created) == 6 + len(picks)
    assert result.meta["completion_pass"]["kept_round"] == 2
    assert "put back" not in result.text_content


@pytest.mark.asyncio
async def test_a_repair_that_looks_worse_gives_way_to_the_version_before_it(tmp_path) -> None:
    app = _app(tmp_path)
    factory = _Factory(
        [
            "NONE",
            PLAN,
            "- app.js: not drawn",
            "done",
            "NONE",
            "- app.js: dim",
            "done",
            "2",
            "1",
            "1",
        ]
    )
    factory.actions[3] = _rewrites(app, index__html="<p>fixed</p>")
    factory.actions[6] = _rewrites(app, index__html="<p>one white slab</p>")
    player = _Player()

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=player
    )

    assert (app / "index.html").read_text() == "<p>fixed</p>"
    assert len(player.calls) == 3
    # The first version is then compared with the fix, which a pick prefers.
    assert len(factory.created) == 10
    last = factory.calls[9]["prompt"]
    assert "Version 1, its screenshots in order: .ouroboros/playtest/round-2/01-shot.png" in last
    assert "Version 2, its screenshots in order: .ouroboros/playtest/round-1/01-shot.png" in last
    record = result.meta["completion_pass"]
    assert (record["replay_3"], record["kept_round"], record["last_round"]) == ("played", 2, 3)
    assert "The version played in round 2 looked better than the last one" in result.text_content


@pytest.mark.asyncio
async def test_a_first_build_that_beats_the_fix_kept_over_the_repair_is_put_back(
    tmp_path,
) -> None:
    app = _app(tmp_path)
    factory = _Factory(
        [
            "NONE",
            PLAN,
            "- app.js: not drawn",
            "done",
            "NONE",
            "- app.js: dim",
            "done",
            "2",
            "1",
            "2",
            "1",
        ]
    )
    factory.actions[3] = _rewrites(app, index__html="<p>a narrower track</p>", extra__js="x")
    factory.actions[6] = _rewrites(app, index__html="<p>narrower still</p>")

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=_Player()
    )

    assert (app / "index.html").read_text() == "<canvas></canvas>"
    assert (app / "extra.js").read_text() == "x"  # nothing is deleted
    assert len(factory.created) == 11
    first, second = factory.calls[9]["prompt"], factory.calls[10]["prompt"]
    assert "Version 1, its screenshots in order: .ouroboros/playtest/round-2/01-shot.png" in first
    assert "Version 1, its screenshots in order: .ouroboros/playtest/round-1/01-shot.png" in second
    record = result.meta["completion_pass"]
    assert (record["kept_round"], record["last_round"]) == (1, 3)
    assert "put_back" not in record
    assert (
        "The version played in round 1 looked better than the last one, so it was put back."
        in result.text_content
    )


@pytest.mark.asyncio
async def test_a_repair_that_cannot_be_played_again_is_left_as_it_is(tmp_path) -> None:
    app = _app(tmp_path)
    factory = _Factory(
        ["NONE", PLAN, "- app.js: not drawn", "done", "NONE", "- app.js: dim", "done"]
    )
    factory.actions[6] = _rewrites(app, index__html="<p>repaired</p>")

    class _FailsThird(_Player):
        def __call__(
            self,
            site: Path,
            script: Any,
            out_dir: Path,
            browser: str,
            stop: threading.Event | None = None,
        ) -> Any:
            if len(self.calls) == 2:
                self.calls.append((site, script, out_dir, browser))
                raise RuntimeError("browser closed the pipe")
            return super().__call__(site, script, out_dir, browser, stop)

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=_FailsThird()
    )

    assert (app / "index.html").read_text() == "<p>repaired</p>"
    assert len(factory.created) == 7
    record = result.meta["completion_pass"]
    assert record["replay_3"] == "play_failed"
    assert "kept_round" not in record
    assert "Playing it once more did not finish." in result.text_content


@pytest.mark.asyncio
async def test_a_version_too_large_to_keep_is_never_asked_about(tmp_path, monkeypatch) -> None:
    from ouroboros.orchestrator import app_versions

    monkeypatch.setattr(app_versions, "MAX_FILES", 0)
    app = _app(tmp_path)
    factory = _Factory(list(FIXED))
    factory.actions[3] = _rewrites(app, index__html="<p>new</p>")

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=_Player()
    )

    assert len(factory.created) == 6
    assert (app / "index.html").read_text() == "<p>new</p>"
    assert result.meta["completion_pass"]["kept_round"] == 2


@pytest.mark.asyncio
async def test_a_version_that_cannot_be_put_back_leaves_the_latest(tmp_path, monkeypatch) -> None:
    from ouroboros.orchestrator import app_versions

    def refuse(working_dir: Path, version: Any) -> int:
        raise OSError("the app's files cannot all be kept")

    monkeypatch.setattr(app_versions, "put_back", refuse)
    app = _app(tmp_path)
    factory = _Factory([*FIXED, "2", "1"])
    factory.actions[3] = _rewrites(app, index__html="<p>new</p>")

    result = await _complete(
        factory, working_dir=app, browser_finder=lambda: "/bin/chrome", player=_Player()
    )

    record = result.meta["completion_pass"]
    assert (record["put_back"], record["kept_round"]) == ("failed", 2)
    assert "An earlier version looked better, but it could not be put back." in (
        result.text_content
    )


def test_a_pick_compares_what_is_drawn_and_answers_with_one_number(tmp_path: Path) -> None:
    seed = Seed.from_dict(yaml.safe_load(_seed_content()))
    script = playtest.PlayScript(page="index.html", steps=({"shot": "start"},))
    first = (("r2/01-start.png",), playtest.PlayReport(notes=["the browser kept 9 of 106 rules"]))
    second = (("r1/01-start.png",), playtest.PlayReport())

    pick = completion_pass.build_pick_prompt(seed, script, first, second)

    assert GOAL in pick
    assert (
        "Version 1, its screenshots in order: r2/01-start.png\nIts page logged or met these"
        " problems:\n- the browser kept 9 of 106 rules\nVersion 2, its screenshots in order:"
        " r1/01-start.png\nIts page logged no errors."
    ) in pick
    assert "Do not modify any file." in pick
    drawn = "Compare what is drawn, not counts or claims the page writes about itself."
    assert drawn in pick
    assert drawn in completion_pass.PLAY_LOOK_BEFORE
    assert pick.endswith("Reply with 1 or 2 only, or 0 when neither is clearly better.")


@pytest.mark.parametrize(
    ("reply", "picked"),
    [
        ("1", 1),
        ("2", 2),
        ("0", 0),
        ("Version 2.", 2),
        ("2 - version 2 beats version 1", 2),
        ("", 0),
        (None, 0),
        ("neither", 0),
        ("12", 0),
    ],
)
def test_a_pick_reads_the_first_number_it_answers(reply: str | None, picked: int) -> None:
    assert completion_pass.parse_pick(reply) == picked
