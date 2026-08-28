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

import asyncio
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
from typing import Any

import structlog

from ouroboros.backends import backend_supports_tool_envelope
from ouroboros.bigbang.ambiguity import qualifies_for_seed_completion
from ouroboros.bigbang.answer_provenance import extraction_rounds
from ouroboros.bigbang.interview import (
    MIN_ROUNDS_BEFORE_EARLY_EXIT,
    InterviewState,
)
from ouroboros.bigbang.pm_completion import (
    build_pm_completion_summary,
    maybe_complete_pm_interview,
)
from ouroboros.bigbang.pm_document import save_pm_document
from ouroboros.bigbang.pm_interview import (
    PM_UNCERTAINTY_GUIDANCE,
    PMInterviewEngine,
    PMInterviewTurnPlan,
    decision_round_count,
)
from ouroboros.config import get_llm_backend_for_role, get_llm_model_for_role
from ouroboros.core.initial_context import resolve_initial_context_input
from ouroboros.core.owner_only import secure_directory, write_owner_only
from ouroboros.core.pm_snapshot import refresh_pm_snapshot_worktrees
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.host_context import resolve_request_subagent_dispatch
from ouroboros.mcp.tools.advisory_dispatch import append_question_advisory_dispatch
from ouroboros.mcp.tools.fanout import FanoutRegistry
from ouroboros.mcp.tools.pm_batch import (
    batch_entries_for_turns,
    batch_turn_meta_and_text,
    externalize_advisory_payloads,
    interview_answer_lock,
    record_turn_answers,
    skip_hint_suffix,
    turn_answers,
)
from ouroboros.mcp.tools.question_advisory import attach_question_advisory
from ouroboros.mcp.tools.subagent import (
    DELEGATED_TO_SUBAGENT,
    build_pm_interview_subagent,
    dispatch_plugin_terminal,
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
from ouroboros.orchestrator.capabilities.pm_schemas import pm_repository_roster
from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore
from ouroboros.persistence.event_store import EventStore
from ouroboros.pm.handoff import build_pm_dev_handoff_next_step
from ouroboros.providers import create_llm_adapter, resolve_llm_backend

log = structlog.get_logger()

# Hard cap on interview rounds in MCP mode.  The engine's ambiguity scorer
# should trigger completion well before this, but this prevents runaway loops.


_DATA_DIR = Path.home() / ".ouroboros" / "data"


def _refresh_plugin_repo_records(paths: list[Any]) -> list[Any]:
    """Return plugin repo records with scan and durable source paths.

    Plugin dispatch forwards ``selected_repos`` (path strings) to a child
    session that reads them directly, so the snapshot redirection must
    happen before the repos are persisted and handed to the subagent. The
    complete snapshot record is retained in pm_meta so later ``generate``
    turns can restore the durable source checkout identity.

    Non-string entries and repos that cannot be snapshotted (not a git
    repo, git/filesystem failure) pass through unchanged.
    """
    result: list[Any] = []
    for path in paths:
        if not isinstance(path, str):
            result.append(path)
            continue
        refreshed = refresh_pm_snapshot_worktrees([{"path": path}])
        result.append(refreshed[0])
    return result


def _plugin_repo_paths(repos: list[Any], *, durable: bool = False) -> list[Any]:
    """Project persisted plugin repo records to child-visible path strings."""
    result: list[Any] = []
    for repo in repos:
        if not isinstance(repo, dict):
            result.append(repo)
            continue
        preferred_key = "source_path" if durable else "path"
        path = repo.get(preferred_key) or repo.get("path") or repo.get("source_path")
        if path:
            result.append(path)
    return result


def _refresh_plugin_repo_paths(paths: list[Any]) -> list[Any]:
    """Backward-compatible scan-path projection for plugin repo refresh."""
    return _plugin_repo_paths(_refresh_plugin_repo_records(paths))


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
            # The reframe routing of the turn on the wire, whole — a batch
            # holds several, and the single entry above stays for older
            # readers. Planning replaces this map rather than adding to it, so
            # an abandoned turn's routing cannot reach a later question that
            # merely reads the same (RFC #2222 revision 4).
            "pending_reframes": dict(getattr(engine, "_reframe_map", {})),
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
    if data_dir is None:
        secure_directory(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    durability_confirmed = write_owner_only(
        path,
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not durability_confirmed:
        log.warning(
            "pm_handler.meta_save_durability_unconfirmed",
            session_id=session_id,
            path=str(path),
        )
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


def _format_pm_transcript(state: InterviewState, *, withhold_observations: bool = False) -> str:
    """Format PM rounds for question generation or requirement extraction.

    Resume/start subagents need raw observations to sharpen their next
    question.  The plugin ``generate`` action is a requirement-producing
    consumer, so it opts into the shared provenance projection instead.
    """
    if not state.rounds:
        return ""
    if withhold_observations:
        rendered = [
            (item.round_number, item.question, item.answer) for item in extraction_rounds(state)
        ]
    else:
        rendered = [(r.round_number, r.question, r.user_response) for r in state.rounds]
    lines: list[str] = []
    if state.initial_context:
        lines.append(f"**Product Idea:** {state.initial_context}")
        lines.append("")
    for round_number, question, answer in rendered:
        lines.append(f"**Q{round_number}:** {question}")
        if answer:
            lines.append(f"**A{round_number}:** {answer}")
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
    fanout_registry: FanoutRegistry | None = field(default=None, repr=False)
    findings_store: Any | None = field(default=None, repr=False)
    _answer_locks: dict[str, asyncio.Lock] = field(default_factory=dict, repr=False)

    async def _attach_advisory(self, meta: dict[str, Any], session_id: str, question: str) -> None:
        """Attach the evidence lanes to one PM turn that shows ``question``.

        The lanes, their contracts and their requiredness all come from this
        tool's catalog in the capability registry, through the same fan-out the
        interview runs. Nothing here is PM-shaped except the roster, which is
        the boundary PM's code lane is bounded by.

        The roster is read from persisted PM meta rather than from the engine,
        because two of the four question turns run on a session loaded from disk
        and have no engine state to read it from. Reading one source on all four
        is what keeps the roster from depending on how the turn was reached.

        ``findings_store`` travels the same way and for the same reason: any of
        the four turns can have recent findings behind it, so a turn reached by
        resume must be able to say where they are just as the turn that asked
        directly can.
        """
        pm_meta = _load_pm_meta(session_id, data_dir=self.data_dir)
        attach_question_advisory(
            meta,
            tool_name="ouroboros_pm_interview",
            session_id=session_id,
            question=question,
            repository_roster=pm_repository_roster(
                pm_meta.get("brownfield_repos") if pm_meta else None
            ),
            dispatch_mode=resolve_request_subagent_dispatch(
                self.agent_runtime_backend,
                self.opencode_mode,
            ),
            runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
            fanout_registry=self.fanout_registry,
            findings_store=self.findings_store,
        )
        # RFC #2222: briefs travel as references, not bodies (see pm_batch).
        await externalize_advisory_payloads(meta, self.findings_store)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition with flat optional parameters."""
        definition = MCPToolDefinition(
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
                    description=(
                        "PM's response to a single-question turn. Pass the question it "
                        "answers as 'last_question'. This singular form is mutually "
                        "exclusive with 'answers'; for a turn that asked more than one "
                        "question, use 'answers' instead."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="answers",
                    type=ToolInputType.ARRAY,
                    description=(
                        "A turn's answers, sent together: [{question, answer}, ...], one "
                        "entry per question the turn asked. This batch form is mutually "
                        "exclusive with 'answer'. A turn is recorded whole, so collect "
                        "every answer before calling. Each entry names its own question "
                        "and the batch accepts one to three entries."
                    ),
                    required=False,
                    items={
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "minLength": 1},
                            "answer": {"type": "string", "minLength": 1},
                        },
                        "required": ["question", "answer"],
                        "additionalProperties": False,
                    },
                    min_items=1,
                    max_items=3,
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
                MCPToolParameter(
                    name="last_question",
                    type=ToolInputType.STRING,
                    description=(
                        "The question this answer is answering. A turn persists "
                        "nothing when it asks, on any runtime, so an answer without "
                        "its question is refused — there is no remembered question "
                        "to file it under, and the round behind it is one somebody "
                        "already answered."
                    ),
                    required=False,
                ),
            ),
        )
        input_schema = definition.to_input_schema()
        input_schema["not"] = {"required": ["answer", "answers"]}
        return replace(definition, input_schema=input_schema)

    def _get_engine(self) -> PMInterviewEngine:
        """Return the injected engine or create a new one using the server's configured backend."""
        if self.pm_engine is not None:
            return self.pm_engine
        backend = get_llm_backend_for_role("pm_interview", explicit_backend=self.llm_backend)
        # ``strict_mcp_config=True`` mirrors InterviewHandler's #765 opt-in:
        # the PM question-generation subprocess runs as a child of Claude
        # Code's MCP host, and without strict isolation it re-discovers every
        # plugin/project MCP server — under a heavy harness the front-loaded
        # catalog alone overflows the context from round two (#1768).
        adapter = self.llm_adapter or create_llm_adapter(
            backend=backend,
            max_turns=1,
            use_case="interview",
            allowed_tools=[]
            if backend_supports_tool_envelope(resolve_llm_backend(backend))
            else None,
            strict_mcp_config=True,
        )
        model = get_llm_model_for_role("pm_interview", backend=backend)
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
        answers = arguments.get("answers")
        cwd_arg = arguments.get("cwd")
        selected_repos: list[str] | None = arguments.get("selected_repos")
        last_question = arguments.get("last_question")

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

                # The selected roster is recorded on the state, in the field the
                # other start paths already write. It was held only in
                # ``pm_meta``, so anything reading the state saw a session with
                # no repositories: the direct CLI's brownfield refusal, and the
                # seed generator's own check.
                if selected_repos:
                    state.codebase_paths = [
                        {"path": str(path), "role": "primary"} for path in selected_repos
                    ]
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
                #
                # Selected repos are redirected to refreshed snapshot
                # worktrees before persistence so the child session reads
                # remote-main state instead of a stale local checkout. The
                # cwd-derived fallback is deliberately NOT redirected: cwd is
                # the user's live working repo and may hold intentional WIP.
                persisted_repos: list[Any] = []
                if selected_repos is not None:
                    persisted_repos = await asyncio.to_thread(
                        _refresh_plugin_repo_records, selected_repos
                    )
                    selected_repos = _plugin_repo_paths(persisted_repos)
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
                # Redirect selected repos to refreshed snapshot worktrees so
                # the child session explores remote-main state, then update
                # pm_meta with them and mark interview_started.
                persisted_repos = await asyncio.to_thread(
                    _refresh_plugin_repo_records, selected_repos
                )
                selected_repos = _plugin_repo_paths(persisted_repos)
                meta["brownfield_repos"] = persisted_repos
                meta["status"] = "interview_started"
                # The other entrance to the same roster, recorded the same way.
                if selected_repos:
                    repo_state = await _plugin_load_state(state_dir, session_id)
                    if repo_state.is_ok:
                        repo_state.value.codebase_paths = [
                            {"path": str(path), "role": "primary"} for path in selected_repos
                        ]
                        repo_state.value.mark_updated()
                        marked = await _plugin_save_state(state_dir, repo_state.value)
                        if marked.is_err:
                            return Result.err(
                                MCPToolError(str(marked.error), tool_name="ouroboros_pm_interview")
                            )
                _save_pm_meta(
                    session_id,
                    engine=None,
                    cwd=meta.get("cwd", cwd_arg or os.getcwd()),
                    data_dir=self.data_dir,
                    status="interview_started",
                    extra={
                        "initial_context": meta.get("initial_context", ""),
                        "brownfield_repos": persisted_repos,
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
                if selected_repos is None:
                    meta = _load_pm_meta(session_id, data_dir=self.data_dir)
                    if meta:
                        if meta.get("brownfield_repos") is not None:
                            selected_repos = _plugin_repo_paths(
                                meta["brownfield_repos"],
                                durable=action == "generate",
                            )
                        # Also restore initial_context for generate prompts
                        if not initial_context and meta.get("initial_context"):
                            initial_context = meta["initial_context"]

                # Gate: generate requires an interview decision.  In plugin
                # mode is_complete is never set (child owns progression), so
                # we gate on the rounds instead.  The child session performs
                # the real completeness validation.
                #
                # A round is not a decision.  A confirmed lane finding occupies
                # one while being a fact the user adopted, and the transcript
                # built three lines down withholds its content -- so counting it
                # here would authorise a PM seed from a session where the user
                # decided nothing and hand the child an empty transcript under a
                # prompt saying the interview is complete.  Read ``provenance``
                # rather than the marker, the same field ``check_completion``
                # counts in-process, so the two runtimes agree by default.
                if action == "generate":
                    decided_rounds = [
                        r
                        for r in state.rounds
                        if r.user_response is not None and r.provenance == "user"
                    ]
                    if not state.is_complete and not decided_rounds:
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
                #
                # The ``last_question`` parameter solves the question-text gap:
                # the parent LLM sees the child's response (which contains the
                # question) and passes it back here so we can persist the real
                # question text instead of a placeholder.
                # A singular legacy/plugin resume may intentionally replace a
                # stale placeholder through ``last_question``. Only the batch
                # transport claims the persisted pending round is the turn it
                # is answering, so only that shape makes the stored identity
                # authoritative enough to validate.
                planned_questions = (
                    [state.rounds[-1].question]
                    if answers is not None
                    and state.rounds
                    and state.rounds[-1].user_response is None
                    else None
                )
                pairs, pair_error = turn_answers(
                    answers,
                    answer,
                    last_question,
                    planned_questions=planned_questions,
                )
                if pair_error:
                    return Result.err(MCPToolError(pair_error, tool_name="ouroboros_pm_interview"))
                if pairs:
                    # The same recorder the in-process branch uses, not a
                    # second one that agrees with it today. A pair became a
                    # round here once by a loop of its own, and that loop
                    # spelled `[decide_later]` as a sentence the user typed:
                    # a control token committed as a decision, and an open
                    # question the generated seed never heard was open.
                    #
                    # No engine is passed, and none is built: this runtime
                    # dispatches generation to a child precisely so the server
                    # need not hold an LLM adapter, and an answer being written
                    # down must not be what finally requires one.
                    #
                    # Every answer names its question, on every runtime: a turn
                    # persists nothing when it asks (RFC #2222 revision 4), so
                    # there is no stored question to prefer over the echo.
                    record_result = await record_turn_answers(None, state, pairs)
                    if record_result.is_err:
                        return Result.err(
                            MCPToolError(
                                str(record_result.error), tool_name="ouroboros_pm_interview"
                            )
                        )
                    state = record_result.value
                    state.mark_updated()
                    save_result = await _plugin_save_state(state_dir, state)
                    if save_result.is_err:
                        return Result.err(
                            MCPToolError(str(save_result.error), tool_name="ouroboros_pm_interview")
                        )
                # Build transcript from persisted rounds
                transcript = _format_pm_transcript(
                    state,
                    withhold_observations=action == "generate",
                )

            payload = build_pm_interview_subagent(
                session_id=real_session_id or "new",
                action=action,
                initial_context=initial_context,
                answer=answer,
                cwd=cwd_arg,
                selected_repos=selected_repos,
                transcript=transcript,
            )
            return await dispatch_plugin_terminal(
                self.event_store,
                session_id=real_session_id,
                payload=payload,
                response_shape={
                    "session_id": real_session_id,
                    "action": action,
                    "status": DELEGATED_TO_SUBAGENT,
                    "dispatch_mode": "plugin",
                    "next_turn_hint": (
                        "When the user answers, send the turn's answers as "
                        "'answers': [{question, answer}] — each answer names "
                        "its own question. A single answer may instead pass "
                        "the child session's question text as 'last_question' "
                        "alongside 'answer'. Either way the question travels "
                        "with the answer, which is what the transcript keeps."
                    ),
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
                async with interview_answer_lock(self._answer_locks, session_id):
                    return await self._handle_answer(
                        engine,
                        session_id,
                        answer,
                        cwd,
                        last_question=last_question,
                        answers=answers,
                    )

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
        interview_id: str | None = None,
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

        # Snapshot-worktree redirection happens inside
        # engine.ask_opening_and_start so CLI and MCP share one hook.
        result = await engine.ask_opening_and_start(
            user_response=initial_context,
            interview_id=interview_id,
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

        # RFC #2222 revision 4: a turn persists nothing when it asks. The
        # question travels in the response and comes back with its answer.
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
        skip_eligible = classification in ("decide_later", "deferred")

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
        await self._attach_advisory(meta, state.interview_id, question)

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
        start_text = (
            f"PM interview started. Session ID: {state.interview_id}\n\n"
            f"{PM_UNCERTAINTY_GUIDANCE}\n\n{question}"
        )
        start_text += skip_hint_suffix(classification, state.interview_id)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=append_question_advisory_dispatch(start_text, meta),
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
            interview_id=session_id,
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
        than once for the same session. Instead of re-starting the interview
        (which would create duplicate state), the existing ``InterviewState``
        is loaded and a question is planned from it.

        It plans rather than replays because a turn persists nothing when it
        asks (RFC #2222 revision 4): the question the first call returned was
        never written down, so there is none to hand back. This is the same
        trade a reconnect makes — one regenerated question, and no half-written
        state to keep consistent.
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
        engine.restore_meta(meta)
        question_result = await engine.ask_next_question(state)
        if question_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Session {session_id} is already started but a question could "
                    f"not be planned: {question_result.error}",
                    tool_name="ouroboros_pm_interview",
                )
            )
        first_question = question_result.value

        classification = _last_classification(engine)
        skip_eligible = classification in ("decide_later", "deferred")

        resume_meta: dict[str, Any] = {
            "session_id": session_id,
            "status": "interview_started",
            "question": first_question,
            "is_brownfield": state.is_brownfield,
            "idempotent": True,
            "classification": classification,
            "skip_eligible": skip_eligible,
        }
        # A resumed question is shown to the user like any other, so it carries
        # the lanes like any other. This is the turn where the "every question"
        # rule would otherwise fail quietly: the answer that follows looks
        # identical whether or not evidence was ever fetched for it.
        await self._attach_advisory(resume_meta, session_id, first_question)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=append_question_advisory_dispatch(
                            f"PM interview started. Session ID: {session_id}\n\n{first_question}",
                            resume_meta,
                        ),
                    ),
                ),
                is_error=False,
                meta=resume_meta,
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
        last_question: str | None = None,
        answers: Any = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Record a turn's answers, check completion, then ask the next turn.

        A turn is atomic (RFC #2222 revision 4): its answers arrive together,
        each holding the question it belongs to, and the rounds they become are
        the only durable state. A call with no answers is a host that lost its
        turn — the next turn is planned from the transcript rather than
        restored, so there is no pending question to reconcile against.

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

        # ── This turn's answers, each holding its question (RFC #2222 r4) ──
        # ``last_question`` is also the legacy repair path for a stale pending
        # question. A batched answer has no such override semantics: when a
        # pending round exists, choosing ``answers`` proves that stored plan is
        # the identity boundary this call is answering.
        planned_questions = (
            [state.rounds[-1].question]
            if answers is not None and state.rounds and state.rounds[-1].user_response is None
            else None
        )
        pairs, pair_error = turn_answers(
            answers,
            answer,
            last_question,
            planned_questions=planned_questions,
        )
        if pair_error:
            return Result.err(MCPToolError(pair_error, tool_name="ouroboros_pm_interview"))

        # ── Per-round diff snapshot — must be BEFORE any skip/record call ──
        # Snapshot list lengths here so that items appended inside
        # skip_as_decide_later() / skip_as_deferred() are captured in the
        # per-round diff returned at the end of this call.
        deferred_before = len(engine.deferred_items)
        decide_later_before = len(engine.decide_later_items)

        if pairs:
            record_result = await record_turn_answers(engine, state, pairs)
            if record_result.is_err:
                return Result.err(
                    MCPToolError(str(record_result.error), tool_name="ouroboros_pm_interview")
                )
            state = record_result.value
            save_result = await engine.save_state(state)
            if isinstance(save_result, Result) and save_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Failed to persist PM answer: {save_result.error}",
                        tool_name="ouroboros_pm_interview",
                    )
                )
            _save_pm_meta(session_id, engine, cwd=cwd, data_dir=self.data_dir)

        completion: dict[str, Any] | None = None
        supports_atomic_turn = (
            isinstance(PMInterviewEngine, type)
            and isinstance(engine, PMInterviewEngine)
            and engine.supports_atomic_turn is True
        )
        batch_turns: list[PMInterviewTurnPlan] = []
        if supports_atomic_turn:
            turns_result = await engine.plan_next_turns(state)
            if turns_result.is_err:
                error_msg = str(turns_result.error)
                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(
                                type=ContentType.TEXT,
                                text=(
                                    f"Question generation failed. Session ID: {session_id}\n\n"
                                    f'Resume with: session_id="{session_id}"\n\n'
                                    f"Reason: {error_msg[:200]}"
                                ),
                            ),
                        ),
                        is_error=True,
                        meta={"session_id": session_id, "recoverable": True},
                    )
                )
            batch_turns = turns_result.value
            turn: PMInterviewTurnPlan = batch_turns[0]
            question = turn.question
            if turn.ambiguity is not None:
                state.store_ambiguity(
                    score=turn.ambiguity.overall_score,
                    breakdown=turn.ambiguity.breakdown.model_dump(mode="json"),
                )
                answered_rounds = decision_round_count(state)
                if (
                    answered_rounds >= MIN_ROUNDS_BEFORE_EARLY_EXIT
                    and qualifies_for_seed_completion(
                        turn.ambiguity,
                        is_brownfield=state.is_brownfield,
                    )
                ):
                    completion = {
                        "interview_complete": True,
                        "completion_reason": "ambiguity_resolved",
                        "rounds_completed": answered_rounds,
                        "ambiguity_score": turn.ambiguity.overall_score,
                    }
                    complete_result = await engine.complete_interview(state)
                    if complete_result.is_err:
                        return Result.err(
                            MCPToolError(
                                f"Failed to complete interview: {complete_result.error}",
                                tool_name="ouroboros_pm_interview",
                            )
                        )
                    state = complete_result.value
        else:
            completion_result = await maybe_complete_pm_interview(state, engine)
            if completion_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Failed to complete interview: {completion_result.error}",
                        tool_name="ouroboros_pm_interview",
                    )
                )
            state, completion = completion_result.value
            if completion is None:
                question_result = await engine.ask_next_question(state)
                if question_result.is_err:
                    return Result.ok(
                        MCPToolResult(
                            content=(
                                MCPContentItem(
                                    type=ContentType.TEXT,
                                    text=(
                                        f"Question generation failed. Session ID: {session_id}\n\n"
                                        f'Resume with: session_id="{session_id}"'
                                    ),
                                ),
                            ),
                            is_error=True,
                            meta={"session_id": session_id, "recoverable": True},
                        )
                    )
                question = question_result.value
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

        # Compute diff AFTER ask_next_question — new items are the
        # slice from the pre-snapshot length to current length
        diff = _compute_deferred_diff(engine, deferred_before, decide_later_before)

        # ── Batched turn (RFC #2222) — asked whole, answered whole ──
        if len(batch_turns) > 1:
            state.mark_updated()
            save_result = await engine.save_state(state)
            if isinstance(save_result, Result) and save_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Failed to persist resume state: {save_result.error}",
                        tool_name="ouroboros_pm_interview",
                    )
                )
            # The questions go out in the response and nowhere else. Persisting
            # them would recreate the pending list revision 4 removed — the
            # second place a turn was remembered, and the seam every replay and
            # ordering defect lived in.
            batch_entries = batch_entries_for_turns(batch_turns)
            _save_pm_meta(session_id, engine, cwd=cwd, data_dir=self.data_dir)

            # One fan-out per question in its own envelope, so fanout ids and
            # payloads never overwrite each other (one wave, submit per envelope).
            advisories: list[dict[str, Any]] = []
            for t in batch_turns:
                envelope: dict[str, Any] = {"question": t.question}
                await self._attach_advisory(envelope, session_id, t.question)
                advisories.append(envelope)

            response_meta, response_text = batch_turn_meta_and_text(
                session_id,
                batch_entries,
                advisories,
                pending_reframe=engine.get_pending_reframe(),
                diff=diff,
            )
            for envelope in advisories:
                response_text = append_question_advisory_dispatch(response_text, envelope)

            log.info(
                "pm_handler.question_batch_asked",
                session_id=session_id,
                batch_size=len(batch_entries),
                **diff,
            )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=response_text),),
                    is_error=False,
                    meta=response_meta,
                )
            )

        # RFC #2222 revision 4: nothing is persisted until it is whole.
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
        skip_eligible = classification in ("decide_later", "deferred")

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
        await self._attach_advisory(response_meta, session_id, question)

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
        response_text += skip_hint_suffix(classification, session_id)

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=append_question_advisory_dispatch(response_text, response_meta),
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
