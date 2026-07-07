"""Runtime transcript verification for typed leaf evidence."""

from __future__ import annotations

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.ac_classification import _effective_evidence_schema_for_ac
from ouroboros.orchestrator.evidence.claims import (
    _runtime_messages_have_masked_test_command_form,
    _runtime_messages_support_claim,
    _runtime_messages_support_command_claim,
    _runtime_messages_support_file_claim,
    _runtime_support_messages_for_field,
)
from ouroboros.orchestrator.evidence.common import _flatten_evidence_values
from ouroboros.orchestrator.evidence.test_detection import (
    _runtime_messages_have_masked_test_command_for_test_claim,
    _runtime_messages_support_test_claim,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.profile_loader import ExecutionProfile
from ouroboros.orchestrator.verifier import VerifierVerdict


def _verify_atomic_evidence_against_runtime_messages(
    *,
    messages: tuple[AgentMessage, ...],
    typed_evidence: EvidenceRecord,
    ac_content: str,
    execution_profile: ExecutionProfile,
    task_cwd: str | None,
    adapter_working_directory: str | None,
    verify_command: str | None = None,
) -> VerifierVerdict:
    """Verify leaf evidence is backed by runtime transcript events.

    The verifier deliberately ignores the final result message so the
    accepted evidence cannot be supported only by the leaf's self-report.
    """
    support_messages = tuple(messages[:-1] if messages and messages[-1].is_final else messages)
    if not support_messages:
        return VerifierVerdict(
            passed=False,
            reasons=("no runtime transcript evidence supports the typed evidence claims",),
            failure_class="EVIDENCE_MISSING",
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
    effective_schema = _effective_evidence_schema_for_ac(execution_profile, ac_content)
    required_fields = set(effective_schema.required)
    fields_to_verify = list(effective_schema.required)
    workspace_cwd = task_cwd or adapter_working_directory

    for field_name in fields_to_verify:
        values = tuple(_flatten_evidence_values(typed_evidence.get(field_name)))
        if not values:
            if field_name in required_fields:
                unsupported.append(f"{field_name}: no concrete claim values")
            continue
        field_messages = _runtime_support_messages_for_field(field_name, support_messages)
        for value in values:
            if field_name == "commands_run":
                if _runtime_messages_support_command_claim(value, field_messages):
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
                unsupported.append(f"{field_name}: {value}")
                continue
            if field_name == "tests_passed":
                if _runtime_messages_support_test_claim(
                    value=value,
                    backed_commands=backed_commands,
                    messages=support_messages,
                    task_cwd=workspace_cwd,
                    verify_command=verify_command,
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
