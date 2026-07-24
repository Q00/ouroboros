"""Evaluation-phase tool handlers for Ouroboros MCP server.

Contains handlers for drift measurement, evaluation, and lateral thinking tools:
- MeasureDriftHandler: Measures goal deviation from seed specification.
- EvaluateHandler: Three-stage evaluation pipeline (mechanical, semantic, consensus).
- LateralThinkHandler: Generates alternative thinking approaches via personas.
"""

import asyncio
import base64
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.backends import build_runtime_subagent_orchestration_contract
from ouroboros.config import get_llm_backend_for_role, get_llm_model_for_role
from ouroboros.core.errors import ValidationError
from ouroboros.core.project_paths import resolve_path_against_base, resolve_seed_project_path
from ouroboros.core.seed import AcceptanceCriterionSpec, Seed, ac_text
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.tools.background import start_background_tool_job
from ouroboros.mcp.tools.bridge_mixin import BridgeAwareMixin
from ouroboros.mcp.tools.job_observer import build_job_observer_contract
from ouroboros.mcp.tools.subagent import (
    DELEGATED_TO_PLUGIN,
    DELEGATED_TO_SUBAGENT,
    FanoutRegistry,
    build_evaluate_subagent,
    dispatch_plugin_terminal,
    should_dispatch_via_plugin,
    submit_fanout_results,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.observability.drift import (
    DRIFT_THRESHOLD,
    DriftMeasurement,
)
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    allowed_runtime_builtin_tool_names,
)
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers import create_llm_adapter

log = structlog.get_logger(__name__)


def _seed_acceptance_criteria(seed: Seed) -> tuple[str, ...]:
    return tuple(
        stripped
        for criterion in seed.acceptance_criteria
        if (stripped := ac_text(criterion).strip())
    )


def _seed_ac_spec_map(seed: Seed | None) -> dict[str, AcceptanceCriterionSpec]:
    """Map each AC description to its structured spec (PR-H).

    Since PR-F, ``Seed.acceptance_criteria`` holds ``AcceptanceCriterionSpec``
    objects.  Evaluation flows AC text (descriptions) around as bare strings,
    so we key specs by their trimmed ``description`` to reattach the declared
    success contract when building each ``EvaluationContext``.  Only specs
    that actually carry a contract are included — bare-description ACs map to
    nothing and the evaluator judges the AC text alone (today's behavior).
    """
    if seed is None:
        return {}
    spec_map: dict[str, AcceptanceCriterionSpec] = {}
    for criterion in seed.acceptance_criteria:
        if not isinstance(criterion, AcceptanceCriterionSpec):
            continue
        if not criterion.has_success_contract:
            continue
        key = criterion.description.strip()
        if key:
            spec_map.setdefault(key, criterion)
    return spec_map


async def _default_brownfield_project_dir() -> Path | None:
    """Return the registered default brownfield project directory, if any."""
    from ouroboros.persistence.brownfield import BrownfieldStore

    store = BrownfieldStore()
    try:
        await store.initialize()
        default_repo = await store.get_default()
    except Exception as exc:  # noqa: BLE001 - fallback discovery must be best-effort
        log.warning("mcp.tool.evaluate.brownfield_default_lookup_failed", error=str(exc))
        return None
    finally:
        await store.close()

    if default_repo is None or not default_repo.path:
        return None

    resolved = Path(default_repo.path).expanduser().resolve()
    if not resolved.is_dir():
        log.warning(
            "mcp.tool.evaluate.brownfield_default_unusable",
            path=str(resolved),
        )
        return None
    return resolved


def _seed_project_dir(seed: Seed | None, *, stable_base: Path) -> Path | None:
    """Resolve a contained project directory encoded in seed metadata/context."""
    resolution = resolve_seed_project_path(seed, stable_base=stable_base)
    if resolution.path is None:
        return None

    resolved = resolution.path
    if resolved.is_file():
        return resolved.parent
    if resolved.exists() and not resolved.is_dir():
        return None
    return resolved


async def _resolve_evaluate_working_dir(
    explicit_working_dir: str | None,
    seed: Seed | None,
) -> Path:
    """Resolve the project root that gates Stage 1 and Stage 2 evaluation.

    Precedence is explicit tool argument, registered brownfield default,
    seed-declared project directory, then the MCP server cwd. The last
    fallback preserves the historical behavior, but only after project-aware
    sources have been exhausted.
    """
    stable_base = Path.cwd().resolve()
    if explicit_working_dir:
        resolved = resolve_path_against_base(explicit_working_dir, stable_base=stable_base)
        if resolved is not None:
            return resolved

    brownfield_default = await _default_brownfield_project_dir()
    if brownfield_default is not None:
        if brownfield_default.is_dir():
            return brownfield_default.resolve()
        log.warning(
            "mcp.tool.evaluate.brownfield_default_unusable",
            path=str(brownfield_default),
        )

    seed_dir = _seed_project_dir(seed, stable_base=stable_base)
    if seed_dir is not None:
        return seed_dir

    return stable_base


async def _resolve_executor_backend(store: EventStore | None, session_id: str) -> str | None:
    """Best-effort: which runtime backend executed this session (PR-X X2).

    Read from the ``execution.session.completed`` / ``.started`` lifecycle events,
    whose payload carries ``runtime_backend`` per node. Lets formal evaluation
    keep the executor's own vendor out of the reviewer jury. Any failure returns
    ``None`` — evaluation then behaves exactly as before.
    """
    if store is None or not session_id:
        return None
    try:
        for event_type in (
            "execution.session.completed",
            "execution.session.started",
        ):
            events = await store.query_session_related_events(
                session_id, event_type=event_type, limit=50
            )
            for event in events:
                data = event.data if isinstance(event.data, dict) else {}
                backend = data.get("runtime_backend")
                if isinstance(backend, str) and backend.strip():
                    return backend.strip()
    except Exception:
        return None
    return None


def _evaluation_allowed_tools(runtime_backend: str | None) -> list[str]:
    """Return the policy-derived read-only tool envelope for evaluation."""
    return allowed_runtime_builtin_tool_names(
        PolicyContext(
            runtime_backend=runtime_backend,
            session_role=PolicySessionRole.EVALUATION,
            execution_phase=PolicyExecutionPhase.EVALUATION,
        )
    )


