"""Runtime transcript verification for typed leaf evidence."""

from __future__ import annotations

from pathlib import Path

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.ac_classification import _effective_evidence_schema_for_ac
from ouroboros.orchestrator.evidence.claims import (
    _runtime_messages_have_masked_test_command_form,
    _runtime_messages_support_claim,
    _runtime_messages_support_command_claim,
    _runtime_messages_support_file_claim,
    _runtime_support_messages_for_field,
    _workspace_relative_file_claim,
)
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.harness_observation import (
    is_harness_observation_message,
    observation_from_message,
    observations_confirm_unmutated_workspace,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _functional_command_supports_test_claim,
    _runtime_messages_have_masked_test_command_for_test_claim,
    _runtime_messages_support_test_claim,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.profile_loader import ExecutionProfile
from ouroboros.orchestrator.verifier import VerifierVerdict


def _harness_observation_supports_command_claim(
    value: str,
    messages: tuple[AgentMessage, ...],
) -> bool:
    """Return True when the harness itself ran the claimed command successfully."""
    return any(
        observation.supports_command_claim(value)
        for observation in (observation_from_message(message) for message in messages)
        if observation is not None
    )


def _verify_atomic_evidence_against_runtime_messages(
    *,
    messages: tuple[AgentMessage, ...],
    typed_evidence: EvidenceRecord,
    ac_content: str,
    execution_profile: ExecutionProfile,
    task_cwd: str | None,
    adapter_working_directory: str | None,
    has_success_contract: bool = False,
    has_expected_artifacts: bool = False,
    verify_gate_active: bool = False,
) -> VerifierVerdict:
    """Verify leaf evidence is backed by runtime transcript events.

    The verifier deliberately ignores the final result message so accepted
    evidence cannot be supported only by the leaf's self-report. A declared
    verify command is additive and does not remove transcript obligations.
    """
    effective_schema = _effective_evidence_schema_for_ac(
        execution_profile,
        ac_content,
        has_success_contract=has_success_contract,
        has_expected_artifacts=has_expected_artifacts,
        verify_gate_active=verify_gate_active,
    )
    # Exclude the leaf's terminal self-report by identity, not by position: a
    # harness observation may legitimately sit after the final result message.
    final_indices = [index for index, message in enumerate(messages) if message.is_final]
    terminal_index = final_indices[-1] if final_indices else None
    support_messages = tuple(
        message for index, message in enumerate(messages) if index != terminal_index
    )
    # A harness observation is support for claims, not proof that the runtime
    # transcript arrived: an otherwise empty stream is still an infrastructure
    # signal.
    if not any(not is_harness_observation_message(message) for message in support_messages):
        if not effective_schema.required:
            return VerifierVerdict(passed=True)
        # A completely empty transcript is an infrastructure signal, not a
        # gaming signal: a leaf trying to game the verifier leaves
        # plausible-looking messages, not none. Naming it separately keeps a
        # lost transcript from being reported as a worker rejection — and from
        # being read as evidence the leaf did nothing.
        return VerifierVerdict(
            passed=False,
            reasons=(
                "transcript_missing_infrastructure: the runtime transcript reached "
                "the verifier empty, so no claim could be checked; the leaf's work "
                "was not evaluated",
            ),
            failure_class=FailureClass.TRANSCRIPT_MISSING_INFRASTRUCTURE.value,
        )

    unsupported: list[str] = []
    evidence_form_mismatches: list[str] = []
    backed_commands = tuple(
        command
        for command in _flatten_evidence_values(typed_evidence.get("commands_run"))
        if _runtime_messages_support_command_claim(
            command,
            _runtime_support_messages_for_field("commands_run", support_messages),
        )
    )
    required_fields = set(effective_schema.required)
    fields_to_verify = list(effective_schema.required)
    workspace_cwd = task_cwd or adapter_working_directory

    for field_name in fields_to_verify:
        values = tuple(_flatten_evidence_values(typed_evidence.get(field_name)))
        if not values:
            if field_name in required_fields:
                # A pure-verification run may honestly have nothing to touch:
                # when the verify gate is active and the harness's own
                # snapshot diff witnessed zero workspace mutation, an empty
                # files_touched is corroborated truth, not withheld evidence.
                # Whether the AC also carries a success contract does not
                # change what the snapshot proved, so it is not a condition.
                if (
                    field_name == "files_touched"
                    and verify_gate_active
                    and observations_confirm_unmutated_workspace(support_messages)
                ):
                    continue
                unsupported.append(f"{field_name}: no concrete claim values")
            continue
        field_messages = _runtime_support_messages_for_field(field_name, support_messages)
        for value in values:
            if field_name == "commands_run":
                if _runtime_messages_support_command_claim(value, field_messages):
                    continue
                if _harness_observation_supports_command_claim(value, support_messages):
                    continue
                if _runtime_messages_have_masked_test_command_form(
                    value,
                    field_messages,
                ):
                    evidence_form_mismatches.append(f"{field_name}: {value}")
                    unsupported.append(f"{field_name}: {value}")
                    continue
                unsupported.append(f"{field_name}: {value}")
                continue
            if field_name == "files_touched":
                if _runtime_messages_support_file_claim(
                    value,
                    field_messages,
                    task_cwd=workspace_cwd,
                ):
                    continue
                # A dependent AC often finds its files already built by a
                # sibling, verifies them, and lists them as "touched". The
                # harness's own snapshot diff is the witness that the leaf
                # mutated nothing; when it says so, the verify gate is active,
                # and the named path is a real workspace file, the claim is a
                # mislabelled verification, not invented work. The file must
                # exist: a ghost path stays unsupported, and any observed
                # mutation withdraws the waiver so a leaf that wrote something
                # else cannot launder an unrelated claim through it.
                if (
                    verify_gate_active
                    and observations_confirm_unmutated_workspace(support_messages)
                    and _claimed_file_exists_in_workspace(value, task_cwd=workspace_cwd)
                ):
                    continue
                unsupported.append(f"{field_name}: {value}")
                continue
            if field_name == "tests_passed":
                if _runtime_messages_support_test_claim(
                    value=value,
                    backed_commands=backed_commands,
                    messages=support_messages,
                    task_cwd=workspace_cwd,
                ):
                    continue
                # Functional-verification tier: while the verify gate is
                # active, a non-test claim that IS a transcript-backed,
                # zero-exit execution of a real workspace artifact is honest
                # evidence, not fabrication. The tier used to require a
                # success contract on the AC as well; a prose AC is exactly
                # where transcript-backed functional evidence is the only
                # behavioural evidence there is, so that condition kept the
                # tier away from the ACs that needed it.
                if verify_gate_active and _functional_command_supports_test_claim(
                    value=value,
                    messages=support_messages,
                    task_cwd=workspace_cwd,
                ):
                    continue
                if _runtime_messages_have_masked_test_command_for_test_claim(
                    value=value,
                    messages=support_messages,
                    task_cwd=workspace_cwd,
                ):
                    evidence_form_mismatches.append(f"{field_name}: {value}")
                    unsupported.append(f"{field_name}: {value}")
                    continue
                unsupported.append(f"{field_name}: {value}")
                continue
            if not _runtime_messages_support_claim(value, field_messages):
                unsupported.append(f"{field_name}: {value}")

    if unsupported:
        failure_class = (
            "EVIDENCE_FORM_MISMATCH"
            if evidence_form_mismatches and len(evidence_form_mismatches) == len(unsupported)
            else "FABRICATION_SUSPECTED"
        )
        reason_prefix = (
            "evidence form mismatch; unprotected output-filter pipeline "
            "cannot prove a clean command claim"
            if failure_class == "EVIDENCE_FORM_MISMATCH"
            else "unsupported evidence claims"
        )
        return VerifierVerdict(
            passed=False,
            reasons=(reason_prefix + ": " + "; ".join(unsupported),),
            failure_class=failure_class,
        )

    return VerifierVerdict(passed=True)


def _claimed_file_exists_in_workspace(value: str, *, task_cwd: str | None) -> bool:
    """Return True when a ``files_touched`` value names an existing regular file
    inside the workspace. Without a workspace nothing can vouch for it."""
    if task_cwd is None:
        return False
    relative = _workspace_relative_file_claim(value, task_cwd=task_cwd)
    if relative is None:
        return False
    try:
        return (Path(task_cwd).resolve() / relative).is_file()
    except (OSError, RuntimeError, ValueError):
        return False
