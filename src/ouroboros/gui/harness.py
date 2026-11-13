"""Chat harness: drive Ouroboros from the console the way Goose drives an agent.

A conversation is the Ouroboros pipeline told as a chat:

* **Interview** mode asks Socratic questions in the thread (``ouroboros_interview``),
  writes the spec when you are ready (``ouroboros_generate_seed``), then runs it
  (``ouroboros_start_execute_seed``) and follows the run's board until it ends.
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
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

import yaml

from ouroboros.gui.jobs import JobRegistry, ouroboros_command

_MAX_LOG_LINES = 400
_RUN_POLL_SEC = 2.0
_TERMINAL_RUN = frozenset({"completed", "failed", "cancelled"})


def chats_dir() -> Path:
    from ouroboros.config.models import get_config_dir

    path = get_config_dir() / "gui" / "chats"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> float:
    return time.time()


def _msg(role: str, kind: str, text: str = "", **data: Any) -> dict[str, Any]:
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
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


class _Handlers:
    """Pipeline handlers for one backend, wired like ``ouroboros auto`` wires them.

    ``backend=None`` follows ``~/.ouroboros/config.yaml``; a named backend runs
    every stage of this conversation on that agent (models still come from the
    Agents & models settings for that backend).
    """

    def __init__(self, backend: str | None = None) -> None:
        self.backend = backend
        self._built = False
        self.interview: Any = None
        self.seed: Any = None

    def ensure(self) -> None:
        if self._built:
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
        self.interview_handler = InterviewHandler(llm_backend=llm, agent_runtime_backend=backend)
        self.interview = HandlerInterviewBackend(self.interview_handler, cwd=os.getcwd())
        self.seed = HandlerSeedGenerator(
            GenerateSeedHandler(llm_backend=llm, agent_runtime_backend=backend)
        )
        execute = ExecuteSeedHandler(llm_backend=llm, agent_runtime_backend=backend)
        self.start_handler = StartExecuteSeedHandler(
            execute_handler=execute,
            agent_runtime_backend=backend,
            start_evaluate_handler=build_run_successor_handler(agent_runtime_backend=backend),
        )
        self._run_starter = HandlerRunStarter
        self._built = True

    def run_starter(self, cwd: str) -> Any:
        self.ensure()
        return self._run_starter(self.start_handler, cwd=cwd)


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
    """Conversations, their background work, and live subscribers."""

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._loop: _Loop | None = None
        self._handlers: dict[str, _Handlers] = {}
        self._jobs = JobRegistry()
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
            with contextlib.suppress(OSError):
                (chats_dir() / f"{session_id}.json").unlink()
            self._changed.notify_all()

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise ValueError("Say what you want to build.")
        cwd = str(payload.get("cwd") or "").strip() or os.getcwd()
        cwd = str(Path(cwd).expanduser())
        if not Path(cwd).is_dir():
            raise ValueError(f"Folder not found: {cwd}")
        mode = payload.get("mode") if payload.get("mode") in ("auto", "ask") else "interview"
        backend = str(payload.get("backend") or "").strip() or None
        model = str(payload.get("model") or "").strip() or None
        if backend:
            _validate_backend(backend, mode)
        session_id = uuid.uuid4().hex[:12]
        session: dict[str, Any] = {
            "id": session_id,
            "title": goal.splitlines()[0][:80],
            "goal": goal,
            "cwd": cwd,
            "mode": mode,
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

    def send(self, session_id: str, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ValueError("Type an answer first.")
        with self._lock:
            session = self._require(session_id)
            ask = session.get("mode") == "ask"
            ready = (
                ("waiting_answer", "done", "failed", "interrupted") if ask else ("waiting_answer",)
            )
            if session.get("status") not in ready:
                raise ValueError("Ouroboros is not waiting for a message right now.")
            session["messages"].append(_msg("user", "text", text))
            session["status"] = "thinking"
            self._touch(session)
        if ask:
            self._spawn(session_id, self._ask(session_id))
        else:
            self._spawn(session_id, self._interview_answer(session_id, text))
        return self.get(session_id) or {}

    def action(self, session_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            session = self._require(session_id)
            status = session.get("status")
            if action == "seed":
                if status not in ("waiting_answer", "seed_ready_to_write", "interrupted", "failed"):
                    raise ValueError("The interview is still working.")
                if not session.get("interview_session_id"):
                    raise ValueError("There is no interview to turn into a spec yet.")
                session["status"] = "thinking"
                session["messages"].append(_msg("system", "note", "Writing the spec…"))
                self._touch(session)
                coro = self._write_seed(session_id, force=bool(payload.get("force")))
            elif action == "run":
                if not session.get("seed_yaml"):
                    raise ValueError("Write the spec before running it.")
                if status == "running":
                    raise ValueError("This spec is already running.")
                problem = _workspace_problem(str(session["cwd"]))
                if problem:
                    raise ValueError(problem)
                session["status"] = "running"
                self._touch(session)
                coro = self._start_run(session_id)
            elif action == "suggest":
                if status != "waiting_answer" or not session.get("last_question"):
                    raise ValueError("There is no open question to answer.")
                if session.get("suggestion", {}).get("pending"):
                    raise ValueError("A suggestion is already being drafted.")
                question = str(session["last_question"])
                session["suggestion"] = {"pending": True, "question": question}
                self._touch(session)
                self._spawn_quiet(session_id, self._suggest(session_id, question))
                return self.get(session_id) or {}
            elif action == "stop":
                job_id = session.get("auto_job_id")
                job = self._jobs.get(job_id) if job_id else None
                if job is None or job.status != "running":
                    raise ValueError("Nothing to stop.")
                session["messages"].append(_msg("system", "note", "Stop requested."))
                self._touch(session)
                self._terminate_job(job_id)
                return self.get(session_id) or {}
            else:
                raise ValueError(f"Unknown action: {action}")
        self._spawn(session_id, coro)
        return self.get(session_id) or {}

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

    def _apply_turn(self, session_id: str, turn: Any) -> None:
        def apply(session: dict[str, Any]) -> None:
            session["interview_session_id"] = turn.session_id
            ready = bool(turn.seed_ready or turn.completed)
            if turn.ambiguity_score is not None:
                session["ambiguity"] = turn.ambiguity_score
                ready = ready or turn.ambiguity_score <= 0.2
            if turn.question and not turn.completed:
                session["messages"].append(
                    _msg(
                        "assistant",
                        "question",
                        turn.question,
                        ambiguity=turn.ambiguity_score,
                        ready=ready,
                    )
                )
            session["last_question"] = turn.question
            session["seed_ready"] = ready
            session["status"] = "seed_ready_to_write" if turn.completed else "waiting_answer"
            if turn.completed:
                session["messages"].append(
                    _msg("assistant", "note", "The interview is complete. Write the spec next.")
                )

        self._update(session_id, apply)

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
        handlers = self._handlers_for(session)
        await asyncio.to_thread(handlers.ensure)
        turn = await handlers.interview.start(session["goal"], cwd=session["cwd"])
        self._apply_turn(session_id, turn)

    async def _interview_answer(self, session_id: str, text: str) -> None:
        session = self.get(session_id) or {}
        handlers = self._handlers_for(session)
        await asyncio.to_thread(handlers.ensure)
        turn = await handlers.interview.answer(
            session["interview_session_id"], text, last_question=session.get("last_question")
        )
        self._apply_turn(session_id, turn)

    async def _write_seed(self, session_id: str, *, force: bool) -> None:
        session = self.get(session_id) or {}
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

    async def _suggest(self, session_id: str, question: str) -> None:
        """Draft an answer: from the repository when it settles the question, else a default."""
        session = self.get(session_id) or {}
        backend = session.get("backend") or None
        suggestion: dict[str, Any] = {"pending": False, "question": question}
        investigation = None
        try:
            from ouroboros.auto.investigator import build_repo_investigator
            from ouroboros.config.loader import get_agent_runtime_backend

            runtime = backend or await asyncio.to_thread(get_agent_runtime_backend)
            investigator = build_repo_investigator(runtime)
            if investigator is not None:
                investigation = await investigator.investigate(
                    session["goal"], question, Path(session["cwd"])
                )
        except Exception:
            investigation = None
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

        def apply(s: dict[str, Any]) -> None:
            s["messages"].append(
                _msg("assistant", "text", reply.content, model=getattr(reply, "model", model))
            )
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
            snap = job.snapshot(since=cursor)
            cursor = snap["cursor"]
            if snap["lines"]:

                def apply(s: dict[str, Any], lines: list[str] = snap["lines"]) -> None:
                    for message in reversed(s["messages"]):
                        if message.get("kind") == "log" and message.get("job_id") == job_id:
                            message["lines"] = (message.get("lines", []) + lines)[-_MAX_LOG_LINES:]
                            break
                    for line in lines:
                        found = _execution_id_in(line)
                        if found:
                            s["execution_id"] = found

                self._update(session_id, apply)
            if snap["status"] != "running":

                def finish(s: dict[str, Any], snap: dict[str, Any] = snap) -> None:
                    ok = snap["status"] == "succeeded"
                    s["status"] = "done" if ok else "failed"
                    text = (
                        "ouroboros auto finished."
                        if ok
                        else f"ouroboros auto exited with {snap['exit_code']}."
                    )
                    s["messages"].append(_msg("assistant", "note" if ok else "error", text))

                self._update(session_id, finish)
                return
            time.sleep(0.5)

    def _terminate_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.cancel()


def _workspace_problem(cwd: str) -> str | None:
    """Why a run cannot start in ``cwd`` (each task runs in a git worktree), or ``None``."""
    import subprocess

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed git arguments
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10, check=False
        )

    try:
        inside = git("rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.TimeoutExpired):
        return None  # no git binary: let the run report its own error
    if inside.returncode != 0:
        return (
            "Ouroboros runs each task in a git worktree, and this folder is not a git "
            'repository. Run `git init && git commit --allow-empty -m "init"` in it first.'
        )
    if git("rev-parse", "--verify", "HEAD").returncode != 0:
        return (
            "This git repository has no commits yet, so Ouroboros cannot create task "
            'worktrees. Run `git commit --allow-empty -m "init"` in it first.'
        )
    return None


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
        return subprocess.run(  # noqa: S603 - fixed git arguments
            ["git", *args], capture_output=True, text=True, timeout=10, check=False
        ).stdout

    base = git("-C", cwd, "rev-parse", "HEAD").strip()
    committed = (
        git("-C", worktree, "diff", "--name-only", f"{base}...HEAD").splitlines() if base else []
    )
    pending = [line[3:] for line in git("-C", worktree, "status", "--porcelain").splitlines()]
    files = sorted(
        {
            name
            for name in [*committed, *pending]
            if name and "__pycache__" not in name and not name.endswith(".pyc")
        }
    )
    return {
        "worktree": worktree,
        "branch": branch,
        "base": base[:12],
        "files": files[:50],
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
        raise ValueError(f"Unknown agent or provider backend: {backend}")
    allowed = llm_backend_choices() if mode == "ask" else runtime_backend_choices()
    if capability.name not in allowed:
        kind = "chat with" if mode == "ask" else "run Ouroboros work on"
        raise ValueError(f"{backend} cannot {kind} in this mode.")


def _execution_id_in(line: str) -> str | None:
    import re

    match = re.search(r"\b(exec_[A-Za-z0-9_-]{6,})\b", line)
    return match.group(1) if match else None


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


__all__ = ["Harness", "chats_dir"]
