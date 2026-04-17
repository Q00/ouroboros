"""Evaluation-phase tool handlers for Ouroboros MCP server.

Contains handlers for drift measurement, evaluation, and lateral thinking tools:
- MeasureDriftHandler: Measures goal deviation from seed specification.
- EvaluateHandler: Three-stage evaluation pipeline (mechanical, semantic, consensus).
- LateralThinkHandler: Generates alternative thinking approaches via personas.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
import structlog
import yaml

from ouroboros.config import get_semantic_model
from ouroboros.core.errors import ValidationError
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.subagent import (
    build_evaluate_subagent,
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
                        "Project working directory for language auto-detection of Stage 1 "
                        "mechanical verification commands. Auto-detects language from marker "
                        "files (build.zig, Cargo.toml, go.mod, package.json, etc.). "
                        "Supports .ouroboros/mechanical.toml for custom overrides."
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
        from pathlib import Path

        from ouroboros.evaluation import (
            EvaluationContext,
            EvaluationPipeline,
            PipelineConfig,
            SemanticConfig,
            build_mechanical_config,
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

        log.info(
            "mcp.tool.evaluate",
            session_id=session_id,
            has_seed=seed_content is not None,
            multi_ac_count=len(acceptance_criteria),
            trigger_consensus=trigger_consensus,
        )

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        payload = build_evaluate_subagent(
            session_id=session_id,
            artifact=artifact,
            artifact_type=artifact_type,
            seed_content=seed_content,
            acceptance_criterion=acceptance_criterion,
            working_dir=arguments.get("working_dir"),
            trigger_consensus=trigger_consensus,
        )
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            await emit_subagent_dispatched_event(
                self.event_store,
                session_id=session_id,
                payload=payload,
            )
            return build_subagent_result(payload)

        # Fall-through: real in-process evaluation pipeline (subprocess / non-opencode runtimes).

        store = self.event_store
        owns_event_store = False

        try:
            # Extract goal/constraints from seed if provided
            goal = ""
            constraints: tuple[str, ...] = ()
            seed_id = session_id  # fallback

            if seed_content:
                try:
                    seed_dict = yaml.safe_load(seed_content)
                    seed = Seed.from_dict(seed_dict)
                    goal = seed.goal
                    constraints = tuple(seed.constraints)
                    seed_id = seed.metadata.seed_id
                except (yaml.YAMLError, ValidationError, PydanticValidationError) as e:
                    log.warning("mcp.tool.evaluate.seed_parse_warning", error=str(e))
                    # Continue without seed data - not fatal

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

            # Derive current_ac from the unified acceptance_criteria tuple.
            # The tuple already incorporates both the plural and singular params,
            # so we only need to index or fall back to a default.
            current_ac = (
                acceptance_criteria[0]
                if acceptance_criteria
                else "Verify execution output meets requirements"
            )

            # Evaluation reads multiple spec files (one Read call per AC).
            # Use a dedicated adapter with a higher turn budget — the shared
            # MCP adapter is max_turns=1 (tuned for interview/seed single-shot).
            llm_adapter = create_llm_adapter(
                backend=self.llm_backend,
                allowed_tools=_evaluation_allowed_tools(self.llm_backend),
                max_turns=20,
            )
            working_dir_str = arguments.get("working_dir")
            working_dir = Path(working_dir_str).resolve() if working_dir_str else Path.cwd()
            log.info(
                "mcp.tool.evaluate.started",
                session_id=session_id,
                artifact_type=artifact_type,
                working_dir=str(working_dir),
                llm_backend=self.llm_backend,
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

            mechanical_config = build_mechanical_config(working_dir)
            config = PipelineConfig(
                mechanical=mechanical_config,
                semantic=SemanticConfig(model=get_semantic_model(self.llm_backend)),
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
                )

            context = EvaluationContext(
                execution_id=session_id,
                seed_id=seed_id,
                current_ac=current_ac,
                artifact=artifact,
                artifact_type=artifact_type,
                goal=goal,
                constraints=constraints,
                trigger_consensus=trigger_consensus,
                artifact_bundle=artifact_bundle,
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
                    llm_backend=self.llm_backend,
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
            artifact=artifact,
            artifact_type=artifact_type,
            goal=goal,
            constraints=constraints,
            trigger_consensus=trigger_consensus,
            artifact_bundle=artifact_bundle,
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
                artifact=artifact,
                artifact_type=artifact_type,
                goal=goal,
                constraints=constraints,
                trigger_consensus=trigger_consensus,
                artifact_bundle=artifact_bundle,
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
      channel workflow) decide when and how to retry.  No decisions
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
            text.strip() for text in seed.acceptance_criteria if text and text.strip()
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
class LateralThinkHandler:
    """Handler for the lateral_think tool.

    Generates alternative thinking approaches using lateral thinking personas
    to break through stagnation in problem-solving.
    """

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
        persona_arg = arguments.get("persona", "contrarian")
        dispatch_all = persona_arg == "all"

        if explicit_list or dispatch_all:
            from ouroboros.mcp.tools.subagent import (
                build_lateral_multi_subagent,
                build_multi_subagent_result,
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

            return build_multi_subagent_result(payloads)

        # --- Single-persona direct-response path (back-compat) ---
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
