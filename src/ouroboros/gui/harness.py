"""Chat harness: drive Ouroboros from the console the way Goose drives an agent.

A conversation is the Ouroboros pipeline told as a chat:

* **Interview** mode asks Socratic questions in the thread (``ouroboros_interview``),
  writes the spec when you are ready (``ouroboros_generate_seed``), then runs it
  (``ouroboros_start_execute_seed``) and follows the run's board until it ends.
  It is a brownfield interview only when the chat was started for an existing
  project folder. Then each question the server marks as answerable from code
  goes to the repository investigator first, and the answer comes back for
  one-click confirmation, or is sent at once when the skill's PATH 1a allows it
  (``gui/interview_assist.py``).
* **Auto** mode hands the goal to ``ouroboros auto`` and streams its output.

The handlers are the same objects the CLI and MCP server build, called in-process
on one background event loop, so every turn lands in the EventStore exactly as
an ``ooo`` session would. Conversations are saved to
``~/.ouroboros/gui/chats/<id>.json``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterator
import contextlib
import contextvars
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Literal, cast
import uuid

import yaml

from ouroboros.gui import workspace
from ouroboros.gui.interview_assist import (
    AUTO_CONFIRMED_PREFIX,
    CONFIRMED_PREFIX,
    MAX_ASSISTED_IN_A_ROW,
    RecordingInterviewHandler,
    auto_confirm_source,
    load_interview_view,
)
from ouroboros.gui.jobs import JobRegistry, ouroboros_command

_MAX_LOG_LINES = 400
_RUN_POLL_SEC = 2.0
_TERMINAL_RUN = frozenset({"completed", "failed", "cancelled"})
# What an Auto result needs from `ouroboros auto`'s output. The job reads them
# from every line as it arrives (``JobRegistry.start(watch=...)``), so a long
# run cannot push them out of the job's bounded log.
_AUTO_WATCH = {
    "execution_id": re.compile(r"\b(exec_[A-Za-z0-9_-]{6,})\b"),
    "handoff_status": re.compile(r"^\s*Run handoff status:\s*(\S+)\s*$"),
}

# The project folder of the chat whose interview or Seed call this task runs.
# Handlers are shared by every chat on the same agent; each call runs as its
# own task, so each sees its own chat's folder.
_CHAT_FOLDER: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gui_chat_folder", default=None
)


class Refusal(ValueError):
    """A request the console refuses, with a stable code the page translates.

    ``extra`` carries the values the page's sentence needs (a folder, an agent).
    """

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


def chats_dir() -> Path:
    from ouroboros.config.models import get_config_dir

    path = get_config_dir() / "gui" / "chats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> float:
    return time.time()


def _msg(role: str, kind: str, text: str = "", **data: Any) -> dict[str, Any]:
    """A transcript entry. A ``key`` names a fixed sentence the page translates; ``text`` is its English."""
    return {
        "id": uuid.uuid4().hex[:10],
        "role": role,
        "kind": kind,
        "text": text,
        "ts": _now(),
        **data,
    }


class _Loop:
    """One asyncio loop on a daemon thread for every handler call."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="gui-harness", daemon=True
        )
        self.thread.start()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(_as_console_surface(coro), self.loop)


class _FolderAdapter:
    """The LLM adapter an authoring handler builds for itself, run in the chat's folder.

    ``InterviewHandler`` and ``GenerateSeedHandler`` build their adapter without a
    ``cwd``, so the agent CLI would start in the console's launch folder and load
    that folder's CLAUDE.md, AGENTS.md and settings. This builds the same adapter
    (the role's backend, the one sealed turn the handler asks for) with the
    calling chat's folder (``_CHAT_FOLDER``) as ``cwd``. It is built for each
    completion, from the settings of that moment, as the handler's own adapter
    is built for each call.
    """

    def __init__(self, role: Literal["interview", "seed_generation"], backend: str | None) -> None:
        self.role = role
        self.backend = backend

    def resolved_backend(self) -> str:
        from ouroboros.config import get_llm_backend_for_role

        return get_llm_backend_for_role(self.role, explicit_backend=self.backend)

    def seals_tools(self) -> bool:
        """Whether the handler would seal this backend's tools (it then drops tool cues)."""
        from ouroboros.backends import backend_supports_tool_envelope
        from ouroboros.providers import resolve_llm_backend

        return backend_supports_tool_envelope(resolve_llm_backend(self.resolved_backend()))

    def build(self) -> Any:
        from ouroboros.providers import create_llm_adapter

        backend = self.resolved_backend()
        sealed = self.seals_tools()
        cwd = _CHAT_FOLDER.get()
        if self.role == "interview":
            return create_llm_adapter(
                backend=backend,
                max_turns=1,
                use_case="interview",
                allowed_tools=[] if sealed else None,
                strict_mcp_config=True,
                cwd=cwd,
            )
        return create_llm_adapter(
            backend=backend,
            max_turns=1,
            allowed_tools=[] if sealed else None,
            cwd=cwd,
        )

    async def complete(self, messages: Any, config: Any) -> Any:
        return await self.build().complete(messages, config)


