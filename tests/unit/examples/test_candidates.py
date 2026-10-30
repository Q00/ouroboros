"""The candidate step with fake runtimes on a real git checkout and real probe runs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

import pytest

from ouroboros.config.models import ExampleCandidateConfig, ExamplesConfig, OuroborosConfig
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.examples import candidates as cand
from ouroboros.examples.candidates import (
    MAX_PROBES_PER_CANDIDATE,
    PROBE_DIR,
    CandidatePair,
    SkipReason,
    plan_candidates,
    reported_cost,
    resolve_pairs,
    run_candidate_step,
    take_probes,
)
from ouroboros.examples.probe_runner import Probe, ProbeKind
from ouroboros.examples.readings import Reading, reading_id_for
from ouroboros.examples.record import sha256_hex
from ouroboros.orchestrator.adapter import AgentMessage, TaskResult
from ouroboros.providers.base import CompletionConfig, CompletionResponse, UsageInfo

GOAL = "parse_duration should accept compound values like 1h30 and return seconds."
READINGS_REPLY = json.dumps(
    {
        "readings": [
            {"span": "compound values like 1h30", "reading": "1h30 is one hour thirty minutes"},
            {"span": "return seconds", "reading": "return whole minutes instead"},
        ]
    }
)
CALL_PROBE = 'from dur import parse\n\ndef probe():\n    return parse("1h30")\n'


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)
    return done.stdout


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    (repo / "dur.py").write_text("def parse(text):\n    return None\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _worktrees(repo: Path) -> list[str]:
    return [
        line
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


class _Llm:
    def __init__(self, content: str = READINGS_REPLY) -> None:
        self.content = content
        self.calls = 0

    async def complete(self, messages: Any, config: Any) -> Any:
        self.calls += 1
        return Result.ok(CompletionResponse(self.content, "m", UsageInfo(1, 1, 2)))


Behavior = Callable[[Path, str], Any]


def writes(
    value: str, *, probes: dict[str, str] | None = None, cost: float | None = 0.25
) -> Behavior:
    """A candidate that makes ``parse`` return ``value`` and writes ``probes``."""

    async def act(cwd: Path, prompt: str) -> Any:
        (cwd / "dur.py").write_text(f"def parse(text):\n    return {value}\n")
        probe_dir = cwd / PROBE_DIR
        probe_dir.mkdir(exist_ok=True)
        for name, text in (probes if probes is not None else {"p1.py": CALL_PROBE}).items():
            (probe_dir / name).write_text(text)
        data = {"subtype": "success"} if cost is None else {"total_cost_usd": cost}
        message = AgentMessage(type="result", content="done", data=data)
        return Result.ok(TaskResult(True, "done", (message,)))

    return act


def sleeps() -> Behavior:
    async def act(cwd: Path, prompt: str) -> Any:
        await asyncio.sleep(3600)

    return act


def fails() -> Behavior:
    async def act(cwd: Path, prompt: str) -> Any:
        return Result.err(ProviderError("runtime down"))

    return act


class _Runtime:
    def __init__(self, backend: str, cwd: Path, behavior: Behavior, blocker: str | None) -> None:
        self.backend, self.cwd, self.behavior, self.blocker = backend, cwd, behavior, blocker
        self.prompts: list[str] = []

    def preflight(self) -> str | None:
        return self.blocker

    async def execute_task_to_result(self, prompt: str, tools: Any = None, **_: Any) -> Any:
        self.prompts.append(prompt)
        return await self.behavior(self.cwd, prompt)


class _Factory:
    """Builds fake runtimes: ``behaviors`` per backend; ``blocked``/``broken`` backends."""

    def __init__(
        self,
        behaviors: dict[str, Behavior],
        *,
        blocked: frozenset[str] = frozenset(),
        broken: frozenset[str] = frozenset(),
        slow_build: float = 0.0,
    ) -> None:
        self.behaviors, self.blocked, self.broken = behaviors, blocked, broken
        self.slow_build = slow_build
        self.built: list[_Runtime] = []

    def __call__(self, *, backend: str, model: str | None, cwd: Path) -> _Runtime:
        if self.slow_build and Path(cwd).name != "base":
            time.sleep(self.slow_build)  # a slow candidate start; preflight stays fast
        if backend in self.broken:
            raise RuntimeError("not installed")
        runtime = _Runtime(
            backend,
            Path(cwd),
            self.behaviors[backend],
            "missing CLI" if backend in self.blocked else None,
        )
        self.built.append(runtime)
        return runtime

    def dispatched(self) -> list[_Runtime]:
        return [runtime for runtime in self.built if runtime.prompts]


def config(**overrides: Any) -> ExamplesConfig:
    data: dict[str, Any] = {
        "enabled": True,
        "k": 2,
        "candidates": [{"backend": "codex"}, {"backend": "claude"}],
    }
    data.update(overrides)
    return ExamplesConfig.model_validate(data)


async def run(
    workspace: Path,
    factory: _Factory,
    *,
    cfg: ExamplesConfig | None = None,
    llm: _Llm | None = None,
    **kwargs: Any,
) -> cand.CandidateStepResult:
    return await run_candidate_step(
        cfg or config(),
        workspace=workspace,
        goal=GOAL,
        repo_facts=["dur.py defines parse"],
        session_id="sess-1",
        llm=llm or _Llm(),
        readings_config=CompletionConfig(model="m"),
        execute_backend="codex",
        runtime_factory=factory,
        **kwargs,
    )


def types(result: cand.CandidateStepResult) -> list[str]:
    return [event.type for event in result.events]


# --- flag and config -------------------------------------------------------


def test_the_flag_is_off_by_default() -> None:
    assert ExamplesConfig().enabled is False
    assert OuroborosConfig().examples.enabled is False
    assert ExamplesConfig().k == 4 and ExamplesConfig().parallel == 4


@pytest.mark.parametrize("k", [1, 9])
def test_k_is_bounded(k: int) -> None:
    with pytest.raises(ValueError):
        ExamplesConfig(k=k)


async def test_flag_off_calls_nothing(workspace: Path) -> None:
    llm = _Llm()
    factory = _Factory({})
    result = await run(workspace, factory, cfg=ExamplesConfig(), llm=llm)
    assert result.enabled is False and result.events == ()
    assert llm.calls == 0 and factory.built == []
    assert len(_worktrees(workspace)) == 1


# --- planning --------------------------------------------------------------


def _readings(n: int) -> list[Reading]:
    return [Reading(reading_id_for(f"r{i}"), "span", f"r{i}") for i in range(1, n + 1)]


A, B = CandidatePair("codex"), CandidatePair("claude")


def test_two_pairs_four_readings_alternate_pairs_with_fresh_readings() -> None:
    r = _readings(4)
    slots = plan_candidates([A, B], r, 4)
    assert [(s.candidate_id, s.pair, s.reading) for s in slots] == [
        ("cand-1", A, r[0]),
        ("cand-2", B, r[1]),
        ("cand-3", A, r[2]),
        ("cand-4", B, r[3]),
    ]


def test_with_fewer_readings_a_reading_is_reused_on_a_different_pair() -> None:
    r = _readings(2)
    slots = plan_candidates([A, B], r, 4)
    assert [(s.pair, s.reading) for s in slots] == [(A, r[0]), (B, r[1]), (A, r[1]), (B, r[0])]


@pytest.mark.parametrize("k", range(1, 9))
@pytest.mark.parametrize("n", range(0, 9))
def test_no_reading_repeats_while_an_unused_one_remains(k: int, n: int) -> None:
    r = _readings(n)
    slots = plan_candidates([A, B], r, k)
    assert len(slots) == k
    used = [s.reading for s in slots]
    for index, reading in enumerate(used):
        if n == 0:
            assert reading is None
        elif index < n:
            assert reading == r[index]
    assert plan_candidates([A, B], r, k) == slots


def test_no_usable_pair_plans_nothing() -> None:
    assert plan_candidates([], _readings(2), 4) == ()


def test_default_pairs_are_the_execute_pair_then_the_allowlist() -> None:
    off = ExamplesConfig()
    assert resolve_pairs(off, execute_backend="codex", execute_model="m1") == (
        CandidatePair("codex", "m1"),
        CandidatePair("codex"),
        CandidatePair("claude"),
    )
    assert resolve_pairs(off, execute_backend="codex", execute_model=None) == (
        CandidatePair("codex"),
        CandidatePair("claude"),
    )
    assert resolve_pairs(off, execute_backend="opencode", execute_model=None) == (
        CandidatePair("opencode"),
        CandidatePair("codex"),
        CandidatePair("claude"),
    )
    configured = ExamplesConfig(
        candidates=(
            ExampleCandidateConfig(backend="claude", model="x"),
            ExampleCandidateConfig(backend="claude", model="x"),
        )
    )
    assert resolve_pairs(configured, execute_backend="codex", execute_model=None) == (
        CandidatePair("claude", "x"),
    )


def test_reported_cost_reads_the_last_finite_cost() -> None:
    messages = (
        AgentMessage("assistant", "a", data={"total_cost_usd": 1.0}),
        AgentMessage("result", "r", data={"total_cost_usd": 2.5}),
    )
    assert reported_cost(TaskResult(True, "r", messages)) == 2.5
    for bad in (True, float("nan"), "1.0", None):
        result = TaskResult(True, "r", (AgentMessage("result", "r", data={"total_cost_usd": bad}),))
        assert reported_cost(result) is None


# --- probes a candidate wrote ---------------------------------------------


def test_take_probes_reads_in_name_order_caps_and_removes_the_directory(tmp_path: Path) -> None:
    probe_dir = tmp_path / PROBE_DIR
    probe_dir.mkdir()
    for index in range(MAX_PROBES_PER_CANDIDATE + 2):
        (probe_dir / f"p{index}.py").write_text(f"def probe():\n    return {index}\n")
    probes, refused = take_probes(tmp_path)
    assert [p.source for p in probes] == [
        f"def probe():\n    return {index}\n" for index in range(MAX_PROBES_PER_CANDIDATE)
    ]
    assert refused == 2
    assert not probe_dir.exists()


def test_take_probes_reads_commands_and_refuses_malformed_entries(tmp_path: Path) -> None:
    probe_dir = tmp_path / PROBE_DIR
    probe_dir.mkdir()
    (probe_dir / "a.json").write_text(json.dumps({"argv": ["python3", "-V"], "stdin": "x"}))
    (probe_dir / "b.json").write_text(json.dumps({"argv": ["ok"], "expected": "no"}))
    (probe_dir / "c.json").write_text(json.dumps({"argv": []}))
    (probe_dir / "d.json").write_text("not json")
    (probe_dir / "e.py").write_text("")
    (probe_dir / "f.txt").write_text("def probe(): pass")
    (probe_dir / "g.py").write_bytes(b"\xff\xfe")
    (probe_dir / "h.py").write_text("x" * (cand.MAX_PROBE_FILE_BYTES + 1))
    (probe_dir / "sub").mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def probe():\n    return 1\n")
    os.symlink(outside, probe_dir / "i.py")
    probes, refused = take_probes(tmp_path)
    assert probes == (Probe(ProbeKind.COMMAND, argv=("python3", "-V"), stdin="x"),)
    assert refused == 9
    assert not probe_dir.exists() and outside.exists()


def test_take_probes_skips_and_restores_the_commits_own_directory(tmp_path: Path) -> None:
    base, tree = tmp_path / "base", tmp_path / "tree"
    for root in (base, tree):
        (root / PROBE_DIR).mkdir(parents=True)
        (root / PROBE_DIR / "keep.py").write_text("def probe():\n    return 'tracked'\n")
        (root / PROBE_DIR / "edited.py").write_text("def probe():\n    return 0\n")
    (tree / PROBE_DIR / "edited.py").write_text("def probe():\n    return 1\n")
    (tree / PROBE_DIR / "new.py").write_text("def probe():\n    return 2\n")
    probes, refused = take_probes(tree, base)
    assert [p.source for p in probes] == [
        "def probe():\n    return 1\n",
        "def probe():\n    return 2\n",
    ]
    assert refused == 0
    assert sorted(os.listdir(tree / PROBE_DIR)) == ["edited.py", "keep.py"]
    for name in ("edited.py", "keep.py"):
        assert (tree / PROBE_DIR / name).read_bytes() == (base / PROBE_DIR / name).read_bytes()


def test_take_probes_without_a_directory_is_empty(tmp_path: Path) -> None:
    assert take_probes(tmp_path) == ((), 0)
    (tmp_path / PROBE_DIR).write_text("a file, not a directory")
    assert take_probes(tmp_path) == ((), 1)
    assert not (tmp_path / PROBE_DIR).exists()


# --- the step end to end ---------------------------------------------------


async def test_two_disagreeing_candidates_give_a_card_and_clean_up(workspace: Path) -> None:
    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory)
    assert result.failure is None
    assert [o.skipped for o in result.outcomes] == [None, None]
    assert [o.slot.candidate_id for o in result.outcomes] == ["cand-1", "cand-2"]
    assert [o.slot.pair.backend for o in result.outcomes] == ["codex", "claude"]
    assert all(o.cost_usd == 0.25 and o.wall_clock_seconds is not None for o in result.outcomes)
    assert result.report is not None and len(result.report.disagreements) == 1
    assert len(result.asked) == 1 and result.recorded == ()
    card = result.asked[0]
    values = sorted(option.outcomes[0].outcome["value"] for option in card.options)
    assert values == [90, 5400]
    # Each candidate got its own reading, in its own worktree, never the main one.
    prompts = {runtime.backend: runtime.prompts[0] for runtime in factory.dispatched()}
    assert "1h30 is one hour thirty minutes" in prompts["codex"]
    assert "return whole minutes instead" in prompts["claude"]
    assert all(runtime.cwd != workspace for runtime in factory.dispatched())
    # Worktrees are gone and the main checkout is untouched.
    assert len(_worktrees(workspace)) == 1
    assert not any(runtime.cwd.exists() for runtime in factory.built)
    assert (workspace / "dur.py").read_text() == "def parse(text):\n    return None\n"
    assert types(result)[0] == "examples.readings.stated"
    assert sorted(types(result)[1:]) == [
        "examples.candidate.completed",
        "examples.candidate.completed",
        "examples.candidate.started",
        "examples.candidate.started",
    ]


async def test_events_carry_digests_never_patch_prompt_or_probe_text(workspace: Path) -> None:
    sunk: list[Any] = []

    async def sink(event: Any) -> None:
        sunk.append(event)

    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, event_sink=sink)
    assert [e.id for e in sunk] == [e.id for e in result.events]
    blob = json.dumps([e.data for e in result.events])
    assert "def parse" not in blob and "def probe" not in blob and GOAL not in blob
    assert "return 5400" not in blob
    completed = [e for e in result.events if e.type == "examples.candidate.completed"]
    assert {e.data["patch_digest"] for e in completed} == {o.patch_digest for o in result.outcomes}
    assert all(
        e.aggregate_type == "auto_session" and e.aggregate_id == "sess-1" for e in result.events
    )
    stated = result.events[0]
    assert [r["text"] for r in stated.data["readings"]] == [
        "1h30 is one hour thirty minutes",
        "return whole minutes instead",
    ]


async def test_a_failing_event_sink_does_not_end_the_step(workspace: Path) -> None:
    async def sink(event: Any) -> None:
        raise RuntimeError("store down")

    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, event_sink=sink)
    assert len(result.asked) == 1


async def test_the_patch_digest_excludes_the_probe_directory(workspace: Path) -> None:
    def probes_only() -> Behavior:
        async def act(cwd: Path, prompt: str) -> Any:
            (cwd / PROBE_DIR).mkdir()
            (cwd / PROBE_DIR / "p.py").write_text(CALL_PROBE)
            return Result.ok(TaskResult(True, "done", ()))

        return act

    factory = _Factory({"codex": probes_only(), "claude": writes("90")})
    result = await run(workspace, factory)
    first = result.outcomes[0]
    assert first.patch_digest == sha256_hex(b"") and first.cost_usd is None
    assert len(first.probes) == 1


async def test_a_tracked_probe_directory_stays_out_of_the_digest(workspace: Path) -> None:
    (workspace / PROBE_DIR).mkdir()
    (workspace / PROBE_DIR / "keep.py").write_text("def probe():\n    return 'tracked'\n")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-qm", "tracked probe dir")

    async def probes_only(cwd: Path, prompt: str) -> Any:
        (cwd / PROBE_DIR / "p.py").write_text(CALL_PROBE)
        return Result.ok(TaskResult(True, "done", ()))

    factory = _Factory({"codex": probes_only, "claude": writes("90")})
    result = await run(workspace, factory)
    first = result.outcomes[0]
    assert first.patch_digest == sha256_hex(b"")
    assert [p.source for p in first.probes] == [CALL_PROBE] and first.probes_refused == 0


async def test_a_slow_start_never_runs_past_the_step_deadline(workspace: Path) -> None:
    factory = _Factory({"codex": writes("5400"), "claude": writes("90")}, slow_build=1.0)
    result = await run(workspace, factory, step_timeout=0.5)
    assert [o.skipped for o in result.outcomes] == [SkipReason.STEP_DEADLINE] * 2
    assert factory.dispatched() == []
    assert "examples.candidate.started" not in types(result)
    assert len(_worktrees(workspace)) == 1


async def test_a_slow_base_commit_lookup_fails_the_step_at_its_deadline(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cand.RunWorktreeManager.head_commit

    def slow(self: Any) -> str:
        time.sleep(1.0)
        return original(self)

    monkeypatch.setattr(cand.RunWorktreeManager, "head_commit", slow)
    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, step_timeout=0.3)
    assert result.failure == "step_deadline"
    assert result.outcomes == () and factory.built == []
    assert len(_worktrees(workspace)) == 1


async def test_a_slow_worktree_creation_skips_its_slot_and_is_still_removed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = cand.RunWorktreeManager.create

    def slow(self: Any, label: str, *, ref: str = "HEAD") -> Path:
        if label != "base":
            time.sleep(1.0)
        return original(self, label, ref=ref)

    monkeypatch.setattr(cand.RunWorktreeManager, "create", slow)
    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, step_timeout=0.5)
    assert [o.skipped for o in result.outcomes] == [SkipReason.STEP_DEADLINE] * 2
    assert factory.dispatched() == []
    # The cleanup waited for the creation it abandoned, so nothing is left.
    assert len(_worktrees(workspace)) == 1


async def test_a_preflight_that_raises_skips_only_its_pair(workspace: Path) -> None:
    class _Raising(_Factory):
        def __call__(self, *, backend: str, model: str | None, cwd: Path) -> _Runtime:
            runtime = super().__call__(backend=backend, model=model, cwd=cwd)
            if backend == "claude":

                def boom() -> str | None:
                    raise RuntimeError("preflight crashed")

                runtime.preflight = boom  # type: ignore[method-assign]
            return runtime

    calls = iter(["5400", "90"])

    async def act(cwd: Path, prompt: str) -> Any:
        return await writes(next(calls))(cwd, prompt)

    factory = _Raising({"codex": act, "claude": writes("1")})
    result = await run(workspace, factory)
    assert [(s.pair.backend, s.reason, s.detail) for s in result.skipped_pairs] == [
        ("claude", SkipReason.PREFLIGHT_FAILED, "RuntimeError")
    ]
    assert [o.slot.pair.backend for o in result.outcomes] == ["codex", "codex"]
    assert len(result.asked) == 1


async def test_no_worktree_root_is_made_once_the_deadline_passed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[Path] = []
    original = cand.RunWorktreeManager.__init__

    def tracked(self: Any, root: Any) -> None:
        original(self, root)
        made.append(self._root)

    monkeypatch.setattr(cand.RunWorktreeManager, "__init__", tracked)

    class _SlowLlm(_Llm):
        async def complete(self, messages: Any, config: Any) -> Any:
            await asyncio.sleep(1.0)
            return await super().complete(messages, config)

    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, llm=_SlowLlm(), step_timeout=0.3)
    assert result.failure == "step_deadline" and made == []
    assert result.readings is not None and result.readings.call_failed == "call_timed_out"


async def test_cancelling_right_after_the_readings_leaves_no_worktree_root(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[Path] = []
    original = cand.RunWorktreeManager.__init__

    def tracked(self: Any, root: Any) -> None:
        original(self, root)
        made.append(self._root)

    monkeypatch.setattr(cand.RunWorktreeManager, "__init__", tracked)
    factory = _Factory({"codex": sleeps(), "claude": sleeps()})
    task = asyncio.create_task(run(workspace, factory))
    for _ in range(200):
        if made:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(200):
        if not any(root.exists() for root in made):
            break
        await asyncio.sleep(0.05)
    assert made and not any(root.exists() for root in made)


async def test_a_slow_event_sink_never_dispatches_past_the_step_deadline(
    workspace: Path,
) -> None:
    async def sink(event: Any) -> None:
        if event.type == "examples.candidate.started":
            await asyncio.sleep(1.0)

    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, event_sink=sink, step_timeout=0.5)
    assert all(o.skipped is SkipReason.STEP_DEADLINE for o in result.outcomes)
    assert factory.dispatched() == []
    assert len(_worktrees(workspace)) == 1


async def test_a_probe_pool_past_the_deadline_builds_no_card(workspace: Path) -> None:
    async def stuck_pool(probes: Any, trees: Any, *, deadline: float) -> Any:
        await asyncio.sleep(3600)

    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, probe_pool_runner=stuck_pool, step_timeout=2.0)
    assert result.failure == "probe_pool_step_deadline"
    assert len(result.finished) == 2
    assert result.pool is None and result.asked == ()
    assert len(_worktrees(workspace)) == 1


async def test_agreeing_candidates_give_no_card(workspace: Path) -> None:
    factory = _Factory({"codex": writes("5400"), "claude": writes("5400")})
    result = await run(workspace, factory)
    assert result.report is not None and result.report.disagreements == ()
    assert len(result.report.consensus_changes) == 1
    assert result.asked == () and result.recorded == ()


async def test_the_card_budget_splits_asked_and_recorded(workspace: Path) -> None:
    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, cfg=config(max_cards=0))
    assert result.asked == () and len(result.recorded) == 1


# --- caps, missing pairs, fewer than two finished --------------------------


async def test_a_candidate_at_its_cap_is_skipped_and_no_card_is_built(workspace: Path) -> None:
    factory = _Factory({"codex": sleeps(), "claude": writes("90")})
    result = await run(workspace, factory, candidate_timeout=0.5)
    assert [o.skipped for o in result.outcomes] == [SkipReason.TIMED_OUT, None]
    assert result.pool is None and result.report is None and result.asked == ()
    skipped = [e for e in result.events if e.type == "examples.candidate.skipped"]
    assert [e.data["reason"] for e in skipped] == ["timed_out"]
    assert skipped[0].data["wall_clock_seconds"] is not None
    assert len(_worktrees(workspace)) == 1


async def test_the_step_cap_stops_waiting_candidates(workspace: Path) -> None:
    factory = _Factory({"codex": sleeps(), "claude": writes("90")})
    result = await run(workspace, factory, cfg=config(parallel=1), step_timeout=0.5)
    assert [o.skipped for o in result.outcomes] == [
        SkipReason.STEP_DEADLINE,
        SkipReason.STEP_DEADLINE,
    ]
    assert result.asked == ()
    assert len(_worktrees(workspace)) == 1


async def test_a_missing_pair_is_skipped_and_recorded(workspace: Path) -> None:
    for kwargs in ({"blocked": frozenset({"claude"})}, {"broken": frozenset({"claude"})}):
        calls = iter(["5400", "90"])

        async def act(cwd: Path, prompt: str) -> Any:
            return await writes(next(calls))(cwd, prompt)

        factory = _Factory({"codex": act, "claude": writes("1")}, **kwargs)
        result = await run(workspace, factory)
        assert [s.pair.backend for s in result.skipped_pairs] == ["claude"]
        assert [o.slot.pair.backend for o in result.outcomes] == ["codex", "codex"]
        assert [o.slot.reading.text for o in result.outcomes if o.slot.reading] == [
            "1h30 is one hour thirty minutes",
            "return whole minutes instead",
        ]
        pair_skip = [e for e in result.events if e.type == "examples.candidate.skipped"]
        assert len(pair_skip) == 1 and pair_skip[0].data["candidate_id"] is None
        assert pair_skip[0].data["detail"] in {"missing CLI", "RuntimeError"}
        assert len(result.asked) == 1


async def test_a_failed_runtime_leaves_fewer_than_two_and_no_card(workspace: Path) -> None:
    factory = _Factory({"codex": fails(), "claude": writes("90")})
    result = await run(workspace, factory)
    assert [o.skipped for o in result.outcomes] == [SkipReason.RUNTIME_FAILED, None]
    skipped = [e for e in result.events if e.type == "examples.candidate.skipped"]
    assert [e.data["detail"] for e in skipped] == ["ProviderError"]
    assert len(result.finished) == 1
    assert result.pool is None and result.asked == ()


async def test_no_usable_pair_runs_no_candidate(workspace: Path) -> None:
    factory = _Factory({}, broken=frozenset({"codex", "claude"}))
    result = await run(workspace, factory)
    assert result.outcomes == () and result.asked == ()
    assert len(result.skipped_pairs) == 2
    assert len(_worktrees(workspace)) == 1


async def test_candidates_without_probes_build_no_card(workspace: Path) -> None:
    factory = _Factory({"codex": writes("5400", probes={}), "claude": writes("90", probes={})})
    result = await run(workspace, factory)
    assert len(result.finished) == 2 and result.pool is None and result.asked == ()


async def test_without_readings_candidates_get_the_goal_alone(workspace: Path) -> None:
    factory = _Factory({"codex": writes("5400"), "claude": writes("90")})
    result = await run(workspace, factory, llm=_Llm("not json"))
    assert result.readings is not None and result.readings.call_failed == "reply_not_json"
    assert all(o.slot.reading is None for o in result.outcomes)
    assert "No reading was stated" in factory.dispatched()[0].prompts[0]
    assert len(result.asked) == 1


async def test_a_workspace_that_is_not_a_git_checkout_fails_the_step(tmp_path: Path) -> None:
    factory = _Factory({"codex": writes("1"), "claude": writes("2")})
    result = await run(tmp_path, factory)
    assert result.failure is not None and result.failure.startswith("base_worktree_failed")
    assert result.outcomes == () and factory.built == []


async def test_cancelling_the_step_removes_every_worktree(workspace: Path) -> None:
    factory = _Factory({"codex": sleeps(), "claude": sleeps()})
    task = asyncio.create_task(run(workspace, factory))
    for _ in range(200):
        if len(factory.dispatched()) == 2:
            break
        await asyncio.sleep(0.05)
    assert len(_worktrees(workspace)) == 4
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The shielded cleanup runs in a thread; give it a moment to finish.
    for _ in range(200):
        if len(_worktrees(workspace)) == 1:
            break
        await asyncio.sleep(0.05)
    assert len(_worktrees(workspace)) == 1
    assert not any(runtime.cwd.exists() for runtime in factory.built)
