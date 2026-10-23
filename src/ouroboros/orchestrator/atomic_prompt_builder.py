"""Atomic leaf prompt assembly for :class:`ParallelACExecutor`.

Extracted verbatim from ``ParallelACExecutor._execute_atomic_ac`` (work order
R4). This module owns the prompt/task-section assembly portion of an atomic
leaf dispatch: label/indent derivation, the governed task section, the success
contract block, the retry/parallel-awareness sections, the working-directory
scan, and the completion-instruction contract. The behaviour is unchanged — the
executor delegates to :class:`AtomicPromptBuilder` and receives the same prompt
it used to build inline.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from typing import TYPE_CHECKING, Any

from ouroboros.core.seed import AcceptanceCriterionSpec, SourcedScope
from ouroboros.core.seed_contract_prompt import (
    render_auto_recursion_guard,
    render_sourced_objective,
)
from ouroboros.orchestrator.ac_execution_capsule import ACExecutionCapsule
from ouroboros.orchestrator.evidence.ac_classification import (
    _effective_evidence_schema_for_ac,
    _is_documentation_only_ac,
    _is_validation_only_ac,
)
from ouroboros.orchestrator.level_context import LevelContext, build_context_prompt
from ouroboros.orchestrator.profile_strategy import COMPLETION_CHECK
from ouroboros.orchestrator.shared_attempt import shares_one_attempt, sourced_criteria_aligned

if TYPE_CHECKING:
    from ouroboros.orchestrator.evidence.runtime_metadata import _SiblingACRef
    from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
    from ouroboros.orchestrator.parallel_executor import ParallelACExecutor


def _build_success_contract_block(spec: AcceptanceCriterionSpec | None) -> str:
    """Render the assertion-safe worker-facing success contract, or ``""``.

    Artifact names are part of the work contract, but the harness-owned
    ``verify_command`` and ``output_assertion`` are deliberately hidden.  The
    worker must satisfy the requested outcome without receiving the grader's
    answer key. Contract-less ACs return ``""``.
    """
    if spec is None or not spec.has_success_contract:
        return ""
    lines = ["SUCCESS CONTRACT for this AC:"]
    if spec.expected_artifacts:
        lines.append(
            "- Expected artifacts: "
            + ", ".join(spec.expected_artifacts)
            + " — ensure they exist in the workspace"
        )
    lines.append(
        "- The harness independently verifies this contract; complete the work "
        "from the AC requirements and provide truthful evidence."
    )
    return "\n".join(lines)


# Executor attributes holding the run Seed's ``sourced_scope`` and its own
# criteria texts. The runner sets them (``install_sourced_scope``) the way the
# check package sets its hooks, and a derived executor inherits them
# (``inherit_check_package_hooks``).
SOURCED_SCOPE_ATTR = "sourced_scope"
SEED_CRITERIA_ATTR = "seed_criteria"


def install_sourced_scope(
    executor: object, scope: SourcedScope | None, criteria: tuple[str, ...] = ()
) -> None:
    """Record the run Seed's sourced scope and criteria on ``executor`` for its leaf prompts."""
    setattr(executor, SOURCED_SCOPE_ATTR, scope)
    setattr(executor, SEED_CRITERIA_ATTR, criteria)


@dataclass(frozen=True)
class AtomicPromptBundle:
    """The assembled prompt plus the surface metadata the executor still needs.

    ``label`` and ``indent`` drive the memory-gate wait and console rendering;
    ``context_governance_audit`` feeds the context-governed event emission.
    """

    prompt: str
    label: str
    indent: str
    context_governance_audit: dict[str, Any] | None