@dataclass
class MeasureDriftHandler:
    """Handler for the measure_drift tool.

    Measures goal deviation from the original seed specification
    using DriftMeasurement with weighted components:
    goal (50%), constraint (30%), ontology (20%).
    """

    event_store: EventStore | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_measure_drift",
            description=(
                "Measure drift from the original seed goal. "
                "Calculates goal deviation score using weighted components: "
                "goal drift (50%), constraint drift (30%), ontology drift (20%). "
                "Returns drift metrics, analysis, and suggestions if drift exceeds threshold."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID to measure drift for",
                    required=True,
                ),
                MCPToolParameter(
                    name="current_output",
                    type=ToolInputType.STRING,
                    description="Current execution output to measure drift against the seed goal",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Original seed YAML content for drift calculation",
                    required=True,
                ),
                MCPToolParameter(
                    name="constraint_violations",
                    type=ToolInputType.ARRAY,
                    description="Known constraint violations (e.g., ['Missing tests', 'Wrong language'])",
                    required=False,
                ),
                MCPToolParameter(
                    name="current_concepts",
                    type=ToolInputType.ARRAY,
                    description="Concepts present in the current output (for ontology drift)",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a drift measurement request.

        Args:
            arguments: Tool arguments including session_id, current_output, and seed_content.

        Returns:
            Result containing drift metrics or error.
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        current_output = arguments.get("current_output")
        if not current_output:
            return Result.err(
                MCPToolError(
                    "current_output is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
                    tool_name="ouroboros_measure_drift",
                )
            )

        constraint_violations_raw = arguments.get("constraint_violations") or []
        current_concepts_raw = arguments.get("current_concepts") or []

        log.info(
            "mcp.tool.measure_drift",
            session_id=session_id,
            output_length=len(current_output),
            violations_count=len(constraint_violations_raw),
        )

        try:
            # Parse seed YAML
            seed_dict = yaml.safe_load(seed_content)
            seed = Seed.from_dict(seed_dict)
        except yaml.YAMLError as e:
            return Result.err(
                MCPToolError(
                    f"Failed to parse seed YAML: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )
        except (ValidationError, PydanticValidationError) as e:
            return Result.err(
                MCPToolError(
                    f"Seed validation failed: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )

        try:
            # Calculate drift using real DriftMeasurement
            measurement = DriftMeasurement()
            metrics = measurement.measure(
                current_output=current_output,
                constraint_violations=[str(v) for v in constraint_violations_raw],
                current_concepts=[str(c) for c in current_concepts_raw],
                seed=seed,
            )

            drift_text = (
                f"Drift Measurement Report\n"
                f"=======================\n"
                f"Session: {session_id}\n"
                f"Seed ID: {seed.metadata.seed_id}\n"
                f"Goal: {seed.goal}\n\n"
                f"Combined Drift: {metrics.combined_drift:.2f}\n"
                f"Acceptable Threshold: {DRIFT_THRESHOLD}\n"
                f"Status: {'ACCEPTABLE' if metrics.is_acceptable else 'EXCEEDED'}\n\n"
                f"Component Breakdown:\n"
                f"  Goal Drift: {metrics.goal_drift:.2f} (50% weight)\n"
                f"  Constraint Drift: {metrics.constraint_drift:.2f} (30% weight)\n"
                f"  Ontology Drift: {metrics.ontology_drift:.2f} (20% weight)\n"
            )

            suggestions: list[str] = []
            if not metrics.is_acceptable:
                suggestions.append("Drift exceeds threshold - consider consensus review")
                suggestions.append("Review execution path against original goal")
                if metrics.constraint_drift > 0:
                    suggestions.append(
                        f"Constraint violations detected: {constraint_violations_raw}"
                    )

            if suggestions:
                drift_text += "\nSuggestions:\n"
                for s in suggestions:
                    drift_text += f"  - {s}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=drift_text),),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "seed_id": seed.metadata.seed_id,
                        "goal_drift": metrics.goal_drift,
                        "constraint_drift": metrics.constraint_drift,
                        "ontology_drift": metrics.ontology_drift,
                        "combined_drift": metrics.combined_drift,
                        "is_acceptable": metrics.is_acceptable,
                        "threshold": DRIFT_THRESHOLD,
                        "suggestions": suggestions,
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.measure_drift.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to measure drift: {e}",
                    tool_name="ouroboros_measure_drift",
                )
            )


@dataclass
class EvaluateHandler:
    """Handler for the ouroboros_evaluate tool.

    Evaluates an execution session using the three-stage evaluation pipeline:
    Stage 1: Mechanical Verification ($0)
    Stage 2: Semantic Evaluation (Standard tier)
    Stage 3: Multi-Model Consensus (Frontier tier, if triggered)
    """

    event_store: EventStore | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    TIMEOUT_SECONDS: int = 0  # No server-side timeout; client/runtime decides.

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evaluate",
            description=(
                "Evaluate an Ouroboros execution session using the three-stage evaluation pipeline. "
                "Stage 1 performs mechanical verification (lint, build, test). "
                "Stage 2 performs semantic evaluation of AC compliance and goal alignment. "
                "Stage 3 runs multi-model consensus if triggered by uncertainty or manual request."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID to evaluate",
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact",
                    type=ToolInputType.STRING,
                    description="The execution output/artifact to evaluate",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Original seed YAML for goal/constraints extraction",
                    required=False,
                ),
                MCPToolParameter(
                    name="acceptance_criterion",
                    type=ToolInputType.STRING,
                    description="Specific acceptance criterion to evaluate against",
                    required=False,
                ),
                MCPToolParameter(
                    name="acceptance_criteria",
                    type=ToolInputType.ARRAY,
                    description=(
                        "Multiple acceptance criteria for checklist evaluation. "
                        "When two or more items are provided, each AC is evaluated "
                        "independently and the results are aggregated into a "
                        "pass/fail checklist (#366). Overrides acceptance_criterion."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="artifact_type",
                    type=ToolInputType.STRING,
                    description="Type of artifact: code, docs, config. Default: code",
                    required=False,
                    default="code",
                    enum=("code", "docs", "config"),
                ),
                MCPToolParameter(
                    name="trigger_consensus",
                    type=ToolInputType.BOOLEAN,
                    description="Force Stage 3 consensus evaluation. Default: False",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="working_dir",
                    type=ToolInputType.STRING,
                    description=(
                        "Project root used to resolve Stage 1 mechanical verification "
                        "commands and Stage 2 source-file visibility. Commands are "
                        "read from .ouroboros/mechanical.toml; "
                        "when the file is missing, the evaluator makes one AI detect "
                        "call that inspects manifests (package.json, pyproject.toml, "
                        "Cargo.toml, Makefile, ...) and authors the toml. Stage 1 "
                        "skips every check when no toml is produced — it never guesses."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an evaluation request.

        Args:
            arguments: Tool arguments including session_id, artifact, and optional seed_content.

        Returns:
            Result containing evaluation results or error.
        """
        from ouroboros.evaluation import (
            EvaluationContext,
            EvaluationPipeline,
            PipelineConfig,
            SemanticConfig,
            build_mechanical_config,
            ensure_mechanical_toml,
            has_mechanical_toml,
        )

        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_evaluate",
                )
            )

        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_evaluate",
                )
            )

        seed_content = arguments.get("seed_content")
        acceptance_criterion = arguments.get("acceptance_criterion")
        acceptance_criteria_raw = arguments.get("acceptance_criteria")
        artifact_type = arguments.get("artifact_type", "code")
        trigger_consensus = arguments.get("trigger_consensus", False)

        # Normalize all AC inputs into a single tuple (#366 fix):
        # 1. If acceptance_criteria (plural, ARRAY) has valid entries, use them.
        # 2. Else if acceptance_criterion (singular, STRING) is set, wrap it.
        # 3. Else empty — single-AC path will use a default.
        # This ensures a 1-item list is honoured as the effective AC,
        # fixing the contract violation where the input shape was accepted
        # but its meaning was silently ignored.
        acceptance_criteria: tuple[str, ...] = ()
        if isinstance(acceptance_criteria_raw, list):
            acceptance_criteria = tuple(
                str(item).strip()
                for item in acceptance_criteria_raw
                if isinstance(item, (str, int, float)) and str(item).strip()
            )
        if not acceptance_criteria and acceptance_criterion and str(acceptance_criterion).strip():
            acceptance_criteria = (str(acceptance_criterion).strip(),)

        # Parse seed before dispatch so working_dir fallback is available for
        # both plugin/subagent and in-process evaluation paths.
        goal = ""
        constraints: tuple[str, ...] = ()
        seed_id = session_id  # fallback
        seed: Seed | None = None

        if seed_content:
            try:
                seed_dict = yaml.safe_load(seed_content)
                seed = Seed.from_dict(seed_dict)
                goal = seed.goal
                constraints = tuple(seed.constraints)
                seed_id = seed.metadata.seed_id
                if not acceptance_criteria:
                    acceptance_criteria = _seed_acceptance_criteria(seed)
            except (yaml.YAMLError, ValidationError, PydanticValidationError) as e:
                # Seed failed to parse — leave seed as None; spec map stays empty.
                log.warning("mcp.tool.evaluate.seed_parse_warning", error=str(e))
                # Continue without seed data - not fatal

        log.info(
            "mcp.tool.evaluate",
            session_id=session_id,
            has_seed=seed_content is not None,
            multi_ac_count=len(acceptance_criteria),
            trigger_consensus=trigger_consensus,
        )

        working_dir = await _resolve_evaluate_working_dir(arguments.get("working_dir"), seed)

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        if len(acceptance_criteria) > 1:
            ac_for_payload: str | None = "\n".join(
                f"{i + 1}. {ac}" for i, ac in enumerate(acceptance_criteria)
            )
        elif acceptance_criteria:
            ac_for_payload = acceptance_criteria[0]
        else:
            ac_for_payload = None
        payload = build_evaluate_subagent(
            session_id=session_id,
            artifact=artifact,
            artifact_type=artifact_type,
            seed_content=seed_content,
            acceptance_criterion=ac_for_payload,
            working_dir=str(working_dir),
            trigger_consensus=trigger_consensus,
        )
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            # Preserve public response shape (#442): session_id + status are
            # part of the documented contract for ouroboros_evaluate.
            return await dispatch_plugin_terminal(
                self.event_store,
                session_id=session_id,
                payload=payload,
                response_shape={
                    "session_id": session_id,
                    "status": DELEGATED_TO_SUBAGENT,
                    "dispatch_mode": "plugin",
                    "artifact_type": artifact_type,
                    "trigger_consensus": trigger_consensus,
                },
            )

        # Fall-through: real in-process evaluation pipeline (subprocess / non-opencode runtimes).

        store = self.event_store
        owns_event_store = False

        try:
            # Try to enrich from session repository if event_store available
            if not goal:
                if store is None:
                    store = EventStore()
                    owns_event_store = True
                try:
                    await store.initialize()
                    repo = SessionRepository(store)
                    session_result = await repo.reconstruct_session(session_id)
                    if session_result.is_ok:
                        tracker = session_result.value
                        seed_id = tracker.seed_id
                except Exception:
                    pass  # Best-effort enrichment

            # PR-X X2: resolve which runtime backend executed this session so
            # consensus can keep that vendor out of the reviewer jury. Best-effort
            # — None leaves today's behavior untouched.
            executor_backend = await _resolve_executor_backend(store, session_id)

            # Derive current_ac from the unified acceptance_criteria tuple.
            # The tuple already incorporates both the plural and singular params,
            # so we only need to index or fall back to a default.
            current_ac = (
                acceptance_criteria[0]
                if acceptance_criteria
                else "Verify execution output meets requirements"
            )

            # Reattach declared success contracts (PR-H): map AC text back to
            # its structured spec so Stage 2 grades against verify_command /
            # expected_artifacts / output_assertion, not the bare wording.
            ac_spec_map = _seed_ac_spec_map(seed)

            # Evaluation reads multiple spec files (one Read call per AC).
            # Use a dedicated adapter with a higher turn budget — the shared
            # MCP adapter is max_turns=1 (tuned for interview/seed single-shot).
            backend = get_llm_backend_for_role(
                "semantic_evaluation",
                explicit_backend=self.llm_backend,
            )
            llm_adapter = create_llm_adapter(
                backend=backend,
                allowed_tools=_evaluation_allowed_tools(backend),
                max_turns=20,
            )
            log.info(
                "mcp.tool.evaluate.started",
                session_id=session_id,
                artifact_type=artifact_type,
                working_dir=str(working_dir),
                llm_backend=backend,
                adapter_type=type(llm_adapter).__name__,
            )

            # Collect file-based artifacts for richer semantic evaluation.
            # working_dir is used as the project root for artifact resolution.
            #
            # Write the artifact text to a file in working_dir so the
            # ArtifactCollector can pick it up naturally during its scan
            # instead of inlining the full text (potentially 50KB+) into
            # the evaluation prompt.
            from ouroboros.evaluation.artifact_collector import ArtifactCollector

            artifact_file = working_dir / ".ouroboros_eval_artifact.md"
            try:
                artifact_file.write_text(artifact, encoding="utf-8")
            except OSError:
                pass  # Non-critical — evaluator falls back to text_summary

            try:
                artifact_bundle = ArtifactCollector().collect(artifact, str(working_dir))
            except Exception as exc:
                log.warning(
                    "mcp.tool.evaluate.artifact_collection_failed",
                    error=str(exc),
                    working_dir=str(working_dir),
                )
                artifact_bundle = None

            # Stage 1 trusts .ouroboros/mechanical.toml only. When the file is
            # absent we run the AI detector once to author it — silent
            # best-effort, so a failed detect simply leaves Stage 1 empty and
            # the pipeline falls through to Stage 2 instead of phantom-failing
            # on hardcoded preset guesses.
            if not has_mechanical_toml(working_dir):
                try:
                    await ensure_mechanical_toml(
                        working_dir,
                        llm_adapter,
                        backend=backend,
                    )
                except Exception as exc:  # noqa: BLE001 — detector must never break eval
                    log.warning(
                        "mcp.tool.evaluate.detect_failed",
                        working_dir=str(working_dir),
                        error=str(exc),
                    )
            mechanical_config = build_mechanical_config(working_dir)
            config = PipelineConfig(
                mechanical=mechanical_config,
                semantic=SemanticConfig(
                    model=get_llm_model_for_role("semantic_evaluation", backend=backend)
                ),
            )
            pipeline = EvaluationPipeline(llm_adapter, config)

            # Multi-AC checklist path (#366):
            # When the caller provides >= 2 acceptance criteria we run the
            # pipeline once per AC and aggregate the results into a
            # checklist.  Single-AC callers keep the original single-pass
            # behaviour — no extra cost or behaviour change for them.
            if len(acceptance_criteria) >= 2:
                return await self._handle_multi_ac(
                    session_id=session_id,
                    seed_id=seed_id,
                    acceptance_criteria=acceptance_criteria,
                    artifact=artifact,
                    artifact_type=artifact_type,
                    goal=goal,
                    constraints=constraints,
                    trigger_consensus=trigger_consensus,
                    artifact_bundle=artifact_bundle,
                    pipeline=pipeline,
                    working_dir=working_dir,
                    executor_backend=executor_backend,
                    ac_spec_map=ac_spec_map,
                )

            context = EvaluationContext(
                execution_id=session_id,
                seed_id=seed_id,
                current_ac=current_ac,
                current_ac_spec=ac_spec_map.get(current_ac),
                artifact=artifact,
                artifact_type=artifact_type,
                goal=goal,
                constraints=constraints,
                trigger_consensus=trigger_consensus,
                artifact_bundle=artifact_bundle,
                executor_backend=executor_backend,
            )
            result = await pipeline.evaluate(context)

            if result.is_err:
                rendered_error = (
                    result.error.format_details()
                    if hasattr(result.error, "format_details")
                    else str(result.error)
                )
                log.warning(
                    "mcp.tool.evaluate.pipeline_failed",
                    session_id=session_id,
                    working_dir=str(working_dir),
                    llm_backend=backend,
                    error=rendered_error,
                )
                return Result.err(
                    MCPToolError(
                        f"Evaluation failed: {rendered_error}",
                        tool_name="ouroboros_evaluate",
                    )
                )

            eval_result = result.value

            # Detect code changes when Stage 1 fails (presentation concern)
            code_changes: bool | None = None
            if eval_result.stage1_result and not eval_result.stage1_result.passed:
                code_changes = await self._has_code_changes(working_dir)

            # Build result text
            result_text = self._format_evaluation_result(eval_result, code_changes=code_changes)

            # Build metadata
            meta = {
                "session_id": session_id,
                "final_approved": eval_result.final_approved,
                "highest_stage": eval_result.highest_stage_completed,
                "stage1_passed": eval_result.stage1_result.passed
                if eval_result.stage1_result
                else None,
                "stage2_ac_compliance": eval_result.stage2_result.ac_compliance
                if eval_result.stage2_result
                else None,
                "stage2_score": eval_result.stage2_result.score
                if eval_result.stage2_result
                else None,
                "stage3_approved": eval_result.stage3_result.approved
                if eval_result.stage3_result
                else None,
                "stage3_reviewer_independence": eval_result.stage3_result.reviewer_independence
                if eval_result.stage3_result
                else None,
                "code_changes_detected": code_changes,
            }

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                    is_error=False,
                    meta=meta,
                )
            )
        except (ValueError, RuntimeError) as e:
            # Configuration/bootstrap errors (unsupported backend, missing
            # provider install) — actionable by the user, safe to surface.
            log.warning("mcp.tool.evaluate.config_error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Evaluation setup failed: {e}",
                    tool_name="ouroboros_evaluate",
                )
            )
        except Exception:
            log.exception("mcp.tool.evaluate.error")
            return Result.err(
                MCPToolError(
                    "Evaluation failed due to an internal error. Check server logs for details.",
                    tool_name="ouroboros_evaluate",
                )
            )
        finally:
            if owns_event_store and store is not None:
                await store.close()

    async def _handle_multi_ac(
        self,
        *,
        session_id: str,
        seed_id: str,
        acceptance_criteria: tuple[str, ...],
        artifact: str,
        artifact_type: str,
        goal: str,
        constraints: tuple[str, ...],
        trigger_consensus: bool,
        artifact_bundle: object | None,
        pipeline: object,  # EvaluationPipeline — typed as object to avoid import cycle
        working_dir: Path,
        executor_backend: str | None = None,
        ac_spec_map: dict[str, AcceptanceCriterionSpec] | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Evaluate each AC individually and return an aggregated checklist (#366).

        Stage 1 (mechanical verification — lint/build/test) is AC-agnostic,
        so we run it exactly once via the first AC's full pipeline call and
        inject the result into the remaining per-AC evaluations.  Only
        Stage 2+ (semantic evaluation) is parallelized per AC via
        ``asyncio.gather``.

        Per-AC results are then folded into a single ``ACChecklistResult``
        so the caller sees one pass/fail checklist with per-item evidence
        and failure reasons.

        Single-AC callers never reach this path — see ``handle()``.
        """
        import asyncio

        from ouroboros.evaluation import EvaluationContext
        from ouroboros.evaluation.checklist import (
            aggregate_results,
            build_run_feedback,
            format_checklist,
        )

        spec_map = ac_spec_map or {}

        log.info(
            "mcp.tool.evaluate.multi_ac_started",
            session_id=session_id,
            ac_count=len(acceptance_criteria),
        )

        # --- Stage 1: run once via the first AC's full pipeline call ---
        first_context = EvaluationContext(
            execution_id=session_id,
            seed_id=seed_id,
            current_ac=acceptance_criteria[0],
            current_ac_spec=spec_map.get(acceptance_criteria[0]),
            artifact=artifact,
            artifact_type=artifact_type,
            goal=goal,
            constraints=constraints,
            trigger_consensus=trigger_consensus,
            artifact_bundle=artifact_bundle,
            executor_backend=executor_backend,
        )
        first_result = await pipeline.evaluate(first_context)  # type: ignore[attr-defined]
        if first_result.is_err:
            err = first_result.error
            rendered = err.format_details() if hasattr(err, "format_details") else str(err)
            return Result.err(
                MCPToolError(
                    f"Evaluation failed: {rendered}",
                    tool_name="ouroboros_evaluate",
                )
            )

        # Extract Stage 1 result to share with remaining ACs.
        shared_stage1 = first_result.value.stage1_result

        # --- Stage 2+: parallelize remaining ACs (Stage 1 injected) ---
        async def _run_one(ac_text: str) -> Result[object, object]:
            context = EvaluationContext(
                execution_id=session_id,
                seed_id=seed_id,
                current_ac=ac_text,
                current_ac_spec=spec_map.get(ac_text),
                artifact=artifact,
                artifact_type=artifact_type,
                goal=goal,
                constraints=constraints,
                trigger_consensus=trigger_consensus,
                artifact_bundle=artifact_bundle,
                executor_backend=executor_backend,
            )
            return await pipeline.evaluate(  # type: ignore[attr-defined]
                context,
                stage1_result=shared_stage1,
            )

        remaining_gathered = await asyncio.gather(
            *(_run_one(ac) for ac in acceptance_criteria[1:]),
            return_exceptions=True,
        )
        gathered = (first_result, *remaining_gathered)

        # Any exception or err-Result aborts the whole checklist —
        # otherwise we'd aggregate over a half-evaluated set.
        for entry in gathered:
            if isinstance(entry, BaseException):
                log.exception(
                    "mcp.tool.evaluate.multi_ac_exception",
                    session_id=session_id,
                )
                return Result.err(
                    MCPToolError(
                        f"Evaluation failed during multi-AC run: {entry}",
                        tool_name="ouroboros_evaluate",
                    )
                )
            if entry.is_err:  # type: ignore[union-attr]
                err = entry.error  # type: ignore[union-attr]
                rendered = err.format_details() if hasattr(err, "format_details") else str(err)
                log.warning(
                    "mcp.tool.evaluate.multi_ac_pipeline_failed",
                    session_id=session_id,
                    error=rendered,
                )
                return Result.err(
                    MCPToolError(
                        f"Evaluation failed: {rendered}",
                        tool_name="ouroboros_evaluate",
                    )
                )

        eval_results = tuple(entry.value for entry in gathered)  # type: ignore[union-attr]
        checklist = aggregate_results(acceptance_criteria, eval_results)
        feedback = build_run_feedback(checklist)

        code_changes: bool | None = None
        if any(r.stage1_result and not r.stage1_result.passed for r in eval_results):
            code_changes = await self._has_code_changes(working_dir)

        text_parts = [format_checklist(checklist)]
        if code_changes is False:
            text_parts.append("\nNote: no code changes detected in the working tree.")
        result_text = "\n".join(text_parts)

        meta = {
            "session_id": session_id,
            "final_approved": checklist.all_passed,
            "multi_ac": True,
            "ac_count": checklist.total,
            "passed_count": checklist.passed_count,
            "pass_rate": checklist.pass_rate,
            "checklist": [
                {
                    "ac_text": item.ac_text,
                    "passed": item.passed,
                    "reasoning": item.reasoning,
                    "evidence": list(item.evidence),
                    "questions_used": list(item.questions_used),
                    "failure_reason": item.failure_reason,
                }
                for item in checklist.items
            ],
            "run_feedback": list(feedback),
            "code_changes_detected": code_changes,
        }

        log.info(
            "mcp.tool.evaluate.multi_ac_completed",
            session_id=session_id,
            passed=checklist.passed_count,
            total=checklist.total,
            all_passed=checklist.all_passed,
        )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                is_error=False,
                meta=meta,
            )
        )

    async def _has_code_changes(self, working_dir: Path) -> bool | None:
        """Detect whether the working tree has code changes.

        Runs ``git status --porcelain`` to check for modifications.

        Returns:
            True if changes detected, False if clean, None if not a git repo
            or git is unavailable.
        """
        from ouroboros.evaluation.mechanical import run_command

        try:
            cmd_result = await run_command(
                ("git", "status", "--porcelain"),
                timeout=10,
                working_dir=working_dir,
            )
            if cmd_result.return_code != 0:
                return None
            return bool(cmd_result.stdout.strip())
        except Exception:
            return None

    def _format_evaluation_result(self, result, *, code_changes: bool | None = None) -> str:
        """Format evaluation result as human-readable text.

        Args:
            result: EvaluationResult from pipeline.
            code_changes: Whether working tree has code changes (Stage 1 context).

        Returns:
            Formatted text representation.
        """
        lines = [
            "Evaluation Results",
            "=" * 60,
            f"Execution ID: {result.execution_id}",
            f"Final Approval: {'APPROVED' if result.final_approved else 'REJECTED'}",
            f"Highest Stage Completed: {result.highest_stage_completed}",
            "",
        ]

        # Stage 1 results
        if result.stage1_result:
            s1 = result.stage1_result
            lines.extend(
                [
                    "Stage 1: Mechanical Verification",
                    "-" * 40,
                    f"Status: {'PASSED' if s1.passed else 'FAILED'}",
                    f"Coverage: {s1.coverage_score:.1%}" if s1.coverage_score else "Coverage: N/A",
                ]
            )
            for check in s1.checks:
                status = "PASS" if check.passed else "FAIL"
                lines.append(f"  [{status}] {check.check_type}: {check.message}")
                if not check.passed:
                    details = check.details
                    command = details.get("command")
                    if isinstance(command, list) and command:
                        lines.append(f"    command: {' '.join(str(part) for part in command)}")
                    working_dir = details.get("working_dir")
                    if working_dir:
                        lines.append(f"    cwd: {working_dir}")
                    stdout_tail = str(details.get("stdout_tail") or "").strip()
                    stderr_tail = str(details.get("stderr_tail") or "").strip()
                    if stdout_tail:
                        lines.append("    stdout tail:")
                        lines.extend(f"      {line}" for line in stdout_tail.splitlines())
                    if stderr_tail:
                        lines.append("    stderr tail:")
                        lines.extend(f"      {line}" for line in stderr_tail.splitlines())
            lines.append("")

        # Stage 2 results
        if result.stage2_result:
            s2 = result.stage2_result
            lines.extend(
                [
                    "Stage 2: Semantic Evaluation",
                    "-" * 40,
                    f"Score: {s2.score:.2f}",
                    f"AC Compliance: {'YES' if s2.ac_compliance else 'NO'}",
                    f"Goal Alignment: {s2.goal_alignment:.2f}",
                    f"Drift Score: {s2.drift_score:.2f}",
                    f"Uncertainty: {s2.uncertainty:.2f}",
                    f"Reasoning: {s2.reasoning[:200]}..."
                    if len(s2.reasoning) > 200
                    else f"Reasoning: {s2.reasoning}",
                ]
            )
            # Anti-reward-hacking transparency (#367): surface the concrete
            # Socratic questions and evidence the evaluator relied on so
            # the user can audit whether the verdict was earned.
            if s2.questions_used:
                lines.append("Questions Used:")
                for question in s2.questions_used:
                    lines.append(f"  - {question}")
            if s2.evidence:
                lines.append("Evidence:")
                for item in s2.evidence:
                    lines.append(f"  - {item}")
            lines.append("")

        # Stage 3 results
        if result.stage3_result:
            s3 = result.stage3_result
            lines.extend(
                [
                    "Stage 3: Multi-Model Consensus",
                    "-" * 40,
                    f"Status: {'APPROVED' if s3.approved else 'REJECTED'}",
                    f"Majority Ratio: {s3.majority_ratio:.1%}",
                    f"Total Votes: {s3.total_votes}",
                    f"Approving: {s3.approving_votes}",
                ]
            )
            reviewer_independence = getattr(s3, "reviewer_independence", None)
            if reviewer_independence:
                lines.append(f"Reviewer Independence: {reviewer_independence}")
            for vote in s3.votes:
                decision = "APPROVE" if vote.approved else "REJECT"
                lines.append(f"  [{decision}] {vote.model} (confidence: {vote.confidence:.2f})")
            if s3.disagreements:
                lines.append("Disagreements:")
                for d in s3.disagreements:
                    lines.append(f"  - {d[:100]}...")
            lines.append("")

        # Failure reason
        if not result.final_approved:
            lines.extend(
                [
                    "Failure Reason",
                    "-" * 40,
                    result.failure_reason or "Unknown",
                ]
            )
            # Contextual annotation for Stage 1 failures
            stage1_failed = result.stage1_result and not result.stage1_result.passed
            if stage1_failed and code_changes is True:
                lines.extend(
                    [
                        "",
                        "⚠ Code changes detected — these are real build/test failures "
                        "that need to be fixed before re-evaluating.",
                    ]
                )
            elif stage1_failed and code_changes is False:
                lines.extend(
                    [
                        "",
                        "ℹ No code changes detected in the working tree. These failures "
                        "are expected if you haven't run `ooo run` yet to produce code.",
                    ]
                )

        return "\n".join(lines)


@dataclass
class ChecklistVerifyHandler:
    """Handler for the ``ouroboros_checklist_verify`` tool (#366).

    Given a seed (containing ``acceptance_criteria``) and an execution
    artifact, this handler routes each AC through the Stage 2 evaluation
    pipeline and returns an aggregated checklist.  It is intentionally
    thin — it composes ``EvaluateHandler`` rather than reimplementing
    pipeline orchestration, so it stays in sync with any future changes
    to the main evaluator.

    Why this is a separate tool instead of a flag on ``ouroboros_execute_seed``:

    - ``ExecuteSeed`` is already complex (background execution, resume,
      delegation) and has a stable public contract.  Adding a retry
      loop inside it would entangle with Ralph mode and the Job system.
    - This tool lets the *caller* (a human, a ``/ralph`` loop, or a
      runtime workflow) decide when and how to retry.  No decisions
      are hidden inside background tasks.
    - It is opt-in: existing callers are unaffected.
    """

    evaluate_handler: EvaluateHandler | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_checklist_verify",
            description=(
                "Verify that a Run artifact satisfies every acceptance criterion "
                "in a Seed.  Returns a per-AC checklist (pass/fail with evidence "
                "and failure reasons) plus ready-to-use run_feedback strings the "
                "caller can inject into a re-run prompt.  Does NOT automatically "
                "re-execute — the caller (Ralph, workflow, or human) decides."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The execution session ID being verified",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description=(
                        "Seed YAML containing acceptance_criteria, goal, constraints. "
                        "The seed's acceptance_criteria list is evaluated in full."
                    ),
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact",
                    type=ToolInputType.STRING,
                    description="The Run output/artifact to verify against the seed's ACs",
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact_type",
                    type=ToolInputType.STRING,
                    description="Type of artifact: code, docs, config. Default: code",
                    required=False,
                    default="code",
                    enum=("code", "docs", "config"),
                ),
                MCPToolParameter(
                    name="working_dir",
                    type=ToolInputType.STRING,
                    description="Project working directory (for language auto-detection).",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Verify the seed's full AC list against the artifact."""
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        # Extract acceptance criteria from seed.
        try:
            seed_dict = yaml.safe_load(seed_content)
            seed = Seed.from_dict(seed_dict)
        except yaml.YAMLError as exc:
            log.warning("mcp.tool.checklist_verify.yaml_error", error=str(exc))
            return Result.err(
                MCPToolError(
                    f"Failed to parse seed YAML: {exc}",
                    tool_name="ouroboros_checklist_verify",
                )
            )
        except (ValidationError, PydanticValidationError) as exc:
            log.warning("mcp.tool.checklist_verify.seed_validation_error", error=str(exc))
            return Result.err(
                MCPToolError(
                    f"Seed validation failed: {exc}",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        acceptance_criteria = tuple(
            text for criterion in seed.acceptance_criteria if (text := ac_text(criterion).strip())
        )
        if not acceptance_criteria:
            return Result.err(
                MCPToolError(
                    "Seed has no acceptance_criteria — cannot build checklist.",
                    tool_name="ouroboros_checklist_verify",
                )
            )

        # Delegate to EvaluateHandler in multi-AC mode.  Re-using the
        # evaluator means language detection, artifact bundling, event
        # logging, and LLM backend handling stay consistent.
        evaluator = self.evaluate_handler or EvaluateHandler(llm_backend=self.llm_backend)

        evaluate_args = {
            "session_id": session_id,
            "artifact": artifact,
            "seed_content": seed_content,
            "acceptance_criteria": list(acceptance_criteria),
            "artifact_type": arguments.get("artifact_type", "code"),
        }
        if "working_dir" in arguments:
            evaluate_args["working_dir"] = arguments["working_dir"]

        log.info(
            "mcp.tool.checklist_verify.started",
            session_id=session_id,
            ac_count=len(acceptance_criteria),
        )

        result = await evaluator.handle(evaluate_args)

        if result.is_err:
            log.warning(
                "mcp.tool.checklist_verify.evaluate_failed",
                session_id=session_id,
                error=str(result.error),
            )
            return result

        # Augment the MCP result meta so callers can distinguish the
        # verify path from a plain multi-AC evaluate call.
        meta = dict(result.value.meta or {})
        meta["checklist_verify"] = True
        meta["seed_goal"] = seed.goal
        augmented = MCPToolResult(
            content=result.value.content,
            is_error=result.value.is_error,
            meta=meta,
        )

        log.info(
            "mcp.tool.checklist_verify.completed",
            session_id=session_id,
            all_passed=meta.get("final_approved"),
            passed_count=meta.get("passed_count"),
            ac_count=meta.get("ac_count"),
        )

        return Result.ok(augmented)


@dataclass
class LateralThinkHandler(BridgeAwareMixin):
    """Handler for the lateral_think tool.

    Generates alternative thinking approaches using lateral thinking personas
    to break through stagnation in problem-solving.

    Inherits :class:`BridgeAwareMixin` (#475) so the composition root's
    loop-injection populates ``mcp_manager`` and ``mcp_tool_prefix``
    automatically when an MCP bridge is configured. The bridge fields
    are not consumed by this PR — a follow-up slice forwards them into
    the lateral-think dispatch path so dynamic external MCP servers
    reach the unstuck pipeline.

    The multi-persona fan-out path resolves a 3-way dispatch mode via
    ``resolve_subagent_dispatch(agent_runtime_backend, opencode_mode)``:

    - ``PLUGIN_PASSIVE`` (OpenCode + ``opencode_mode=plugin``): emit a
      ``_subagents`` envelope for the bridge plugin to consume.
    - ``HOST_DRIVEN`` (e.g. Codex): no passive bridge, but the host model can
      spawn subagents itself, so emit the inline result stamped with
      ``dispatch_mode=host_driven`` / ``host_action=spawn_subagents`` so the
      host fans out via its native primitive.
    - ``SEQUENTIAL`` (subprocess / runtimes without a parallel primitive): fall
      back to a plain inline multi-persona ``sequential`` text response
      (`inline_fallback` is preserved as a legacy alias in metadata).

    Attributes:
        agent_runtime_backend: Configured runtime (e.g. ``"opencode"``).
        opencode_mode: Configured ``orchestrator.opencode_mode`` value
            (``"plugin"`` or ``"subprocess"``). ``None`` falls through as
            non-plugin (safe default — see ``resolve_subagent_dispatch``).
    """

    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    fanout_registry: FanoutRegistry | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_lateral_think",
            description=(
                "Generate alternative thinking approaches using lateral thinking personas. "
                "Use this tool when stuck on a problem to get fresh perspectives from "
                "different thinking modes: hacker (unconventional workarounds), "
                "researcher (seeks information), simplifier (reduces complexity), "
                "architect (restructures approach), or contrarian (challenges assumptions). "
                "Set persona='all' (or pass personas=['hacker','architect',...]) to "
                "fan out to MULTIPLE personas in parallel — each runs in its own "
                "Task pane with an independent LLM context (no cross-contamination)."
            ),
            parameters=(
                MCPToolParameter(
                    name="problem_context",
                    type=ToolInputType.STRING,
                    description="Description of the stuck situation or problem",
                    required=True,
                ),
                MCPToolParameter(
                    name="current_approach",
                    type=ToolInputType.STRING,
                    description="What has been tried so far that isn't working",
                    required=True,
                ),
                MCPToolParameter(
                    name="persona",
                    type=ToolInputType.STRING,
                    description=(
                        "Single persona (hacker, researcher, simplifier, architect, "
                        "contrarian) OR 'all' to dispatch ALL 5 personas in parallel "
                        "as separate Task panes."
                    ),
                    required=False,
                    enum=(
                        "hacker",
                        "researcher",
                        "simplifier",
                        "architect",
                        "contrarian",
                        "all",
                    ),
                ),
                MCPToolParameter(
                    name="stagnation_pattern",
                    type=ToolInputType.STRING,
                    description=(
                        "Detected stagnation pattern used to suggest a persona when "
                        "persona is omitted."
                    ),
                    required=False,
                    enum=(
                        "spinning",
                        "oscillation",
                        "no_drift",
                        "diminishing_returns",
                    ),
                ),
                MCPToolParameter(
                    name="personas",
                    type=ToolInputType.ARRAY,
                    description=(
                        "Explicit list of personas to dispatch in parallel. "
                        "Takes precedence over 'persona' arg. Example: "
                        "['hacker','contrarian','architect']. Each runs in its "
                        "own parallel Task pane."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="failed_attempts",
                    type=ToolInputType.ARRAY,
                    description="Previous failed approaches to avoid repeating",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a lateral thinking request.

        Two modes:
        - Single persona (default): return one prompt directly as text.
        - Multi-persona parallel: when ``persona='all'`` or ``personas=[...]``
          is passed, dispatch N subagents in parallel (one per persona) via
          the ``_subagents`` bridge payload. Each runs in its own Task pane
          with an independent LLM context.

        Args:
            arguments: Tool arguments including problem_context and current_approach.

        Returns:
            Result containing lateral thinking prompt(s) or error.
        """
        from ouroboros.resilience.lateral import LateralThinker, ThinkingPersona
        from ouroboros.resilience.stagnation import StagnationPattern

        problem_context = arguments.get("problem_context")
        if not problem_context:
            return Result.err(
                MCPToolError(
                    "problem_context is required",
                    tool_name="ouroboros_lateral_think",
                )
            )

        current_approach = arguments.get("current_approach")
        if not current_approach:
            return Result.err(
                MCPToolError(
                    "current_approach is required",
                    tool_name="ouroboros_lateral_think",
                )
            )

        failed_attempts_raw = arguments.get("failed_attempts") or []
        failed_attempts = tuple(str(a) for a in failed_attempts_raw if a)

        # --- Parallel multi-persona dispatch path ---
        explicit_list = arguments.get("personas")
        raw_persona_arg = arguments.get("persona")
        if explicit_list or raw_persona_arg is None:
            persona_arg = ""
        else:
            persona_arg = str(raw_persona_arg).strip()
            if not persona_arg:
                return Result.err(
                    MCPToolError(
                        "persona cannot be blank",
                        tool_name="ouroboros_lateral_think",
                    )
                )
        dispatch_all = persona_arg == "all"

        if explicit_list or dispatch_all:
            from ouroboros.mcp.tools.subagent import (
                SubagentDispatchMode,
                build_lateral_multi_subagent,
                build_multi_subagent_result,
                lateral_persona_panel_metadata_from_capability_definitions,
                register_lateral_persona_fanout,
                resolve_subagent_dispatch,
                stamp_fanout_meta,
            )

            if explicit_list:
                # Coerce each item to str, drop blanks/nulls, dedupe preserving order.
                seen_p: set[str] = set()
                personas_list: list[str] = []
                for item in explicit_list:
                    s = str(item).strip() if item is not None else ""
                    if s and s not in seen_p:
                        seen_p.add(s)
                        personas_list.append(s)
                if not personas_list:
                    return Result.err(
                        MCPToolError(
                            "personas list is empty or contains only blank/null items",
                            tool_name="ouroboros_lateral_think",
                        )
                    )
            else:
                # persona="all" → use every persona
                personas_list = [p.value for p in ThinkingPersona]

            try:
                payloads = build_lateral_multi_subagent(
                    personas=personas_list,
                    problem_context=str(problem_context),
                    current_approach=str(current_approach),
                    failed_attempts=failed_attempts,
                )
            except ValueError as e:
                return Result.err(
                    MCPToolError(
                        str(e),
                        tool_name="ouroboros_lateral_think",
                    )
                )
            except Exception as e:  # noqa: BLE001
                log.error("mcp.tool.lateral_think.multi.error", error=str(e))
                return Result.err(
                    MCPToolError(
                        f"Unexpected error building multi-persona dispatch: {e}",
                        tool_name="ouroboros_lateral_think",
                    )
                )

            log.info(
                "mcp.tool.lateral_think.multi",
                persona_count=len(payloads),
                context_length=len(str(problem_context)),
                failed_count=len(failed_attempts),
            )

            # Resolve the 3-way dispatch mode (the production source of truth).
            #   - PLUGIN_PASSIVE: a bridge plugin will consume the ``_subagents``
            #     envelope, so emit it and skip the inline work.
            #   - HOST_DRIVEN: no passive receiver, but the host model can spawn
            #     from inline payloads via its own primitive (e.g. Codex). Emit
            #     the inline result stamped with ``host_action=spawn_subagents``.
            #   - SEQUENTIAL: no parallel surface at all → plain inline fallback.
            dispatch = resolve_subagent_dispatch(self.agent_runtime_backend, self.opencode_mode)
            if dispatch is SubagentDispatchMode.PLUGIN_PASSIVE:
                # Preserve public response shape (#442): ouroboros_lateral_think
                # natural response documents alternative-thinking metadata.
                # Expose persona_count + dispatch status at top level so callers
                # can branch on delegation without parsing the envelope.
                return build_multi_subagent_result(
                    payloads,
                    response_shape={
                        "status": "delegated_to_subagent",
                        "dispatch_mode": "plugin",
                        "persona_count": len(payloads),
                    },
                )

            # --- Inline/sequential fallback: concatenate persona prompts ---
            thinker = LateralThinker()
            sections: list[str] = []
            for p_str in personas_list:
                try:
                    p_enum = ThinkingPersona(p_str)
                except ValueError:
                    continue
                lateral_res = thinker.generate_alternative(
                    persona=p_enum,
                    problem_context=str(problem_context),
                    current_approach=str(current_approach),
                    failed_attempts=failed_attempts,
                )
                if lateral_res.is_err:
                    continue
                lr = lateral_res.unwrap()
                sections.append(f"# Lateral Thinking: {lr.approach_summary}\n\n{lr.prompt}")

            if not sections:
                return Result.err(
                    MCPToolError(
                        "No valid personas produced output for inline fallback",
                        tool_name="ouroboros_lateral_think",
                    )
                )

            combined = "\n\n---\n\n".join(sections)
            # Expose the canonical per-persona payloads on inline responses
            # too, so non-plugin runtimes (Claude Code, Codex CLI, OpenCode
            # subprocess) can drive their own sub-agent fan-out from the
            # same structured prompts that plugin mode dispatches via
            # `_subagents`. The FastMCP adapter now preserves `meta`, but
            # older bridge consumers still read only `text_content`, so the
            # dispatch payload continues to ride inside `content`.
            #
            # Format: a hidden HTML-comment block with a versioned sentinel,
            # carrying the dispatch JSON base64-encoded inside the comment.
            # Two reasons for base64:
            #   1. Base64's alphabet is [A-Za-z0-9+/=]. It cannot contain
            #      `-->`, so a user-supplied `problem_context` like an
            #      HTML/JS debugging snippet that itself includes `-->`
            #      cannot prematurely close the comment and leak the
            #      payload into the visible markdown.
            #   2. Base64 has no significant whitespace, so line wrapping
            #      and trimming can't corrupt the encoded body.
            # HOST_DRIVEN runtimes (e.g. Codex) have no passive bridge but can
            # spawn subagents themselves. SEQUENTIAL runtimes now get the same
            # machine-readable contract vocabulary, while preserving
            # ``inline_fallback`` as a legacy alias for older skill prose.
            host_driven = dispatch is SubagentDispatchMode.HOST_DRIVEN
            payload_dicts = [p.to_dict() for p in payloads]
            panel_metadata = lateral_persona_panel_metadata_from_capability_definitions()
            contract_backend = self.agent_runtime_backend
            if not contract_backend:
                contract_backend = "codex" if host_driven else "gemini"
            contract = build_runtime_subagent_orchestration_contract(
                contract_backend,
                directive_metadata=panel_metadata,
                opencode_mode=self.opencode_mode,
            )
            dispatch_record: dict[str, Any] = {
                "persona_count": len(sections),
                "payloads": payload_dicts,
                "subagent_orchestration_instruction": contract.runtime_instruction_handling,
            }
            # Stamp the PR-C-standardized 3-mode contract (dispatch_mode /
            # host_action / result_correlation_key) via the shared helper. Only
            # HOST_DRIVEN and SEQUENTIAL reach here (PLUGIN_PASSIVE returned the
            # ``_subagents`` envelope above), so a cue is always stamped.
            stamp_fanout_meta(
                dispatch_record,
                prefix="",
                dispatch_mode=dispatch,
                payloads=payloads,
                correlation_key="context.persona",
            )
            if not host_driven:
                dispatch_record["legacy_dispatch_mode"] = "inline_fallback"
            if self.fanout_registry is not None:
                lateral_fanout_id = register_lateral_persona_fanout(
                    self.fanout_registry,
                    session_id=str(arguments.get("session_id") or ""),
                    payloads=payloads,
                )
                # A fan-out id whose record could not be persisted is not
                # redeemable at re-entry: skip stamping instead of advertising
                # an id whose first submission is unknown_fanout_id.
                if lateral_fanout_id is not None:
                    dispatch_record["fanout_id"] = lateral_fanout_id
            dispatch_blob = json.dumps(dispatch_record)
            dispatch_b64 = base64.b64encode(dispatch_blob.encode("utf-8")).decode("ascii")
            host_banner = (
                (
                    "> **Host action — spawn subagents:** this runtime drives "
                    "fan-out itself. Spawn one subagent per payload below with "
                    "your native subagent primitive, correlate results by "
                    f"`context.persona`, then synthesise. Payloads: {len(sections)} "
                    "(structured copy in `meta` and the dispatch block).\n\n"
                )
                if host_driven
                else ""
            )
            content_text = (
                f"{host_banner}{combined}\n\n"
                "<!-- ouroboros-lateral-inline-dispatch-v1 base64\n"
                f"{dispatch_b64}\n"
                "-->"
            )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=content_text),),
                    is_error=False,
                    meta=dispatch_record,
                )
            )

        # --- Single-persona path ---
        if not persona_arg:
            stagnation_pattern_arg = arguments.get("stagnation_pattern")
            if stagnation_pattern_arg:
                try:
                    stagnation_pattern = StagnationPattern(str(stagnation_pattern_arg))
                except ValueError:
                    return Result.err(
                        MCPToolError(
                            (
                                f"Invalid stagnation_pattern: {stagnation_pattern_arg}. "
                                "Must be one of: spinning, oscillation, no_drift, "
                                "diminishing_returns"
                            ),
                            tool_name="ouroboros_lateral_think",
                        )
                    )

                from ouroboros.resilience.recovery import suggest_lateral_persona_for_pattern

                suggested = suggest_lateral_persona_for_pattern(
                    stagnation_pattern,
                    failed_attempts=failed_attempts,
                )
                if suggested is None:
                    return Result.err(
                        MCPToolError(
                            (
                                "No available lateral thinking persona remains after "
                                "applying failed_attempts exclusions"
                            ),
                            tool_name="ouroboros_lateral_think",
                        )
                    )
                persona_arg = suggested.value
            else:
                persona_arg = ThinkingPersona.CONTRARIAN.value

        try:
            persona = ThinkingPersona(persona_arg)
        except ValueError:
            return Result.err(
                MCPToolError(
                    f"Invalid persona: {persona_arg}. Must be one of: "
                    f"hacker, researcher, simplifier, architect, contrarian, all",
                    tool_name="ouroboros_lateral_think",
                )
            )

        log.info(
            "mcp.tool.lateral_think",
            persona=persona.value,
            context_length=len(str(problem_context)),
            failed_count=len(failed_attempts),
        )

        # Plugin mode: dispatch even a single persona as a subagent so the
        # LLM in the child Task pane does the actual thinking — the parent
        # session stays responsive and gets the result asynchronously.
        #
        # ``should_dispatch_via_plugin`` is also imported locally in the
        # multi-persona branch above, which makes Python treat it as a
        # function-local name throughout this method — so it must be
        # (re-)imported on this branch too before use, even though it is
        # available at module scope. ``build_subagent_result`` is module
        # scope; importing it here as well keeps the original binding intact.
        from ouroboros.mcp.tools.subagent import (  # noqa: F811
            build_subagent_result,
            should_dispatch_via_plugin,
        )

        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            from ouroboros.mcp.tools.subagent import build_lateral_multi_subagent

            try:
                payloads = build_lateral_multi_subagent(
                    personas=[persona.value],
                    problem_context=str(problem_context),
                    current_approach=str(current_approach),
                    failed_attempts=failed_attempts,
                )
            except (ValueError, Exception) as e:  # noqa: BLE001
                log.error("mcp.tool.lateral_think.single_dispatch.error", error=str(e))
                return Result.err(
                    MCPToolError(
                        f"Failed to build single-persona subagent: {e}",
                        tool_name="ouroboros_lateral_think",
                    )
                )

            # Single payload → single _subagent envelope (not _subagents array)
            return build_subagent_result(
                payloads[0],
                response_shape={
                    "status": "delegated_to_subagent",
                    "dispatch_mode": "plugin",
                    "persona": persona.value,
                },
            )

        # Inline fallback for subprocess / non-OpenCode runtimes.
        try:
            thinker = LateralThinker()
            result = thinker.generate_alternative(
                persona=persona,
                problem_context=str(problem_context),
                current_approach=str(current_approach),
                failed_attempts=failed_attempts,
            )

            if result.is_err:
                return Result.err(
                    MCPToolError(
                        result.error,
                        tool_name="ouroboros_lateral_think",
                    )
                )

            lateral_result = result.unwrap()

            # Build the response
            response_text = (
                f"# Lateral Thinking: {lateral_result.approach_summary}\n\n"
                f"{lateral_result.prompt}\n\n"
                "## Questions to Consider\n"
            )
            for question in lateral_result.questions:
                response_text += f"- {question}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=response_text),),
                    is_error=False,
                    meta={
                        "persona": lateral_result.persona.value,
                        "approach_summary": lateral_result.approach_summary,
                        "questions_count": len(lateral_result.questions),
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.lateral_think.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Lateral thinking failed: {e}",
                    tool_name="ouroboros_lateral_think",
                )
            )


# Re-entry input bounds: fan-out results become durable state (received
# results AND the terminal outcome), so an unbounded submission is a direct
# disk/memory amplification vector. Both caps are generous multiples of the
# largest legitimate lane set.
_MAX_FANOUT_RESULT_ITEMS = 32
_MAX_FANOUT_RESULTS_BYTES = 262_144  # 256 KB serialized per submission


@dataclass
class SubmitFanoutResultsHandler:
    """Handler for the ``ouroboros_submit_fanout_results`` re-entry tool.

    After a host fans out a set of advisory/persona/investigation subagents
    (declared by :func:`stamp_fanout_meta` with a stamped ``fanout_id``), it
    submits the correlated child outputs back through this tool. The server
    validates the expected keys against the persisted fan-out record and, when
    complete, routes to the revived synthesizer for the record's kind, returning
    the correlated synthesis for the host to continue with. Sequential hosts
    submit after processing payloads one-by-one — same tool, same contract.
    """

    fanout_registry: FanoutRegistry | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._registry = self.fanout_registry or FanoutRegistry()

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_submit_fanout_results",
            description=(
                "Submit correlated results from a subagent fan-out back to "
                "Ouroboros. After spawning the advisory/persona/investigation "
                "subagents declared by a prior tool's `meta` (which stamped a "
                "`fanout_id` and a `result_correlation_key`), call this tool with "
                "one {key, content} per child output — `key` is the value of the "
                "correlation field for that child. Statuses: `complete` returns "
                "the synthesis (with `missing_optional_keys`, rejected "
                "`unexpected_keys`, and any `contract_violations`); `partial` "
                "lists `missing_keys`/`missing_required_keys` (and "
                "`synthesis_rejected_keys` for content the synthesizer refused) "
                "so you can resubmit the remainder — `accumulation_persisted: "
                "false` means resubmit those lanes too; `accumulated` "
                "(finalize=false) holds the batch without completing so later "
                "optional lanes are not discarded; "
                "`completion_not_persisted` means the synthesis succeeded but "
                "the terminal write failed, so resubmit; `already_complete` "
                "replays the immutable terminal outcome; `correlation_mismatch` "
                "/ `unknown_fanout_id` are clean errors. Submissions are "
                f"bounded: at most {_MAX_FANOUT_RESULT_ITEMS} results and "
                f"{_MAX_FANOUT_RESULTS_BYTES // 1024} KB serialized per call."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description=(
                        "Interview/lateral session id the fan-out belongs to. "
                        "Effectively required: every current producer registers "
                        "it, and omitting a registered identity is a "
                        "correlation_mismatch. Schema-optional ONLY for legacy "
                        "records registered without a session identity."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="fanout_id",
                    type=ToolInputType.STRING,
                    description="The fanout_id stamped into the originating tool's meta.",
                    required=True,
                ),
                MCPToolParameter(
                    name="correlation_key",
                    type=ToolInputType.STRING,
                    description=(
                        "The result_correlation_key from the originating meta "
                        "(e.g. 'context.persona' or 'code_facts'). Effectively "
                        "required, like session_id: omitting a registered "
                        "correlation key is a correlation_mismatch; "
                        "schema-optional only for legacy identity-less records."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="results",
                    type=ToolInputType.ARRAY,
                    description=(
                        "Correlated child outputs: a list of objects each with a "
                        "'key' (the correlation value) and a 'content' (the child "
                        "result, object or text)."
                    ),
                    required=True,
                ),
                MCPToolParameter(
                    name="finalize",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Sequential-submission control (question-advisory "
                        "fan-outs only): pass false to accumulate this batch "
                        "WITHOUT completing the fan-out (status='accumulated'), "
                        "so optional lanes submitted after the required set are "
                        "not discarded. Omit (or pass true) on the last "
                        "submission to complete. Single-batch submissions omit "
                        "it; other fan-out kinds validate content at synthesis "
                        "and reject finalize=false (status="
                        "'finalize_unsupported')."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Validate the submitted fan-out results and route them to synthesis."""
        fanout_id = str(arguments.get("fanout_id") or "").strip()
        if not fanout_id:
            return Result.err(
                MCPToolError(
                    "fanout_id is required",
                    tool_name="ouroboros_submit_fanout_results",
                )
            )

        raw_results = arguments.get("results")
        if not isinstance(raw_results, (list, tuple)):
            return Result.err(
                MCPToolError(
                    "results must be a list of {key, content} objects",
                    tool_name="ouroboros_submit_fanout_results",
                )
            )
        # Bound the RAW submission BEFORE any filtering, validation, or
        # persistence: results are written into durable fan-out state twice
        # (received results + the terminal outcome), so unbounded input is a
        # disk/memory exhaustion vector — and non-dict items count against
        # the caps too (round-6 probe: 33 strings totaling 330 KB previously
        # bypassed both advertised limits by being filtered out first).
        if len(raw_results) > _MAX_FANOUT_RESULT_ITEMS:
            return Result.err(
                MCPToolError(
                    f"results carries {len(raw_results)} items; at most "
                    f"{_MAX_FANOUT_RESULT_ITEMS} results are accepted per "
                    "submission",
                    tool_name="ouroboros_submit_fanout_results",
                )
            )
        try:
            serialized_size = len(
                json.dumps(list(raw_results), ensure_ascii=False).encode("utf-8", "replace")
            )
        except (TypeError, ValueError):
            return Result.err(
                MCPToolError(
                    "results must be JSON-serializable",
                    tool_name="ouroboros_submit_fanout_results",
                )
            )
        if serialized_size > _MAX_FANOUT_RESULTS_BYTES:
            return Result.err(
                MCPToolError(
                    f"results serializes to {serialized_size} bytes; at most "
                    f"{_MAX_FANOUT_RESULTS_BYTES} bytes are accepted per "
                    "submission — trim child outputs to their findings",
                    tool_name="ouroboros_submit_fanout_results",
                )
            )
        results = [item for item in raw_results if isinstance(item, dict)]

        raw_finalize = arguments.get("finalize")
        outcome = submit_fanout_results(
            self._registry,
            session_id=str(arguments.get("session_id") or ""),
            correlation_key=str(arguments.get("correlation_key") or ""),
            results=results,
            fanout_id=fanout_id,
            finalize=raw_finalize if isinstance(raw_finalize, bool) else None,
        )

        if outcome.get("status") == "unknown_fanout_id":
            return Result.err(
                MCPToolError(
                    str(outcome.get("error") or "unknown fanout_id"),
                    tool_name="ouroboros_submit_fanout_results",
                )
            )

        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=json.dumps(outcome, ensure_ascii=False, sort_keys=True),
                    ),
                ),
                is_error=False,
                meta=outcome,
            )
        )


@dataclass
class StartEvaluateHandler:
    """Start an evaluation asynchronously and return a job ID immediately.

    The three-stage evaluation pipeline (mechanical + semantic + optional
    consensus) routinely runs longer than an MCP client's default tool-call
    timeout (Claude Code's MCP layer caps tool calls at ~120s). This handler
    wraps :class:`EvaluateHandler` in a :class:`JobManager`-backed background
    job so the caller gets a ``job_id`` immediately and polls for the verdict
    via ``ouroboros_job_status`` / ``ouroboros_job_wait`` /
    ``ouroboros_job_result``.

    Plugin mode (OpenCode subagent dispatch) is terminal here, mirroring
    :class:`StartExecuteSeedHandler` and :class:`StartEvolveStepHandler`:
    the envelope is emitted directly and no background job is enqueued, so
    polling never targets a non-existent job.
    """

    evaluate_handler: EvaluateHandler | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    deadline_seconds: float = 1800.0

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._evaluate_handler = self.evaluate_handler or EvaluateHandler(
            event_store=self._event_store,
            llm_backend=self.llm_backend,
            agent_runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
        )

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_start_evaluate",
            description=(
                "Start an evaluation in the background and return a job ID immediately. "
                "Use this instead of ouroboros_evaluate when the three-stage pipeline "
                "(mechanical + semantic + optional consensus) is expected to exceed the "
                "MCP client tool-call timeout. Poll with ouroboros_job_status / "
                "ouroboros_job_wait and read the verdict via ouroboros_job_result. "
                "In plugin mode, evaluation is delegated to an OpenCode Task pane and "
                "job_id is None — results appear in the Task pane instead of being "
                "pollable via job_status/job_result."
            ),
            parameters=EvaluateHandler().definition.parameters,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_start_evaluate",
                )
            )
        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_start_evaluate",
                )
            )

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        # Plugin mode is terminal — return the delegation envelope without
        # enqueuing a background job, matching StartExecuteSeedHandler /
        # StartEvolveStepHandler. Polling a fake job_id would break the
        # ouroboros_job_status contract.
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            # Mirror EvaluateHandler.handle's AC normalization so plugin
            # dispatch does not silently drop multi-AC checklist input
            # (PR #882 review feedback): the parameter surface advertises
            # both `acceptance_criterion` (singular) and
            # `acceptance_criteria` (plural list), so both must be honoured
            # here exactly as the non-plugin path honours them via the inner
            # handler. ``build_evaluate_subagent`` only accepts the singular
            # field, so a multi-item list is rendered as a numbered checklist
            # before being forwarded.
            acceptance_criteria_raw = arguments.get("acceptance_criteria")
            acceptance_criteria: tuple[str, ...] = ()
            if isinstance(acceptance_criteria_raw, list):
                acceptance_criteria = tuple(
                    str(item).strip()
                    for item in acceptance_criteria_raw
                    if isinstance(item, (str, int, float)) and str(item).strip()
                )
            ac_singular_raw = arguments.get("acceptance_criterion")
            if not acceptance_criteria and ac_singular_raw and str(ac_singular_raw).strip():
                acceptance_criteria = (str(ac_singular_raw).strip(),)

            seed: Seed | None = None
            seed_content = arguments.get("seed_content")
            if seed_content:
                try:
                    seed_dict = yaml.safe_load(seed_content)
                    seed = Seed.from_dict(seed_dict)
                    if not acceptance_criteria:
                        acceptance_criteria = _seed_acceptance_criteria(seed)
                except (yaml.YAMLError, ValidationError, PydanticValidationError) as e:
                    log.warning("mcp.tool.start_evaluate.seed_parse_warning", error=str(e))

            if len(acceptance_criteria) > 1:
                ac_for_payload: str | None = "\n".join(
                    f"{i + 1}. {ac}" for i, ac in enumerate(acceptance_criteria)
                )
            elif acceptance_criteria:
                ac_for_payload = acceptance_criteria[0]
            else:
                ac_for_payload = None

            working_dir = await _resolve_evaluate_working_dir(
                arguments.get("working_dir"),
                seed,
            )

            payload = build_evaluate_subagent(
                session_id=session_id,
                artifact=artifact,
                artifact_type=arguments.get("artifact_type", "code"),
                seed_content=seed_content,
                acceptance_criterion=ac_for_payload,
                working_dir=str(working_dir),
                trigger_consensus=arguments.get("trigger_consensus", False),
            )
            return await dispatch_plugin_terminal(
                self._event_store,
                session_id=session_id,
                payload=payload,
                response_shape={
                    "job_id": None,
                    "session_id": session_id,
                    "status": DELEGATED_TO_PLUGIN,
                    "dispatch_mode": "plugin",
                    "artifact_type": arguments.get("artifact_type", "code"),
                    "trigger_consensus": arguments.get("trigger_consensus", False),
                },
            )

        # Fall-through: real background job path.
        #
        # NOTE: this path now routes through ``start_background_tool_job``,
        # which gives StartEvaluate the same job-scoped ``cancel_key`` and
        # AgentProcess ``process_id`` as evolve/execute/ralph.  Before this
        # extraction StartEvaluate passed neither, so the durable
        # ``mcp_job:{job_id}`` cancel marker written by
        # ``JobManager.cancel_job`` was never observable by the evaluate
        # agent process — a restart-visible cancel was silently dropped.
        async def _runner(_handle) -> MCPToolResult:
            try:
                if self.deadline_seconds > 0:
                    result = await asyncio.wait_for(
                        self._evaluate_handler.handle(arguments),
                        timeout=self.deadline_seconds,
                    )
                else:
                    result = await self._evaluate_handler.handle(arguments)
            except TimeoutError:
                retry_step = f"ooo evaluate {session_id}"
                return MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=(
                                "Evaluation timed out before the formal verdict completed.\n"
                                f"Retry: {retry_step}"
                            ),
                        ),
                    ),
                    is_error=True,
                    meta={
                        "session_id": session_id,
                        "status": "timed_out",
                        "evaluation_status": "timed_out",
                        "next_step": retry_step,
                    },
                )
            if result.is_err:
                raise RuntimeError(str(result.error))
            return result.value

        snapshot = await start_background_tool_job(
            job_manager=self._job_manager,
            event_store=self._event_store,
            job_type="evaluate",
            intent="evaluate",
            process_scope=f"evaluate:{session_id}",
            initial_message=f"Queued evaluation for {session_id}",
            links=JobLinks(session_id=session_id),
            work_fn=_runner,
            cancelled_text="Evaluation cancelled before work began.",
            detached_tool_name="ouroboros_start_evaluate",
            detached_arguments=arguments,
            runtime_backend=self.agent_runtime_backend,
            llm_backend=self.llm_backend,
            opencode_mode=self.opencode_mode,
        )

        text = (
            f"Started background evaluation.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Session ID: {session_id}\n\n"
            "Use ouroboros_job_status, ouroboros_job_wait, or ouroboros_job_result "
            "to monitor it."
        )
        meta = {
            "job_id": snapshot.job_id,
            "session_id": session_id,
            "status": snapshot.status.value,
            "cursor": snapshot.cursor,
            "job_observer": build_job_observer_contract(
                job_id=snapshot.job_id,
                cursor=snapshot.cursor,
                session_id=session_id,
            ),
        }
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta=meta,
                structured_content=dict(meta),
            )
        )
