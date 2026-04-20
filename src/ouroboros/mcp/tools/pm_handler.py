"""PM Interview Handler for MCP server.

Mirrors the existing InterviewHandler pattern from definitions.py but wraps
PMInterviewEngine instead of InterviewEngine.  The handler adds a thin MCP
layer on top of the engine: flat optional parameters, pm_meta persistence,
and deferred/decide-later diff computation.

The diff computation is the core value-add of this handler: before calling
``ask_next_question`` it snapshots the lengths of the engine's
``deferred_items`` and ``decide_later_items`` lists, and after the call
it slices the new entries to produce accurate per-call diffs that are
returned in the response metadata.

Interview completion is determined **solely** by the engine — either by
ambiguity scoring (score ≤ 0.2 means requirements are clear enough) or by
ambiguity scoring.  User controls when to stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

import structlog

from ouroboros.bigbang.interview import (
    InterviewRound,
    InterviewState,
)
from ouroboros.bigbang.pm_completion import (
    build_pm_completion_summary,
    maybe_complete_pm_interview,
)
from ouroboros.bigbang.pm_document import save_pm_document
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.config import get_clarification_model
from ouroboros.core.initial_context import resolve_initial_context_input
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.subagent import (
    build_pm_interview_subagent,
    build_subagent_result,
    emit_subagent_dispatched_event,
    should_dispatch_via_plugin,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore
from ouroboros.persistence.event_store import EventStore
from ouroboros.pm.handoff import build_pm_dev_handoff_next_step
from ouroboros.providers import create_llm_adapter

log = structlog.get_logger()

# Hard cap on interview rounds in MCP mode.  The engine's ambiguity scorer
# should trigger completion well before this, but this prevents runaway loops.


_DATA_DIR = Path.home() / ".ouroboros" / "data"


def _meta_path(session_id: str, data_dir: Path | None = None) -> Path:
    """Return the path to the pm_meta JSON file for a session."""
    base = data_dir or _DATA_DIR
    return base / f"pm_meta_{session_id}.json"


def _save_pm_meta(
    session_id: str,
    engine: PMInterviewEngine | None = None,
    cwd: str = "",
    data_dir: Path | None = None,
    *,
    status: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Persist PM-specific metadata that isn't in InterviewState.

    Fields:
        deferred_items: list[str]
        decide_later_items: list[str]
        codebase_context: str
        pending_reframe: dict | None
        cwd: str
        status: str | None  — e.g. "interview_started"
    """
    # Engine may be None when saving before interview start
    if engine is not None:
        pending_reframe = engine.get_pending_reframe()

        # Collapse deferred_items into decide_later_items so the persisted
        # metadata uses the same canonical schema as PMSeed.
        combined_decide_later = list(engine.decide_later_items)
        for item in engine.deferred_items:
            if item not in combined_decide_later:
                combined_decide_later.append(item)

        meta: dict[str, Any] = {
            "deferred_items": [],  # Deprecated: merged into decide_later_items
            "decide_later_items": combined_decide_later,
            "codebase_context": engine.codebase_context,
            "pending_reframe": pending_reframe,
            "cwd": cwd,
            "brownfield_repos": list(getattr(engine, "_selected_brownfield_repos", [])),
            "classifications": [
                c.output_type.value for c in getattr(engine, "classifications", [])
            ],
            "initial_context": getattr(engine, "_initial_context", ""),
        }
    else:
        meta = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": None,
            "cwd": cwd,
            "brownfield_repos": [],
            "classifications": [],
        }

    # Preserve status from existing meta if not explicitly overridden.
    # This prevents later saves from dropping the "interview_started" marker
    # that _handle_select_repos() depends on for idempotent replay.
    if status is not None:
        meta["status"] = status
    else:
        existing = _load_pm_meta(session_id, data_dir)
        if existing and "status" in existing:
            meta["status"] = existing["status"]

    if extra:
        meta.update(extra)

    path = _meta_path(session_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.debug("pm_handler.meta_saved", session_id=session_id, path=str(path))


def _load_pm_meta(
    session_id: str,
    data_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Load PM-specific metadata from disk.  Returns None if not found."""
    path = _meta_path(session_id, data_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("pm_handler.meta_load_failed", error=str(exc))
        return None


def _restore_engine_meta(engine: PMInterviewEngine, meta: dict[str, Any]) -> None:
    """Restore PM-specific state into an engine from loaded meta.

    Delegates to ``engine.restore_meta()``.
    """
    engine.restore_meta(meta)


def _last_classification(engine: PMInterviewEngine) -> str | None:
    """Return the output_type string of the engine's last classification, or None.

    Delegates to ``engine.get_last_classification()``.
    """
    return engine.get_last_classification()


def _format_pm_transcript(state: InterviewState) -> str:
    """Format persisted PM interview rounds as readable transcript for subagent context."""
    if not state.rounds:
        return ""
    lines: list[str] = []
    if state.initial_context:
        lines.append(f"**Product Idea:** {state.initial_context}")
        lines.append("")
    for r in state.rounds:
        lines.append(f"**Q{r.round_number}:** {r.question}")
        if r.user_response:
            lines.append(f"**A{r.round_number}:** {r.user_response}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _detect_action(arguments: dict[str, Any]) -> str:
    """Auto-detect the action from parameter presence when action param is omitted.

    Detection rules (evaluated in order):
    1. If ``action`` is explicitly provided, return it as-is.
    2. If ``selected_repos`` **and** ``initial_context`` both present →
       ``"start"`` (backward-compat 1-step, AC 8).
    3. If ``selected_repos`` is present (without ``initial_context``) →
       ``"select_repos"`` (2-step start step 2).
    4. If ``initial_context`` is present → ``"start"``
    5. If ``session_id`` is present (with or without ``answer``) → ``"resume"``
    6. Otherwise → ``"unknown"`` (caller should return an error).
    """
    explicit = arguments.get("action")
    if explicit:
        return explicit

    if arguments.get("selected_repos") is not None:
        # Backward compat (AC 8): when both initial_context and selected_repos
        # are present, treat as 1-step start so the caller skips step 1.
        if arguments.get("initial_context"):
            return "start"
        return "select_repos"

    if arguments.get("initial_context"):
        return "start"

    if arguments.get("session_id"):
        return "resume"

    return "unknown"


def _compute_deferred_diff(
    engine: PMInterviewEngine,
    deferred_len_before: int,
    decide_later_len_before: int,
) -> dict[str, Any]:
    """Compute the diff of deferred/decide-later items after ask_next_question.

    Delegates to ``engine.compute_deferred_diff()``.

    This is the core diff computation for AC 8.
    """
    return engine.compute_deferred_diff(deferred_len_before, decide_later_len_before)


async def _check_completion(
    state: InterviewState,
    engine: PMInterviewEngine,
) -> dict[str, Any] | None:
    """Check whether the interview should complete based on ambiguity or rounds.

    Delegates to ``engine.check_completion()``.

    Returns a dict with completion metadata if the interview should end,
    or ``None`` if the interview should continue.
    """
    return await engine.check_completion(state)


@dataclass
class PMInterviewHandler:
    """Handler for the ouroboros_pm_interview MCP tool.

    Manages PM-focused interviews with question classification,
    deferred item tracking, and per-call diff computation.

    Interview completion is determined by the engine's ambiguity
    scorer (score ≤ 0.2).  User controls when to stop.

    The handler wraps PMInterviewEngine and adds:
    - Flat MCP parameter interface (session_id, action, answer, cwd, initial_context)
    - pm_meta_{session_id}.json persistence for PM-specific state
    - Deferred/decide-later diff computation per ask_next_question call
    - Automatic completion detection via ambiguity scoring
    """

    pm_engine: PMInterviewEngine | None = field(default=None, repr=False)
    data_dir: Path | None = field(default=None, repr=False)
    llm_adapter: Any | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition with flat optional parameters."""
        return MCPToolDefinition(
            name="ouroboros_pm_interview",
            description=(
                "PM interview for product requirements gathering. "
                "Start with initial_context, continue with session_id + answer, "
                "or generate PM seed with action='generate'. "
                "In plugin mode, returns a delegation receipt "
                "(status=delegated_to_subagent) and the PM interview executes in an "
                "OpenCode Task pane — the real session_id is returned there."
            ),
            parameters=(
                MCPToolParameter(
                    name="initial_context",
                    type=ToolInputType.STRING,
                    description="Initial product description to start a new PM interview",
                    required=False,
                ),
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Session ID to resume an existing PM interview",
                    required=False,
                ),
                MCPToolParameter(
                    name="answer",
                    type=ToolInputType.STRING,
                    description="PM's response to the current interview question",
                    required=False,
                ),
                MCPToolParameter(
                    name="action",
                    type=ToolInputType.STRING,
                    description=(
                        "Action to perform. Auto-detected from parameter presence when omitted: "
                        "initial_context → 'start', session_id + answer → 'resume'. "
                        "Use 'generate' explicitly to produce PM seed from completed interview."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="cwd",
                    type=ToolInputType.STRING,
                    description=(
                        "Working directory for PM document output. "
                        "Defaults to current working directory. "
                        "Brownfield context is loaded from DB (is_default=true)."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="selected_repos",
                    type=ToolInputType.ARRAY,
                    description=(
                        "List of repository paths selected for brownfield context "
                        "(2-step start: returned by step 1, sent back in step 2). "
                        "All repos are assigned role=main. "
                        "When provided with initial_context, starts the interview "
                        "with the selected brownfield repos."
                    ),
                    required=False,
                    items={"type": "string"},
                ),
            ),
        )

    def _get_engine(self) -> PMInterviewEngine:
        """Return the injected engine or create a new one using the server's configured backend."""
        if self.pm_engine is not None:
            return self.pm_engine
        adapter = self.llm_adapter or create_llm_adapter(
            backend=self.llm_backend,
            max_turns=1,
            use_case="interview",
            allowed_tools=[],
        )
        model = get_clarification_model(self.llm_backend)
        return PMInterviewEngine.create(
            llm_adapter=adapter,
            state_dir=self.data_dir or _DATA_DIR,
            model=model,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a PM interview request.

        Action is auto-detected from parameter presence when ``action`` is
        omitted:

        - ``initial_context`` present → ``start``
        - ``session_id`` (+ optional ``answer``) present → ``resume``
        - ``action="generate"`` + ``session_id`` → ``generate``
        """
        initial_context = arguments.get("initial_context")
        session_id = arguments.get("session_id")
        answer = arguments.get("answer")
        cwd_arg = arguments.get("cwd")
        selected_repos: list[str] | None = arguments.get("selected_repos")

        # Auto-detect action from parameter presence (AC 13)
        action = _detect_action(arguments)

        # --- Argument validation (before any dispatch) ---
        # Reject invalid action+args combos early — applies to both plugin and subprocess.
        _valid_combo = (
            (action == "start" and initial_context)
            or (action == "select_repos" and selected_repos is not None)
            or (action == "resume" and session_id)
            or (action == "generate" and session_id)
        )
        if not _valid_combo:
            return Result.err(
                MCPToolError(
                    "Must provide initial_context to start, or session_id to resume/generate",
                    tool_name="ouroboros_pm_interview",
                )
            )

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            # Plugin mode: persist BOTH generic InterviewState AND PM-specific
            # metadata (pm_meta) server-side WITHOUT creating an LLM adapter.
            # Subagent handles all LLM work. This preserves the 2-step PM flow:
            #   step 1 (start): writes InterviewState + pm_meta(initial_context, cwd)
            #   step 2 (select_repos): loads pm_meta, updates brownfield_repos, re-saves
            #   resume/answer: loads state + pm_meta, records answer, builds transcript
            #   generate: delegates seed generation to subagent (state on disk)
            from ouroboros.mcp.tools.authoring_handlers import (
                _plugin_load_state,
                _plugin_save_state,
            )

            state_dir = self.data_dir or _DATA_DIR
            state_dir.mkdir(parents=True, exist_ok=True)

            transcript = ""
            real_session_id = session_id

            if action == "start" and initial_context:
                cwd = cwd_arg or os.getcwd()
                resolved = resolve_initial_context_input(initial_context, cwd=cwd)
                if resolved.is_err:
                    return Result.err(
                        MCPToolError(str(resolved.error), tool_name="ouroboros_pm_interview")
                    )
                from ouroboros.core.security import InputValidator

                is_valid, error_msg = InputValidator.validate_initial_context(resolved.value)
                if not is_valid:
                    return Result.err(MCPToolError(error_msg, tool_name="ouroboros_pm_interview"))
                from uuid import uuid4

                interview_id = f"interview_{uuid4().hex[:16]}"
                state = InterviewState(
                    interview_id=interview_id,
                    initial_context=resolved.value,
                )
                if cwd:
                    from ouroboros.bigbang.explore import detect_brownfield

                    if detect_brownfield(cwd):
                        state.is_brownfield = True
                        state.codebase_paths = [{"path": cwd, "role": "primary"}]

                save_result = await _plugin_save_state(state_dir, state)
                if save_result.is_err:
                    return Result.err(
                        MCPToolError(str(save_result.error), tool_name="ouroboros_pm_interview")
                    )
                # Persist PM-specific metadata (no engine needed for initial save)
                # For 1-step start (initial_context + selected_repos), persist
                # the caller's selected_repos so later resume/generate turns
                # can restore them.  Fall back to cwd-derived codebase_paths
                # when no explicit repos provided.
                persisted_repos: list[Any] = []
                if selected_repos:
                    persisted_repos = selected_repos
                elif state.codebase_paths:
                    persisted_repos = [
                        {"path": p["path"], "role": p.get("role", "primary")}
                        for p in state.codebase_paths
                    ]
                _save_pm_meta(
                    interview_id,
                    engine=None,
                    cwd=cwd,
                    data_dir=self.data_dir,
                    extra={
                        "initial_context": resolved.value,
                        "brownfield_repos": persisted_repos,
                    },
                )
                real_session_id = state.interview_id

            elif action == "select_repos" and selected_repos is not None:
                # 2-step PM flow step 2: recover initial_context from pm_meta,
                # persist selected repos, then dispatch to subagent.
                if not session_id:
                    return Result.err(
                        MCPToolError(
                            "select_repos requires session_id (from step 1) "
                            "or initial_context for 1-step start",
                            tool_name="ouroboros_pm_interview",
                        )
                    )
                meta = _load_pm_meta(session_id, data_dir=self.data_dir)
                if meta is None:
                    return Result.err(
                        MCPToolError(
                            f"No pm_meta found for session {session_id}. "
                            "The session may have expired or never been created.",
                            tool_name="ouroboros_pm_interview",
                        )
                    )
                # Update pm_meta with selected repos and mark interview_started
                meta["brownfield_repos"] = selected_repos
                meta["status"] = "interview_started"
                _save_pm_meta(
                    session_id,
                    engine=None,
                    cwd=meta.get("cwd", cwd_arg or os.getcwd()),
                    data_dir=self.data_dir,
                    status="interview_started",
                    extra={
                        "initial_context": meta.get("initial_context", ""),
                        "brownfield_repos": selected_repos,
                    },
                )
                # Use initial_context from pm_meta for subagent prompt
                initial_context = meta.get("initial_context", initial_context)
                real_session_id = session_id

            elif session_id:
                # resume / answer / generate — load state + build transcript
                load_result = await _plugin_load_state(state_dir, session_id)
                if load_result.is_err:
                    return Result.err(
                        MCPToolError(str(load_result.error), tool_name="ouroboros_pm_interview")
                    )
                state = load_result.value

                # Restore brownfield repos from pm_meta if not provided in
                # the current request.  The user selects repos during
                # select_repos action; subsequent resume/generate turns omit
                # them from the request params.  Without this, the child
                # subagent loses repo context on later turns.
                if not selected_repos:
                    meta = _load_pm_meta(session_id, data_dir=self.data_dir)
                    if meta:
                        if meta.get("brownfield_repos"):
                            selected_repos = meta["brownfield_repos"]
                        # Also restore initial_context for generate prompts
                        if not initial_context and meta.get("initial_context"):
                            initial_context = meta["initial_context"]

                # Gate: generate requires interview evidence.  In plugin
                # mode is_complete is never set (child owns progression),
                # so we gate on answered rounds instead.  The child session
                # performs the real completeness validation.
                if action == "generate":
                    answered_rounds = [r for r in state.rounds if r.user_response is not None]
                    if not state.is_complete and not answered_rounds:
                        return Result.err(
                            MCPToolError(
                                "Interview has no answered rounds and is not "
                                "marked complete. Continue the interview "
                                "before generating a PM seed.",
                                tool_name="ouroboros_pm_interview",
                            )
                        )

                # Record answer into persisted state.
                # In plugin mode each dispatch = new child session. The child
                # generates questions but can't write back to server-side state.
                # We must always persist user answers for transcript continuity.
                if answer:
                    if state.rounds and state.rounds[-1].user_response is None:
                        state.rounds[-1].user_response = answer
                    else:
                        # No rounds yet or all answered — append new round
                        from ouroboros.bigbang.interview import InterviewRound

                        state.rounds.append(
                            InterviewRound(
                                round_number=len(state.rounds) + 1,
                                question="(continued from subagent)",
                                user_response=answer,
                            )
                        )
                    state.mark_updated()
                    save_result = await _plugin_save_state(state_dir, state)
                    if save_result.is_err:
                        return Result.err(
                            MCPToolError(str(save_result.error), tool_name="ouroboros_pm_interview")
                        )
                # Build transcript from persisted rounds
                transcript = _format_pm_transcript(state)

            payload = build_pm_interview_subagent(
                session_id=real_session_id or "new",
                action=action,
                initial_context=initial_context,
                answer=answer,
                cwd=cwd_arg,
                selected_repos=selected_repos,
                transcript=transcript,
            )
            await emit_subagent_dispatched_event(
                self.event_store,
                session_id=real_session_id,
                payload=payload,
            )
            return build_subagent_result(
                payload,
                response_shape={
                    "session_id": real_session_id,
                    "action": action,
                    "status": "delegated_to_subagent",
                    "dispatch_mode": "plugin",
                },
            )

        # Fall-through: real in-process PM interview (subprocess / non-opencode runtimes).

        # For resume/generate, prefer persisted session cwd over os.getcwd()
        # so artifacts land in the workspace where the interview started.
        if cwd_arg:
            cwd = cwd_arg
        elif session_id and action in ("resume", "generate"):
            meta = _load_pm_meta(session_id, self.data_dir)
            cwd = (meta.get("cwd") if meta else None) or os.getcwd()
        else:
            cwd = os.getcwd()

        engine = self._get_engine()

        try:
            # ── Generate PM seed ──────────────────────────────────
            if action == "generate" and session_id:
                return await self._handle_generate(engine, session_id, cwd)

            # ── Step 2: repo selection (AC 4) ─────────────────────
            if action == "select_repos" and selected_repos is not None:
                return await self._handle_select_repos(
                    engine,
                    selected_repos,
                    session_id,
                    initial_context,
                    cwd,
                )

            # ── Start new interview ────────────────────────────────
            if action == "start" and initial_context:
                return await self._handle_start(
                    engine,
                    initial_context,
                    cwd,
                    selected_repos=selected_repos,
                )

            # ── Resume with answer ─────────────────────────────────
            if action == "resume" and session_id:
                return await self._handle_answer(engine, session_id, answer, cwd)

            return Result.err(
                MCPToolError(
                    "Must provide initial_context to start, or session_id to resume/generate",
                    tool_name="ouroboros_pm_interview",
                )
            )

        except Exception as e:
            log.error("pm_handler.unexpected_error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"PM interview failed: {e}",
                    tool_name="ouroboros_pm_interview",
                )
            )

    # ──────────────────────────────────────────────────────────────
    # Start
    # ──────────────────────────────────────────────────────────────

    async def _handle_start(
        self,
        engine: PMInterviewEngine,
        initial_context: str,
        cwd: str,
        *,
        selected_repos: list[str] | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Start a new PM interview session.

        Automatically loads is_default=true repos from DB as brownfield
        context. No user selection needed — repo defaults are managed
        via ``ooo setup``.

        If ``selected_repos`` is provided, uses those instead (backward compat).
        """
        # ── Load brownfield from DB defaults ────────────────────
        brownfield_repos = None
        if selected_repos is not None and len(selected_repos) > 0:
            # Backward compat: explicit selected_repos — fail explicitly if none resolve
            resolved = await self._resolve_repos_from_db(selected_repos)
            if not resolved:
                return Result.err(
                    MCPToolError(
                        f"None of the selected repos could be resolved: {selected_repos}. "
                        "Register them first via 'ouroboros setup scan' or the brownfield tool.",
                        tool_name="ouroboros_pm_interview",
                    )
                )
        elif selected_repos is None:
            # Auto-load defaults from DB (missing defaults → greenfield is OK)
            resolved = await self._query_default_repos()
        else:
            # Empty list explicitly passed → greenfield
            resolved = []

        if resolved:
            brownfield_repos = [
                {
                    "path": r.path,
                    "name": r.name,
                    "role": "main",
                    **({"desc": r.desc} if r.desc else {}),
                }
                for r in resolved
            ]
            log.info(
                "pm_handler.start.brownfield_repos",
                count=len(resolved),
                paths=[r.path for r in resolved],
            )

        result = await engine.ask_opening_and_start(
            user_response=initial_context,
            brownfield_repos=brownfield_repos,
        )
        if result.is_err:
            return Result.err(MCPToolError(str(result.error), tool_name="ouroboros_pm_interview"))

        state = result.value

        # Snapshot before asking first question
        deferred_before = len(engine.deferred_items)
        decide_later_before = len(engine.decide_later_items)

        question_result = await engine.ask_next_question(state)
        if question_result.is_err:
            return Result.err(
                MCPToolError(
                    str(question_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )

        question = question_result.value

        # Compute diff
        diff = _compute_deferred_diff(engine, deferred_before, decide_later_before)

        # Record unanswered round
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=None,
            )
        )
        state.mark_updated()

        # Persist — check save result to avoid handing back a session that wasn't written
        save_result = await engine.save_state(state)
        if isinstance(save_result, Result) and save_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Failed to persist interview state: {save_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )
        _save_pm_meta(
            state.interview_id,
            engine,
            cwd=cwd,
            data_dir=self.data_dir,
            status="interview_started",
        )

        # Include pending_reframe in response meta if a reframe occurred
        pending_reframe = engine.get_pending_reframe()

        # Check classification to signal skip eligibility
        classification = _last_classification(engine)
        is_decide_later = classification == "decide_later"
        is_deferred = classification == "deferred"
        skip_eligible = is_decide_later or is_deferred

        meta = {
            "session_id": state.interview_id,
            "status": "interview_started",
            "input_type": "freeText",
            "response_param": "answer",
            "question": question,
            "is_brownfield": state.is_brownfield,
            "classification": classification,
            "skip_eligible": skip_eligible,
            "pending_reframe": pending_reframe,
            **diff,
        }

        log.info(
            "pm_handler.started",
            session_id=state.interview_id,
            is_brownfield=state.is_brownfield,
            classification=classification,
            skip_eligible=skip_eligible,
            has_pending_reframe=pending_reframe is not None,
            **diff,
        )

        # Build response text — include skip hint when applicable
        start_text = f"PM interview started. Session ID: {state.interview_id}\n\n{question}"
        if is_decide_later:
            start_text += (
                "\n\n💡 This question can be deferred. "
                'The user may answer now, or choose "decide later" to skip it. '
                "If they choose to decide later, pass "
                f'answer="[decide_later]" with session_id="{state.interview_id}".'
            )
        elif is_deferred:
            start_text += (
                "\n\n💡 This is a technical question that can be deferred to the dev phase. "
                "The user may answer now, or choose to defer it. "
                "If they choose to defer, pass "
                f'answer="[deferred]" with session_id="{state.interview_id}".'
            )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=start_text,
                    ),
                ),
                is_error=False,
                meta=meta,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Brownfield repo helpers
    # ──────────────────────────────────────────────────────────────

    async def _query_default_repos(self) -> list[BrownfieldRepo]:
        """Query DB for is_default=true repos."""
        try:
            store = BrownfieldStore()
            await store.initialize()
            try:
                return list(await store.get_defaults())
            finally:
                await store.close()
        except Exception as exc:
            log.warning("pm_handler.query_defaults_failed", error=str(exc))
            return []

    async def _query_all_repos(self) -> list[BrownfieldRepo]:
        """Query DB for all registered brownfield repos."""
        try:
            store = BrownfieldStore()
            await store.initialize()
            try:
                return await store.list()
            finally:
                await store.close()
        except Exception as exc:
            log.warning("pm_handler.query_repos_failed", error=str(exc))
            return []

    async def _resolve_repos_from_db(
        self,
        paths: list[str],
    ) -> list[BrownfieldRepo]:
        """Look up selected paths in the DB, returning only those that exist.

        Paths that are not registered in the brownfield_repos table are
        silently ignored.  If *all* paths are missing the caller should
        treat the session as greenfield.

        Args:
            paths: List of absolute filesystem paths chosen by the user.

        Returns:
            List of :class:`BrownfieldRepo` instances for paths found in DB,
            preserving the order of *paths*.
        """
        all_repos = await self._query_all_repos()
        repo_by_path: dict[str, BrownfieldRepo] = {r.path: r for r in all_repos}

        resolved: list[BrownfieldRepo] = []
        for p in paths:
            repo = repo_by_path.get(p)
            if repo is not None:
                resolved.append(repo)
            else:
                log.warning(
                    "pm_handler.resolve_repos.path_not_in_db",
                    path=p,
                )
        return resolved

    # ──────────────────────────────────────────────────────────────
    # Step 2: select_repos (AC 4)
    # ──────────────────────────────────────────────────────────────

    async def _handle_select_repos(
        self,
        engine: PMInterviewEngine,
        selected_repos: list[str],
        session_id: str | None,
        initial_context: str | None,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle step 2 of the 2-step start: user has selected repos.

        Backward compat: if ``initial_context`` is provided alongside
        ``selected_repos``, behave identically to the old 1-step flow
        (no pm_meta lookup needed).

        Otherwise, ``session_id`` is required to recover the saved
        ``initial_context`` from pm_meta written during step 1.
        """
        # ── Backward-compat 1-step: both selected_repos + initial_context ──
        if initial_context:
            return await self._handle_start(
                engine,
                initial_context,
                cwd,
                selected_repos=selected_repos,
            )

        # ── 2-step: recover initial_context from pm_meta ──────────────
        if not session_id:
            return Result.err(
                MCPToolError(
                    "select_repos requires session_id (from step 1) "
                    "or initial_context for 1-step start",
                    tool_name="ouroboros_pm_interview",
                )
            )

        meta = _load_pm_meta(session_id, data_dir=self.data_dir)
        if meta is None:
            return Result.err(
                MCPToolError(
                    f"No pm_meta found for session {session_id}. "
                    "The session may have expired or never been created.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        # ── Idempotency (AC 9): session already started ──────────
        # If select_repos is called again on an already-started session,
        # return the first question from InterviewState instead of
        # re-starting the interview.
        if meta.get("status") == "interview_started":
            return await self._idempotent_select_repos(engine, session_id, meta)

        saved_context = meta.get("initial_context", "")
        if not saved_context:
            return Result.err(
                MCPToolError(
                    f"pm_meta for {session_id} has no initial_context. "
                    "Cannot proceed with repo selection.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        log.info(
            "pm_handler.select_repos.step2",
            session_id=session_id,
            repo_count=len(selected_repos),
        )

        # Do NOT update global DB defaults — PM interview selection is session-scoped
        return await self._handle_start(
            engine,
            saved_context,
            cwd,
            selected_repos=selected_repos,
        )

    # ──────────────────────────────────────────────────────────────
    # Idempotency guard (AC 9)
    # ──────────────────────────────────────────────────────────────

    async def _idempotent_select_repos(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        meta: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Return the first question when select_repos is called on an already-started session.

        This handles the case where the caller sends ``select_repos`` more
        than once for the same session.  Instead of re-starting the
        interview (which would create duplicate state), we load the existing
        ``InterviewState`` and replay the first question from its rounds.
        """
        log.info(
            "pm_handler.select_repos.idempotent",
            session_id=session_id,
        )

        load_result = await engine.load_state(session_id)
        if load_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Session {session_id} is marked as started but state "
                    f"could not be loaded: {load_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )

        state = load_result.value
        # Return the last unanswered round's question (the pending PM-facing prompt),
        # not rounds[0] which may be a hidden auto-deferred/auto-decided question.
        pending = next(
            (r for r in reversed(state.rounds) if r.user_response is None),
            None,
        )
        first_question = (
            pending.question
            if pending
            else (state.rounds[-1].question if state.rounds else "No question available.")
        )

        engine.restore_meta(meta)
        classification = _last_classification(engine)
        is_decide_later = classification == "decide_later"
        is_deferred = classification == "deferred"
        skip_eligible = is_decide_later or is_deferred

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM interview started. Session ID: {session_id}\n\n{first_question}"
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "status": "interview_started",
                    "question": first_question,
                    "is_brownfield": state.is_brownfield,
                    "idempotent": True,
                    "classification": classification,
                    "skip_eligible": skip_eligible,
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Answer (resume + record)
    # ──────────────────────────────────────────────────────────────

    async def _handle_answer(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        answer: str | None,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Resume session, record an answer, check completion, then ask next question.

        Completion is determined by the engine's ambiguity score dropping
        below the threshold (requirements are clear).  User controls when
        to stop.
        """
        # Load interview state
        load_result = await engine.load_state(session_id)
        if load_result.is_err:
            return Result.err(
                MCPToolError(str(load_result.error), tool_name="ouroboros_pm_interview")
            )
        state = load_result.value

        # Restore PM meta into engine
        meta = _load_pm_meta(session_id, self.data_dir)
        if meta:
            engine.restore_meta(meta)

        # If no answer provided, re-display the pending question (retry/reconnect)
        if not answer and state.rounds and state.rounds[-1].user_response is None:
            pending_question = state.rounds[-1].question
            classification = _last_classification(engine)
            is_decide_later = classification == "decide_later"
            is_deferred = classification == "deferred"
            skip_eligible = is_decide_later or is_deferred

            pending_reframe = engine.get_pending_reframe()

            # Include skip hint in re-displayed question
            pending_text = f"Session {session_id}\n\n{pending_question}"
            if is_decide_later:
                pending_text += (
                    "\n\n💡 This question can be deferred. "
                    'The user may answer now, or choose "decide later" to skip it. '
                    "If they choose to decide later, pass "
                    f'answer="[decide_later]" with session_id="{session_id}".'
                )
            elif is_deferred:
                pending_text += (
                    "\n\n💡 This is a technical question that can be deferred to the dev phase. "
                    "The user may answer now, or choose to defer it. "
                    "If they choose to defer, pass "
                    f'answer="[deferred]" with session_id="{session_id}".'
                )

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=pending_text,
                        ),
                    ),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "input_type": "freeText",
                        "response_param": "answer",
                        "question": pending_question,
                        "is_complete": False,
                        "classification": classification,
                        "skip_eligible": skip_eligible,
                        "deferred_this_round": [],
                        "decide_later_this_round": [],
                        "interview_complete": False,
                        "pending_reframe": pending_reframe,
                        "new_deferred": [],
                        "new_decide_later": [],
                        "deferred_count": 0,
                        "decide_later_count": len(engine.deferred_items)
                        + len(engine.decide_later_items),
                    },
                )
            )

        # ── Per-round diff snapshot — must be BEFORE any skip/record call ──
        # Snapshot list lengths here so that items appended inside
        # skip_as_decide_later() / skip_as_deferred() are captured in the
        # per-round diff returned at the end of this call.
        deferred_before = len(engine.deferred_items)
        decide_later_before = len(engine.decide_later_items)

        # Record answer if provided
        if answer and not state.rounds:
            return Result.err(
                MCPToolError(
                    "Cannot record answer: no questions have been asked yet.",
                    tool_name="ouroboros_pm_interview",
                )
            )
        if answer and state.rounds:
            last_question = state.rounds[-1].question
            if state.rounds[-1].user_response is None:
                state.rounds.pop()

            # ── User chose to skip (decide later / defer to dev) ───
            # The main session detects classification via response_meta
            # and offers skip options.  The user's choice arrives as:
            #   answer="[decide_later]" → skip_as_decide_later()
            #   answer="[deferred]"     → skip_as_deferred()
            # Guard: only honour the sentinel when the last question was
            # actually classified as that type.  If a client sends
            # "[decide_later]" for a passthrough/reframed question, treat
            # it as a normal answer so no data is silently discarded.
            stripped = answer.strip()
            last_classification = _last_classification(engine)
            if stripped == "[decide_later]" and last_classification == "decide_later":
                skip_result = await engine.skip_as_decide_later(state, last_question)
                if skip_result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(skip_result.error),
                            tool_name="ouroboros_pm_interview",
                        )
                    )
                state = skip_result.value
                state.clear_stored_ambiguity()
            elif stripped == "[deferred]" and last_classification == "deferred":
                skip_result = await engine.skip_as_deferred(state, last_question)
                if skip_result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(skip_result.error),
                            tool_name="ouroboros_pm_interview",
                        )
                    )
                state = skip_result.value
                state.clear_stored_ambiguity()
            else:
                record_result = await engine.record_response(state, answer, last_question)
                if record_result.is_err:
                    return Result.err(
                        MCPToolError(
                            str(record_result.error),
                            tool_name="ouroboros_pm_interview",
                        )
                    )
                state = record_result.value
                state.clear_stored_ambiguity()

        # ── Completion check (AC 12) ─────────────────────────────
        # Completion is determined by engine ambiguity scoring.
        # When complete, auto-generate the PM document immediately
        # (no separate "generate" call needed from the skill).
        completion_result = await maybe_complete_pm_interview(state, engine)
        if completion_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Failed to complete interview: {completion_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )

        state, completion = completion_result.value
        if completion is not None:
            save_result = await engine.save_state(state)
            if isinstance(save_result, Result) and save_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Failed to persist completed state: {save_result.error}",
                        tool_name="ouroboros_pm_interview",
                    )
                )
            _save_pm_meta(session_id, engine, cwd=cwd, data_dir=self.data_dir)

            log.info(
                "pm_handler.interview_complete",
                session_id=session_id,
                **completion,
            )

            # Auto-generate PM document on completion
            seed_result = await engine.generate_pm_seed(state)
            if seed_result.is_err:
                # Generation failed — still report completion but without document
                summary_text = (
                    f"Interview complete but PM generation failed: {seed_result.error}\n"
                    f"Session ID: {session_id}\n"
                    f'Retry with: action="generate", session_id="{session_id}"'
                )
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text=summary_text),),
                        is_error=False,
                        meta={
                            "session_id": session_id,
                            "is_complete": True,
                            "generation_failed": True,
                            **completion,
                        },
                    )
                )

            seed = seed_result.value
            try:
                seed_path = engine.save_pm_seed(seed)
                pm_output_dir = Path(cwd) / ".ouroboros"
                pm_path = save_pm_document(seed, output_dir=pm_output_dir)
            except Exception as e:
                log.error("pm_handler.save_failed", error=str(e), session_id=session_id)
                summary_text = (
                    f"Interview complete but saving PM artifacts failed: {e}\n"
                    f"Session ID: {session_id}\n"
                    f'Retry with: action="generate", session_id="{session_id}"'
                )
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text=summary_text),),
                        is_error=False,
                        meta={
                            "session_id": session_id,
                            "is_complete": True,
                            "generation_failed": True,
                            **completion,
                        },
                    )
                )

            decide_later_summary = engine.format_decide_later_summary()
            summary_text = build_pm_completion_summary(
                session_id=session_id,
                completion=completion,
                stored_ambiguity_score=getattr(state, "ambiguity_score", None),
                deferred_count=0,
                decide_later_count=len(engine.deferred_items) + len(engine.decide_later_items),
                decide_later_summary=decide_later_summary,
            )
            summary_text += f"\n\nPM document: {pm_path}\nSeed: {seed_path}"

            response_meta = {
                "session_id": session_id,
                "question": None,
                "is_complete": True,
                "classification": _last_classification(engine),
                "deferred_this_round": [],
                "decide_later_this_round": [],
                **completion,
                "deferred_count": 0,
                "decide_later_count": len(engine.deferred_items) + len(engine.decide_later_items),
                "seed_path": str(seed_path),
                "pm_path": str(pm_path),
            }

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=summary_text,
                        ),
                    ),
                    is_error=False,
                    meta=response_meta,
                )
            )

        question_result = await engine.ask_next_question(state)
        if question_result.is_err:
            error_msg = str(question_result.error)
            if "empty response" in error_msg.lower():
                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text=(
                                    f"Question generation failed. "
                                    f"Session ID: {session_id}\n\n"
                                    f'Resume with: session_id="{session_id}"'
                                ),
                            ),
                        ),
                        is_error=True,
                        meta={"session_id": session_id, "recoverable": True},
                    )
                )
            return Result.err(MCPToolError(error_msg, tool_name="ouroboros_pm_interview"))

        question = question_result.value

        # Compute diff AFTER ask_next_question — new items are the
        # slice from the pre-snapshot length to current length
        diff = _compute_deferred_diff(engine, deferred_before, decide_later_before)

        # Save unanswered round
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=None,
            )
        )
        state.mark_updated()

        save_result = await engine.save_state(state)
        if isinstance(save_result, Result) and save_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Failed to persist resume state: {save_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )
        _save_pm_meta(session_id, engine, cwd=cwd, data_dir=self.data_dir)

        # Include pending_reframe in response meta if a new reframe occurred
        pending_reframe = engine.get_pending_reframe()

        # Extract classification from the last classify call
        classification = _last_classification(engine)

        # Signal to the caller that the user can skip this question
        is_decide_later = classification == "decide_later"
        is_deferred = classification == "deferred"
        skip_eligible = is_decide_later or is_deferred

        response_meta = {
            "session_id": session_id,
            "input_type": "freeText",
            "response_param": "answer",
            "question": question,
            "is_complete": False,
            "classification": classification,
            "skip_eligible": skip_eligible,
            "deferred_this_round": diff["new_deferred"],
            "decide_later_this_round": diff["new_decide_later"],
            # Keep backward-compat fields from AC 8
            "interview_complete": False,
            "pending_reframe": pending_reframe,
            **diff,
        }

        log.info(
            "pm_handler.question_asked",
            session_id=session_id,
            classification=classification,
            skip_eligible=skip_eligible,
            has_pending_reframe=pending_reframe is not None,
            **diff,
        )

        # Build response text — include skip hint when applicable
        response_text = f"Session {session_id}\n\n{question}"
        if is_decide_later:
            response_text += (
                "\n\n💡 This question can be deferred. "
                'The user may answer now, or choose "decide later" to skip it. '
                "If they choose to decide later, pass "
                f'answer="[decide_later]" with session_id="{session_id}".'
            )
        elif is_deferred:
            response_text += (
                "\n\n💡 This is a technical question that can be deferred to the dev phase. "
                "The user may answer now, or choose to defer it. "
                "If they choose to defer, pass "
                f'answer="[deferred]" with session_id="{session_id}".'
            )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=response_text,
                    ),
                ),
                is_error=False,
                meta=response_meta,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Generate PM seed
    # ──────────────────────────────────────────────────────────────

    async def _handle_generate(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Generate PM seed from completed interview (path-idempotent).

        Loads InterviewState and pm_meta, restores engine via restore_meta(),
        runs generate_pm_seed, saves PM seed to ~/.ouroboros/seeds/ and
        pm.md to {cwd}/.ouroboros/.

        Path-idempotent: file paths are deterministic for a given session_id
        (seed → ``pm_seed_{interview_id}.json``, document → ``pm.md``).
        Content timestamps (created_at, Generated header) may differ on retry.

        Rejects incomplete interviews with an error to prevent partial-spec
        artifacts from being generated.
        """
        load_result = await engine.load_state(session_id)
        if load_result.is_err:
            return Result.err(
                MCPToolError(str(load_result.error), tool_name="ouroboros_pm_interview")
            )
        state = load_result.value

        # Guard: reject incomplete interviews
        if not state.is_complete:
            return Result.err(
                MCPToolError(
                    f"Interview '{session_id}' is not complete. "
                    "Finish the interview before generating a PM document.",
                    tool_name="ouroboros_pm_interview",
                )
            )

        # Restore PM meta into engine via engine.restore_meta()
        meta = _load_pm_meta(session_id, self.data_dir)
        if meta:
            engine.restore_meta(meta)

        seed_result = await engine.generate_pm_seed(state)
        if seed_result.is_err:
            return Result.err(
                MCPToolError(
                    str(seed_result.error),
                    tool_name="ouroboros_pm_interview",
                )
            )

        seed = seed_result.value

        # Save seed to ~/.ouroboros/seeds/ (idempotent — overwrites on retry)
        # Save seed and PM document with recovery contract
        try:
            seed_path = engine.save_pm_seed(seed)
            pm_output_dir = Path(cwd) / ".ouroboros"
            pm_path = save_pm_document(seed, output_dir=pm_output_dir)
        except Exception as e:
            log.error("pm_handler.generate_save_failed", error=str(e), session_id=session_id)
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=(
                                f"PM generation succeeded but saving artifacts failed: {e}\n"
                                f"Session ID: {session_id}\n"
                                f'Retry with: action="generate", session_id="{session_id}"'
                            ),
                        ),
                    ),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "is_complete": True,
                        "generation_failed": True,
                    },
                )
            )

        next_step = build_pm_dev_handoff_next_step(seed_path)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM seed generated: {seed.product_name}\n"
                            f"PM seed: {seed_path}\n"
                            f"PM document: {pm_path}\n\n"
                            "This PM seed is a handoff artifact for the dev interview, "
                            "not the runnable Seed.\n"
                            f"Decide-later items: {len(seed.decide_later_items)}\n"
                            f"Next: {next_step}"
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "seed_path": str(seed_path),
                    "pm_seed_path": str(seed_path),
                    "pm_path": str(pm_path),
                    "artifact_kind": "pm_seed",
                    "runnable": False,
                    "next_step": next_step,
                },
            )
        )
