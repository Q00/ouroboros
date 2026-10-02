"""Runtime transcript verification for typed leaf evidence."""

from __future__ import annotations

from pathlib import Path

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.ac_classification import _effective_evidence_schema_for_ac
from ouroboros.orchestrator.evidence.cited_evidence import (
    CITABLE_FIELDS,
    CitedEvidence,
    verify_cited_record,
)
from ouroboros.orchestrator.evidence.claims import (
    _runtime_messages_have_masked_test_command_form,
    _runtime_messages_support_claim,
    _runtime_messages_support_command_claim,
    _runtime_messages_support_file_claim,
    _runtime_support_messages_for_field,
    _workspace_relative_file_claim,
)
from ouroboros.orchestrator.evidence.command_replay import replayed_command_supports_claim
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.harness_observation import (
    is_harness_observation_message,
    observation_from_message,
    observations_confirm_unmutated_workspace,
)
from ouroboros.orchestrator.evidence.observed_runs import (
    SCRIPT_ABSENT_FROM_ARTIFACT,
    observed_zero_exit_run,
)
from ouroboros.orchestrator.evidence.test_detection import (
    _functional_command_supports_test_claim,
    _runtime_messages_have_completed_command_for_test_claim,
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
    skip_fields: frozenset[str] = frozenset(),
) -> VerifierVerdict:
    """Verify leaf evidence is backed by runtime transcript events.

    The verifier deliberately ignores the final result message so accepted
    evidence cannot be supported only by the leaf's self-report. A declared
    verify command is additive and does not remove transcript obligations.

    A record the evidence turn produced (``typed_evidence.cited``) proves its
    command fields by the call numbers the worker cited
    (``cited_evidence.verify_cited_record``); this function then verifies only
    its other fields (``skip_fields``). A record without citations takes the
    command-string path below, unchanged.
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

    if isinstance(typed_evidence.cited, CitedEvidence) and not skip_fields:
        return verify_cited_record(
            typed_evidence.cited,
            messages=support_messages,
            required_fields=effective_schema.required,
            task_cwd=task_cwd or adapter_working_directory,
            verify_other_fields=lambda: _verify_atomic_evidence_against_runtime_messages(
                messages=messages,
                typed_evidence=typed_evidence,
                ac_content=ac_content,
                execution_profile=execution_profile,
                task_cwd=task_cwd,
                adapter_working_directory=adapter_working_directory,
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
                skip_fields=frozenset(CITABLE_FIELDS),
            ),
        )

    unsupported: list[str] = []
    evidence_form_mismatches: list[str] = []
    # Observed runs whose script is absent from the artifact: not replayable,
    # so they yield no evidence; they are not fabrication.
    not_replayed: list[str] = []
    masked_command_mismatch = False
    completed_command_mismatch = False
    absent_script_mismatch = False
    backed_commands = tuple(
        command
        for command in _flatten_evidence_values(typed_evidence.get("commands_run"))
        if _runtime_messages_support_command_claim(
            command,
            _runtime_support_messages_for_field("commands_run", support_messages),
        )
    )
    required_fields = set(effective_schema.required) - skip_fields
    fields_to_verify = [name for name in effective_schema.required if name not in skip_fields]
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
                # Replay-first: a transcript command linked to this claim
                # exited 0 when the harness replayed it in a workspace copy.
                if replayed_command_supports_claim(value, support_messages):
                    continue
                # The transcript recorded this command running with exit 0
                # as part of a larger Bash call (after the edit that set it
                # up): the same structured support the whole-command alias
                # gives, narrowed to a recorded zero exit.
                if observed_zero_exit_run(value, support_messages, task_cwd=workspace_cwd):
                    continue
                if _runtime_messages_have_masked_test_command_form(
                    value,
                    field_messages,
                ):
                    masked_command_mismatch = True
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
                # claim is literally false, so it is admitted only where it is
                # harmless: the AC carries a success contract whose hidden
                # verify gate is the behavioural authority (a leaf that did
                # nothing still has to pass it), the harness's own snapshot
                # diff witnessed zero mutation, and the named path is a real
                # workspace file. A prose AC keeps the rejection -- there a
                # stale file must not prove this run touched it. A ghost path
                # stays unsupported, and any observed mutation withdraws the
                # waiver so a leaf that wrote something else cannot launder an
                # unrelated claim through it.
                if (
                    has_success_contract
                    and verify_gate_active
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
                if replayed_command_supports_claim(value, support_messages):
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
                # The same functional tier for a run the transcript recorded
                # inside a larger Bash call. When its script is no longer in
                # the workspace (a scratch script the worker ran and deleted),
                # the run cannot be replayed and nothing vouches for what it
                # ran: no evidence, never fabrication. Without the verify gate
                # it stays a rejection, as an evidence-form mismatch.
                observed = observed_zero_exit_run(value, support_messages, task_cwd=workspace_cwd)
                if observed is not None and observed.script is not None:
                    if observed.script_present:
                        if verify_gate_active:
                            continue
                    elif verify_gate_active:
                        not_replayed.append(f"{field_name}: {value}")
                        continue
                    else:
                        absent_script_mismatch = True
                        evidence_form_mismatches.append(f"{field_name}: {value}")
                        unsupported.append(f"{field_name}: {value}")
                        continue
                if _runtime_messages_have_masked_test_command_for_test_claim(
                    value=value,
                    messages=support_messages,
                    task_cwd=workspace_cwd,
                ):
                    masked_command_mismatch = True
                    evidence_form_mismatches.append(f"{field_name}: {value}")
                    unsupported.append(f"{field_name}: {value}")
                    continue
                if _runtime_messages_have_completed_command_for_test_claim(
                    value=value, messages=support_messages
                ):
                    completed_command_mismatch = True
                    evidence_form_mismatches.append(f"{field_name}: {value}")
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
        reason_prefix = "unsupported evidence claims"
        if failure_class == "EVIDENCE_FORM_MISMATCH":
            details = []
            if masked_command_mismatch:
                details.append(
                    "unprotected output-filter pipeline cannot prove a clean command claim"
                )
            if completed_command_mismatch:
                details.append(
                    "recorded command completion cannot prove tests_passed; "
                    "retry with contract-compliant test evidence from the runtime "
                    "or a runner-owned adapter"
                )
            if absent_script_mismatch:
                details.append(
                    f"{SCRIPT_ABSENT_FROM_ARTIFACT}: the recorded run's script is not "
                    "in the workspace, so it cannot be replayed; keep the script or "
                    "cite a command that checks the delivered artifact"
                )
            reason_prefix = "evidence form mismatch; " + "; ".join(details)
        return VerifierVerdict(
            passed=False,
            reasons=(reason_prefix + ": " + "; ".join(unsupported),),
            failure_class=failure_class,
        )

    if not_replayed:
        # Every other claim is supported; these runs happened but cannot be
        # replayed, so they are recorded and prove nothing. A criterion with
        # another tests_passed claim proven passes on that claim. One whose
        # only tests_passed claims are these has no evidence: the verifier
        # withholds its pass without rejecting the work.
        record = tuple(f"{SCRIPT_ABSENT_FROM_ARTIFACT}: {entry}" for entry in not_replayed)
        test_claims = (
            tuple(_flatten_evidence_values(typed_evidence.get("tests_passed")))
            if "tests_passed" in required_fields
            else ()
        )
        # Only tests_passed claims are ever not replayed, and every other
        # one reached here proven.
        if len(test_claims) > len(not_replayed):
            return VerifierVerdict(passed=True, not_replayed=record)
        return VerifierVerdict(
            passed=False,
            reasons=(f"not_replayed: {SCRIPT_ABSENT_FROM_ARTIFACT}: " + "; ".join(not_replayed),),
            failure_class=FailureClass.SCRIPT_ABSENT_FROM_ARTIFACT.value,
            not_replayed=record,
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
