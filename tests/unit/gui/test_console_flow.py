"""Chat flows a first-time console user goes through, end to end within the console.

No agent CLI runs: the ``ouroboros auto`` child is a scripted Python process,
runs are stubbed, and LLM adapters are recorders.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.gui import api, harness
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def no_ambient_git_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)


def _state() -> api.ConsoleState:
    return api.ConsoleState(stop=lambda *_args, **_kwargs: None)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@x.invalid",
        "commit",
        "-qm",
        "i",
        "--allow-empty",
    )
    return path


def _wait(h: harness.Harness, chat_id: str, check: Any, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = h.get(chat_id) or {}
        if check(session):
            return session
        time.sleep(0.05)
    raise AssertionError(f"timed out; last state: {json.dumps(h.get(chat_id), default=str)[:3000]}")


def _spec_chat(h: harness.Harness, cwd: Path) -> str:
    """A chat whose spec is written and ready to hand off."""
    h._sessions["c1"] = {
        "id": "c1",
        "title": "t",
        "goal": "t",
        "cwd": str(cwd),
        "mode": "interview",
        "status": "seed_ready",
        "seed_yaml": "goal: t\n",
        "version": 0,
        "messages": [],
    }
    return "c1"


# ── 1. a folder that is not a git repository ─────────────────────────


@pytest.fixture
def started_runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    async def start_run(self: harness.Harness, session_id: str) -> None:
        started.append(session_id)

    monkeypatch.setattr(harness.Harness, "_start_run", start_run)
    return started


@pytest.mark.parametrize("kind", ["not_repo", "no_commits"])
def test_hand_off_from_a_folder_without_commits_is_refused_with_its_own_code(
    tmp_path: Path, kind: str, started_runs: list[str]
) -> None:
    folder = tmp_path / "new"
    folder.mkdir()
    if kind == "no_commits":
        _git(folder, "init", "-q")
    state = _state()
    chat_id = _spec_chat(state.harness, folder)

    with pytest.raises(api.ApiError) as refused:
        api.post_chat_action(state, chat_id, query={}, body={"action": "run"})

    assert refused.value.status == 409
    assert refused.value.code == "needs_git"
    assert refused.value.extra == {"reason": kind}
    assert state.harness.get(chat_id)["status"] == "seed_ready"
    assert started_runs == []
    assert harness.workspace.git_state(str(folder)).kind == kind  # untouched


def test_prepare_git_makes_the_folder_ready_then_hands_off(
    tmp_path: Path, started_runs: list[str]
) -> None:
    folder = tmp_path / "new"
    folder.mkdir()
    state = _state()
    chat_id = _spec_chat(state.harness, folder)

    session = api.post_chat_action(
        state, chat_id, query={}, body={"action": "run", "prepare_git": True}
    )

    assert harness.workspace.git_state(str(folder)).kind == "ready"
    assert session["status"] == "running"
    note = session["messages"][-1]
    assert (note["kind"], note["key"]) == ("note", "git_initialized")
    _wait(state.harness, chat_id, lambda _s: started_runs == [chat_id])


def test_prepare_git_on_a_ready_repository_changes_nothing(
    tmp_path: Path, started_runs: list[str]
) -> None:
    folder = _repo(tmp_path / "app")
    head = _git(folder, "rev-parse", "HEAD")
    state = _state()
    chat_id = _spec_chat(state.harness, folder)

    session = api.post_chat_action(
        state, chat_id, query={}, body={"action": "run", "prepare_git": True}
    )

    assert _git(folder, "rev-parse", "HEAD") == head
    assert session["messages"] == []  # nothing was done, so nothing is claimed
    assert session["status"] == "running"


def test_prepare_git_must_be_a_boolean(tmp_path: Path, started_runs: list[str]) -> None:
    folder = tmp_path / "new"
    folder.mkdir()
    state = _state()
    chat_id = _spec_chat(state.harness, folder)
    with pytest.raises(api.ApiError) as refused:
        api.post_chat_action(state, chat_id, query={}, body={"action": "run", "prepare_git": "yes"})
    assert refused.value.code == "bad_prepare_git"
    assert harness.workspace.git_state(str(folder)).kind == "not_repo"


def test_a_failed_prepare_is_reported_and_nothing_starts(
    tmp_path: Path, started_runs: list[str]
) -> None:
    folder = tmp_path / "new"
    folder.mkdir()
    _git(folder, "init", "-q")
    hook = folder / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    state = _state()
    chat_id = _spec_chat(state.harness, folder)

    with pytest.raises(api.ApiError) as refused:
        api.post_chat_action(state, chat_id, query={}, body={"action": "run", "prepare_git": True})

    assert refused.value.code == "git_prepare_failed"
    assert "git commit failed" in refused.value.extra["detail"]
    assert state.harness.get(chat_id)["status"] == "seed_ready"
    assert started_runs == []


@pytest.mark.parametrize("prepare", [False, True])
@pytest.mark.parametrize("kind", ["dangling_symlink", "missing_gitdir_file"])
def test_hand_off_from_a_broken_git_entry_is_refused_and_never_prepared(
    tmp_path: Path, kind: str, prepare: bool, started_runs: list[str]
) -> None:
    folder = tmp_path / "broken"
    folder.mkdir()
    if kind == "dangling_symlink":
        (folder / ".git").symlink_to("/nonexistent/ouroboros-gitdir")
    else:
        (folder / ".git").write_text("gitdir: /nonexistent/ouroboros-gitdir\n")
    state = _state()
    chat_id = _spec_chat(state.harness, folder)
    body: dict[str, Any] = {"action": "run", **({"prepare_git": True} if prepare else {})}

    with pytest.raises(api.ApiError) as refused:
        api.post_chat_action(state, chat_id, query={}, body=body)

    assert (refused.value.status, refused.value.code) == (409, "git_unreadable")
    assert started_runs == []
    assert state.harness.get(chat_id)["messages"] == []  # nothing claimed
    entry = folder / ".git"
    assert entry.is_symlink() if kind == "dangling_symlink" else entry.is_file()  # no git init
    probe = api.get_chat_folder(state, query={"path": str(folder)}, body=None)
    assert probe["git"] == "unreadable"


def test_a_run_result_lists_and_counts_every_path_git_reports(tmp_path: Path) -> None:
    project = _repo(tmp_path / "proj")
    session = "orch_0123456789ab"
    worktree = tmp_path / "worktrees" / session
    _git(project, "worktree", "add", "-q", "-b", f"ooo/{session}", str(worktree))
    for name in ("median.py", "test_median.py"):
        (worktree / name).write_text("x\n")
    (worktree / "__pycache__").mkdir()
    (worktree / "__pycache__" / "median.cpython-312.pyc").write_bytes(b"")

    result = harness._run_result(str(project), session)

    assert result is not None
    assert result["files"] == ["__pycache__/", "median.py", "test_median.py"]
    # The card's two numbers come from the same listing: 3 changed, 3 uncommitted.
    assert (result["file_count"], result["uncommitted"]) == (3, 3)
    assert result["branch"] == f"ooo/{session}"


def test_the_folder_probe_reads_git_state_and_creates_nothing(tmp_path: Path) -> None:
    state = _state()
    empty = tmp_path / "empty"
    empty.mkdir()
    view = api.get_chat_folder(state, query={"path": str(empty)}, body=None)
    assert view == {"path": str(empty), "exists": True, "git": "not_repo", "detail": ""}
    assert list(empty.iterdir()) == []
    ready = api.get_chat_folder(state, query={"path": str(_repo(tmp_path / "r"))}, body=None)
    assert ready["git"] == "ready"
    missing = api.get_chat_folder(state, query={"path": str(tmp_path / "nope")}, body=None)
    assert missing == {"path": str(tmp_path / "nope"), "exists": False, "git": None}
    assert not (tmp_path / "nope").exists()
    for bad in ("", "  ", "a\x00b", "x" * 5000):
        with pytest.raises(api.ApiError) as refused:
            api.get_chat_folder(state, query={"path": bad}, body=None)
        assert refused.value.code == "bad_path"


# ── 2 and 7. Auto jobs: listed in Activity, and ending with a result ──

_AUTO_SCRIPT = """
import sys
from pathlib import Path
Path("palindrome.py").write_text("def is_palindrome(s): ...\\n")
print("[auto] interview — answered round 1/50")
print("Execution started:")
print("  Execution ID: exec_20e9d3785fd6")
print("Run handoff status: {handoff}")
sys.exit({code})
"""


def _fake_auto(
    monkeypatch: pytest.MonkeyPatch, *, handoff: str = "completed", code: int = 0
) -> None:
    script = _AUTO_SCRIPT.format(handoff=handoff, code=code)
    monkeypatch.setattr(harness, "ouroboros_command", lambda *_args: [sys.executable, "-c", script])


def test_auto_jobs_are_listed_in_activity_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_auto(monkeypatch)
    state = _state()
    chat = state.harness.create({"goal": "palindrome", "mode": "auto", "cwd": str(tmp_path)})
    job_id = chat["auto_job_id"]

    listed = api.get_jobs(state, query={}, body=None)["jobs"]
    assert [(job["id"], job["kind"], job["title"]) for job in listed] == [
        (job_id, "auto", "palindrome")
    ]
    assert api.get_job(state, job_id, query={}, body=None)["kind"] == "auto"
    _wait(state.harness, chat["id"], lambda s: s.get("status") == "done")


def test_a_finished_auto_job_ends_with_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_auto(monkeypatch)
    folder = _repo(tmp_path / "proj")
    state = _state()
    chat = state.harness.create({"goal": "palindrome", "mode": "auto", "cwd": str(folder)})

    session = _wait(state.harness, chat["id"], lambda s: s.get("status") == "done")

    note, result = session["messages"][-2:]
    assert (note["kind"], note["key"]) == ("note", "auto_finished")
    assert result["kind"] == "result"
    assert {key: result[key] for key in result if key not in ("id", "ts", "role", "text")} == {
        "kind": "result",
        "source": "auto",
        "outcome": "completed",
        "handoff_status": "completed",
        "execution_id": "exec_20e9d3785fd6",
        "folder": str(folder),
        "in_repo": True,
        "files": ["palindrome.py"],
        "file_count": 1,
        "internal_files": [],
        "internal_count": 0,
    }


_NOISY_AUTO_SCRIPT = """
from pathlib import Path
Path("md2csv.py").write_text("x\\n")
Path(".ouroboros").mkdir()
Path(".ouroboros/state.json").write_text("{{}}")
Path("__pycache__").mkdir()
Path("__pycache__/md2csv.cpython-312.pyc").write_bytes(b"")
print("  Execution ID: exec_20e9d3785fd6")
print("Run handoff status: completed")
"""


def test_an_auto_result_lists_ouroboros_state_and_bytecode_apart_and_counts_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        harness,
        "ouroboros_command",
        lambda *_args: [sys.executable, "-c", _NOISY_AUTO_SCRIPT.format()],
    )
    folder = _repo(tmp_path / "proj")
    state = _state()
    chat = state.harness.create({"goal": "md2csv", "mode": "auto", "cwd": str(folder)})

    session = _wait(state.harness, chat["id"], lambda s: s.get("status") == "done")

    result = session["messages"][-1]
    assert (result["files"], result["file_count"]) == (["md2csv.py"], 1)
    assert (result["internal_files"], result["internal_count"]) == (
        [".ouroboros/", "__pycache__/"],
        2,
    )


@pytest.mark.parametrize(
    ("handoff", "code", "outcome", "status"),
    [("started", 0, "unverified", "done"), ("failed", 3, "failed", "failed")],
)
def test_an_auto_job_that_did_not_complete_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handoff: str,
    code: int,
    outcome: str,
    status: str,
) -> None:
    _fake_auto(monkeypatch, handoff=handoff, code=code)
    state = _state()
    chat = state.harness.create({"goal": "palindrome", "mode": "auto", "cwd": str(tmp_path)})

    session = _wait(state.harness, chat["id"], lambda s: s.get("status") in ("done", "failed"))

    assert session["status"] == status
    result = session["messages"][-1]
    assert (result["outcome"], result["handoff_status"]) == (outcome, handoff)
    # Not a repository: git cannot say what changed, and the result does not pretend.
    assert (result["in_repo"], result["files"], result["file_count"]) == (False, [], 0)
    if code:
        error = session["messages"][-2]
        assert (error["kind"], error["key"], error["vars"]) == ("error", "auto_exited", {"code": 3})


_LONG_AUTO_SCRIPT = """
import sys
print("Execution started:")
print("  Execution ID: exec_20e9d3785fd6")
{early}
for index in range(2500):
    print(f"[auto] progress line {{index}}")
{late}
"""


@pytest.mark.parametrize("handoff_early", [False, True])
def test_the_result_keeps_ids_printed_more_than_the_log_holds_ago(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handoff_early: bool
) -> None:
    from ouroboros.gui import jobs

    line = 'print("Run handoff status: completed")'
    script = _LONG_AUTO_SCRIPT.format(
        early=line if handoff_early else "", late="" if handoff_early else line
    )
    monkeypatch.setattr(harness, "ouroboros_command", lambda *_args: [sys.executable, "-c", script])
    state = _state()
    chat = state.harness.create({"goal": "long run", "mode": "auto", "cwd": str(tmp_path)})

    session = _wait(state.harness, chat["id"], lambda s: s.get("status") == "done")

    job = state.jobs.get(chat["auto_job_id"])
    assert job is not None and len(job.lines) == jobs._MAX_LINES  # the id line is long gone
    assert not any("exec_20e9d3785fd6" in kept for kept in job.lines)
    result = session["messages"][-1]
    assert (result["execution_id"], result["handoff_status"], result["outcome"]) == (
        "exec_20e9d3785fd6",
        "completed",
        "completed",
    )
    assert session["execution_id"] == "exec_20e9d3785fd6"


def test_a_job_whose_id_line_left_the_log_still_gives_the_board_link(tmp_path: Path) -> None:
    """Deterministic: the follow loop first sees the job after the id scrolled out."""
    from ouroboros.gui import jobs

    state = _state()
    job = jobs.Job(
        id="j-long", kind="auto", title="t", argv=["ouroboros", "auto"], watch=harness._AUTO_WATCH
    )
    job.read_output("  Execution ID: exec_20e9d3785fd6")
    for index in range(jobs._MAX_LINES + 100):
        job.read_output(f"[auto] progress line {index}")
    job.read_output("Run handoff status: completed")
    job.exit_code, job.status = 0, "succeeded"
    state.jobs._jobs[job.id] = job
    assert not any("exec_" in line for line in job.lines)
    state.harness._sessions["a1"] = {
        "id": "a1",
        "title": "t",
        "goal": "t",
        "cwd": str(tmp_path),
        "mode": "auto",
        "status": "running",
        "auto_job_id": job.id,
        "version": 0,
        "messages": [_msg_log(job.id)],
    }

    state.harness._follow_auto("a1", job.id)

    session = state.harness.get("a1") or {}
    result = session["messages"][-1]
    assert result["kind"] == "result"
    assert (result["execution_id"], result["outcome"]) == ("exec_20e9d3785fd6", "completed")
    assert session["execution_id"] == "exec_20e9d3785fd6"
    log = session["messages"][0]
    assert len(log["lines"]) == harness._MAX_LOG_LINES  # the transcript log stays bounded


def _msg_log(job_id: str) -> dict[str, Any]:
    return {
        "id": "l1",
        "role": "assistant",
        "kind": "log",
        "text": "ouroboros auto",
        "ts": 0.0,
        "lines": [],
        "job_id": job_id,
    }


def test_a_stopped_auto_job_is_reported_as_stopped() -> None:
    assert harness._auto_outcome("failed", True, None) == "stopped"
    assert harness._auto_outcome("succeeded", False, None) == "unverified"


# ── 3. an agent that is not installed ────────────────────────────────


@pytest.mark.parametrize("mode", ["interview", "auto", "ask"])
def test_an_uninstalled_agent_is_refused_before_the_chat_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from ouroboros.backends import model_catalog

    monkeypatch.setattr(model_catalog, "detect_backend_cli", lambda _name: None)
    state = _state()

    with pytest.raises(api.ApiError) as refused:
        api.post_chat_session(
            state,
            query={},
            body={"goal": "x", "mode": mode, "backend": "gemini", "cwd": str(tmp_path)},
        )

    assert refused.value.status == 400
    assert refused.value.code == "backend_not_installed"
    assert refused.value.extra == {
        "agent": "gemini",
        "cli": "gemini",
        "install": "npm install -g @google/gemini-cli",
    }
    assert "npm install -g @google/gemini-cli" in refused.value.message
    assert state.harness.list_sessions() == []
    assert state.jobs.list() == []


def test_an_installed_agent_or_one_without_a_cli_is_not_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ouroboros.backends import model_catalog

    asked: list[str] = []

    def detect(name: str) -> str | None:
        asked.append(name)
        return "/usr/local/bin/gemini"

    monkeypatch.setattr(model_catalog, "detect_backend_cli", detect)
    harness._require_installed("gemini")
    harness._require_installed("litellm")  # a provider route: nothing to detect
    assert asked == ["gemini"]


# ── 4. interview and Seed agents run in the chat's folder ─────────────


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        recorder = self

        class Adapter:
            async def complete(self, messages: Any, config: Any) -> Any:
                recorder.calls[-1]["completed"] = True
                return Result.ok(
                    CompletionResponse(content="ok", model="m", usage=UsageInfo(0, 0, 0))
                )

        return Adapter()


def _prompt() -> tuple[list[Message], CompletionConfig]:
    return [Message(role=MessageRole.USER, content="q")], CompletionConfig(model="m")


@pytest.mark.parametrize("backend", ["", "codex", "gemini"])
async def test_interview_and_seed_adapters_run_in_the_chat_folder_with_the_handlers_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    from ouroboros.mcp.tools import authoring_handlers
    from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler

    folder = tmp_path / "project"
    folder.mkdir()
    assert str(folder) != os.getcwd()
    llm = backend or None

    # What the handlers build for themselves (no cwd).
    own = _Recorder()
    monkeypatch.setattr(authoring_handlers, "create_llm_adapter", own)
    own_engine, _ = InterviewHandler(llm_backend=llm)._create_interview_engine()
    await GenerateSeedHandler(llm_backend=llm).handle({"session_id": "interview_missing"})
    own_interview, own_seed = own.calls

    # What the console's handlers for this chat build. The run and evaluation
    # handlers are out of scope here (and would probe the agent CLI's version).
    from ouroboros.mcp.tools import execution_handlers, run_successors

    for module, name in (
        (execution_handlers, "ExecuteSeedHandler"),
        (execution_handlers, "StartExecuteSeedHandler"),
        (run_successors, "build_run_successor_handler"),
    ):
        monkeypatch.setattr(module, name, lambda **_kwargs: object())
    console = _Recorder()
    monkeypatch.setattr("ouroboros.providers.create_llm_adapter", console)
    handlers = harness.Harness()._handlers_for({"backend": backend, "cwd": str(folder)})
    handlers.ensure()
    harness._in_chat_folder({"cwd": str(folder)})
    engine, adapter = handlers.interview_handler._handler._create_interview_engine()
    await adapter.complete(*_prompt())
    await handlers.seed.handler.llm_adapter.complete(*_prompt())
    interview, seed = console.calls

    assert interview.pop("completed") and seed.pop("completed")
    assert interview == {**own_interview, "cwd": str(folder)}
    assert seed == {**own_seed, "cwd": str(folder)}
    # The handler keeps an injected adapter's prompt mode; this one matches its own.
    assert engine.suppress_tool_use_prompt_cues == own_engine.suppress_tool_use_prompt_cues
    assert engine.model == own_engine.model


class _FolderProbingHandlers:
    """Shared by every chat, like the real per-agent handlers: each call asks its agent once."""

    def __init__(self) -> None:
        self.cwds: dict[str, list[object]] = {}
        self.started = 0

    def ensure(self) -> None:
        return None

    @property
    def interview(self) -> _FolderProbingHandlers:
        return self

    async def _ask(self, role: str, label: str) -> None:
        recorder = _Recorder()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("ouroboros.providers.create_llm_adapter", recorder)
            await harness._FolderAdapter(role, None).complete(*_prompt())  # type: ignore[arg-type]
        self.cwds.setdefault(label, []).append(recorder.calls[0]["cwd"])

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> Any:
        from ouroboros.auto.interview_driver import InterviewTurn

        self.started += 1
        while self.started < 2:  # both chats are mid-call at once
            await asyncio.sleep(0.01)
        await self._ask("interview", goal)
        return InterviewTurn(question="Who uses it?", session_id=f"interview_{goal}")

    def code_request(self, interview_session_id: str) -> None:
        return None

    def interview_view(self, interview_session_id: str) -> tuple[None, str]:
        return None, "Who uses it?"

    async def seed(self, interview_session_id: str, *, force: bool = False) -> Any:
        goal = interview_session_id.removeprefix("interview_")
        await self._ask("seed_generation", goal)

        class Seed:
            def to_dict(self) -> dict[str, Any]:
                return {"goal": goal, "acceptance_criteria": [], "constraints": []}

        return Seed()


def test_concurrent_chats_ask_their_agents_in_their_own_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probing = _FolderProbingHandlers()
    monkeypatch.setattr(harness.Harness, "_handlers_for", lambda *_args: probing)
    h = harness.Harness()
    folders = {name: tmp_path / name for name in ("alpha", "beta")}
    chats = {}
    for name, folder in folders.items():
        folder.mkdir()
        chats[name] = h.create({"goal": name, "cwd": str(folder)})["id"]
    for chat_id in chats.values():
        _wait(h, chat_id, lambda s: s.get("status") == "waiting_answer")
        h.action(chat_id, "seed", {"force": True})
        _wait(h, chat_id, lambda s: s.get("status") == "seed_ready")

    assert str(tmp_path) != os.getcwd()
    assert probing.cwds == {name: [str(folder)] * 2 for name, folder in folders.items()}


def test_without_a_chat_folder_the_adapter_is_the_handlers_own() -> None:
    import contextvars

    def build() -> dict[str, Any]:
        recorder = _Recorder()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("ouroboros.providers.create_llm_adapter", recorder)
            harness._FolderAdapter("seed_generation", None).build()
        return recorder.calls[0]

    # A fresh context: no chat folder was set, so cwd stays unset as the handler leaves it.
    assert contextvars.Context().run(build)["cwd"] is None


# ── 5. refusals and notes carry keys the page translates ──────────────


def _catalog_keys() -> set[str]:
    text = (_REPO / "gui" / "src" / "i18n" / "en.ts").read_text(encoding="utf-8")
    return set(re.findall(r'^\s*"([^"]+)":', text, flags=re.MULTILINE))


def test_every_refusal_code_and_note_key_has_a_translation() -> None:
    source = (_REPO / "src" / "ouroboros" / "gui" / "harness.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'Refusal\(\s*"([a-z_]+)"', source))
    notes = set(re.findall(r'key="([a-z_]+)"', source)) | {"git_initialized", "git_committed"}
    keys = _catalog_keys()
    assert codes and notes
    assert sorted(code for code in codes if f"error.{code}" not in keys) == []
    assert sorted(note for note in notes if f"chat.note.{note}" not in keys) == []


def test_refusals_reach_the_page_with_their_code(tmp_path: Path) -> None:
    state = _state()
    with pytest.raises(api.ApiError) as refused:
        api.post_chat_session(state, query={}, body={"goal": "x", "cwd": str(tmp_path / "nope")})
    assert (refused.value.status, refused.value.code) == (400, "folder_missing")
    assert refused.value.extra == {"folder": str(tmp_path / "nope")}
    chat_id = _spec_chat(state.harness, tmp_path)
    with pytest.raises(api.ApiError) as refused:
        api.post_chat_action(state, chat_id, query={}, body={"action": "explode"})
    assert (refused.value.status, refused.value.code) == (409, "unknown_action")


# ── 6. the model a sentinel backend reports ───────────────────────────


def _recording_auto(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """The fake ``ouroboros auto``, recording the arguments each job was started with."""
    launched: list[tuple[str, ...]] = []
    script = _AUTO_SCRIPT.format(handoff="completed", code=0)

    def command(*args: str) -> list[str]:
        launched.append(args)
        return [sys.executable, "-c", script]

    monkeypatch.setattr(harness, "ouroboros_command", command)
    return launched


def _config(home: Path, text: str) -> None:
    path = home / ".ouroboros" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(f'model = "{model}"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def _runtime_arg(args: tuple[str, ...]) -> str | None:
    return args[args.index("--runtime") + 1] if "--runtime" in args else None


def test_an_explicit_codex_chat_names_codex_and_its_own_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.backends import model_catalog

    _codex_home(tmp_path, monkeypatch, "gpt-6.2-astra")
    monkeypatch.setattr(model_catalog, "detect_backend_cli", lambda _name: "/bin/codex")
    launched = _recording_auto(monkeypatch)
    state = _state()
    chat = state.harness.create(
        {"goal": "palindrome", "mode": "auto", "backend": "codex", "cwd": str(tmp_path)}
    )

    session = _wait(state.harness, chat["id"], lambda s: s.get("status") == "done")

    assert [_runtime_arg(args) for args in launched] == ["codex"]
    assert session["auto_model"] == {
        "model": "default",
        "backend": "codex",
        "configured": "gpt-6.2-astra",
    }


def test_a_default_claude_chat_without_the_sdk_names_the_cli_it_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    """``--runtime claude-cli`` drives every stage, so a configured Codex Execute stage is not it."""
    import importlib.util

    from ouroboros.config import loader

    _config(
        isolated_home,
        "orchestrator:\n  runtime_backend: claude\n"
        "  runtime_profile:\n    stages:\n      execute: codex\n",
    )
    _codex_home(tmp_path, monkeypatch, "gpt-6.2-astra")
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *rest: None if name == "claude_agent_sdk" else real_find_spec(name, *rest),
    )
    asked: list[str | None] = []
    real_model = loader.resolve_execution_model

    def execution_model(backend: str | None) -> str | None:
        asked.append(backend)
        return real_model(backend)

    monkeypatch.setattr(loader, "resolve_execution_model", execution_model)
    launched = _recording_auto(monkeypatch)
    state = _state()
    chat = state.harness.create({"goal": "palindrome", "mode": "auto", "cwd": str(tmp_path)})

    session = _wait(state.harness, chat["id"], lambda s: s.get("status") == "done")

    assert [_runtime_arg(args) for args in launched] == ["claude-cli"]
    # The model is the one the Execute handler gives the Claude Code CLI worker,
    # not Codex's configured default.
    assert asked == ["claude_mcp"]
    assert session["auto_model"] == {
        "model": real_model("claude_mcp") or "default",
        "backend": "claude-cli",
        "configured": None,
    }


def test_without_an_override_the_configured_execute_stage_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    _config(
        isolated_home,
        "orchestrator:\n  runtime_backend: codex\n"
        "  runtime_profile:\n    stages:\n      execute: opencode\n",
    )
    launched = _recording_auto(monkeypatch)
    state = _state()
    chat = state.harness.create({"goal": "palindrome", "mode": "auto", "cwd": str(tmp_path)})

    session = _wait(state.harness, chat["id"], lambda s: s.get("status") == "done")

    assert [_runtime_arg(args) for args in launched] == [None]
    assert session["auto_model"]["backend"] == "opencode"


def test_a_pinned_execute_model_is_looked_up_for_the_runtime_that_runs(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    from ouroboros.config import loader

    _config(
        isolated_home,
        "orchestrator:\n  runtime_backend: codex\n"
        "  runtime_profile:\n    stages:\n      execute: opencode\n",
    )
    pins = {"codex": "gpt-6-sol", "opencode": "glm-5"}
    monkeypatch.setattr(loader, "resolve_execution_model", lambda backend: pins.get(backend))

    # An explicit runtime drives Execute too; no override follows the profile.
    assert harness._auto_model("codex") == {
        "model": "gpt-6-sol",
        "backend": "codex",
        "configured": None,
    }
    assert harness._auto_model(None) == {
        "model": "glm-5",
        "backend": "opencode",
        "configured": None,
    }


def test_an_unreadable_config_gives_no_auto_model_rather_than_a_guess(
    isolated_home: Path,
) -> None:
    _config(isolated_home, "- not a mapping\n")
    assert harness._auto_model(None) is None
    assert harness._auto_model("not-an-agent") is None


@pytest.mark.parametrize(
    ("goal", "title"),
    [
        ("Build a palindrome checker", "Build a palindrome checker"),
        ("\n  First line  \nsecond line", "First line"),
        (
            "Build a Python CLI that converts Markdown tables to CSV (file or stdin in, "
            "CSV to stdout), with pytest tests.",
            "Build a Python CLI that converts Markdown tables to CSV (file or stdin in, CSV…",
        ),
        ("x" * 100, "x" * 79 + "…"),
        ("a" * 70 + " " + "b" * 40, "a" * 70 + "…"),
        ("", ""),
        (None, ""),
    ],
)
def test_a_chat_title_is_cut_at_a_word_and_says_so(goal: object, title: str) -> None:
    assert harness.chat_title(goal) == title
    assert len(harness.chat_title(goal)) <= 80


def test_the_chat_list_titles_old_chats_from_their_goal(tmp_path: Path) -> None:
    h = harness.Harness()
    goal = (
        "Build a Python CLI that converts Markdown tables to CSV (file or stdin in, CSV to stdout)"
    )
    h._sessions["old"] = {
        "id": "old",
        "title": goal[:80],  # saved before titles were cut at a word
        "goal": goal,
        "cwd": str(tmp_path),
        "mode": "auto",
        "status": "done",
        "version": 0,
        "messages": [],
    }
    h._sessions["bare"] = {"id": "bare", "title": "", "mode": "ask", "messages": []}

    rows = {row["id"]: row["title"] for row in h.list_sessions()}

    assert rows["old"] == harness.chat_title(goal)
    assert rows["old"].endswith("…")
    assert rows["bare"] == "Untitled"


def test_a_default_model_names_the_agent_and_its_configured_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert harness._default_model_hint("codex", "default") == {
        "model_backend": "codex",
        "model_default": None,
    }
    (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
    assert harness._default_model_hint("codex", "default") == {
        "model_backend": "codex",
        "model_default": "gpt-5.5",
    }
    assert harness._default_model_hint("codex", "gpt-5.5") == {}  # a real model: shown as is


def test_an_ask_reply_with_the_default_model_carries_the_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.backends import model_catalog
    from ouroboros.providers import factory

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.5"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(model_catalog, "detect_backend_cli", lambda _name: "/bin/codex")
    sent: list[str] = []

    class Codex:
        async def complete(self, messages: Any, config: Any) -> Any:
            sent.append(config.model)
            return Result.ok(
                CompletionResponse(content="hi", model="default", usage=UsageInfo(0, 0, 0))
            )

    monkeypatch.setattr(factory, "create_llm_adapter", lambda **_kwargs: Codex())
    h = harness.Harness()
    chat = h.create({"goal": "hello?", "mode": "ask", "backend": "codex", "cwd": str(tmp_path)})

    session = _wait(h, chat["id"], lambda s: s.get("status") == "waiting_answer")

    reply = session["messages"][-1]
    assert (reply["model"], reply["model_backend"], reply["model_default"]) == (
        "default",
        "codex",
        "gpt-5.5",
    )
    assert sent == ["default"]  # display only: the request is unchanged