class _Handlers:
    """Pipeline handlers for one backend, wired like ``ouroboros auto`` wires them.

    ``backend=None`` follows ``~/.ouroboros/config.yaml``; a named backend runs
    every stage of this conversation on that agent (models still come from the
    Agents & models settings for that backend). The interview and Seed agents
    run in the calling chat's folder (``_CHAT_FOLDER``).
    """

    def __init__(self, backend: str | None = None) -> None:
        self.backend = backend
        self._built = False
        self.interview: Any = None
        self.seed: Any = None

    def ensure(self) -> None:
        if self._built:
            self._match_prompt_mode()
            return
        from ouroboros.auto.adapters import (
            HandlerInterviewBackend,
            HandlerRunStarter,
            HandlerSeedGenerator,
        )
        from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler, InterviewHandler
        from ouroboros.mcp.tools.execution_handlers import (
            ExecuteSeedHandler,
            StartExecuteSeedHandler,
        )
        from ouroboros.mcp.tools.run_successors import build_run_successor_handler

        backend = effective_runtime(self.backend)
        llm = self.backend if self.backend and _supports_llm(self.backend) else None
        self._interview_adapter = _FolderAdapter("interview", llm)
        self._interview_inner = InterviewHandler(
            llm_backend=llm, agent_runtime_backend=backend, llm_adapter=self._interview_adapter
        )
        self._match_prompt_mode()
        self.interview_handler = RecordingInterviewHandler(self._interview_inner)
        self.interview = HandlerInterviewBackend(cast(Any, self.interview_handler), cwd=os.getcwd())
        self.seed = HandlerSeedGenerator(
            GenerateSeedHandler(
                llm_backend=llm,
                agent_runtime_backend=backend,
                llm_adapter=_FolderAdapter("seed_generation", llm),
            )
        )
        execute = ExecuteSeedHandler(llm_backend=llm, agent_runtime_backend=backend)
        self.start_handler = StartExecuteSeedHandler(
            execute_handler=execute,
            agent_runtime_backend=backend,
            start_evaluate_handler=build_run_successor_handler(agent_runtime_backend=backend),
        )
        self._run_starter = HandlerRunStarter
        self._built = True

    def _match_prompt_mode(self) -> None:
        """Ask without tool cues exactly when the handler would for its own adapter.

        The handler keeps an injected adapter's caller-chosen prompt mode
        (#1537); this adapter is the one the handler would build, so it gets
        the mode the handler picks for that one. Refreshed before every call.
        """
        self._interview_inner.suppress_tool_use_prompt_cues = self._interview_adapter.seals_tools()

    def run_starter(self, cwd: str) -> Any:
        self.ensure()
        return self._run_starter(self.start_handler, cwd=cwd)

    def code_request(self, interview_session_id: str) -> dict[str, Any] | None:
        """The server's code-fact request on this interview's latest question, if any."""
        return self.interview_handler.code_request(interview_session_id) if self._built else None

    def interview_view(self, interview_session_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """The completion gate and the issued question, as the server persisted them."""
        if not self._built:
            return None, None
        return load_interview_view(
            self.interview_handler.resolved_state_dir(), interview_session_id
        )


def effective_runtime(backend: str | None, *, public: bool = False) -> str | None:
    """The runtime this process can actually execute for ``backend``.

    The Claude Agent SDK cannot share an interpreter with MCP 2 (the console's
    profile), so Claude work runs on the Claude Code CLI worker (``claude-cli``)
    when the SDK is missing — the same choice ``ooo setup`` makes for MCP hosts.
    ``None`` keeps following config.yaml unless that resolves to SDK-less Claude.
    ``public=True`` returns the CLI spelling (for ``ouroboros auto --runtime``).
    """
    import importlib.util

    from ouroboros.package_profiles import CLAUDE_CLI_PROFILE, public_runtime_backend

    name = backend
    if name is None:
        try:
            from ouroboros.config.loader import get_agent_runtime_backend

            name = get_agent_runtime_backend()
        except Exception:
            return None
    lowered = name.strip().lower()
    if (
        lowered in ("claude", "claude_code", "claude-sdk")
        and importlib.util.find_spec("claude_agent_sdk") is None
    ):
        return CLAUDE_CLI_PROFILE if public else public_runtime_backend(CLAUDE_CLI_PROFILE)
    if backend is None:
        return None
    return backend if public else public_runtime_backend(backend)


def _supports_llm(backend: str) -> bool:
    from ouroboros.backends import get_backend_capability

    capability = get_backend_capability(backend)
    return bool(capability and capability.supports_llm)


_ASK_SYSTEM = (
    "You are the chat assistant inside the Ouroboros console. Answer the user's "
    "questions directly and concisely. When they describe something to build, you may "
    "suggest starting an Interview chat so Ouroboros can write and run a spec."
)


class Harness:
    """Conversations, their background work, and live subscribers.

    ``jobs`` is the console's job registry, so the ``ouroboros auto`` jobs chats
    start are listed in Activity with every other job the console runs.
    """

    def __init__(self, jobs: JobRegistry | None = None) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._loop: _Loop | None = None
        self._handlers: dict[str, _Handlers] = {}
        # One repository investigator per chat, so its call cap and the answers
        # it already found carry across the chat's questions.
        self._investigators: dict[str, Any] = {}
        self._jobs = jobs if jobs is not None else JobRegistry()
        self._load_all()

    # ── persistence ─────────────────────────────────────────────────

    def _load_all(self) -> None:
        for path in sorted(chats_dir().glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("id"), str):
                if data.get("status") in ("thinking", "running"):
                    # The process that owned this work is gone.
                    data["status"] = "interrupted"
                if isinstance(data.get("suggestion"), dict) and data["suggestion"].get("pending"):
                    data["suggestion"] = {"pending": False}
                self._sessions[data["id"]] = data

    def _save(self, session: dict[str, Any]) -> None:
        path = chats_dir() / f"{session['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(session, default=str, indent=1), encoding="utf-8")
        tmp.replace(path)

    def _touch(self, session: dict[str, Any]) -> None:
        session["updated_at"] = _now()
        session["version"] = int(session.get("version", 0)) + 1
        self._save(session)
        self._changed.notify_all()

    # ── public API ──────────────────────────────────────────────────

    def defaults(self) -> dict[str, Any]:
        """Folder, agents and providers the new-chat form offers."""
        from ouroboros.backends import llm_backend_choices
        from ouroboros.backends.model_catalog import installed_backends
        from ouroboros.gui.settings import SettingsModel, static_models

        installed = installed_backends()
        settings = SettingsModel()
        execute_agent = settings.effective_stage_backend(settings_stage("execute"))
        agents = [
            {"id": name, "installed": bool(path), "models": static_models(name)}
            for name, path in installed.items()
            if name not in ("host", "codex_mcp", "claude_mcp")
        ]
        llm = [
            {
                "id": name,
                "installed": bool(installed.get(name)) or name == "litellm",
                "models": static_models(name),
            }
            for name in llm_backend_choices()
            if name not in ("ourocode", "dsh", "megacode")
        ]
        try:
            from ouroboros.gui.providers import view as providers_view

            providers = [
                {"id": row["id"], "label": row["label"], "model_format": row["model_format"]}
                for row in providers_view()["providers"]
                if row["configured"]
            ]
        except Exception:
            providers = []
        return {
            "cwd": os.getcwd(),
            "home": str(Path.home()),
            "default_runtime": settings.effective_default_runtime(),
            "execute_agent": execute_agent,
            "agents": agents,
            "llm": llm,
            "providers": providers,
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                {
                    "id": s["id"],
                    "title": s.get("title") or "Untitled",
                    "mode": s.get("mode"),
                    "backend": s.get("backend"),
                    "model": s.get("model"),
                    "status": s.get("status"),
                    "cwd": s.get("cwd"),
                    "updated_at": s.get("updated_at"),
                    "created_at": s.get("created_at"),
                }
                for s in self._sessions.values()
            ]
        return sorted(rows, key=lambda r: r.get("updated_at") or 0, reverse=True)

    def get(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return json.loads(json.dumps(session, default=str)) if session else None

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._investigators.pop(session_id, None)
            with contextlib.suppress(OSError):
                (chats_dir() / f"{session_id}.json").unlink()
            self._changed.notify_all()

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise Refusal("goal_missing", "Say what you want to build.")
        cwd = str(payload.get("cwd") or "").strip() or os.getcwd()
        cwd = str(Path(cwd).expanduser())
        if not Path(cwd).is_dir():
            raise Refusal("folder_missing", f"Folder not found: {cwd}", folder=cwd)
        mode = payload.get("mode") if payload.get("mode") in ("auto", "ask") else "interview"
        project = _interview_project(payload.get("project"), cwd) if mode == "interview" else None
        backend = str(payload.get("backend") or "").strip() or None
        model = str(payload.get("model") or "").strip() or None
        if backend:
            _validate_backend(backend, mode)
            _require_installed(backend)
        session_id = uuid.uuid4().hex[:12]
        session: dict[str, Any] = {
            "id": session_id,
            "title": goal.splitlines()[0][:80],
            "goal": goal,
            "cwd": cwd,
            "mode": mode,
            "project": project,
            "backend": backend,
            "model": model,
            "status": "thinking",
            "created_at": _now(),
            "updated_at": _now(),
            "version": 0,
            "messages": [_msg("user", "text", goal)],
        }
        with self._lock:
            self._sessions[session_id] = session
            self._touch(session)
        if mode == "auto":
            self._start_auto(session_id)
        elif mode == "ask":
            self._spawn(session_id, self._ask(session_id))
        else:
            self._spawn(session_id, self._interview_start(session_id))
        return self.get(session_id) or session

    def send(self, session_id: str, text: str, *, corrects: str | None = None) -> dict[str, Any]:
        """Send what the user typed. ``corrects`` names an auto-confirmed answer it corrects."""
        text = text.strip()
        if not text:
            raise Refusal("answer_empty", "Type an answer first.")
        with self._lock:
            session = self._require(session_id)
            ask = session.get("mode") == "ask"
            ready = (
                ("waiting_answer", "done", "failed", "interrupted") if ask else ("waiting_answer",)
            )
            if session.get("status") not in ready:
                raise Refusal("not_waiting", "Ouroboros is not waiting for a message right now.")
            payload = text
            extra: dict[str, Any] = {}
            if corrects and not ask:
                payload = _correction_payload(session, corrects, text)
                extra["corrects"] = corrects
            session["messages"].append(_msg("user", "text", text, **extra))
            session["status"] = "thinking"
            session["suggestion"] = {"pending": False}
            # A human answer resets the skill's Dialectic Rhythm Guard.
            session["assisted_in_a_row"] = 0
            self._touch(session)
        if ask:
            self._spawn(session_id, self._ask(session_id))
        else:
            self._spawn(session_id, self._interview_answer(session_id, payload))
        return self.get(session_id) or {}

    def action(self, session_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            status = session.get("status")
            if action == "seed":
                if status not in ("waiting_answer", "seed_ready_to_write", "interrupted", "failed"):
                    raise Refusal("interview_busy", "The interview is still working.")
                if not session.get("interview_session_id"):
                    raise Refusal("no_interview", "There is no interview to turn into a spec yet.")
                session["status"] = "thinking"
                session["messages"].append(
                    _msg("system", "note", "Writing the spec…", key="writing_spec")
                )
                self._touch(session)
                coro = self._write_seed(session_id, force=bool(payload.get("force")))
            elif action == "run":
                if not session.get("seed_yaml"):
                    raise Refusal("no_spec", "Write the spec before running it.")
                if status == "running":
                    raise Refusal("already_running", "This spec is already running.")
                prepare = payload.get("prepare_git", False)
                if not isinstance(prepare, bool):
                    raise Refusal("bad_prepare_git", "prepare_git must be true or false.")
                self._ready_folder(session, prepare=prepare)
                session["status"] = "running"
                self._touch(session)
                coro = self._start_run(session_id)
            elif action == "suggest":
                if status != "waiting_answer" or not session.get("last_question"):
                    raise Refusal("no_question", "There is no open question to answer.")
                current = session.get("suggestion") or {}
                if current.get("pending"):
                    raise Refusal("suggestion_pending", "A suggestion is already being drafted.")
                question = str(session["last_question"])
                # A new project has no repository to read, and a question the
                # repository was already checked for gets the default at once.
                use_repo = session.get("project") != "new" and not (
                    current.get("repo_checked") and current.get("question") == question
                )
                session["suggestion"] = {"pending": True, "question": question}
                self._touch(session)
                self._spawn_quiet(session_id, self._suggest(session_id, question, use_repo))
                return self.get(session_id) or {}
            elif action == "accept":
                suggestion = session.get("suggestion") or {}
                question = session.get("last_question")
                if (
                    status != "waiting_answer"
                    or suggestion.get("pending")
                    or not suggestion.get("text")
                    or suggestion.get("question") != question
                ):
                    raise Refusal("no_suggestion", "There is no suggested answer to send.")
                text = str(suggestion["text"])
                repo = suggestion.get("source") == "repo"
                session["messages"].append(
                    _msg(
                        "user",
                        "text",
                        text,
                        via="repo_confirmed" if repo else "suggested",
                        evidence=list(suggestion.get("evidence") or []),
                        question=question,
                    )
                )
                session["status"] = "thinking"
                session["suggestion"] = {"pending": False}
                # Accepting a repository fact is not a judgment; picking a
                # drafted answer to a product question is.
                session["assisted_in_a_row"] = (
                    int(session.get("assisted_in_a_row") or 0) + 1 if repo else 0
                )
                self._touch(session)
                coro = self._interview_answer(
                    session_id, f"{CONFIRMED_PREFIX} {text}" if repo else text
                )
            elif action == "dismiss":
                suggestion = session.get("suggestion") or {}
                if not suggestion.get("text") or suggestion.get("question") != session.get(
                    "last_question"
                ):
                    raise Refusal("no_suggestion", "There is no suggested answer to dismiss.")
                session["suggestion"] = {
                    "pending": False,
                    "question": suggestion.get("question"),
                    "repo_checked": bool(suggestion.get("repo_checked"))
                    or suggestion.get("source") == "repo",
                }
                self._touch(session)
                return self.get(session_id) or {}
            elif action == "stop":
                job_id = session.get("auto_job_id")
                job = self._jobs.get(job_id) if job_id else None
                if job is None or job.status != "running":
                    raise Refusal("nothing_to_stop", "Nothing to stop.")
                session["messages"].append(
                    _msg("system", "note", "Stop requested.", key="stop_requested")
                )
                self._touch(session)
                self._terminate_job(job_id)
                return self.get(session_id) or {}
            else:
                raise Refusal("unknown_action", f"Unknown action: {action}", action=action)
        self._spawn(session_id, coro)
        return self.get(session_id) or {}

    def _ready_folder(self, session: dict[str, Any], *, prepare: bool) -> None:
        """Refuse a hand-off the folder cannot take, or prepare the folder when asked to.

        Runs under the lock, so two clicks cannot prepare the same folder twice.
        """
        cwd = str(session["cwd"])
        state = workspace.git_state(cwd)
        if state.needs_prepare and prepare:
            try:
                done = workspace.prepare_git(cwd)
            except workspace.GitPrepareError as exc:
                raise Refusal("git_prepare_failed", str(exc), detail=str(exc)) from exc
            if done != "unchanged":
                session["messages"].append(
                    _msg(
                        "system",
                        "note",
                        "Made this folder a git repository with an empty first commit."
                        if done == "initialized"
                        else "Added an empty first commit to this git repository.",
                        key=f"git_{done}",
                    )
                )
            return
        problem = workspace.workspace_problem(state)
        if state.needs_prepare:
            raise Refusal("needs_git", problem or "", reason=state.kind)
        if problem:
            raise Refusal("git_unreadable", problem, detail=state.detail)

    def folder(self, path: str) -> dict[str, Any]:
        """What the new-chat form shows under the folder field. Reads only; creates nothing."""
        expanded = Path(path).expanduser()
        if not expanded.is_dir():
            return {"path": str(expanded), "exists": False, "git": None}
        state = workspace.git_state(str(expanded))
        return {"path": str(expanded), "exists": True, "git": state.kind, "detail": state.detail}

    def subscribe(
        self, session_id: str
    ) -> tuple[Iterator[dict[str, Any] | None], Callable[[], None]]:
        with self._lock:
            self._require(session_id)
        closed = threading.Event()

        def gen() -> Iterator[dict[str, Any] | None]:
            last = -1
            while not closed.is_set():
                with self._lock:
                    session = self._sessions.get(session_id)
                    if session is None:
                        return
                    version = int(session.get("version", 0))
                    if version == last:
                        self._changed.wait(timeout=15)
                        session = self._sessions.get(session_id)
                        if session is None:
                            return
                        version = int(session.get("version", 0))
                    snapshot = (
                        json.loads(json.dumps(session, default=str)) if version != last else None
                    )
                last = version
                yield snapshot

        return gen(), closed.set

    # ── internals ───────────────────────────────────────────────────

    def _require(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def _loop_ready(self) -> _Loop:
        if self._loop is None:
            self._loop = _Loop()
        return self._loop

    def _spawn(self, session_id: str, coro: Coroutine[Any, Any, None]) -> None:
        future = self._loop_ready().submit(coro)

        def done(fut: Any) -> None:
            exc = fut.exception()
            if exc is not None:
                self._fail(session_id, exc)

        future.add_done_callback(done)

    def _fail(self, session_id: str, exc: BaseException) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session["messages"].append(_msg("assistant", "error", str(exc) or type(exc).__name__))
            session["status"] = "failed"
            self._touch(session)

    def _update(self, session_id: str, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            fn(session)
            self._touch(session)

    # interview ---------------------------------------------------------

    def _apply_turn(
        self,
        session_id: str,
        turn: Any,
        readiness: dict[str, Any] | None = None,
        code_request: dict[str, Any] | None = None,
        issued: str | None = None,
    ) -> tuple[str, int] | None:
        """Show the server's turn; return the question to investigate and its turn, if due.

        Ready means what the server decided: it completed the interview. The
        readiness view carries the gate it applies (score, per-dimension floors,
        streak) for the meter; nothing here compares a score with a threshold.
        ``issued`` is the pending question exactly as the server stored it; the
        answer echoes it back, never the decorated text the thread shows.
        """
        investigate: tuple[str, int] | None = None

        def apply(session: dict[str, Any]) -> None:
            nonlocal investigate
            session["interview_session_id"] = turn.session_id
            # Numbers each server turn, so late work for an earlier one is dropped
            # even when the server asks the same question again.
            session["turn_seq"] = int(session.get("turn_seq") or 0) + 1
            ready = bool(turn.completed or (readiness or {}).get("complete"))
            if turn.ambiguity_score is not None:
                session["ambiguity"] = turn.ambiguity_score
            session["readiness"] = readiness
            if turn.question and not turn.completed:
                session["messages"].append(
                    _msg("assistant", "question", turn.question, ambiguity=turn.ambiguity_score)
                )
            session["last_question"] = turn.question
            session["issued_question"] = issued
            session["seed_ready"] = ready
            session["status"] = "seed_ready_to_write" if turn.completed else "waiting_answer"
            session["suggestion"] = {"pending": False}
            if (
                turn.question
                and not turn.completed
                and code_request is not None
                and session.get("project") == "existing"
                and int(session.get("assisted_in_a_row") or 0) < MAX_ASSISTED_IN_A_ROW
            ):
                session["suggestion"] = {"pending": True, "question": turn.question, "auto": True}
                asked = str(issued or code_request.get("question") or turn.question)
                investigate = (asked, session["turn_seq"])
            if turn.completed:
                session["messages"].append(
                    _msg(
                        "assistant",
                        "note",
                        "The interview is complete. Write the spec next.",
                        key="interview_complete",
                    )
                )

        self._update(session_id, apply)
        return investigate

    async def _after_turn(self, session_id: str, handlers: _Handlers, turn: Any) -> None:
        readiness, issued = await asyncio.to_thread(handlers.interview_view, turn.session_id)
        due = self._apply_turn(
            session_id, turn, readiness, handlers.code_request(turn.session_id), issued
        )
        if due is not None:
            asked, turn_seq = due
            self._spawn_quiet(session_id, self._auto_investigate(session_id, turn_seq, asked))

    def _handlers_for(self, session: dict[str, Any]) -> _Handlers:
        key = session.get("backend") or ""
        with self._lock:
            handlers = self._handlers.get(key)
            if handlers is None:
                handlers = _Handlers(key or None)
                self._handlers[key] = handlers
        return handlers

    async def _interview_start(self, session_id: str) -> None:
        session = self.get(session_id) or {}
        _in_chat_folder(session)
        handlers = self._handlers_for(session)
        await asyncio.to_thread(handlers.ensure)
        if session.get("project") == "existing":
            turn = await handlers.interview.start(session["goal"], cwd=session["cwd"])
        else:
            # The interview detects brownfield from the folder it is given, and
            # a new project has none: give it an empty one, so the launch
            # directory (often a git repository) cannot make it brownfield.
            with tempfile.TemporaryDirectory(prefix="ouroboros-new-project-") as blank:
                turn = await handlers.interview.start(session["goal"], cwd=blank)
        await self._after_turn(session_id, handlers, turn)

    async def _interview_answer(self, session_id: str, text: str) -> None:
        session = self.get(session_id) or {}
        _in_chat_folder(session)
        handlers = self._handlers_for(session)
        await asyncio.to_thread(handlers.ensure)
        # Echo the question exactly as the server issued it. Without one (the
        # state could not be read) send none: the server then records the
        # answer against the question it is waiting on.
        turn = await handlers.interview.answer(
            session["interview_session_id"], text, last_question=session.get("issued_question")
        )
        await self._after_turn(session_id, handlers, turn)

    async def _write_seed(self, session_id: str, *, force: bool) -> None:
        session = self.get(session_id) or {}
        _in_chat_folder(session)
        handlers = self._handlers_for(session)
        await asyncio.to_thread(handlers.ensure)
        seed = await handlers.seed(session["interview_session_id"], force=force)
        data = seed.to_dict()
        seed_yaml = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)

        def apply(s: dict[str, Any]) -> None:
            s["seed_yaml"] = seed_yaml
            s["status"] = "seed_ready"
            s["messages"].append(
                _msg(
                    "assistant",
                    "seed",
                    data.get("goal", ""),
                    acceptance_criteria=[
                        _item_text(c) for c in data.get("acceptance_criteria") or []
                    ],
                    constraints=[_item_text(c) for c in data.get("constraints") or []],
                    seed_yaml=seed_yaml,
                )
            )

        self._update(session_id, apply)

    async def _start_run(self, session_id: str) -> None:
        from ouroboros.core.seed import Seed

        session = self.get(session_id) or {}
        starter = await asyncio.to_thread(self._handlers_for(session).run_starter, session["cwd"])
        seed = Seed.from_dict(yaml.safe_load(session["seed_yaml"]))
        meta = await starter(seed, idempotency_key=f"gui-{session_id}")
        execution_id = meta.get("execution_id")

        def apply(s: dict[str, Any]) -> None:
            s["execution_id"] = execution_id
            s["run_job_id"] = meta.get("job_id")
            s["run_session_id"] = meta.get("session_id")
            s["messages"].append(
                _msg(
                    "assistant",
                    "run",
                    "Run started.",
                    execution_id=execution_id,
                    job_id=meta.get("job_id"),
                    run_status="running",
                    progress=None,
                )
            )

        self._update(session_id, apply)
        if execution_id:
            job_id = meta.get("job_id")
            await asyncio.to_thread(
                self._follow_run, session_id, str(execution_id), str(job_id) if job_id else None
            )

    def _follow_run(self, session_id: str, execution_id: str, job_id: str | None = None) -> None:
        """Mirror the run's board into its chat card until the run or its job ends."""
        from ouroboros.dashboard_web.kanban import reduce_board
        from ouroboros.dashboard_web.reader import (
            EventTail,
            PickerIndexContractError,
            default_db_path,
        )

        db_path = str(default_db_path())
        tail = EventTail(db_path, execution_id)
        events: list[dict[str, Any]] = []
        last_summary = ""
        while True:
            with self._lock:
                if session_id not in self._sessions:
                    return
            try:
                events.extend(tail.fetch_new())
            except PickerIndexContractError:
                # The run's start row is indexed a moment after the job starts.
                pass
            board = reduce_board(events, execution_id=execution_id) if events else {}
            summary = _board_summary(board, events)
            job = _job_terminal(db_path, job_id) if job_id else None
            if (
                job is not None
                and job["status"] == "failed"
                and summary["status"] not in _TERMINAL_RUN
            ):
                summary = {
                    **summary,
                    "status": "failed",
                    "activity": job["message"],
                    "key": "job-failed",
                }
            if summary["key"] != last_summary:
                last_summary = summary["key"]

                def apply(s: dict[str, Any], summary: dict[str, Any] = summary) -> None:
                    for message in reversed(s["messages"]):
                        if (
                            message.get("kind") == "run"
                            and message.get("execution_id") == execution_id
                        ):
                            message["run_status"] = summary["status"]
                            message["progress"] = summary
                            break
                    if summary["status"] in _TERMINAL_RUN:
                        s["status"] = "done" if summary["status"] == "completed" else "failed"
                        if summary["status"] != "completed" and summary.get("activity"):
                            s["messages"].append(
                                _msg("assistant", "error", str(summary["activity"]))
                            )
                        result = _run_result(str(s.get("cwd") or ""), s.get("run_session_id"))
                        if result:
                            s["messages"].append(_msg("assistant", "result", "", **result))

                self._update(session_id, apply)
            if summary["status"] in _TERMINAL_RUN:
                return
            time.sleep(_RUN_POLL_SEC)

    # suggested answers ---------------------------------------------------

    def _spawn_quiet(self, session_id: str, coro: Coroutine[Any, Any, None]) -> None:
        """Like ``_spawn`` but a failure only clears the suggestion, not the chat."""
        future = self._loop_ready().submit(coro)

        def done(fut: Any) -> None:
            exc = fut.exception()
            if exc is None:
                return

            def apply(s: dict[str, Any]) -> None:
                s["suggestion"] = {"pending": False, "error": str(exc) or type(exc).__name__}

            self._update(session_id, apply)

        future.add_done_callback(done)

    async def _repo_investigator(self, session: dict[str, Any]) -> Any | None:
        """This chat's repository investigator (the one ``ooo auto`` uses), or ``None``."""
        with self._lock:
            if session["id"] in self._investigators:
                return self._investigators[session["id"]]
        investigator = None
        try:
            from ouroboros.auto.investigator import build_repo_investigator
            from ouroboros.config.loader import get_agent_runtime_backend

            runtime = session.get("backend") or await asyncio.to_thread(get_agent_runtime_backend)
            investigator = build_repo_investigator(runtime)
        except Exception:
            investigator = None
        with self._lock:
            return self._investigators.setdefault(session["id"], investigator)

    async def _investigate(self, session: dict[str, Any], question: str) -> Any | None:
        investigator = await self._repo_investigator(session)
        if investigator is None:
            return None
        try:
            return await investigator.investigate(session["goal"], question, Path(session["cwd"]))
        except Exception:
            return None

    async def _auto_investigate(self, session_id: str, turn_seq: int, asked: str) -> None:
        """Answer a code-fact question from the repository before the human has to.

        A found answer is offered for one-click confirmation; one PATH 1a allows
        (an exact manifest or config match) is sent at once with a visible
        "auto-confirmed" note the user can correct. A question the repository
        does not settle, or a product decision, stays with the human.
        """
        session = self.get(session_id) or {}
        investigation = await self._investigate(session, asked)
        found = bool(investigation is not None and investigation.found and investigation.answer)
        workspace = Path(str(session.get("cwd") or "."))
        source = await asyncio.to_thread(auto_confirm_source, investigation, workspace)
        submit: str | None = None

        def apply(s: dict[str, Any]) -> None:
            nonlocal submit
            if s.get("turn_seq") != turn_seq or s.get("status") != "waiting_answer":
                return  # the user answered first, or the interview moved on
            shown = s.get("last_question")
            if not found:
                s["suggestion"] = {"pending": False, "question": shown, "repo_checked": True}
                return
            answer = str(investigation.answer).strip()
            evidence = [_span_text(span) for span in investigation.checked][:5]
            assisted = int(s.get("assisted_in_a_row") or 0)
            if source is None or assisted >= MAX_ASSISTED_IN_A_ROW:
                s["suggestion"] = {
                    "pending": False,
                    "question": shown,
                    "text": answer,
                    "source": "repo",
                    "evidence": evidence,
                    "auto": True,
                    "repo_checked": True,
                }
                return
            s["messages"].append(
                _msg(
                    "user",
                    "text",
                    answer,
                    via="auto_confirmed",
                    source=source,
                    evidence=evidence,
                    question=shown,
                )
            )
            s["suggestion"] = {"pending": False}
            s["status"] = "thinking"
            s["assisted_in_a_row"] = assisted + 1
            submit = f"{AUTO_CONFIRMED_PREFIX} {answer}"

        self._update(session_id, apply)
        if submit is not None:
            self._spawn(session_id, self._interview_answer(session_id, submit))

    async def _suggest(self, session_id: str, question: str, use_repo: bool = True) -> None:
        """Draft an answer: from the repository when it settles the question, else a default."""
        session = self.get(session_id) or {}
        suggestion: dict[str, Any] = {"pending": False, "question": question}
        asked = str(session.get("issued_question") or question)
        investigation = await self._investigate(session, asked) if use_repo else None
        if investigation is not None and investigation.found and investigation.answer:
            suggestion.update(
                text=investigation.answer,
                source="repo",
                evidence=[_span_text(span) for span in investigation.checked][:5],
            )
        else:
            suggestion.update(text=await self._default_answer(session, question), source="model")

        def apply(s: dict[str, Any]) -> None:
            if s.get("last_question") == question:
                s["suggestion"] = suggestion

        self._update(session_id, apply)

    async def _default_answer(self, session: dict[str, Any], question: str) -> str:
        from ouroboros.config.loader import get_llm_model_for_role
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole
        from ouroboros.providers.factory import create_llm_adapter

        backend = (
            session.get("backend")
            if session.get("backend") and _supports_llm(session["backend"])
            else None
        )
        model = await asyncio.to_thread(get_llm_model_for_role, "interview", backend=backend)
        adapter = await asyncio.to_thread(
            create_llm_adapter, backend=backend, cwd=session["cwd"], allowed_tools=[], max_turns=1
        )
        transcript = []
        for message in session.get("messages", []):
            if message.get("kind") == "question":
                transcript.append(f"Q: {message.get('text')}")
            elif message.get("kind") == "text" and message.get("role") == "user":
                transcript.append(f"A: {message.get('text')}")
        prompt = (
            f"Goal: {session.get('goal')}\n\nInterview so far:\n"
            + "\n".join(transcript[-12:])
            + f"\n\nOpen question: {question}\n\n"
            "Draft the answer a pragmatic requester would most likely give: one to three "
            "sentences, concrete, the conventional default when unsure. Reply with the answer only."
        )
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content=prompt)],
            CompletionConfig(model=str(model), max_tokens=400),
        )
        if result.is_err:
            raise RuntimeError(str(result.error))
        return result.value.content.strip()

    # ask -----------------------------------------------------------------

    async def _ask(self, session_id: str) -> None:
        """One reply from the chosen backend and model (no tools, no file edits)."""
        from ouroboros.config.loader import get_llm_model_for_role
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole
        from ouroboros.providers.factory import create_llm_adapter

        session = self.get(session_id) or {}
        backend = session.get("backend") or None
        model = session.get("model") or await asyncio.to_thread(
            get_llm_model_for_role, "interview", backend=backend
        )

        def on_message(kind: str, text: str) -> None:
            line = f"{kind}: {text}".strip()[:300]

            def apply(s: dict[str, Any]) -> None:
                s["activity"] = (s.get("activity", []) + [line])[-8:]

            self._update(session_id, apply)

        adapter = await asyncio.to_thread(
            create_llm_adapter,
            backend=backend,
            cwd=session["cwd"],
            allowed_tools=[],
            max_turns=1,
            on_message=on_message,
        )
        history = [Message(role=MessageRole.SYSTEM, content=_ASK_SYSTEM)]
        for message in session.get("messages", []):
            if message.get("kind") != "text":
                continue
            role = MessageRole.USER if message.get("role") == "user" else MessageRole.ASSISTANT
            history.append(Message(role=role, content=str(message.get("text") or "")))
        result = await adapter.complete(history, CompletionConfig(model=str(model)))
        if result.is_err:
            raise RuntimeError(str(result.error))
        reply = result.value
        shown = getattr(reply, "model", model)
        hint = await asyncio.to_thread(_default_model_hint, backend, shown)

        def apply(s: dict[str, Any]) -> None:
            s["messages"].append(_msg("assistant", "text", reply.content, model=shown, **hint))
            s["activity"] = []
            s["status"] = "waiting_answer"

        self._update(session_id, apply)

    # auto ----------------------------------------------------------------

    def _start_auto(self, session_id: str) -> None:
        session = self.get(session_id) or {}
        job = self._jobs.start(
            kind="auto",
            title=session.get("title", "auto"),
            argv=ouroboros_command(
                "auto",
                session["goal"],
                *(
                    ["--runtime", runtime]
                    if (runtime := effective_runtime(session.get("backend"), public=True))
                    else []
                ),
            ),
            meta={"key": session_id},
            cwd=session["cwd"],
            watch=_AUTO_WATCH,
        )

        def apply(s: dict[str, Any]) -> None:
            s["auto_job_id"] = job.id
            s["status"] = "running"
            s["messages"].append(
                _msg("assistant", "log", "ouroboros auto", lines=[], job_id=job.id)
            )

        self._update(session_id, apply)
        threading.Thread(
            target=self._follow_auto, args=(session_id, job.id), name="gui-auto", daemon=True
        ).start()

    def _follow_auto(self, session_id: str, job_id: str) -> None:
        cursor = 0
        while True:
            job = self._jobs.get(job_id)
            if job is None:
                return
            # Read the status first: once it is final, every line is already in.
            running = job.status == "running"
            snap = job.snapshot(since=cursor)
            cursor = snap["cursor"]
            if snap["lines"]:

                def apply(
                    s: dict[str, Any],
                    lines: list[str] = snap["lines"],
                    found: dict[str, str] = snap["found"],
                ) -> None:
                    for message in reversed(s["messages"]):
                        if message.get("kind") == "log" and message.get("job_id") == job_id:
                            message["lines"] = (message.get("lines", []) + lines)[-_MAX_LOG_LINES:]
                            break
                    if found.get("execution_id"):
                        s["execution_id"] = found["execution_id"]

                self._update(session_id, apply)
            if not running:
                self._finish_auto(session_id, snap, cancelled=job.cancelled)
                return
            time.sleep(0.5)

    def _finish_auto(self, session_id: str, snap: dict[str, Any], *, cancelled: bool) -> None:
        """End an Auto chat with what it produced: outcome, run board, folder and changes.

        The execution id and handoff status come from what the job recorded from
        every output line (``snap["found"]``), not from its bounded log.
        """
        cwd = str((self.get(session_id) or {}).get("cwd") or "")
        changes = workspace.folder_changes(cwd) if cwd else None
        found: dict[str, str] = snap.get("found") or {}
        execution_id = found.get("execution_id")
        handoff = found.get("handoff_status")

        def finish(s: dict[str, Any]) -> None:
            ok = snap["status"] == "succeeded"
            s["status"] = "done" if ok else "failed"
            if ok:
                s["messages"].append(
                    _msg("assistant", "note", "ouroboros auto finished.", key="auto_finished")
                )
            else:
                code = snap["exit_code"]
                s["messages"].append(
                    _msg(
                        "assistant",
                        "error",
                        f"ouroboros auto exited with {code}.",
                        key="auto_exited",
                        vars={"code": code},
                    )
                )
            if execution_id:
                s["execution_id"] = execution_id
            s["messages"].append(
                _msg(
                    "assistant",
                    "result",
                    "",
                    source="auto",
                    outcome=_auto_outcome(snap["status"], cancelled, handoff),
                    handoff_status=handoff,
                    execution_id=execution_id,
                    folder=cwd,
                    in_repo=changes is not None,
                    files=list(changes.files) if changes else [],
                    file_count=changes.count if changes else 0,
                )
            )

        self._update(session_id, finish)

    def _terminate_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.cancel()


def _in_chat_folder(session: dict[str, Any]) -> None:
    """Run this task's interview and Seed agents in the chat's folder.

    Called at the top of a coroutine the harness runs as its own task, so the
    setting stays with that task (and the work it starts), not other chats.
    """
    _CHAT_FOLDER.set(str(session.get("cwd") or "") or None)


def _interview_project(choice: object, cwd: str) -> str:
    """``existing`` only when the user chose it for a folder the interview can read.

    The default is a new project: the console is usually launched from wherever
    it was installed or from a login service, so its launch directory says
    nothing about what the user is building. An omitted choice is that default;
    any other value than ``new`` or ``existing`` is refused, not guessed.
    """
    if choice is None or choice == "" or choice == "new":
        return "new"
    if choice != "existing":
        raise Refusal(
            "project_kind_unknown",
            f"Unknown project kind: {choice!r}. Use 'new' or 'existing'.",
            kind=str(choice),
        )
    from ouroboros.bigbang.explore import detect_brownfield

    if not detect_brownfield(cwd):
        raise Refusal(
            "not_a_project",
            f"{cwd} has no git repository or project manifest, so the interview cannot "
            "read it as an existing project. Choose the project's folder, or start a new project.",
            folder=cwd,
        )
    return "existing"


def _correction_payload(session: dict[str, Any], message_id: str, text: str) -> str:
    """The interview answer that corrects an earlier auto-confirmed one."""
    target = next(
        (
            m
            for m in session.get("messages", [])
            if m.get("id") == message_id and m.get("via") == "auto_confirmed"
        ),
        None,
    )
    if target is None:
        raise Refusal("bad_correction", "Only an auto-confirmed answer can be corrected.")
    target["corrected"] = True
    return (
        "[from-user] Correction: an earlier answer was auto-confirmed from the repository "
        f"({target.get('source')}) and is wrong.\n"
        f"Earlier question: {target.get('question')}\n"
        f"Auto-confirmed answer: {target.get('text')}\n"
        f"Correct answer: {text}\n"
        "This corrects the earlier answer; it does not answer the current question."
    )


def _run_result(cwd: str, run_session_id: object) -> dict[str, Any] | None:
    """Where a finished run left its work: the task worktree, its branch and changed files."""
    import subprocess

    if not cwd or not isinstance(run_session_id, str) or not run_session_id:
        return None
    try:
        listing = subprocess.run(  # noqa: S603 - fixed git arguments
            ["git", "-C", cwd, "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    worktree = branch = None
    for block in listing.split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        if run_session_id in fields.get("worktree", ""):
            worktree = fields["worktree"]
            branch = fields.get("branch", "").removeprefix("refs/heads/") or None
            break
    if worktree is None:
        return None

    def git(*args: str) -> str:
        try:
            return subprocess.run(  # noqa: S603 - fixed git arguments
                ["git", *args], capture_output=True, text=True, timeout=10, check=False
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    base = git("-C", cwd, "rev-parse", "HEAD").strip()
    committed = (
        git("-C", worktree, "diff", "--name-only", f"{base}...HEAD").splitlines() if base else []
    )
    pending = workspace.status_paths(git("-C", worktree, "status", "--porcelain"))
    # Every path git reports, nothing hidden: both counts come from these lists.
    files = sorted({name for name in [*committed, *pending] if name})
    return {
        "worktree": worktree,
        "branch": branch,
        "base": base[:12],
        "files": files[: workspace.MAX_FILES],
        "file_count": len(files),
        "uncommitted": len(pending),
    }


def _job_terminal(db_path: str, job_id: str) -> dict[str, str] | None:
    """The job's terminal status from the EventStore (read-only), or ``None`` while it runs."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT event_type, payload FROM events WHERE aggregate_id = ? "
            "AND event_type IN ('mcp.job.failed', 'mcp.job.completed', 'mcp.job.cancelled') "
            "ORDER BY rowid DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    try:
        payload = json.loads(row[1]) if isinstance(row[1], str) else {}
    except ValueError:
        payload = {}
    status = row[0].rsplit(".", 1)[-1]
    message = payload.get("message") if isinstance(payload, dict) else None
    return {"status": status, "message": str(message or status)}


def settings_stage(name: str) -> Any:
    from ouroboros.orchestrator_stage import Stage

    return Stage(name)


def _span_text(span: Any) -> str:
    path = getattr(span, "path", None) or getattr(span, "file", None) or ""
    line = getattr(span, "line", None) or getattr(span, "start_line", None)
    quote = getattr(span, "quote", None) or getattr(span, "text", None) or ""
    where = f"{path}:{line}" if path and line else str(path)
    return f"{where} {quote}".strip()[:200] or str(span)[:200]


def _item_text(item: Any) -> str:
    """A spec item as one sentence (criteria may be strings or structured dicts)."""
    if isinstance(item, dict):
        for key in ("description", "text", "criterion", "content"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(item)


def _validate_backend(backend: str, mode: str) -> None:
    from ouroboros.backends import (
        get_backend_capability,
        llm_backend_choices,
        runtime_backend_choices,
    )

    capability = get_backend_capability(backend)
    if capability is None:
        raise Refusal(
            "backend_unknown", f"Unknown agent or provider backend: {backend}", agent=backend
        )
    allowed = llm_backend_choices() if mode == "ask" else runtime_backend_choices()
    if capability.name not in allowed:
        kind = "chat with" if mode == "ask" else "run Ouroboros work on"
        raise Refusal("backend_wrong_mode", f"{backend} cannot {kind} in this mode.", agent=backend)


def _require_installed(backend: str) -> None:
    """Refuse an agent whose CLI is not installed, as the new-chat picker marks it.

    Detection is the picker's own (``detect_backend_cli``). A backend with no CLI
    of its own (LiteLLM, a host session) has nothing to detect and is not refused.
    """
    from ouroboros.backends import get_backend_capability
    from ouroboros.backends.model_catalog import detect_backend_cli
    from ouroboros.gui import installers

    capability = get_backend_capability(backend)
    if capability is None or not capability.cli_name:
        return
    if detect_backend_cli(capability.name) is not None:
        return
    methods = installers.methods_for(capability.name) or installers.methods_for(capability.cli_name)
    install = methods[0]["command"] if methods else None
    how = f"Install it with `{install}`" if install else "Install it"
    raise Refusal(
        "backend_not_installed",
        f"The {capability.cli_name} CLI is not installed, so this chat cannot use {backend}. "
        f"{how} or from the Connect page, then start the chat again.",
        agent=capability.name,
        cli=capability.cli_name,
        install=install,
    )


def _default_model_hint(backend: str | None, model: object) -> dict[str, Any]:
    """For a reply whose model is the ``default`` sentinel: which agent's default, and its model.

    Display only. ``model_default`` is the CLI's configured model when its own
    config names one (``configured_default_model``), else ``None``.
    """
    from ouroboros.backends import get_backend_capability
    from ouroboros.backends.model_catalog import DEFAULT_MODEL_SENTINEL, configured_default_model
    from ouroboros.providers import resolve_llm_backend

    if model != DEFAULT_MODEL_SENTINEL:
        return {}
    try:
        capability = get_backend_capability(resolve_llm_backend(backend))
    except ValueError:
        capability = None
    if capability is None:
        return {"model_backend": None, "model_default": None}
    return {
        "model_backend": capability.name,
        "model_default": configured_default_model(capability.name),
    }


def _auto_outcome(status: str, cancelled: bool, handoff: str | None) -> str:
    """How an Auto job ended: ``completed`` only when its run reported completion."""
    if cancelled:
        return "stopped"
    if status != "succeeded":
        return "failed"
    return "completed" if handoff == "completed" else "unverified"


def _board_summary(board: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts per column and the run status the Runs page would show."""
    from ouroboros.dashboard_web.reader import _summary_status

    columns = board.get("columns") if isinstance(board, dict) else None
    counts = {
        key: len(columns.get(key, [])) if isinstance(columns, dict) else 0
        for key in ("pending", "executing", "completed", "failed")
    }
    status = _summary_status(events, counts) if events else "running"
    meta = board.get("meta") if isinstance(board, dict) else None
    phase = meta.get("phase") if isinstance(meta, dict) else None
    activity = meta.get("activity") if isinstance(meta, dict) else None
    tiles: list[dict[str, Any]] = []
    if isinstance(columns, dict):
        for column in ("executing", "failed", "pending", "completed"):
            for card in columns.get(column, []) or []:
                if isinstance(card, dict) and len(tiles) < 12:
                    tiles.append(
                        {
                            "id": str(card.get("id") or ""),
                            "title": str(card.get("title") or card.get("id") or "")[:160],
                            "status": column,
                            "depth": card.get("depth") or 0,
                        }
                    )
    return {
        "tiles": tiles,
        "status": status,
        "counts": counts,
        "total": sum(counts.values()),
        "done": counts["completed"],
        "phase": phase,
        "activity": activity,
        "key": json.dumps([status, counts, phase, activity], sort_keys=True, default=str),
    }


async def _as_console_surface(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run console work under the ``console`` interview surface label.

    Every coroutine the console runs goes through :meth:`_Loop.submit`, so the
    interviews it starts or answers are labeled ``console`` in the event store
    (``ouroboros.bigbang.interview_closure``). The import stays lazy to keep
    this module light at console start-up.
    """
    from ouroboros.bigbang.interview_closure import InterviewSurface, use_interview_surface

    with use_interview_surface(InterviewSurface.CONSOLE):
        return await coro


__all__ = ["Harness", "chats_dir"]