class AtomicPromptBuilder:
    """Assemble the worker prompt for a single atomic leaf dispatch."""

    def __init__(self, executor: ParallelACExecutor) -> None:
        self._executor = executor

    def build(
        self,
        *,
        ac_index: int,
        ac_content: str,
        seed_goal: str,
        is_sub_ac: bool,
        parent_ac_index: int | None,
        sub_ac_index: int | None,
        node_identity: ExecutionNodeIdentity | None,
        level_contexts: list[LevelContext] | None,
        sibling_acs: list[_SiblingACRef] | None,
        retry_attempt: int,
        retry_prompt_extra: str,
        ac_spec: AcceptanceCriterionSpec | None,
        capsule: ACExecutionCapsule | None = None,
    ) -> AtomicPromptBundle:
        """Build the prompt for one atomic leaf dispatch."""
        executor = self._executor

        if capsule is not None:
            ac_content = capsule.ac_content
            seed_goal = capsule.seed_goal
            retry_attempt = capsule.retry_attempt

        # A Seed with ``sourced_scope`` gives the worker only content with a
        # known source: the user's goal verbatim and the binding criteria (or the
        # request itself). The criterion text below is the Seed's own, kept for
        # the evidence schema so the worker's fields match the verifier's.
        task_text = ac_content
        scope = getattr(executor, SOURCED_SCOPE_ATTR, None)
        criteria: tuple[str, ...] = getattr(executor, SEED_CRITERIA_ATTR, ())
        schema_criteria = (ac_content,)
        root_index = parent_ac_index if is_sub_ac else ac_index
        aligned = (
            sourced_criteria_aligned(scope, criteria)
            and root_index is not None
            and 0 <= root_index < len(criteria)
        )
        sourced_objective = isinstance(scope, SourcedScope) and not (
            aligned and root_index is not None
        )
        if isinstance(scope, SourcedScope):
            # Binding criteria that are the Seed's own, position for position:
            # each leaf sees only its own. Otherwise one leaf runs the request
            # (``shared_attempt``) and asks for every criterion's evidence fields.
            task_text = (
                scope.acceptance_criteria[root_index]
                if aligned and root_index is not None
                else render_sourced_objective(scope)
            )
            if shares_one_attempt(scope, criteria):
                schema_criteria = criteria
            seed_goal = scope.goal
            sibling_acs = None  # siblings would list model-written criteria
            if level_contexts and not aligned:
                # Earlier levels' summaries name their criterion; keep the work
                # summary and show the sourced objective in its place.
                level_contexts = [
                    replace(
                        context,
                        completed_acs=tuple(
                            replace(summary, ac_content=task_text)
                            for summary in context.completed_acs
                        ),
                    )
                    for context in level_contexts
                ]

        # Build prompt
        if node_identity is not None:
            label = (
                f"AC {node_identity.display_path}"
                if node_identity.depth == 0
                else f"Sub-AC {node_identity.display_path}"
            )
            indent = "    " if node_identity.depth > 0 else "  "
        elif is_sub_ac:
            label = f"Sub-AC {sub_ac_index + 1} of AC {parent_ac_index + 1}"
            indent = "    "
        else:
            label = f"AC {ac_index + 1}"
            indent = "  "

        task_section, context_governance_audit = executor._build_atomic_dispatch_context(
            ac_index=ac_index,
            ac_content=task_text,
            label=label,
            level_contexts=level_contexts,
            sibling_acs=sibling_acs,
        )
        # Surface only the worker-owned portion of the success contract. Harness
        # commands and assertions remain hidden grading inputs.
        contract_block = _build_success_contract_block(ac_spec)
        if contract_block:
            task_section = f"{task_section}\n\n{contract_block}"
        legacy_context_section = (
            ""
            if context_governance_audit is not None
            and context_governance_audit.get("context_governed") is True
            else build_context_prompt(level_contexts or [])
        )

        retry_section = ""
        if retry_attempt > 0:
            retry_section = (
                "\n## Retry Context\n"
                f"This is retry attempt {retry_attempt} for this acceptance criterion.\n"
                "Resume from the current shared workspace state, including any "
                "coordinator-reconciled changes already applied.\n"
            )
        if retry_prompt_extra:
            # Verify-by-default retry enrichment (failure taxonomy, error tail,
            # verify-command output, and — on the final attempt — a lateral
            # change-of-approach directive) built by the batch retry loop.
            retry_section += "\n" + retry_prompt_extra + "\n"

        # Build parallel awareness section
        parallel_section = ""
        if sibling_acs and len(sibling_acs) > 1:
            other_acs = [
                content for sibling_index, content in sibling_acs if sibling_index != ac_index
            ]
            if other_acs:
                context_is_governed = (
                    context_governance_audit is not None
                    and context_governance_audit.get("context_governed") is True
                )
                if context_is_governed:
                    if executor._fat_harness_mode and executor._execution_profile is not None:
                        other_list = (
                            "Sibling/future ACs are summarized in the governed "
                            "sibling-status section above as out-of-scope boundary "
                            "context."
                        )
                    else:
                        other_list = (
                            "Sibling tasks in progress are summarized in the governed "
                            "sibling-status section above."
                        )
                else:
                    sibling_heading = (
                        "Sibling/future ACs that are OUT OF SCOPE for this dispatch:"
                        if executor._fat_harness_mode and executor._execution_profile is not None
                        else "Sibling tasks in progress:"
                    )
                    other_list = (
                        sibling_heading + "\n" + "\n".join(f"- {ac[:80]}" for ac in other_acs)
                    )
                if executor._fat_harness_mode and executor._execution_profile is not None:
                    parallel_section = (
                        "\n## Current AC Scope Boundary\n"
                        "Sibling/future ACs are listed only to define work that is "
                        "outside the current dispatch. Do not satisfy those criteria "
                        "now, and do not pre-create their files, tests, docs, or "
                        "evidence. Avoid modifying files that sibling/future ACs are "
                        "likely to own unless the current AC explicitly requires it.\n\n"
                        f"{other_list}\n"
                    )
                else:
                    parallel_section = (
                        "\n## Parallel Execution Notice\n"
                        "Other agents are working on sibling tasks concurrently. "
                        "Avoid modifying files that other agents are likely editing. "
                        "Focus on files directly related to YOUR task.\n\n"
                        f"{other_list}\n"
                    )

        # Scan the requested runtime workspace so prompts stay aligned with the actual task cwd.
        cwd = executor._task_cwd or executor._adapter.working_directory
        if not isinstance(cwd, str) or not cwd:
            cwd = os.getcwd()
        try:
            entries = sorted(os.listdir(cwd))
            file_listing = "\n".join(f"- {e}" for e in entries if not e.startswith("."))
        except OSError:
            file_listing = "(unable to list)"

        if executor._fat_harness_mode and executor._execution_profile is not None:
            has_success_contract = isinstance(ac_spec, AcceptanceCriterionSpec) and bool(
                ac_spec.verify_command
            )
            has_expected_artifacts = isinstance(ac_spec, AcceptanceCriterionSpec) and bool(
                ac_spec.expected_artifacts
            )
            # Only delegate tests_passed/files_touched to the verify gate when it
            # will actually run; with run_verify_commands off the worker's
            # displayed required-fields must still list them.
            verify_gate_active = executor._run_verify_commands
            # Each criterion judged from this attempt checks its own fields: ask
            # for every field one of them requires, in the profile's order.
            required = {
                field
                for text in schema_criteria
                for field in _effective_evidence_schema_for_ac(
                    executor._execution_profile,
                    text,
                    has_success_contract=has_success_contract,
                    has_expected_artifacts=has_expected_artifacts,
                    verify_gate_active=verify_gate_active,
                ).required
            }
            required_fields = ", ".join(
                field
                for field in executor._execution_profile.evidence_schema.required
                if field in required
            )
            doc_only_note = ""
            if all(_is_documentation_only_ac(text) for text in schema_criteria):
                doc_only_note = (
                    "This is a documentation-only current AC: verify the requested docs "
                    "with current-session README/docs evidence such as Edit plus a direct "
                    "read/grep/diff command when that command is the validation for the docs change. "
                    "Do not include tests_passed at all for documentation-only ACs. "
                    "If you ran tests as a sanity check, cite only the validation command "
                    "in commands_run when it directly validates the current docs change; "
                    "do not list individual test names or prior test IDs.\n"
                )
            validation_only_note = ""
            if all(_is_validation_only_ac(text) for text in schema_criteria):
                validation_only_note = (
                    "This is a validation-only current AC: prove it with commands_run "
                    "and tests_passed from this runtime session. Do not include "
                    "files_touched unless you actually edited, wrote, or generated files "
                    "for this current AC. Read-only inspection or running tests does not "
                    "count as files_touched.\n"
                )
            # The sibling boundary applies only when other criteria exist: a
            # multi-criterion Seed, a decomposed sub-AC, or a parallel batch. A
            # single-criterion run or a sourced objective has no sibling work.
            has_sibling_criteria = not sourced_objective and (
                len(criteria) > 1 or is_sub_ac or bool(sibling_acs and len(sibling_acs) > 1)
            )
            sibling_scope_note = (
                "You are responsible only for the current acceptance criterion in "
                "this dispatch. Do not implement, test, document, or pre-create work "
                "that belongs only to sibling or future ACs. If another AC mentions "
                "related files, future functions, tests, or docs, treat that work as "
                "out of scope unless the current AC explicitly requires it.\n"
                if has_sibling_criteria
                else ""
            )
            completion_instruction = (
                "## Current AC Scope Contract\n"
                f"{sibling_scope_note}"
                "Do not substitute a narrower, safer, or smaller solution than the "
                "goal asks for. Before you finish, audit that the change covers every "
                "case the goal names.\n"
                "Your final evidence JSON must cite only files, commands, and tests "
                "directly changed or run for this current AC in this runtime session. "
                "For files_touched, cite workspace-relative paths only, never absolute "
                "paths such as /tmp/... or /private/tmp/..., and never paths outside "
                "the working directory. "
                "For commands_run, include only validation/production commands such "
                "as test, build, lint, generation, or docs verification commands; omit "
                "exploratory discovery commands such as rg, grep, sed, cat, ls, find, "
                "or pwd unless the current AC explicitly requires that command as validation.\n"
                "For tests_passed and commands_run, list the exact command strings you "
                "executed, one command per item, with no annotations such as test counts, "
                "pass markers, or explanations.\n"
                f"{doc_only_note}{validation_only_note}\n"
                "Use the available tools to accomplish this task. Report progress through "
                "tool-visible work, not a prose-only completion claim.\n"
                f"{COMPLETION_CHECK}\n"
                "When complete, emit exactly ONE fenced JSON evidence record as the "
                "final response. Populate the active profile fields "
                f"directly ({required_fields}); do not emit a generic command_result "
                "wrapper. Do not prefix it with [TASK_COMPLETE] or any prose; the "
                "harness decides success from typed evidence plus the verifier PASS."
            )
            interfaces = getattr(executor, "check_package_interfaces", None) or {}
            root_index = parent_ac_index if is_sub_ac else ac_index
            if root_index in interfaces:
                # An admitted oracle checks this root: ask for its entry_points.
                from ouroboros.boundary.binding import entry_points_request

                completion_instruction += entry_points_request(interfaces[root_index])
        else:
            completion_instruction = (
                "Use the available tools to accomplish this task. Report your progress "
                "clearly.\nWhen complete, explicitly state: [TASK_COMPLETE]"
            )

        prompt = f"""Execute the following task:

## Working Directory
`{cwd}`

Files present:
{file_listing}

**Important**: Use the runtime's file-search tools to discover files. Never guess absolute paths.

## Goal Context
{seed_goal}

{capsule.to_prompt_reference_block() if capsule is not None else ""}

{render_auto_recursion_guard()}

{task_section}
{legacy_context_section}{retry_section}{parallel_section}
{completion_instruction}
"""

        return AtomicPromptBundle(
            prompt=prompt,
            label=label,
            indent=indent,
            context_governance_audit=context_governance_audit,
        )
