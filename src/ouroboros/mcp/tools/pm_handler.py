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
reaching the maximum round limit.  There is no user "done" signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

import structlog

from ouroboros.bigbang.ambiguity import AmbiguityScorer
from ouroboros.bigbang.interview import (
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewRound,
    InterviewState,
)
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore

log = structlog.get_logger()

# Hard cap on interview rounds in MCP mode.  The engine's ambiguity scorer
# should trigger completion well before this, but this prevents runaway loops.
MAX_PM_INTERVIEW_ROUNDS = 20

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
        status: str | None  — e.g. "awaiting_repo_selection", "interview_started"
    """
    # Engine may be None for step-1 (awaiting_repo_selection) saves
    if engine is not None:
        reframe_map = engine._reframe_map
        # pending_reframe: single {reframed, original} object or None
        pending_reframe: dict[str, str] | None = None
        if reframe_map:
            # Take the most recent entry (last inserted)
            reframed = next(reversed(reframe_map))
            pending_reframe = {
                "reframed": reframed,
                "original": reframe_map[reframed],
            }

        meta: dict[str, Any] = {
            "deferred_items": list(engine.deferred_items),
            "decide_later_items": list(engine.decide_later_items),
            "codebase_context": engine.codebase_context,
            "pending_reframe": pending_reframe,
            "cwd": cwd,
        }
    else:
        meta = {
            "deferred_items": [],
            "decide_later_items": [],
            "codebase_context": "",
            "pending_reframe": None,
            "cwd": cwd,
        }

    if status is not None:
        meta["status"] = status

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
    """Restore PM-specific state into an engine from loaded meta."""
    engine.deferred_items = list(meta.get("deferred_items", []))
    engine.decide_later_items = list(meta.get("decide_later_items", []))
    engine.codebase_context = meta.get("codebase_context", "")
    # Restore the reframe map from pending_reframe if present
    pending = meta.get("pending_reframe")
    if pending and isinstance(pending, dict):
        engine._reframe_map[pending["reframed"]] = pending["original"]


def _last_classification(engine: PMInterviewEngine) -> str | None:
    """Return the output_type string of the engine's last classification, or None."""
    if engine.classifications:
        return engine.classifications[-1].output_type.value
    return None


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

    Compares list lengths before and after the call to determine which
    new items were added during classification.  Returns a dict with:
        new_deferred: list of newly deferred question texts
        new_decide_later: list of newly decide-later question texts
        deferred_count: total deferred items
        decide_later_count: total decide-later items

    This is the core diff computation for AC 8.
    """
    new_deferred = engine.deferred_items[deferred_len_before:]
    new_decide_later = engine.decide_later_items[decide_later_len_before:]

    return {
        "new_deferred": list(new_deferred),
        "new_decide_later": list(new_decide_later),
        "deferred_count": len(engine.deferred_items),
        "decide_later_count": len(engine.decide_later_items),
    }


async def _check_completion(
    state: InterviewState,
    engine: PMInterviewEngine,
) -> dict[str, Any] | None:
    """Check whether the interview should complete based on ambiguity or rounds.

    Completion is determined by two signals (no user "done" signal):

    1. **Ambiguity score** — after at least ``MIN_ROUNDS_BEFORE_EARLY_EXIT``
       answered rounds, the scorer evaluates requirement clarity.  If the score
       is ≤ ``AMBIGUITY_THRESHOLD`` (0.2) the interview is ready for PM
       generation.

    2. **Max-rounds safety cap** — after ``MAX_PM_INTERVIEW_ROUNDS`` rounds
       the interview is force-completed to prevent runaway loops.

    Returns a dict with completion metadata if the interview should end,
    or ``None`` if the interview should continue.
    """
    # Count only answered rounds (exclude the pending unanswered round)
    answered_rounds = sum(1 for r in state.rounds if r.user_response is not None)

    # ── Max-rounds hard cap ────────────────────────────────────────
    if answered_rounds >= MAX_PM_INTERVIEW_ROUNDS:
        log.info(
            "pm_handler.completion.max_rounds",
            session_id=state.interview_id,
            rounds=answered_rounds,
        )
        return {
            "interview_complete": True,
            "completion_reason": "max_rounds",
            "rounds_completed": answered_rounds,
            "ambiguity_score": None,
        }

    # ── Ambiguity check (only after minimum rounds) ────────────────
    if answered_rounds < MIN_ROUNDS_BEFORE_EARLY_EXIT:
        return None

    try:
        # Build additional context for scorer: decide-later items are
        # intentional deferrals that should not penalise clarity.
        additional_context = ""
        if engine.decide_later_items:
            additional_context = "Decide-later items (intentional deferrals):\n"
            additional_context += "\n".join(
                f"- {item}" for item in engine.decide_later_items
            )

        scorer = AmbiguityScorer(
            llm_adapter=engine.llm_adapter,
            model=engine.model,
        )
        score_result = await scorer.score(
            state,
            is_brownfield=state.is_brownfield,
            additional_context=additional_context,
        )

        if score_result.is_err:
            log.warning(
                "pm_handler.completion.scoring_failed",
                session_id=state.interview_id,
                error=str(score_result.error),
            )
            # Scoring failed — continue the interview rather than blocking
            return None

        ambiguity = score_result.value

        # Persist score on state for downstream use
        state.store_ambiguity(
            score=ambiguity.overall_score,
            breakdown=ambiguity.breakdown.model_dump(mode="json"),
        )

        if ambiguity.is_ready_for_seed:
            log.info(
                "pm_handler.completion.ambiguity_resolved",
                session_id=state.interview_id,
                ambiguity_score=ambiguity.overall_score,
                rounds=answered_rounds,
            )
            return {
                "interview_complete": True,
                "completion_reason": "ambiguity_resolved",
                "rounds_completed": answered_rounds,
                "ambiguity_score": ambiguity.overall_score,
            }

        log.debug(
            "pm_handler.completion.continuing",
            session_id=state.interview_id,
            ambiguity_score=ambiguity.overall_score,
            rounds=answered_rounds,
        )

    except Exception as e:
        log.warning(
            "pm_handler.completion.check_error",
            session_id=state.interview_id,
            error=str(e),
        )

    return None


@dataclass
class PMInterviewHandler:
    """Handler for the ouroboros_pm_interview MCP tool.

    Manages PM-focused interviews with question classification,
    deferred item tracking, and per-call diff computation.

    Interview completion is determined solely by the engine's ambiguity
    scorer (score ≤ 0.2) or max-rounds cap — there is no user "done"
    signal.

    The handler wraps PMInterviewEngine and adds:
    - Flat MCP parameter interface (session_id, action, answer, cwd, initial_context)
    - pm_meta_{session_id}.json persistence for PM-specific state
    - Deferred/decide-later diff computation per ask_next_question call
    - Automatic completion detection via ambiguity scoring and max-rounds
    """

    pm_engine: PMInterviewEngine | None = field(default=None, repr=False)
    data_dir: Path | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition with flat optional parameters."""
        return MCPToolDefinition(
            name="ouroboros_pm_interview",
            description=(
                "PM interview for product requirements gathering. "
                "Start with initial_context, continue with session_id + answer, "
                "or generate PM seed with action='generate'."
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
        """Return the injected engine or create a new one with default adapter."""
        if self.pm_engine is not None:
            return self.pm_engine
        adapter = ClaudeAgentAdapter(permission_mode="bypassPermissions")
        return PMInterviewEngine.create(
            llm_adapter=adapter,
            state_dir=self.data_dir or _DATA_DIR,
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
        cwd = arguments.get("cwd") or os.getcwd()
        selected_repos: list[str] | None = arguments.get("selected_repos")

        # Auto-detect action from parameter presence (AC 13)
        action = _detect_action(arguments)

        engine = self._get_engine()

        try:
            # ── Generate PM seed ──────────────────────────────────
            if action == "generate" and session_id:
                return await self._handle_generate(engine, session_id, cwd)

            # ── Step 2: repo selection (AC 4) ─────────────────────
            if action == "select_repos" and selected_repos is not None:
                return await self._handle_select_repos(
                    engine, selected_repos, session_id, initial_context, cwd,
                )

            # ── Start new interview ────────────────────────────────
            if action == "start" and initial_context:
                return await self._handle_start(
                    engine, initial_context, cwd, selected_repos=selected_repos,
                )

            # ── Resume with answer ─────────────────────────────────
            if action == "resume" and session_id:
                # Check pm_meta for pending states (project type or repo selection)
                meta_data = _load_pm_meta(session_id, data_dir=self.data_dir)
                if meta_data:
                    status = meta_data.get("status", "")

                    # User answered brownfield/greenfield question
                    if status == "awaiting_project_type" and answer:
                        return await self._handle_project_type_answer(
                            engine, session_id, answer, cwd, meta_data,
                        )

                    # User answered repo selection (recovery case)
                    if status == "awaiting_repo_selection":
                        recovered = await self._maybe_recover_awaiting_session(
                            session_id, cwd,
                        )
                        if recovered is not None:
                            return recovered

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
        """Start a new PM interview session (supports 2-step start).

        2-step start pattern:
            Step 1 — ``selected_repos`` absent:
                Query DB for all repos.  If repos exist, return the list
                and save ``pm_meta.status = "awaiting_repo_selection"``
                without starting the actual interview engine.

            Step 2 — ``selected_repos`` present (+ ``initial_context``):
                Start the interview with the selected repos as brownfield
                context (all repos assigned ``role: main``).

        Greenfield fast-path: if no repos in DB at step 1, skip straight
        to the full interview start (no step 2 needed).

        Backward compat: ``selected_repos`` with ``initial_context`` in
        a single call behaves identically to the old 1-step flow.
        """
        # ── Step-1: ask brownfield vs greenfield ────────────────
        if selected_repos is None:
            return self._step1_ask_project_type(initial_context, cwd)

        # ── Full interview start (step-2 or greenfield) ───────────
        brownfield_repos = None
        if selected_repos:
            resolved = await self._resolve_repos_from_db(selected_repos)
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
                    "pm_handler.start.selected_repos",
                    count=len(resolved),
                    paths=[r.path for r in resolved],
                )
            else:
                # All selected paths missing from DB → auto-greenfield
                log.info(
                    "pm_handler.start.selected_repos_all_missing",
                    requested=selected_repos,
                )

        result = await engine.ask_opening_and_start(
            user_response=initial_context,
            brownfield_repos=brownfield_repos,
        )
        if result.is_err:
            return Result.err(
                MCPToolError(str(result.error), tool_name="ouroboros_pm_interview")
            )

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
                round_number=1,
                question=question,
                user_response=None,
            )
        )
        state.mark_updated()

        # Persist
        await engine.save_state(state)
        _save_pm_meta(
            state.interview_id, engine, cwd=cwd, data_dir=self.data_dir,
            status="interview_started",
        )

        # Include pending_reframe in response meta if a reframe occurred
        pending_reframe = None
        if engine._reframe_map:
            reframed = next(reversed(engine._reframe_map))
            pending_reframe = {
                "reframed": reframed,
                "original": engine._reframe_map[reframed],
            }

        meta = {
            "session_id": state.interview_id,
            "status": "interview_started",
            "input_type": "freeText",
            "response_param": "answer",
            "question": question,
            "is_brownfield": state.is_brownfield,
            "pending_reframe": pending_reframe,
            **diff,
        }

        log.info(
            "pm_handler.started",
            session_id=state.interview_id,
            is_brownfield=state.is_brownfield,
            has_pending_reframe=pending_reframe is not None,
            **diff,
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM interview started. Session ID: {state.interview_id}\n\n"
                            f"{question}"
                        ),
                    ),
                ),
                is_error=False,
                meta=meta,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Step-1: brownfield vs greenfield choice
    # ──────────────────────────────────────────────────────────────

    async def _handle_project_type_answer(
        self,
        engine: PMInterviewEngine,
        session_id: str,
        answer: str,
        cwd: str,
        meta_data: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle the brownfield/greenfield answer from step 1."""
        initial_context = meta_data.get("initial_context", "")

        if "brownfield" in answer.lower() or "기존" in answer or "existing" in answer.lower():
            # Brownfield → show repo selection
            all_repos = await self._query_all_repos()
            if not all_repos:
                # No repos in DB → auto-scan home directory
                log.info("pm_handler.brownfield_auto_scan")
                all_repos = await self._auto_scan_repos()

            if all_repos:
                return self._step1_awaiting_repo_selection(
                    initial_context, cwd, all_repos, session_id=session_id,
                )

            # Scan found nothing → greenfield with note
            log.info("pm_handler.brownfield_no_repos_after_scan")
            return await self._start_greenfield_interview(
                engine, initial_context, cwd,
                note="No GitHub repos found on this machine. Proceeding in greenfield mode.",
            )

        # Greenfield → start interview directly
        return await self._start_greenfield_interview(
            engine, initial_context, cwd,
        )

    async def _start_greenfield_interview(
        self,
        engine: PMInterviewEngine,
        initial_context: str,
        cwd: str,
        *,
        note: str = "",
    ) -> Result[MCPToolResult, MCPServerError]:
        """Start interview without brownfield context (greenfield)."""
        log.info("pm_handler.start.greenfield")
        result = await engine.ask_opening_and_start(
            user_response=initial_context,
            brownfield_repos=None,
        )
        if result.is_err:
            return Result.err(
                MCPToolError(str(result.error), tool_name="ouroboros_pm_interview")
            )

        state = result.value
        deferred_before = len(engine.deferred_items)
        decide_later_before = len(engine.decide_later_items)

        question_result = await engine.ask_next_question(state)
        if question_result.is_err:
            return Result.err(
                MCPToolError(str(question_result.error), tool_name="ouroboros_pm_interview")
            )

        question = question_result.value
        diff = _compute_deferred_diff(engine, deferred_before, decide_later_before)

        state.rounds.append(
            InterviewRound(round_number=1, question=question, user_response=None)
        )
        state.mark_updated()
        await engine.save_state(state)
        _save_pm_meta(
            state.interview_id, engine, cwd=cwd, data_dir=self.data_dir,
            status="interview_started",
        )

        pending_reframe = None
        if engine._reframe_map:
            reframed = next(reversed(engine._reframe_map))
            pending_reframe = {"reframed": reframed, "original": engine._reframe_map[reframed]}

        prefix = f"{note}\n\n" if note else ""
        meta = {
            "session_id": state.interview_id,
            "status": "interview_started",
            "input_type": "freeText",
            "response_param": "answer",
            "question": question,
            "is_brownfield": False,
            "pending_reframe": pending_reframe,
            **diff,
        }

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"{prefix}PM interview started. Session ID: {state.interview_id}\n\n{question}",
                    ),
                ),
                is_error=False,
                meta=meta,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Step-1: brownfield vs greenfield choice
    # ──────────────────────────────────────────────────────────────

    def _step1_ask_project_type(
        self,
        initial_context: str,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Step 1: Ask the user whether this is brownfield or greenfield."""
        from datetime import UTC, datetime

        session_id = f"interview_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

        _save_pm_meta(
            session_id,
            engine=None,
            cwd=cwd,
            data_dir=self.data_dir,
            status="awaiting_project_type",
            extra={"initial_context": initial_context},
        )

        options = [
            {
                "value": "brownfield",
                "label": "Add to existing project",
                "selected": False,
            },
            {
                "value": "greenfield",
                "label": "Start a new project",
                "selected": False,
            },
        ]

        meta = {
            "session_id": session_id,
            "status": "awaiting_project_type",
            "input_type": "singleSelect",
            "options": options,
            "response_param": "answer",
        }

        log.info("pm_handler.step1_ask_project_type", session_id=session_id)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM interview session created: {session_id}\n\n"
                            "Is this an addition to an existing codebase, "
                            "or a brand-new project?"
                        ),
                    ),
                ),
                is_error=False,
                meta=meta,
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Step-2 helpers (repo selection or greenfield start)
    # ──────────────────────────────────────────────────────────────

    async def _auto_scan_repos(self) -> list[BrownfieldRepo]:
        """Scan home directory for GitHub repos and register them in DB."""
        try:
            from ouroboros.bigbang.brownfield import scan_and_register

            store = BrownfieldStore()
            await store.initialize()
            try:
                repos = await scan_and_register(store=store)
                log.info("pm_handler.auto_scan_complete", found=len(repos))
                return repos
            finally:
                await store.close()
        except Exception as exc:
            log.warning("pm_handler.auto_scan_failed", error=str(exc))
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
        self, paths: list[str],
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

    def _step1_awaiting_repo_selection(
        self,
        initial_context: str,
        cwd: str,
        repos: list[BrownfieldRepo],
        *,
        session_id: str | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Return repo list and save pm_meta with awaiting_repo_selection status.

        Generates a session_id using the interview naming pattern but does NOT
        start the interview engine yet — that happens in step 2 when the caller
        sends back ``selected_repos``.

        If ``session_id`` is provided (e.g. during restart recovery), reuses it
        instead of generating a new one.
        """
        if session_id is None:
            from datetime import UTC, datetime

            session_id = f"interview_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"

        # Serialize repos for the response
        repo_list = [
            {
                "path": r.path,
                "name": r.name,
                "desc": r.desc or "",
                "is_default": r.is_default,
            }
            for r in repos
        ]

        # Build multiSelect options for the caller UI
        options = [
            {
                "value": r.path,
                "label": f"{r.name}" + (f" — {r.desc}" if r.desc else ""),
                "selected": r.is_default,
            }
            for r in repos
        ]

        # Persist minimal pm_meta so step 2 can pick up
        _save_pm_meta(
            session_id,
            engine=None,
            cwd=cwd,
            data_dir=self.data_dir,
            status="awaiting_repo_selection",
            extra={"initial_context": initial_context},
        )

        log.info(
            "pm_handler.step1_awaiting_repo_selection",
            session_id=session_id,
            repo_count=len(repo_list),
        )

        meta: dict[str, Any] = {
            "session_id": session_id,
            "status": "awaiting_repo_selection",
            "input_type": "multiSelect",
            "options": options,
            "response_param": "selected_repos",
            "repos": repo_list,
            "repo_count": len(repo_list),
        }

        # Build human-readable repo list text
        repo_lines = []
        for r in repo_list:
            label = r["name"]
            if r.get("desc"):
                label += f" — {r['desc']}"
            if r.get("is_default"):
                label += " (default)"
            repo_lines.append(f"  • {r['path']}  [{label}]")
        repo_list_text = "\n".join(repo_lines)

        content_text = (
            f"Session created: {session_id}\n\n"
            f"Found {len(repo_list)} registered repository(ies). "
            f"Please select which repos to include as brownfield context:\n\n"
            f"{repo_list_text}\n\n"
            f"Reply with selected_repos (list of paths) + initial_context "
            f"to start the interview."
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=content_text,
                    ),
                ),
                is_error=False,
                meta=meta,
            )
        )

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
                engine, initial_context, cwd, selected_repos=selected_repos,
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

        # Update is_default in DB for selected repos
        await self._sync_defaults_to_db(selected_repos)

        return await self._handle_start(
            engine, saved_context, cwd, selected_repos=selected_repos,
        )

    async def _sync_defaults_to_db(self, selected_paths: list[str]) -> None:
        """Update is_default in DB: selected paths → true, others → false.

        Also triggers LLM desc generation for newly-selected repos with empty desc.
        """
        from pathlib import Path as _Path

        from sqlalchemy import update

        from ouroboros.bigbang.brownfield import generate_desc
        from ouroboros.persistence.schema import brownfield_repos_table as t

        try:
            store = BrownfieldStore()
            await store.initialize()
            try:
                engine = store._ensure_initialized("_sync_defaults_to_db")
                selected_set = set(selected_paths)

                async with engine.begin() as conn:
                    # Clear all defaults
                    await conn.execute(
                        update(t).where(t.c.is_default.is_(True)).values(is_default=False)
                    )
                    # Set selected as default
                    if selected_set:
                        await conn.execute(
                            update(t).where(t.c.path.in_(selected_set)).values(is_default=True)
                        )

                log.info(
                    "pm_handler.sync_defaults_done",
                    selected_count=len(selected_paths),
                )

                # Generate desc for selected repos that lack one
                all_repos = await store.list()
                for repo in all_repos:
                    if repo.path in selected_set and not repo.desc:
                        try:
                            llm_adapter = self._get_engine().inner.llm_adapter
                            desc = await generate_desc(_Path(repo.path), llm_adapter)
                            if desc:
                                await store.update_desc(repo.path, desc)
                                log.info("pm_handler.desc_generated", path=repo.path)
                        except Exception as exc:
                            log.warning(
                                "pm_handler.desc_generation_failed",
                                path=repo.path, error=str(exc),
                            )
            finally:
                await store.close()
        except Exception as exc:
            log.warning("pm_handler.sync_defaults_failed", error=str(exc))

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
        first_question = (
            state.rounds[0].question if state.rounds else "No question available."
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM interview started. Session ID: {session_id}\n\n"
                            f"{first_question}"
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
                },
            )
        )

    # ──────────────────────────────────────────────────────────────
    # Restart recovery (AC 10)
    # ──────────────────────────────────────────────────────────────

    async def _maybe_recover_awaiting_session(
        self,
        session_id: str,
        cwd: str,
    ) -> Result[MCPToolResult, MCPServerError] | None:
        """Recover a session stuck in ``awaiting_repo_selection`` after restart.

        After an MCP server restart, all in-memory state is lost.  If a
        session was between step 1 and step 2 of the 2-step start, there
        is a ``pm_meta`` file on disk with ``status="awaiting_repo_selection"``
        but no ``InterviewState`` file (the engine was never started).

        This method detects that situation and re-sends the repo selection
        prompt so the user can pick up where they left off.

        Returns ``None`` if the session is *not* in awaiting state (i.e.
        the normal resume path should be used).
        """
        meta = _load_pm_meta(session_id, self.data_dir)
        if meta is None or meta.get("status") != "awaiting_repo_selection":
            return None

        log.info(
            "pm_handler.restart_recovery.awaiting_repo_selection",
            session_id=session_id,
        )

        # Re-query repos from DB (they may have changed since step 1)
        all_repos = await self._query_all_repos()

        if not all_repos:
            # Repos were removed between step 1 and restart → auto-greenfield
            saved_context = meta.get("initial_context", "")
            if not saved_context:
                return Result.err(
                    MCPToolError(
                        f"Session {session_id} was awaiting repo selection "
                        "but has no initial_context in pm_meta.",
                        tool_name="ouroboros_pm_interview",
                    )
                )

            log.info(
                "pm_handler.restart_recovery.auto_greenfield",
                session_id=session_id,
            )
            engine = self._get_engine()
            return await self._handle_start(engine, saved_context, cwd)

        # Re-send the repo selection prompt (preserves the same session_id)
        return self._step1_awaiting_repo_selection(
            meta.get("initial_context", ""),
            cwd,
            all_repos,
            session_id=session_id,
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

        Completion is determined solely by the engine — either the ambiguity
        score drops below the threshold (requirements are clear) or the
        max-rounds cap is reached.  There is no user "done" signal.
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
            _restore_engine_meta(engine, meta)

        # Record answer if provided
        if answer and state.rounds:
            last_question = state.rounds[-1].question
            if state.rounds[-1].user_response is None:
                state.rounds.pop()

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
        # No user "done" signal — completion is determined solely by
        # engine ambiguity scoring and max-rounds cap.
        completion = await _check_completion(state, engine)
        if completion is not None:
            # Mark interview as complete
            await engine.complete_interview(state)
            await engine.save_state(state)
            _save_pm_meta(session_id, engine, cwd=cwd, data_dir=self.data_dir)

            decide_later_summary = engine.format_decide_later_summary()
            summary_text = (
                f"Interview complete. Session ID: {session_id}\n"
                f"Rounds completed: {completion['rounds_completed']}\n"
                f"Completion reason: {completion['completion_reason']}\n"
            )
            if completion.get("ambiguity_score") is not None:
                summary_text += f"Ambiguity score: {completion['ambiguity_score']:.2f}\n"
            summary_text += (
                f"\nDeferred items: {len(engine.deferred_items)}\n"
                f"Decide-later items: {len(engine.decide_later_items)}\n"
            )
            if decide_later_summary:
                summary_text += f"\n{decide_later_summary}\n"
            summary_text += (
                f'\nGenerate PM with: action="generate", session_id="{session_id}"'
            )

            response_meta = {
                "session_id": session_id,
                "question": None,
                "is_complete": True,
                "classification": _last_classification(engine),
                "deferred_this_round": [],
                "decide_later_this_round": [],
                **completion,
                "deferred_count": len(engine.deferred_items),
                "decide_later_count": len(engine.decide_later_items),
            }

            log.info(
                "pm_handler.interview_complete",
                session_id=session_id,
                **completion,
            )

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

        # ── Core diff computation (AC 8) ──────────────────────────
        # Snapshot list lengths BEFORE ask_next_question
        deferred_before = len(engine.deferred_items)
        decide_later_before = len(engine.decide_later_items)

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
            return Result.err(
                MCPToolError(error_msg, tool_name="ouroboros_pm_interview")
            )

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

        await engine.save_state(state)
        _save_pm_meta(session_id, engine, cwd=cwd, data_dir=self.data_dir)

        # Include pending_reframe in response meta if a new reframe occurred
        pending_reframe = None
        if engine._reframe_map:
            reframed = next(reversed(engine._reframe_map))
            pending_reframe = {
                "reframed": reframed,
                "original": engine._reframe_map[reframed],
            }

        # Extract classification from the last classify call
        classification = _last_classification(engine)

        response_meta = {
            "session_id": session_id,
            "input_type": "freeText",
            "response_param": "answer",
            "question": question,
            "is_complete": False,
            "classification": classification,
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
            has_pending_reframe=pending_reframe is not None,
            **diff,
        )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=f"Session {session_id}\n\n{question}",
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
        """Generate PM seed from completed interview (idempotent).

        Loads InterviewState and pm_meta, restores engine via restore_meta(),
        runs generate_pm_seed, saves PM seed to ~/.ouroboros/seeds/ and
        pm.md to {cwd}/.ouroboros/.  Idempotent — overwrites on retry with
        the same session_id.
        """
        load_result = await engine.load_state(session_id)
        if load_result.is_err:
            return Result.err(
                MCPToolError(str(load_result.error), tool_name="ouroboros_pm_interview")
            )
        state = load_result.value

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
        seed_path = engine.save_pm_seed(seed)

        # Save pm.md to {cwd}/.ouroboros/
        pm_output_dir = Path(cwd) / ".ouroboros"
        pm_path = engine.save_pm_document(seed, output_dir=pm_output_dir)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=(
                            f"PM seed generated: {seed.product_name}\n"
                            f"Seed: {seed_path}\n"
                            f"Document: {pm_path}\n\n"
                            f"Deferred items: {len(seed.deferred_items)}\n"
                            f"Decide-later items: {len(seed.decide_later_items)}"
                        ),
                    ),
                ),
                is_error=False,
                meta={
                    "session_id": session_id,
                    "pm_path": str(pm_path),
                    "seed_path": str(seed_path),
                },
            )
        )
