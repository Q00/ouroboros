"""Interview mode of the chat harness against a scripted interview server.

The interview handler is replaced by a scripted backend (no model is called),
so these tests cover what the console decides: which folder the interview
reads, which questions reach the human, what is sent back to the server, and
where readiness comes from.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import subprocess
import time
from typing import Any

import pytest

from ouroboros.auto.interview_driver import InterviewTurn
from ouroboros.auto.investigator import Investigation, RepoInvestigator
from ouroboros.core.types import Result
from ouroboros.gui import harness
from ouroboros.orchestrator.adapter import TaskResult

SESSION = "interview_00000000000000aa"


@dataclass
class Step:
    """One server turn: the question as shown, as stored, and what came with it."""

    shown: str
    issued: str
    code_request: bool = True
    readiness: dict[str, Any] | None = None
    completed: bool = False
    ambiguity: float | None = None


@dataclass
class ScriptedHandlers:
    """Stands in for ``harness._Handlers``: a scripted ``ouroboros_interview``."""

    steps: list[Step]
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)
    position: int = -1

    def ensure(self) -> None:
        return None

    @property
    def interview(self) -> ScriptedHandlers:
        return self

    def _next(self) -> InterviewTurn:
        self.position += 1
        step = self.steps[min(self.position, len(self.steps) - 1)]
        return InterviewTurn(
            question=step.shown,
            session_id=SESSION,
            completed=step.completed,
            ambiguity_score=step.ambiguity,
        )

    def _step(self) -> Step:
        return self.steps[min(self.position, len(self.steps) - 1)]

    async def start(self, goal: str, *, cwd: str, interview_id: str | None = None) -> InterviewTurn:
        self.calls.append(("start", goal, cwd))
        self.start_cwd_existed_empty = Path(cwd).is_dir() and not any(Path(cwd).iterdir())
        return self._next()

    async def answer(
        self, session_id: str, answer: str, *, last_question: str | None = None, **_: Any
    ) -> InterviewTurn:
        self.calls.append(("answer", answer, last_question))
        return self._next()

    def code_request(self, interview_session_id: str) -> dict[str, Any] | None:
        step = self._step()
        return {"question": step.issued} if step.code_request else None

    def interview_view(self, interview_session_id: str) -> tuple[dict[str, Any] | None, str | None]:
        step = self._step()
        return step.readiness, None if step.completed else step.issued

    def answers(self) -> list[tuple[str, str | None]]:
        return [(text, last) for kind, text, last in self.calls if kind == "answer"]


class ScriptedInvestigator:
    """Returns a fixed investigation per question; can hold a reply until released."""

    def __init__(self, replies: dict[str, Investigation], hold: asyncio.Event | None = None):
        self.replies = replies
        self.hold = hold
        self.asked: list[str] = []

    async def investigate(self, goal: str, question: str, workspace: Path) -> Investigation:
        self.asked.append(question)
        if self.hold is not None:
            await self.hold.wait()
        return self.replies.get(question, Investigation("not_found", called=True))


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        + ["commit", "-qm", "init"],
        check=True,
    )
    return path


def _wait(h: harness.Harness, chat_id: str, check: Any, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = h.get(chat_id) or {}
        if check(session):
            return session
        time.sleep(0.02)
    raise AssertionError(f"timed out; last state: {json.dumps(h.get(chat_id), default=str)[:2000]}")


def _idle(session: dict[str, Any]) -> bool:
    suggestion = session.get("suggestion") or {}
    return session.get("status") == "waiting_answer" and not suggestion.get("pending")


def _chat(
    h: harness.Harness,
    steps: list[Step],
    cwd: Path,
    *,
    project: str | None = "existing",
    investigator: Any = None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, ScriptedHandlers]:
    scripted = ScriptedHandlers(steps)
    # Every chat of this harness gets the scripted server, whatever its agent or
    # folder: the real handlers (and an agent CLI) are never reached.
    monkeypatch.setattr(harness.Harness, "_handlers_for", lambda *_args: scripted)

    async def repo_investigator(self: harness.Harness, session: dict[str, Any]) -> Any:
        return investigator

    monkeypatch.setattr(harness.Harness, "_repo_investigator", repo_investigator)
    payload: dict[str, Any] = {"goal": "add a todo CLI to this repo", "cwd": str(cwd)}
    if project is not None:
        payload["project"] = project
    chat = h.create(payload)
    return chat["id"], scripted


def _found(path: str, quote: str, answer: str) -> Investigation:
    from ouroboros.auto.evidence_check import EvidenceSpan

    return Investigation(
        "found", answer=answer, checked=(EvidenceSpan(path, 1, 1, quote),), cited=1, called=True
    )


# ── brownfield only on an explicit choice ─────────────────────────────


def test_a_new_project_never_reads_the_launch_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "launch", {"pyproject.toml": 'requires-python = ">=3.12"\n'})
    investigator = ScriptedInvestigator({})
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [Step("Who uses it?", "Who uses it?")],
        repo,
        project=None,
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    session = _wait(h, chat_id, _idle)
    assert session["project"] == "new"
    kind, _, cwd = scripted.calls[0]
    assert kind == "start" and cwd != str(repo)
    assert scripted.start_cwd_existed_empty  # an empty folder: never brownfield
    assert not Path(str(cwd)).exists()  # and a temporary one
    assert investigator.asked == []  # no repository to answer from
    assert session["cwd"] == str(repo)  # runs still use the chosen folder


def test_an_existing_project_is_read_only_when_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "app", {"README.md": "app\n"})
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [Step("Which command?", "Which command?", code_request=False)],
        repo,
        monkeypatch=monkeypatch,
    )
    session = _wait(h, chat_id, _idle)
    assert session["project"] == "existing"
    assert scripted.calls[0] == ("start", "add a todo CLI to this repo", str(repo))


def test_an_existing_project_must_be_a_project_folder(tmp_path: Path) -> None:
    h = harness.Harness()
    with pytest.raises(ValueError, match="no git repository or project manifest"):
        h.create({"goal": "x", "cwd": str(tmp_path), "project": "existing"})
    with pytest.raises(ValueError, match="Unknown project kind"):
        h.create({"goal": "x", "cwd": str(tmp_path), "project": "brownfield"})
    assert h.list_sessions() == []


# ── code facts answered from the repository ───────────────────────────


class _CodexShapedRuntime:
    _runtime_backend = "codex"

    def __init__(self, reply: str) -> None:
        self._reply = reply

    def _build_permission_args(self) -> list[str]:
        return ["--sandbox", "read-only"]

    async def execute_task_to_result(self, prompt: str, **_: Any) -> Any:
        return Result.ok(TaskResult(success=True, final_message=self._reply, messages=()))


def test_an_exact_manifest_answer_is_auto_confirmed_with_a_visible_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(
        tmp_path / "app", {"pyproject.toml": '[project]\nrequires-python = ">=3.12"\n'}
    )
    reply = json.dumps(
        {
            "kind": "current_behavior",
            "status": "found",
            "answer": "The project requires Python 3.12 or newer.",
            "evidence": [
                {
                    "path": "pyproject.toml",
                    "start_line": 2,
                    "end_line": 2,
                    "quote": 'requires-python = ">=3.12"',
                }
            ],
        }
    )
    # The real investigator: its evidence check reads the real file.
    investigator = RepoInvestigator(
        runtime_backend="codex", runtime_factory=lambda **_: _CodexShapedRuntime(reply)
    )
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [
            Step("(ambiguity: 0.62) Which Python version?", "Which Python version?"),
            Step("What should `todo add` print?", "What should `todo add` print?", False),
        ],
        repo,
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    session = _wait(h, chat_id, lambda s: _idle(s) and len(scripted.answers()) == 1)
    assert scripted.answers() == [
        (
            "[from-code][auto-confirmed] The project requires Python 3.12 or newer.",
            "Which Python version?",
        )
    ]
    note = next(m for m in session["messages"] if m.get("via") == "auto_confirmed")
    assert note["source"] == "pyproject.toml"
    assert note["question"] == "(ambiguity: 0.62) Which Python version?"
    assert session["last_question"] == "What should `todo add` print?"
    assert session["assisted_in_a_row"] == 1


@pytest.mark.parametrize(
    ("answer", "auto_confirmed"),
    [("This project uses React 18.", True), ("This project uses Vue 18.", False)],
)
def test_a_manifest_value_auto_confirms_only_the_dependency_it_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, auto_confirmed: bool
) -> None:
    repo = _git_repo(
        tmp_path / "app", {"package.json": '{\n  "dependencies": {\n    "react": "18"\n  }\n}\n'}
    )
    reply = json.dumps(
        {
            "kind": "current_behavior",
            "status": "found",
            "answer": answer,
            "evidence": [
                {"path": "package.json", "start_line": 3, "end_line": 3, "quote": '"react": "18"'}
            ],
        }
    )
    # The real investigator: the citation passes its evidence check either way.
    investigator = RepoInvestigator(
        runtime_backend="codex", runtime_factory=lambda **_: _CodexShapedRuntime(reply)
    )
    question = "Which UI framework does the project use?"
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [Step(question, question), Step("Next?", "Next?", False)],
        repo,
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    if auto_confirmed:
        session = _wait(h, chat_id, lambda s: _idle(s) and len(scripted.answers()) == 1)
        assert scripted.answers() == [(f"[from-code][auto-confirmed] {answer}", question)]
        note = next(m for m in session["messages"] if m.get("via") == "auto_confirmed")
        assert note["source"] == "package.json"
        return
    session = _wait(h, chat_id, lambda s: _idle(s) and (s.get("suggestion") or {}).get("text"))
    assert scripted.answers() == []  # nothing sent without the human
    assert not any(m.get("via") == "auto_confirmed" for m in session["messages"])
    suggestion = session["suggestion"]
    assert suggestion["text"] == answer
    assert suggestion["source"] == "repo" and suggestion["auto"] is True
    assert suggestion["evidence"] == ['package.json:3 "react": "18"']

    h.action(chat_id, "accept", {})  # still one click away
    _wait(h, chat_id, lambda s: _idle(s) and s["last_question"] == "Next?")
    assert scripted.answers() == [(f"[from-code] {answer}", question)]


def test_an_inferred_answer_waits_for_one_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "app", {"src/cli.py": "import typer\n"})
    question = "Which CLI library does the code use?"
    investigator = ScriptedInvestigator(
        {question: _found("src/cli.py", "import typer", "The CLI uses Typer.")}
    )
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [Step(question, question), Step("Next?", "Next?", False)],
        repo,
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    session = _wait(h, chat_id, lambda s: _idle(s) and (s.get("suggestion") or {}).get("text"))
    assert scripted.answers() == []  # nothing sent without the human
    suggestion = session["suggestion"]
    assert suggestion["source"] == "repo" and suggestion["auto"] is True
    assert suggestion["evidence"] == ["src/cli.py:1 import typer"]

    h.action(chat_id, "accept", {})
    session = _wait(h, chat_id, lambda s: _idle(s) and s["last_question"] == "Next?")
    assert scripted.answers() == [("[from-code] The CLI uses Typer.", question)]
    assert session["assisted_in_a_row"] == 1


def test_a_question_the_repository_does_not_settle_goes_to_the_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "app", {"pyproject.toml": "[project]\n"})
    question = "Should done items be deleted or archived?"
    investigator = ScriptedInvestigator({question: Investigation("intended_behavior")})
    h = harness.Harness()
    chat_id, scripted = _chat(
        h, [Step(question, question)], repo, investigator=investigator, monkeypatch=monkeypatch
    )
    session = _wait(h, chat_id, lambda s: _idle(s) and investigator.asked)
    assert scripted.answers() == []
    assert session["suggestion"] == {"pending": False, "question": question, "repo_checked": True}


def test_dismissing_a_repository_answer_keeps_the_question_with_the_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "app", {"src/cli.py": "import typer\n"})
    question = "Which CLI library?"
    investigator = ScriptedInvestigator({question: _found("src/cli.py", "import typer", "Typer.")})
    h = harness.Harness()
    chat_id, scripted = _chat(
        h, [Step(question, question)], repo, investigator=investigator, monkeypatch=monkeypatch
    )
    _wait(h, chat_id, lambda s: _idle(s) and (s.get("suggestion") or {}).get("text"))
    session = h.action(chat_id, "dismiss", {})
    assert session["suggestion"] == {"pending": False, "question": question, "repo_checked": True}
    with pytest.raises(ValueError):
        h.action(chat_id, "accept", {})
    with pytest.raises(ValueError):
        h.action(chat_id, "dismiss", {})
    assert scripted.answers() == []


def test_a_human_answer_first_wins_over_a_late_repository_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "app", {"pyproject.toml": 'requires-python = ">=3.12"\n'})
    question = "Which Python?"
    hold = asyncio.Event()
    investigator = ScriptedInvestigator(
        {question: _found("pyproject.toml", 'requires-python = ">=3.12"', "Python 3.12.")}, hold
    )
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        # The server asks the same question again: the late answer must still
        # not land on the new turn.
        [Step(question, question), Step(question, question, code_request=False)],
        repo,
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    _wait(h, chat_id, lambda s: s.get("status") == "waiting_answer" and investigator.asked)
    h.send(chat_id, "3.11, actually")
    _wait(h, chat_id, lambda s: _idle(s) and len(scripted.answers()) == 1)
    h._loop_ready().loop.call_soon_threadsafe(hold.set)
    time.sleep(0.3)
    session = h.get(chat_id) or {}
    assert scripted.answers() == [("3.11, actually", question)]
    assert session["suggestion"] == {"pending": False}
    assert not any(m.get("via") for m in session["messages"])


def test_an_auto_confirmed_answer_can_be_corrected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "app", {"pyproject.toml": 'requires-python = ">=3.12"\n'})
    question = "Which Python?"
    investigator = ScriptedInvestigator(
        {question: _found("pyproject.toml", 'requires-python = ">=3.12"', "Python 3.12.")}
    )
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [Step(question, question), Step("Next?", "Next?", False), Step("Then?", "Then?", False)],
        repo,
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    session = _wait(h, chat_id, lambda s: _idle(s) and s["last_question"] == "Next?")
    note = next(m for m in session["messages"] if m.get("via") == "auto_confirmed")
    h.send(chat_id, "We are moving to 3.13.", corrects=note["id"])
    session = _wait(h, chat_id, lambda s: _idle(s) and s["last_question"] == "Then?")
    payload, last = scripted.answers()[-1]
    assert payload.startswith("[from-user] Correction:")
    assert "Earlier question: Which Python?" in payload
    assert "Correct answer: We are moving to 3.13." in payload
    assert last == "Next?"
    assert next(m for m in session["messages"] if m["id"] == note["id"])["corrected"] is True
    with pytest.raises(ValueError):
        h.send(chat_id, "again", corrects="nope")


def test_rhythm_guard_sends_the_fourth_question_to_the_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "app", {"pyproject.toml": 'requires-python = ">=3.12"\n'})
    found = _found("pyproject.toml", 'requires-python = ">=3.12"', "Python 3.12.")
    questions = [f"Fact {n}?" for n in range(1, 6)]
    investigator = ScriptedInvestigator(dict.fromkeys(questions, found))
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [*(Step(q, q) for q in questions), Step("Done?", "Done?", False)],
        repo,
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    session = _wait(h, chat_id, lambda s: _idle(s) and s["last_question"] == "Fact 4?")
    assert len(scripted.answers()) == 3
    assert investigator.asked == questions[:3]
    assert session["suggestion"] == {"pending": False}  # no repository answer offered
    h.send(chat_id, "My own answer.")
    _wait(h, chat_id, lambda s: _idle(s) and s["last_question"] == "Done?")
    assert investigator.asked == [*questions[:3], "Fact 5?"]
    assert [text for text, _ in scripted.answers()][3:] == [
        "My own answer.",
        "[from-code][auto-confirmed] Python 3.12.",
    ]


# ── readiness and the question sent back come from the server ────────


def test_ready_means_the_server_completed_not_a_low_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness = {"score": 0.12, "threshold": 0.2, "score_ok": True, "qualified": False}
    readiness |= {"dimensions": [], "streak": 0, "streak_required": 2, "complete": False}
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [
            Step("Q1?", "Q1?", code_request=False, readiness=readiness, ambiguity=0.12),
            Step(
                "",
                "",
                code_request=False,
                readiness={**readiness, "complete": True},
                completed=True,
            ),
        ],
        tmp_path,
        project=None,
        monkeypatch=monkeypatch,
    )
    session = _wait(h, chat_id, _idle)
    assert session["readiness"] == readiness
    assert session["seed_ready"] is False  # 0.12 is under 0.2, but the server did not complete
    h.send(chat_id, "an answer")
    session = _wait(h, chat_id, lambda s: s.get("status") == "seed_ready_to_write")
    assert session["seed_ready"] is True


def test_answers_echo_the_question_the_server_issued_not_its_display_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [
            Step("(ambiguity: 0.55) Which database?", "Which database?", code_request=False),
            Step("(ambiguity: 0.41) Next?", "Next?", code_request=False),
        ],
        tmp_path,
        project=None,
        monkeypatch=monkeypatch,
    )
    _wait(h, chat_id, _idle)
    h.send(chat_id, "SQLite")
    _wait(h, chat_id, lambda s: _idle(s) and s["last_question"] == "(ambiguity: 0.41) Next?")
    assert scripted.answers() == [("SQLite", "Which database?")]


def test_without_a_readable_state_no_question_is_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness.Harness()
    chat_id, scripted = _chat(
        h,
        [Step("(ambiguity: 0.55) Which database?", "", code_request=False)],
        tmp_path,
        project=None,
        monkeypatch=monkeypatch,
    )
    scripted.interview_view = lambda _sid: (None, None)  # type: ignore[method-assign]
    _wait(h, chat_id, _idle)
    h.send(chat_id, "SQLite")
    _wait(h, chat_id, lambda s: _idle(s) and len(scripted.answers()) == 1)
    assert scripted.answers() == [("SQLite", None)]


def test_suggest_reads_no_repository_for_a_new_project_or_a_checked_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def default_answer(self: harness.Harness, session: dict[str, Any], question: str) -> str:
        return "A sensible default."

    monkeypatch.setattr(harness.Harness, "_default_answer", default_answer)
    repo = _git_repo(tmp_path / "app", {"pyproject.toml": "[project]\n"})
    investigator = ScriptedInvestigator({})
    h = harness.Harness()
    new_id, _ = _chat(
        h,
        [Step("Who uses it?", "Who uses it?")],
        repo,
        project="new",
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    _wait(h, new_id, _idle)
    h.action(new_id, "suggest", {})
    session = _wait(h, new_id, lambda s: (s.get("suggestion") or {}).get("text"))
    assert session["suggestion"]["source"] == "model"
    assert investigator.asked == []

    existing_id, _ = _chat(
        h,
        [Step("Who uses it?", "Who uses it?")],
        repo,
        investigator=investigator,
        monkeypatch=monkeypatch,
    )
    _wait(h, existing_id, lambda s: _idle(s) and investigator.asked)
    assert investigator.asked == ["Who uses it?"]  # the automatic check, not found
    h.action(existing_id, "suggest", {})
    session = _wait(h, existing_id, lambda s: (s.get("suggestion") or {}).get("text"))
    assert session["suggestion"]["source"] == "model"
    assert investigator.asked == ["Who uses it?"]  # not read a second time


def test_message_api_refuses_a_non_string_correction_target() -> None:
    from ouroboros.gui import api

    state = api.ConsoleState(stop=lambda *_args: None)
    with pytest.raises(api.ApiError) as raised:
        api.post_chat_message(state, "abc", query={}, body={"text": "x", "corrects": 5})
    assert raised.value.status == 400
