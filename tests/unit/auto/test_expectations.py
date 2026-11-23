"""Expected-experience research for a new product (``auto/expectations.py``).

For a workspace with no code, auto asks one read-only agent session (with web
search where the backend has it) what a typical current product of the
requested kind includes, and the Seed carries the sanitized list to the worker
as the floor of a good result. These tests pin the prompt, the sanitizer, the
gating and timing, the event, the codex web-search flag, the Seed fields, the
worker rendering, and the pipeline hook. Every agent runtime is a fake.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import subprocess
import time
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from ouroboros.auto.expectations import (
    DEFAULT_TIMEOUT_SECONDS,
    EVENT_TYPE,
    MAX_LINE_CHARS,
    MAX_LINES,
    MAX_SOURCES,
    NO_EXPECTED_EXPERIENCE,
    TIMEOUT_ENV,
    WEB_TOOLS,
    AgentExpectationsResearcher,
    ExpectedExperience,
    build_expectations_researcher,
    build_prompt,
    resolve_timeout,
    sanitize_reply,
    start_expected_experience,
)
from ouroboros.auto.interview_driver import AutoInterviewResult
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.ledger_seed import with_ledger_carryover
from ouroboros.auto.pipeline import AutoPipeline
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps
from ouroboros.auto.state import AutoPhase, AutoPipelineState, AutoStore
from ouroboros.config.untrusted_env import is_untrusted_env_denied_key
from ouroboros.core.errors import ProviderError
from ouroboros.core.seed import Seed, SourcedScope
from ouroboros.core.seed_contract import GOAL_OBJECTIVE_CRITERION, SeedContract
from ouroboros.core.seed_contract_prompt import (
    EXPECTED_EXPERIENCE_HEADING,
    QUALITY_BAR_SECTION,
    render_seed_contract_for_execution,
)
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import ClaudeAgentAdapter, TaskResult
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.runner import build_system_prompt, build_task_prompt
from ouroboros.orchestrator.runtime_factory import create_agent_runtime

GOAL = "Build a kart racing game"
LINES = (
    "Races against AI-controlled karts on several tracks (core)",
    "Drift boosts for cornering (core)",
    "Items such as boosts and shells (common)",
    "Solo time trials with best-lap ghosts (common)",
)
SOURCE = "https://example.com/kart-games"
REPLY = "\n".join([*(f"- {line}" for line in LINES), f"Sources: {SOURCE}"])


# -- the prompt --------------------------------------------------------------------


def test_the_prompt_names_the_goal_the_list_the_local_limit_and_the_language() -> None:
    prompt = build_prompt(f"  {GOAL}\n")

    assert prompt.startswith(f"A user asked: {GOAL}\n")
    assert (
        "Describe what a typical, current product of this kind includes today: the "
        "capabilities a user would expect and how it looks and feels in use (presentation, "
        "motion, feedback), most expected first, 6–12 short lines starting "
        "with '- ', each marked (core) or (common)."
    ) in prompt
    assert (
        "When the request describes the product it wants (simple, minimal, for one person, "
        "and so on), describe a typical current product of that description, not the "
        "fullest of its kind: a simple one does its main task well and keeps its first "
        "screen about that task."
    ) in prompt
    assert (
        "Only include what the product itself can do in a single local install: nothing "
        "that needs a server, network service, account, payment, purchase or personal data."
    ) in prompt
    assert "After the list, one line 'Sources:' with URLs you actually read, if any." in prompt
    assert prompt.endswith("Write the answer in English.")
    assert build_prompt("카트 레이싱 게임을 만들어 주세요").endswith("Write the answer in Korean.")


# -- the sanitizer -----------------------------------------------------------------


def test_the_sanitizer_keeps_dash_lines_without_links_and_splits_the_sources() -> None:
    reply = "\n".join(
        [
            "Here is what such games include:",
            f"- {LINES[0]}",
            "- [Drift boosts](https://example.com/drift) for cornering (core)",
            "- Items such as boosts and shells (common) https://example.com/items",
            "  - Solo time trials with best-lap ghosts (common) (https://example.com/ghosts)",
            "* A star bullet is not a line",
            "Plain prose is not a line",
            "- ",
            "- https://example.com/only-a-url",
            "Sources: https://example.com/a, <https://example.com/b>. [ref](https://example.com/c)",
            "- https://example.com/d",
            "- [A source title](https://example.com/e)",
        ]
    )

    found = sanitize_reply(reply)

    assert found.lines == LINES
    assert found.sources == (
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
        "https://example.com/d",
        "https://example.com/e",
    )
    for line in found.lines:
        assert "http" not in line
        assert "](" not in line


def test_the_sanitizer_drops_site_name_citations_but_keeps_code_names() -> None:
    reply = "\n".join(
        [
            "- (core) Steer around the track. ([example.com](https://example.com/how-to-play/))",
            "- (core) Zoom with the wheel (docs.example.org)",
            "- (common) Change the star count (example.com, www.example.net/docs)",
            "- (common) Read the manual ([www.example.org](https://example.org/m.pdf)) first",
            "- (core) Built with three.js (three.js) and Node.js",
            "Sources: https://example.com/how-to-play/",
        ]
    )

    found = sanitize_reply(reply)

    assert found.lines == (
        "(core) Steer around the track.",
        "(core) Zoom with the wheel",
        "(common) Change the star count",
        "(common) Read the manual first",
        "(core) Built with three.js (three.js) and Node.js",
    )
    assert found.sources == ("https://example.com/how-to-play/",)


def test_the_sanitizer_caps_lines_characters_and_sources() -> None:
    lines = [f"- capability {index} " + "x" * 300 for index in range(20)]
    urls = " ".join(f"https://example.com/{index}" for index in range(10))

    found = sanitize_reply("\n".join([*lines, f"**Sources:** {urls}"]))

    assert len(found.lines) == MAX_LINES
    assert all(len(line) <= MAX_LINE_CHARS for line in found.lines)
    assert found.lines[0].startswith("capability 0 ")
    assert found.sources == tuple(f"https://example.com/{index}" for index in range(MAX_SOURCES))


def test_a_reply_without_a_list_finds_nothing() -> None:
    assert sanitize_reply("") == NO_EXPECTED_EXPERIENCE
    assert sanitize_reply("Racing games have karts.\nSources: https://example.com").lines == ()


# -- the timeout setting -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, DEFAULT_TIMEOUT_SECONDS), ("", DEFAULT_TIMEOUT_SECONDS), ("0", 0.0), ("12.5", 12.5)],
)
def test_the_timeout_comes_from_the_environment(raw: str | None, expected: float) -> None:
    environ = {} if raw is None else {TIMEOUT_ENV: raw}
    assert resolve_timeout(environ) == expected


@pytest.mark.parametrize("raw", ["soon", "-1", "inf", "nan"])
def test_an_invalid_timeout_is_refused(raw: str) -> None:
    with pytest.raises(ValueError, match=TIMEOUT_ENV):
        resolve_timeout({TIMEOUT_ENV: raw})


def test_the_timeout_env_is_denied_from_untrusted_project_env() -> None:
    assert is_untrusted_env_denied_key(TIMEOUT_ENV)
    assert is_untrusted_env_denied_key(TIMEOUT_ENV.lower())


# -- gating, timing and the event --------------------------------------------------


class _Researcher:
    """A fake researcher: a fixed reply after ``delay`` seconds, or an error."""

    def __init__(
        self,
        reply: str = REPLY,
        *,
        delay: float = 0.0,
        timeout_seconds: float = 30.0,
        error: Exception | None = None,
    ) -> None:
        self.reply = reply
        self.delay = delay
        self.timeout_seconds = timeout_seconds
        self.error = error
        self.goals: list[str] = []
        self.cancelled = False

    async def research(self, goal: str) -> str:
        self.goals.append(goal)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return self.reply


class _Events:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, event_type: str, aggregate_id: str, payload: dict[str, Any]) -> None:
        self.recorded.append((event_type, aggregate_id, payload))

    @property
    def outcomes(self) -> list[str]:
        return [payload["outcome"] for _type, _id, payload in self.recorded]


def _workspace(tmp_path: Path, *, code: bool = False) -> Path:
    root = tmp_path / ("repo" if code else "new")
    root.mkdir()
    if code:
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        (root / "main.py").write_text("print('existing')\n", encoding="utf-8")
    return root


def _state(cwd: Path, goal: str = GOAL) -> AutoPipelineState:
    return AutoPipelineState(goal=goal, cwd=str(cwd))


@pytest.mark.asyncio
async def test_a_new_project_gets_the_research_and_one_ok_event(tmp_path: Path) -> None:
    researcher = _Researcher()
    events = _Events()
    state = _state(_workspace(tmp_path))

    run = start_expected_experience(researcher, state, 600.0, events)
    found = await run.result()

    assert researcher.goals == [GOAL]
    assert found == ExpectedExperience(LINES, (SOURCE,))
    assert len(events.recorded) == 1
    event_type, aggregate_id, payload = events.recorded[0]
    assert (event_type, aggregate_id) == (EVENT_TYPE, state.auto_session_id)
    assert payload["outcome"] == "ok"
    assert payload["line_count"] == len(LINES)
    assert payload["elapsed_seconds"] >= 0
    assert payload["timeout_seconds"] == 30.0
    # A second read returns the same value without another event.
    assert await run.result() == found
    assert len(events.recorded) == 1


@pytest.mark.asyncio
async def test_existing_code_skips_the_research(tmp_path: Path) -> None:
    researcher = _Researcher()
    events = _Events()

    run = start_expected_experience(
        researcher, _state(_workspace(tmp_path, code=True)), 600.0, events
    )

    assert await run.result() == NO_EXPECTED_EXPERIENCE
    assert researcher.goals == []
    assert events.outcomes == ["skipped_brownfield"]


@pytest.mark.asyncio
async def test_a_zero_timeout_turns_the_research_off(tmp_path: Path) -> None:
    researcher = _Researcher(timeout_seconds=0)
    events = _Events()

    run = start_expected_experience(researcher, _state(_workspace(tmp_path)), 600.0, events)

    assert await run.result() == NO_EXPECTED_EXPERIENCE
    assert researcher.goals == []
    assert events.outcomes == ["disabled"]


@pytest.mark.asyncio
async def test_the_env_zero_builds_a_researcher_that_records_disabled(tmp_path: Path) -> None:
    researcher = build_expectations_researcher("codex", environ={TIMEOUT_ENV: "0"})
    assert researcher is not None
    events = _Events()

    run = start_expected_experience(researcher, _state(_workspace(tmp_path)), 600.0, events)

    assert await run.result() == NO_EXPECTED_EXPERIENCE
    assert events.outcomes == ["disabled"]


@pytest.mark.asyncio
async def test_a_slow_research_times_out_with_no_list_and_a_timeout_event(tmp_path: Path) -> None:
    researcher = _Researcher(delay=30.0, timeout_seconds=0.05)
    events = _Events()

    started = time.monotonic()
    run = start_expected_experience(researcher, _state(_workspace(tmp_path)), 600.0, events)
    found = await run.result()

    assert time.monotonic() - started < 5
    assert found == NO_EXPECTED_EXPERIENCE
    assert events.outcomes == ["timeout"]
    assert researcher.cancelled


@pytest.mark.asyncio
async def test_the_interview_budget_caps_the_research(tmp_path: Path) -> None:
    researcher = _Researcher(delay=30.0, timeout_seconds=180.0)
    events = _Events()

    run = start_expected_experience(researcher, _state(_workspace(tmp_path)), 0.05, events)

    assert await run.result() == NO_EXPECTED_EXPERIENCE
    assert events.outcomes == ["timeout"]
    assert events.recorded[0][2]["timeout_seconds"] == 0.05


@pytest.mark.asyncio
@pytest.mark.parametrize("researcher", [_Researcher(error=RuntimeError("boom")), _Researcher("")])
async def test_a_failed_or_empty_research_leaves_no_list(
    tmp_path: Path, researcher: _Researcher
) -> None:
    events = _Events()

    run = start_expected_experience(researcher, _state(_workspace(tmp_path)), 600.0, events)

    assert await run.result() == NO_EXPECTED_EXPERIENCE
    assert events.outcomes == ["failed"]


@pytest.mark.asyncio
async def test_no_researcher_records_nothing(tmp_path: Path) -> None:
    events = _Events()

    run = start_expected_experience(None, _state(_workspace(tmp_path)), 600.0, events)

    assert await run.result() == NO_EXPECTED_EXPERIENCE
    assert events.recorded == []


@pytest.mark.asyncio
async def test_cancel_stops_a_pending_research(tmp_path: Path) -> None:
    researcher = _Researcher(delay=30.0)
    run = start_expected_experience(researcher, _state(_workspace(tmp_path)), 600.0, _Events())
    await asyncio.sleep(0.01)

    run.cancel()
    await asyncio.sleep(0.01)

    assert researcher.cancelled
    assert await run.result() == NO_EXPECTED_EXPERIENCE


@pytest.mark.asyncio
async def test_a_failing_event_store_never_stops_the_run(tmp_path: Path) -> None:
    async def failing(*_args: Any) -> None:
        raise RuntimeError("store down")

    run = start_expected_experience(_Researcher(), _state(_workspace(tmp_path)), 600.0, failing)

    assert (await run.result()).lines == LINES


# -- the agent call ----------------------------------------------------------------


class _FakeRuntime:
    def __init__(self, cwd: str, *, error: str | None = None, blocker: str | None = None) -> None:
        self.cwd = Path(cwd)
        self.error = error
        self.blocker = blocker
        self.calls: list[tuple[str, Any, Any]] = []
        self.cwd_was_empty_dir: bool | None = None
        self.closed = False

    def preflight(self) -> str | None:
        return self.blocker

    async def execute_task_to_result(self, prompt: str, tools=None, system_prompt=None, **_kw):
        self.calls.append((prompt, tools, system_prompt))
        self.cwd_was_empty_dir = self.cwd.is_dir() and not any(self.cwd.iterdir())
        if self.error is not None:
            return Result.err(ProviderError(message=self.error, provider="fake"))
        return Result.ok(TaskResult(success=True, final_message=REPLY, messages=()))

    async def aclose(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self, **runtime_kwargs: Any) -> None:
        self.runtime_kwargs = runtime_kwargs
        self.kwargs: dict[str, Any] = {}
        self.runtime: _FakeRuntime | None = None

    def __call__(self, **kwargs: Any) -> _FakeRuntime:
        self.kwargs = kwargs
        self.runtime = _FakeRuntime(kwargs["cwd"], **self.runtime_kwargs)
        return self.runtime


@pytest.mark.asyncio
async def test_the_call_is_read_only_with_web_tools_in_a_removed_empty_directory() -> None:
    factory = _Factory()
    researcher = AgentExpectationsResearcher(runtime_backend="codex", runtime_factory=factory)

    reply = await researcher.research(GOAL)

    assert reply == REPLY
    assert factory.kwargs["backend"] == "codex"
    assert factory.kwargs["permission_mode"] == "default"
    assert factory.kwargs["model"] is None
    assert factory.kwargs["web_search"] is True
    runtime = factory.runtime
    assert runtime is not None
    assert runtime.calls == [(build_prompt(GOAL), list(WEB_TOOLS), None)]
    assert runtime.cwd_was_empty_dir is True
    assert not runtime.cwd.exists()
    assert runtime.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"error": "quota"}, {"blocker": "no CLI"}])
async def test_a_failed_call_raises_and_still_closes_the_runtime(kwargs: dict[str, str]) -> None:
    factory = _Factory(**kwargs)
    researcher = AgentExpectationsResearcher(runtime_backend="claude", runtime_factory=factory)

    with pytest.raises(RuntimeError):
        await researcher.research(GOAL)
    assert factory.runtime is not None and factory.runtime.closed
    assert not factory.runtime.cwd.exists()


def test_the_builder_needs_a_backend_that_runs_in_process() -> None:
    assert build_expectations_researcher(None) is None
    assert build_expectations_researcher("not-a-backend") is None
    assert build_expectations_researcher("opencode", "plugin") is None
    researcher = build_expectations_researcher("codex", environ={})
    assert isinstance(researcher, AgentExpectationsResearcher)
    assert researcher.runtime_backend == "codex"
    assert researcher.timeout_seconds == DEFAULT_TIMEOUT_SECONDS
    with pytest.raises(ValueError, match=TIMEOUT_ENV):
        build_expectations_researcher("codex", environ={TIMEOUT_ENV: "later"})


# -- codex web search --------------------------------------------------------------


@pytest.fixture
def fake_cli(tmp_path: Path) -> str:
    cli = tmp_path / "fake-codex"
    cli.write_text("#!/bin/sh\necho fake-codex 1.0\n", encoding="utf-8")
    cli.chmod(0o755)
    return str(cli)


def test_the_codex_command_gets_live_web_search_only_when_requested(
    tmp_path: Path, fake_cli: str
) -> None:
    out = str(tmp_path / "out.txt")
    plain = CodexCliRuntime(cli_path=fake_cli, cwd=str(tmp_path))._build_command(out)
    searching = CodexCliRuntime(cli_path=fake_cli, cwd=str(tmp_path), web_search=True)
    command = searching._build_command(out)

    assert 'web_search="live"' not in plain
    assert command[command.index('web_search="live"') - 1] == "-c"
    assert command.count('web_search="live"') == 1


def test_the_factory_passes_web_search_to_codex_only(tmp_path: Path, fake_cli: str) -> None:
    with patch(
        "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
        return_value=object(),
    ):
        searching = create_agent_runtime(
            backend="codex", cli_path=fake_cli, cwd=str(tmp_path), web_search=True
        )
        plain = create_agent_runtime(backend="codex", cli_path=fake_cli, cwd=str(tmp_path))
    claude = create_agent_runtime(backend="claude", cwd=str(tmp_path), web_search=True)

    assert isinstance(searching, CodexCliRuntime) and searching._web_search is True
    assert isinstance(plain, CodexCliRuntime) and plain._web_search is False
    assert isinstance(claude, ClaudeAgentAdapter)


# -- the Seed fields ---------------------------------------------------------------


def _seed(scope: SourcedScope | None) -> Seed:
    data: dict[str, Any] = {
        "goal": "Make a kart game with good racing",
        "acceptance_criteria": ["`npm test` exits 0."],
        "ontology_schema": {"name": "Kart", "description": "d"},
        "metadata": {"ambiguity_score": 0.1},
    }
    if scope is not None:
        data["sourced_scope"] = scope.model_dump(mode="json")
    return Seed.from_dict(data)


def test_the_new_fields_round_trip_through_the_seed_file() -> None:
    scope = SourcedScope(
        goal=GOAL,
        assumptions=("A guess.",),
        expected_experience=LINES,
        expected_experience_sources=(SOURCE,),
    )
    seed = _seed(scope)

    restored = Seed.from_dict(yaml.safe_load(yaml.safe_dump(seed.to_dict(), allow_unicode=True)))

    assert restored.sourced_scope == scope


def test_an_old_seed_without_the_fields_loads_and_serializes_as_before() -> None:
    old = {"goal": GOAL, "constraints": [], "acceptance_criteria": []}
    loaded = Seed.from_dict({**_seed(None).to_dict(), "sourced_scope": old})

    assert loaded.sourced_scope is not None
    assert loaded.sourced_scope.expected_experience == ()
    assert loaded.sourced_scope.expected_experience_sources == ()
    assert loaded.to_dict()["sourced_scope"] == old


# -- the worker's prompt -----------------------------------------------------------


def _prompt(seed: Seed) -> str:
    return render_seed_contract_for_execution(SeedContract.from_seed(seed))


def test_an_auto_seed_renders_the_quality_bar_after_its_assumptions() -> None:
    prompt = _prompt(_seed(SourcedScope(goal=GOAL, assumptions=("A guess.",))))

    assert QUALITY_BAR_SECTION in prompt
    assert QUALITY_BAR_SECTION == (
        "## Quality bar\n"
        "Anything this task creates is built at the level a user expects from a typical "
        "current product of that kind: the acceptance criteria are the floor, not the ceiling. "
        "Existing behavior and code that the goal does not ask to change stay as they are."
    )
    assert prompt.index("## Assumptions (chosen by auto") < prompt.index("## Quality bar")
    assert "## Expected experience" not in prompt


def test_an_auto_seed_renders_the_list_after_the_bar_and_never_its_sources() -> None:
    scope = SourcedScope(
        goal=GOAL, expected_experience=LINES, expected_experience_sources=(SOURCE,)
    )
    seed = _seed(scope)
    prompt = _prompt(seed)

    listed = EXPECTED_EXPERIENCE_HEADING + "\n" + "\n".join(f"- {line}" for line in LINES)
    assert listed in prompt
    assert prompt.index("## Quality bar") < prompt.index("## Expected experience")
    assert EXPECTED_EXPERIENCE_HEADING.startswith(
        "## Expected experience (typical current products of this kind)\n"
        "Research on current products of this kind found that users typically expect the "
        "capabilities below. Treat them as the floor of a good result, not a limit."
    )
    worker = "\n".join(
        (build_system_prompt(seed, resolved_context_pack_fragment=""), build_task_prompt(seed))
    )
    assert SOURCE not in worker
    assert "example.com" not in worker
    # Not acceptance criteria: nothing verifies, decomposes or prices the list.
    contract = SeedContract.from_seed(seed)
    assert contract.acceptance_criteria == (GOAL_OBJECTIVE_CRITERION,)
    assert not any(line in build_task_prompt(seed) for line in LINES)


def test_a_human_seed_renders_neither_section_and_ignores_the_list() -> None:
    contract = SeedContract.from_seed(_seed(None))
    prompt = render_seed_contract_for_execution(contract)

    assert "## Quality bar" not in prompt
    assert "## Expected experience" not in prompt
    assert contract.expected_experience == ()
    with_list = replace(contract, expected_experience=LINES)
    assert render_seed_contract_for_execution(with_list) == prompt


def test_a_recarried_seed_keeps_its_list() -> None:
    ledger = SeedDraftLedger.from_goal(GOAL)
    first = with_ledger_carryover(
        _seed(None), ledger, expected=ExpectedExperience(LINES, (SOURCE,))
    )
    again = with_ledger_carryover(first, ledger)

    assert first.sourced_scope is not None
    assert first.sourced_scope.expected_experience == LINES
    assert again.sourced_scope == first.sourced_scope


# -- the pipeline ------------------------------------------------------------------


class _RecordingEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent, **_: Any) -> None:
        self.appended.append(event)


class _Driver:
    """An interview that closes the ledger with safe defaults, or blocks, or hangs."""

    progress_callback = None

    def __init__(self, *, status: str = "seed_ready", hang: bool = False) -> None:
        self.event_store = _RecordingEventStore()
        self.status = status
        self.hang = hang

    async def run(self, state: AutoPipelineState, ledger: SeedDraftLedger) -> AutoInterviewResult:
        if state.phase == AutoPhase.CREATED:
            state.transition(AutoPhase.INTERVIEW, "starting interview")
        await asyncio.sleep(0.01)  # the research starts meanwhile
        finalize_safe_defaultable_gaps(ledger, goal=state.goal, provenance="test")
        if self.hang:
            await asyncio.sleep(3600)
        blocker = "the interview blocked" if self.status == "blocked" else None
        return AutoInterviewResult(self.status, None, ledger, 1, blocker=blocker)


async def _no_seed_generator(_session_id: str) -> Seed:  # pragma: no cover - not reached
    raise AssertionError("the ledger path writes the Seed")


def _pipeline(tmp_path: Path, driver: _Driver, researcher: _Researcher) -> AutoPipeline:
    return AutoPipeline(
        driver,  # type: ignore[arg-type]
        _no_seed_generator,
        store=AutoStore(tmp_path / "store"),
        skip_run=True,
        expectations_researcher=researcher,
    )


def _events(driver: _Driver) -> list[dict[str, Any]]:
    return [event.data for event in driver.event_store.appended if event.type == EVENT_TYPE]


@pytest.mark.asyncio
async def test_the_generated_seed_carries_the_research(tmp_path: Path) -> None:
    driver = _Driver()
    # Slower than the interview: the pipeline waits for it before the Seed.
    researcher = _Researcher(delay=0.2)
    state = _state(_workspace(tmp_path))

    await _pipeline(tmp_path, driver, researcher).run(state)

    assert state.phase == AutoPhase.COMPLETE
    scope = (state.seed_artifact or {})["sourced_scope"]
    assert tuple(scope["expected_experience"]) == LINES
    assert tuple(scope["expected_experience_sources"]) == (SOURCE,)
    assert [event["outcome"] for event in _events(driver)] == ["ok"]
    seed = Seed.from_dict(state.seed_artifact or {})
    assert "## Expected experience" in build_system_prompt(seed, resolved_context_pack_fragment="")


@pytest.mark.asyncio
async def test_the_research_is_saved_with_the_interview(tmp_path: Path) -> None:
    driver = _Driver()
    state = _state(_workspace(tmp_path))
    pipeline = _pipeline(tmp_path, driver, _Researcher())

    await pipeline.run(state)

    saved = AutoStore(tmp_path / "store").load(state.auto_session_id)
    assert saved.expected_experience == list(LINES)
    assert saved.expected_experience_sources == [SOURCE]


@pytest.mark.asyncio
async def test_a_session_resumed_after_its_interview_writes_the_saved_research(
    tmp_path: Path,
) -> None:
    # The process stopped after the interview and before the Seed: the resumed
    # run does not research again and still writes the saved list.
    state = _state(_workspace(tmp_path))
    ledger = SeedDraftLedger.from_goal(GOAL)
    finalize_safe_defaultable_gaps(ledger, goal=GOAL, provenance="test")
    state.transition(AutoPhase.INTERVIEW, "starting interview")
    state.ledger = ledger.to_dict()
    state.interview_completed = True
    state.interview_closure_mode = "safe_default_no_backend"
    state.expected_experience = list(LINES)
    state.expected_experience_sources = [SOURCE]
    store = AutoStore(tmp_path / "store")
    store.save(state)
    researcher = _Researcher()

    resumed = store.load(state.auto_session_id)
    await _pipeline(tmp_path, _Driver(), researcher).run(resumed)

    assert researcher.goals == []
    assert resumed.phase == AutoPhase.COMPLETE
    scope = (resumed.seed_artifact or {})["sourced_scope"]
    assert tuple(scope["expected_experience"]) == LINES
    assert tuple(scope["expected_experience_sources"]) == (SOURCE,)


def test_an_old_state_without_the_research_loads_and_a_bad_one_is_refused() -> None:
    data = AutoPipelineState(goal=GOAL, cwd="/tmp").to_dict()
    for name in ("expected_experience", "expected_experience_sources"):
        del data[name]

    loaded = AutoPipelineState.from_dict(data)

    assert loaded.expected_experience == []
    assert loaded.expected_experience_sources == []
    with pytest.raises(ValueError, match="expected_experience must be a list of strings"):
        AutoPipelineState.from_dict({**data, "expected_experience": [1]})


@pytest.mark.asyncio
async def test_a_slow_research_gives_a_seed_without_the_list(tmp_path: Path) -> None:
    driver = _Driver()
    researcher = _Researcher(delay=30.0, timeout_seconds=0.05)
    state = _state(_workspace(tmp_path))

    await _pipeline(tmp_path, driver, researcher).run(state)

    assert state.phase == AutoPhase.COMPLETE
    scope = (state.seed_artifact or {})["sourced_scope"]
    assert "expected_experience" not in scope
    assert [event["outcome"] for event in _events(driver)] == ["timeout"]


@pytest.mark.asyncio
async def test_existing_code_gives_a_seed_without_the_list(tmp_path: Path) -> None:
    driver = _Driver()
    researcher = _Researcher()
    state = _state(_workspace(tmp_path, code=True))

    await _pipeline(tmp_path, driver, researcher).run(state)

    assert researcher.goals == []
    assert "expected_experience" not in (state.seed_artifact or {})["sourced_scope"]
    assert [event["outcome"] for event in _events(driver)] == ["skipped_brownfield"]


@pytest.mark.asyncio
async def test_a_blocked_interview_stops_the_research(tmp_path: Path) -> None:
    driver = _Driver(status="blocked")
    researcher = _Researcher(delay=30.0)
    state = _state(_workspace(tmp_path))

    await _pipeline(tmp_path, driver, researcher).run(state)
    await asyncio.sleep(0.01)

    assert researcher.cancelled
    assert _events(driver) == []
    assert not state.seed_artifact


@pytest.mark.asyncio
async def test_an_interview_deadline_seed_carries_the_research(tmp_path: Path) -> None:
    driver = _Driver(hang=True)
    researcher = _Researcher()
    state = _state(_workspace(tmp_path))
    state.timeout_seconds_by_phase[AutoPhase.INTERVIEW.value] = 1
    state.deadline_at = time.monotonic() + 3600
    state.deadline_at_epoch = time.time() + 3600
    state.transition(AutoPhase.INTERVIEW, "starting interview")

    await _pipeline(tmp_path, driver, researcher).run(state)

    assert state.seed_artifact is not None
    assert state.seed_artifact["metadata"]["degraded"] is True
    assert tuple(state.seed_artifact["sourced_scope"]["expected_experience"]) == LINES
    assert [event["outcome"] for event in _events(driver)] == ["ok"]


def test_an_empty_git_checkout_is_a_new_project_and_gets_the_research(tmp_path: Path) -> None:
    """``git init`` alone is not existing code (#3074): the research gate opens."""
    from ouroboros.auto.ledger_seed import workspace_has_existing_code

    root = tmp_path / "fresh"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / ".gitignore").write_text("node_modules/\n", encoding="utf-8")

    assert workspace_has_existing_code(root) is False
