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

from dataclasses import dataclass
import os
from typing import TYPE_CHECKING, Any

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.core.seed_contract_prompt import render_auto_recursion_guard
from ouroboros.orchestrator.evidence.ac_classification import (
    _effective_evidence_schema_for_ac,
    _is_documentation_only_ac,
    _is_validation_only_ac,
)
from ouroboros.orchestrator.level_context import LevelContext, build_context_prompt

if TYPE_CHECKING:
    from ouroboros.orchestrator.evidence.runtime_metadata import _SiblingACRef
    from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
    from ouroboros.orchestrator.parallel_executor import ParallelACExecutor


def _build_success_contract_block(spec: AcceptanceCriterionSpec | None) -> str:
    """Render the worker-facing SUCCESS CONTRACT block for an AC, or ``""``.

    The parallel leaf dispatch builds its own prompt (it does not go through the
    host ``build_execute_subagent`` VERIFY section, nor does the repo-level context
    pack carry a *per-AC* contract), so a worker was never told the exact
    verify_command / expected_artifacts / output_assertion the harness will grade
    it against. When the AC's spec carries a contract, surface it verbatim so the
    worker runs and reports the same evidence the verify gate checks. Contract-less
    ACs return ``""`` — the prompt stays byte-identical to before.
    """
    if spec is None or not spec.has_success_contract:
        return ""
    lines = ["SUCCESS CONTRACT for this AC:"]
    if spec.verify_command:
        lines.append(
            f"- Run locally before completion: {spec.verify_command}. "
            "The verify gate re-runs it and records authoritative evidence."
        )
    if spec.expected_artifacts:
        lines.append(
            "- Expected artifacts: "
            + ", ".join(spec.expected_artifacts)
            + " — ensure they exist in the workspace"
        )
    if spec.output_assertion:
        lines.append(f"- Expected output: {spec.output_assertion}")
    return "\n".join(lines)


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
    ) -> AtomicPromptBundle:
        """Build the prompt for one atomic leaf dispatch."""
        executor = self._executor

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
            ac_content=ac_content,
            label=label,
            level_contexts=level_contexts,
            sibling_acs=sibling_acs,
        )
        # Surface this AC's success contract to the worker so it runs and reports
        # the exact evidence the verify gate will grade. Empty for contract-less
        # ACs → the prompt stays byte-identical to before.
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
            effective_schema = _effective_evidence_schema_for_ac(
                executor._execution_profile,
                ac_content,
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
            )
            required_fields = ", ".join(effective_schema.required)
            doc_only_note = ""
            if _is_documentation_only_ac(ac_content):
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
            if _is_validation_only_ac(ac_content):
                validation_only_note = (
                    "This is a validation-only current AC: prove it with commands_run "
                    "and tests_passed from this runtime session. Do not include "
                    "files_touched unless you actually edited, wrote, or generated files "
                    "for this current AC. Read-only inspection or running tests does not "
                    "count as files_touched.\n"
                )
            completion_instruction = (
                "## Current AC Scope Contract\n"
                "You are responsible only for the current acceptance criterion in "
                "this dispatch. Do not implement, test, document, or pre-create work "
                "that belongs only to sibling or future ACs. If another AC mentions "
                "related files, future functions, tests, or docs, treat that work as "
                "out of scope unless the current AC explicitly requires it.\n"
                "Your final evidence JSON must cite only files, commands, and tests "
                "directly changed or run for this current AC in this runtime session. "
                "For files_touched, cite workspace-relative paths only, never absolute "
                "paths such as /tmp/... or /private/tmp/..., and never paths outside "
                "the working directory. "
                "For commands_run, include only validation/production commands such "
                "as test, build, lint, generation, or docs verification commands; omit "
                "exploratory discovery commands such as rg, grep, sed, cat, ls, find, "
                "or pwd unless the current AC explicitly requires that command as validation.\n"
                f"{doc_only_note}{validation_only_note}\n"
                "Use the available tools to accomplish this task. Report progress through "
                "tool-visible work, not a prose-only completion claim.\n"
                "When complete, emit exactly ONE fenced JSON evidence record as the "
                "final response and then stop. Populate the active profile fields "
                f"directly ({required_fields}); do not emit a generic command_result "
                "wrapper. Do not prefix it with [TASK_COMPLETE] or any prose; the "
                "harness decides success from typed evidence plus the verifier PASS."
            )
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

**Important**: Use Glob to discover files. Never guess absolute paths.

## Goal Context
{seed_goal}

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
