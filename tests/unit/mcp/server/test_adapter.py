"""Tests for MCP server adapter."""

import asyncio
from datetime import UTC, datetime
import json
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from ouroboros import __version__
from ouroboros.core.lineage import ACResult, EvaluationSummary, TaskResult
from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.io_recorder import get_current_io_journal_recorder
from ouroboros.mcp.errors import MCPResourceNotFoundError, MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobSnapshot, JobStatus
from ouroboros.mcp.server.adapter import (
    VALID_TRANSPORTS,
    MCPServerAdapter,
    _agent_results_from_execution_summary,
    _build_prompt_signature_with_aliases,
    _build_tool_signature_with_aliases,
    _evaluation_summary_for_unavailable_spec_verification,
    _evaluation_summary_from_spec_verification,
    _extract_feedback_metadata_from_artifact,
    _parse_legacy_execution_task_summary,
    _project_dir_from_artifact,
    _project_dir_from_seed,
    _safe_cwd,
    _to_fastmcp_tool_result,
    _validate_parameter_constraints,
    validate_transport,
)
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobWaitHandler
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext
from ouroboros.orchestrator.control_bus import ControlBus, ControlBusDrainError
from ouroboros.persistence.event_store import EventStore
from ouroboros.verification.extractor import AssertionExtractor
from ouroboros.verification.models import (
    ACVerificationReport,
    SpecAssertion,
    SpecVerificationResult,
    SpecVerificationSummary,
    VerificationOutcome,
    VerificationTier,
)
from ouroboros.verification.verifier import SpecVerifier


class _FakeEventStore:
    async def append(self, event: object) -> None:
        pass


class AcmePrivateProjectError(Exception):
    """Stand-in for a registered extension's own error class.

    Its class *name* "AcmePrivateProjectError" is not in
    _safe_error_type()'s closed _SAFE_ERROR_TYPE_NAMES vocabulary, so it
    must fold to the fixed ExtensionError literal regardless of anything
    this class's __module__ claims about itself.
    """


class SpoofedBuiltinModuleError(Exception):
    """An extension error class that lies about living in ``builtins``.

    The pre-round-14 gate trusted ``__module__ == "builtins"`` to mean the
    class name was safe to serialize verbatim -- but __module__ is just an
    ordinary class attribute an extension can set to anything. This class's
    real identifying name must still fold since name-only matching never
    looks at __module__ at all.
    """


SpoofedBuiltinModuleError.__module__ = "builtins"


class SpoofedOuroborosPrefixError(Exception):
    """An extension error class whose module merely starts with the
    substring "ouroboros" without being the real ``ouroboros`` package.

    The pre-round-14 gate's ``module.startswith("ouroboros")`` check
    treated this as trusted -- a real, exploitable prefix-collision bug a
    package named e.g. ``ouroboros_acme_private`` could trigger.
    """


SpoofedOuroborosPrefixError.__module__ = "ouroboros_acme_private.errors"


class MalformedModuleMetadataError(Exception):
    """An extension error class whose __module__ is not even a string.

    Nothing requires __module__ to be a string; the pre-round-14 gate's
    ``module.partition(".")`` would raise AttributeError against this,
    replacing the real Result.err payload with a crash instead of
    delivering it to the caller -- exactly the never-raises violation this
    round's fix closes.
    """


MalformedModuleMetadataError.__module__ = 123  # type: ignore[assignment]


class _RaisingNameMeta(type):
    """Metaclass whose __name__ property raises on access.

    Exercises _safe_error_type()'s own try/except: even ``type(error).__name__``
    itself must never be trusted to simply return a string without incident.
    """

    @property
    def __name__(cls) -> str:  # type: ignore[override]
        raise RuntimeError("hostile __name__ access")


class HostileNameError(Exception, metaclass=_RaisingNameMeta):
    """An error class that raises when its own __name__ is read."""


class _RaisingNameMetaKeyboardInterrupt(type):
    """Metaclass whose __name__ property raises KeyboardInterrupt.

    A hostile object choosing this exception class over a plain RuntimeError
    is not an actual user interrupt -- it is an extension-controlled dunder
    read designed to slip past a narrower ``except Exception`` in whatever
    code reads it. _safe_error_type must swallow it all the same.
    """

    @property
    def __name__(cls) -> str:  # type: ignore[override]
        raise KeyboardInterrupt("hostile __name__ access (not a real interrupt)")


class HostileKeyboardInterruptNameError(Exception, metaclass=_RaisingNameMetaKeyboardInterrupt):
    """An error class whose __name__ read raises KeyboardInterrupt."""


class HostileRaisedKeyboardInterrupt(
    KeyboardInterrupt, metaclass=_RaisingNameMetaKeyboardInterrupt
):
    """Directly subclasses KeyboardInterrupt (not Exception) so a handler
    that *raises* this bypasses MCPServerAdapter._call_tool_impl's
    ``except Exception`` entirely -- KeyboardInterrupt/SystemExit are direct
    BaseException subclasses, siblings of Exception, not descendants of it.
    It reaches observe_adapter_tool_call's own ``except BaseException``
    clause with this exact object still intact, letting _safe_error_type's
    isolation be proven through the real adapter path (unlike the
    Result.err(HostileNameError(...)) shape, which is intercepted by
    _call_tool_impl's own pre-existing, unrelated logging call before ever
    reaching this boundary -- see the round-14 report).
    """


class _RaisingNameMetaSystemExit(type):
    """Metaclass whose __name__ property raises SystemExit, not a real exit."""

    @property
    def __name__(cls) -> str:  # type: ignore[override]
        raise SystemExit("hostile __name__ access (not a real exit)")


class HostileSystemExitNameError(Exception, metaclass=_RaisingNameMetaSystemExit):
    """An error class whose __name__ read raises SystemExit."""


class _HostileIsErrorRaisesSystemExit:
    """Stand-in for a handler's MCPToolResult-shaped return value whose
    ``is_error`` property raises SystemExit rather than returning a bool.

    Used inside Result.ok(...) -- the success path, where
    _is_logical_error() reads .is_error to decide whether a completed
    request was actually a logical failure.
    """

    @property
    def is_error(self) -> bool:
        raise SystemExit("hostile is_error access (not a real exit)")


class _HostileIsErrorRaisesKeyboardInterrupt:
    """SystemExit's sibling case for _is_logical_error, for symmetry."""

    @property
    def is_error(self) -> bool:
        raise KeyboardInterrupt("hostile is_error access (not a real interrupt)")


class MockToolHandler:
    """Mock tool handler for testing."""

    def __init__(self, name: str = "test_tool") -> None:
        self._name = name
        self.handle_mock = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Success"),),
                )
            )
        )

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name=self._name,
            description="A test tool",
            parameters=(
                MCPToolParameter(
                    name="input",
                    type=ToolInputType.STRING,
                    description="Input value",
                ),
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        return await self.handle_mock(arguments)


class MockResourceHandler:
    """Mock resource handler for testing."""

    def __init__(self, uri: str = "test://resource") -> None:
        self._uri = uri
        self.handle_mock = AsyncMock(
            return_value=Result.ok(MCPResourceContent(uri=uri, text="Resource content"))
        )

    @property
    def definitions(self) -> list[MCPResourceDefinition]:
        return [
            MCPResourceDefinition(
                uri=self._uri,
                name="Test Resource",
                description="A test resource",
            )
        ]

    async def handle(self, uri: str) -> Result[MCPResourceContent, MCPServerError]:
        return await self.handle_mock(uri)


class TestMCPServerAdapter:
    """Test MCPServerAdapter class."""

    def test_adapter_creation(self) -> None:
        """Adapter is created with correct defaults."""
        adapter = MCPServerAdapter()
        assert adapter.info.name == "ouroboros-mcp"
        assert adapter.info.version == __version__

    def test_adapter_custom_name(self) -> None:
        """Adapter accepts custom name and version."""
        adapter = MCPServerAdapter(name="custom-server", version="2.0.0")
        assert adapter.info.name == "custom-server"
        assert adapter.info.version == "2.0.0"

    def test_adapter_stores_instructions_for_serve(self) -> None:
        """MCP server ``instructions`` are stored for the SDK v2 boundary."""
        adapter = MCPServerAdapter(instructions="hello ubiquitous language")
        assert adapter._instructions == "hello ubiquitous language"
        # Default is None when the caller does not supply instructions.
        assert MCPServerAdapter()._instructions is None

    def test_project_dir_from_seed_uses_primary_brownfield_reference(self, tmp_path) -> None:
        """Brownfield primary context should be treated as the project directory."""
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir=None, working_directory=None),
            brownfield_context=SimpleNamespace(
                context_references=(SimpleNamespace(path=str(tmp_path), role="primary"),)
            ),
        )

        assert _project_dir_from_seed(seed) == str(tmp_path)

    def test_project_dir_from_artifact_detects_package_json_root(self, tmp_path) -> None:
        """Artifact path discovery should support package.json-based projects."""
        project_dir = tmp_path / "web-app"
        nested_dir = project_dir / "src" / "components"
        nested_dir.mkdir(parents=True)
        (project_dir / "package.json").write_text('{"name":"web-app"}')

        artifact = f"Write: {nested_dir / 'app.tsx'}"

        assert _project_dir_from_artifact(artifact) == str(project_dir)

    def test_project_dir_from_artifact_handles_spaces_in_paths(self, tmp_path) -> None:
        """Artifact extraction should detect spaced file paths."""
        project_dir = tmp_path / "my project"
        nested_dir = project_dir / "src" / "components"
        nested_dir.mkdir(parents=True)
        (project_dir / "pyproject.toml").write_text("[build-system]")

        artifact = f"Edit: {nested_dir / 'app.tsx'}"

        assert _project_dir_from_artifact(artifact) == str(project_dir)

    def test_build_tool_signature_sanitizes_non_identifier_parameter_names(self) -> None:
        """Invalid MCP parameter names are sanitized to valid Python signatures."""
        parameters = (
            MCPToolParameter(name="file-path", type=ToolInputType.STRING),
            MCPToolParameter(name="max.tokens", type=ToolInputType.INTEGER),
            MCPToolParameter(name="class", type=ToolInputType.BOOLEAN),
        )

        signature, aliases = _build_tool_signature_with_aliases(parameters)
        names = tuple(param.name for param in signature.parameters.values())
        assert names == ("file_path", "max_tokens", "_class")
        assert aliases == {
            "file_path": "file-path",
            "max_tokens": "max.tokens",
            "_class": "class",
        }

    def test_build_prompt_signature_preserves_wire_argument_aliases(self) -> None:
        definition = MCPPromptDefinition(
            name="review",
            arguments=(
                MCPPromptArgument(name="file-path", description="File to review"),
                MCPPromptArgument(name="class", required=False),
            ),
        )

        signature, aliases = _build_prompt_signature_with_aliases(definition)

        assert tuple(signature.parameters) == ("file_path", "_class")
        assert aliases == {"file_path": "file-path", "_class": "class"}
        assert signature.parameters["file_path"].default.is_required()
        assert signature.parameters["_class"].default is None

    def test_validate_parameter_constraints_rejects_enum_and_array_items(self) -> None:
        parameters = (
            MCPToolParameter(
                name="mode",
                type=ToolInputType.STRING,
                enum=("fast", "safe"),
            ),
            MCPToolParameter(
                name="labels",
                type=ToolInputType.ARRAY,
                items={"type": "string"},
            ),
        )

        _validate_parameter_constraints(parameters, {"mode": "fast", "labels": ["ok"]})
        with pytest.raises(ValueError, match="mode"):
            _validate_parameter_constraints(parameters, {"mode": "unsafe"})
        with pytest.raises(ValueError, match="labels"):
            _validate_parameter_constraints(parameters, {"labels": ["ok", 1]})

    def test_legacy_execution_report_maps_to_task_results_not_ac_verdicts(self) -> None:
        """Legacy AC PASS/FAIL execution lines are worker task completion signals."""
        seed = SimpleNamespace(acceptance_criteria=("Implement feature", "Add tests"))
        artifact = """
Parallel Execution Verification Report

## AC Results
### AC 1: [PASS] Implement feature
### AC 2: [FAIL] Add tests
""".strip()

        summary = _parse_legacy_execution_task_summary(artifact, seed)

        assert summary is not None
        assert summary.ac_results == ()
        assert [task.status for task in summary.task_results] == ["completed", "failed"]
        assert [task.source_ac_index for task in summary.task_results] == [0, 1]
        assert summary.score == 0.5
        assert summary.drift_score is None
        assert summary.approval_status == "not_evaluated"
        assert summary.execution_completion_status == "failed"
        assert summary.run_verdict_passed is False

    def test_current_task_report_maps_to_task_results_not_ac_verdicts(self) -> None:
        """Current Task COMPLETED/FAILED lines are worker task completion signals."""
        seed = SimpleNamespace(acceptance_criteria=("Implement feature", "Add tests"))
        artifact = """
Parallel Execution Verification Report

## Task Results
### Task 1: [COMPLETED] Implement feature
### Task 2: [FAILED] Add tests
""".strip()

        summary = _parse_legacy_execution_task_summary(artifact, seed)

        assert summary is not None
        assert summary.ac_results == ()
        assert [task.status for task in summary.task_results] == ["completed", "failed"]
        assert [task.execution_method for task in summary.task_results] == [
            "parallel_report",
            "parallel_report",
        ]
        assert summary.drift_score is None
        assert summary.approval_status == "not_evaluated"

    def test_legacy_execution_report_completion_does_not_approve_without_evaluation(self) -> None:
        """All worker tasks completing still requires a separate formal AC verdict."""
        seed = SimpleNamespace(acceptance_criteria=("Implement feature",))
        artifact = "### AC 1: [PASS] Implement feature"

        summary = _parse_legacy_execution_task_summary(artifact, seed)

        assert summary is not None
        assert len(summary.task_results) == 1
        assert summary.task_results[0].completed is True
        assert summary.ac_results == ()
        assert summary.execution_completion_status == "completed"
        assert summary.approval_status == "not_evaluated"
        assert summary.drift_score is None
        assert summary.run_verdict == "FAIL"

    def test_duplicate_task_indices_do_not_satisfy_seed_coverage(self) -> None:
        """Repeated Task 1 records cannot stand in for a missing Seed AC."""
        seed = SimpleNamespace(acceptance_criteria=("Create config", "Add docs"))
        artifact = """
### Task 1: [COMPLETED] Create config
### Task 1: [COMPLETED] Duplicate worker record
""".strip()

        summary = _parse_legacy_execution_task_summary(artifact, seed)

        assert summary is not None
        assert [task.source_ac_index for task in summary.task_results] == [0, 0]
        assert summary.score == 0.5
        assert summary.execution_completion_status == "failed"
        assert summary.approval_status == "not_evaluated"
        assert summary.run_verdict == "FAIL"
        assert "duplicate task indices" in (summary.failure_reason or "")

    def test_task_zero_is_rejected_before_formal_spec_projection(self) -> None:
        """A zero task number cannot become a negative Seed or AC identity."""
        seed = SimpleNamespace(acceptance_criteria=("Create marker.txt",))
        mechanical = _parse_legacy_execution_task_summary(
            "### Task 0: [COMPLETED] bogus",
            seed,
        )
        assert mechanical is not None
        assert mechanical.task_results == ()
        assert mechanical.execution_completion_status == "failed"
        assert "invalid one-based task number(s): 0" in (mechanical.failure_reason or "")

        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create marker.txt",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern="marker",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Create marker.txt",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            verified=True,
                            detail="Found marker.txt",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert summary.final_approved is False
        assert summary.execution_completion_status == "failed"
        assert summary.run_verdict == "FAIL"

    @pytest.mark.parametrize(
        "artifact, invalid_number",
        [
            ("### AC 0: [PASS] bogus", "0"),
            ("### Task -1: [COMPLETED] bogus", "-1"),
        ],
    )
    def test_all_non_positive_legacy_task_numbers_are_rejected(
        self,
        artifact: str,
        invalid_number: str,
    ) -> None:
        """Both legacy syntaxes enforce a positive one-based identity."""
        summary = _parse_legacy_execution_task_summary(
            artifact,
            SimpleNamespace(acceptance_criteria=("Create marker.txt",)),
        )

        assert summary is not None
        assert summary.task_results == ()
        assert summary.execution_completion_status == "failed"
        assert f"invalid one-based task number(s): {invalid_number}" in (
            summary.failure_reason or ""
        )

    def test_agent_results_preserve_failed_legacy_task_for_spec_verification(self) -> None:
        """Legacy task failures must remain visible to the verifier input map."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=1,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Implement feature",
                    status="failed",
                    completed=False,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="failed",
            approval_status="not_evaluated",
        )

        assert _agent_results_from_execution_summary(mechanical) == {0: False}

    @pytest.mark.parametrize(
        "reason",
        [
            "Spec assertion extraction failed: unreadable response",
            "Spec assertion extraction produced no independently usable assertions.",
        ],
    )
    def test_unavailable_assertion_extraction_cannot_fall_back_to_mechanical_pass(
        self,
        reason: str,
    ) -> None:
        """Unreadable, rejected, and empty extraction all fail the formal gate."""
        mechanical = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=3,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content="Set MAX_RETRIES to 5",
                    passed=True,
                    score=1.0,
                    evidence="Agent reported PASS",
                ),
            ),
            execution_completion_status="completed",
            approval_status="approved",
        )
        seed = SimpleNamespace(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Set MAX_RETRIES to 5",
                    semantic_ac_key="ac_0123456789abcdef",
                ),
            )
        )

        summary = _evaluation_summary_for_unavailable_spec_verification(
            mechanical,
            seed,
            reason,
        )

        assert summary.final_approved is False
        assert summary.approval_status == "rejected"
        assert summary.run_verdict == "FAIL"
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert summary.ac_results[0].semantic_ac_key == "ac_0123456789abcdef"
        assert summary.failure_reason == reason

    def test_unverifiable_report_preserves_legacy_task_failure_as_ac_failure(self) -> None:
        """Skipped verifier assertions must not upgrade a failed task to approval."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=1,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Implement feature",
                    status="failed",
                    completed=False,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="failed",
            approval_status="not_evaluated",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Implement feature",
                    results=(),
                    agent_reported_pass=False,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.task_results[0].completed is False
        assert summary.ac_results[0].passed is False
        assert summary.execution_completion_status == "failed"
        assert summary.approval_status == "rejected"
        assert summary.run_verdict == "FAIL"

    def test_spec_verification_binds_verdict_to_seed_semantic_identity(self) -> None:
        semantic_key = "ac_0123456789abcdef"
        seed = SimpleNamespace(acceptance_criteria=(SimpleNamespace(semantic_ac_key=semantic_key),))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=1,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Implement feature",
                    status="failed",
                    completed=False,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="failed",
            approval_status="not_evaluated",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Implement feature",
                    results=(),
                    agent_reported_pass=False,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert summary.ac_results[0].semantic_ac_key == semantic_key

    def test_partial_spec_verification_coverage_does_not_approve_run(self) -> None:
        """Verifier reports must cover every expected AC before run approval."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
                TaskResult(
                    task_index=1,
                    task_content="Add docs",
                    status="completed",
                    completed=True,
                    source_ac_index=1,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern="config",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Create config",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            verified=True,
                            detail="Found file: config.py",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert [ac.passed for ac in summary.ac_results] == [True, False]
        assert summary.ac_results[1].ac_content == "Add docs"
        assert "No spec verification report" in summary.ac_results[1].evidence
        assert summary.approval_status == "rejected"
        assert summary.run_verdict == "FAIL"

    @pytest.mark.parametrize(
        ("report_polarity", "mechanical_pass"),
        [
            (True, False),
            (None, False),
            (False, True),
        ],
        ids=[
            "report-claims-pass-while-execution-says-fail",
            "report-omits-polarity-while-execution-says-fail",
            "report-says-fail-while-execution-says-pass",
        ],
    )
    def test_report_polarity_cannot_outrank_the_execution_record(
        self, report_polarity: bool | None, mechanical_pass: bool
    ) -> None:
        """The report's copy of the agent result is not the authoritative one.

        `agent_reported_pass` on a report is a copy the verifier was handed.
        A replayed, stale or externally built report carries whatever copy it
        was constructed with — a `True` contradicting the execution, or no
        field at all, which reads as `True`. The mechanical summary is the
        execution's own account, so both are consulted and either one saying
        "not a pass" settles it. Disagreement is not resolved in favour of the
        more permissive record.
        """
        ac_text = "MUST define a CameraProvider interface"
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=r"[\s\S]+",
        )
        polarity_kwargs = (
            {} if report_polarity is None else {"agent_reported_pass": report_polarity}
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text=ac_text,
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            outcome=VerificationOutcome.VERIFIED,
                            detail="Pattern found in main.py",
                        ),
                    ),
                    **polarity_kwargs,
                ),
            ),
            project_dir="/tmp/project",
        )
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content=ac_text,
                    passed=mechanical_pass,
                    score=1.0 if mechanical_pass else 0.0,
                    evidence="Agent execution record.",
                ),
            ),
            execution_completion_status="completed",
        )
        seed = SimpleNamespace(acceptance_criteria=(ac_text,))

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert summary.final_approved is False
        assert summary.ac_results[0].passed is False
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"

    @pytest.mark.parametrize(
        "rehydrate",
        ["direct", "round_trip", "legacy_booleans"],
    )
    def test_a_verified_report_over_an_agent_fail_cannot_mint_a_formal_pass(
        self, rehydrate: str
    ) -> None:
        """Source-scan evidence cannot reverse a reported FAIL at this boundary.

        `SpecVerifier.verify_all` already refuses to emit an all-VERIFIED
        report against an agent-reported FAIL, but this adapter is a public
        authority boundary that also accepts summaries built elsewhere:
        replayed rows, legacy payloads that carry only the old booleans, and
        compatibility objects from integrations. Enforcing the polarity only
        in the producer would let any of those encode the exact transition the
        producer forbids, so all three shapes are driven through here.
        """
        ac_text = "MUST define a CameraProvider interface"
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=r"[\s\S]+",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text=ac_text,
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            outcome=VerificationOutcome.VERIFIED,
                            detail="Pattern found in main.py",
                        ),
                    ),
                    agent_reported_pass=False,
                ),
            ),
            project_dir="/tmp/project",
        )
        if rehydrate != "direct":
            payload = json.loads(verification.model_dump_json())
            if rehydrate == "legacy_booleans":
                for report in payload["reports"]:
                    for result in report["results"]:
                        result.pop("outcome", None)
            verification = SpecVerificationSummary.model_validate(payload)

        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content=ac_text,
                    passed=False,
                    score=0.0,
                    evidence="Agent reported this criterion failed.",
                ),
            ),
            execution_completion_status="completed",
        )
        seed = SimpleNamespace(acceptance_criteria=(ac_text,))

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert summary.final_approved is False
        assert summary.ac_results[0].passed is False
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert "cannot overturn" in summary.ac_results[0].evidence

    def test_a_verified_report_still_confirms_an_agent_pass(self) -> None:
        """The confirmation direction is unaffected by the polarity gate."""
        ac_text = "MUST define a CameraProvider interface"
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=VerificationTier.T2_STRUCTURAL,
            pattern=r"class\s+CameraProvider",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text=ac_text,
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            outcome=VerificationOutcome.VERIFIED,
                            detail="Pattern found in camera.py",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            ac_results=(
                ACResult(
                    ac_index=0,
                    ac_content=ac_text,
                    passed=True,
                    score=1.0,
                    evidence="Agent reported this criterion passed.",
                ),
            ),
            execution_completion_status="completed",
        )
        seed = SimpleNamespace(acceptance_criteria=(ac_text,))

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert summary.final_approved is True
        assert summary.ac_results[0].rendered_verdict == "PASS"

    def test_seed_coverage_survives_partial_production_extraction(self, tmp_path: Any) -> None:
        """Parser, extractor, verifier, and formal adapter fail closed together."""
        seed = SimpleNamespace(
            seed_id="seed-partial-coverage",
            acceptance_criteria=("Create marker.txt", "Add docs.md"),
        )
        mechanical = _parse_legacy_execution_task_summary(
            "### Task 1: [COMPLETED] first\n### Task 1: [COMPLETED] duplicate",
            seed,
        )
        assert mechanical is not None

        extractor = AssertionExtractor(llm_adapter=AsyncMock(), model="test-model")
        assertions = extractor._parse_response(
            json.dumps(
                [
                    {
                        "ac_index": 0,
                        "tier": "t2_structural",
                        "pattern": "marker",
                        "expected_value": "marker.txt",
                        "file_hint": "marker.txt",
                    }
                ]
            ),
            seed.acceptance_criteria,
        )
        assert assertions is not None
        (tmp_path / "marker.txt").write_text("marker\n")
        verification = SpecVerifier(project_dir=str(tmp_path)).verify_all(
            assertions,
            agent_results=_agent_results_from_execution_summary(mechanical),
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert [result.ac_index for result in summary.ac_results] == [0, 1]
        assert summary.ac_results[0].rendered_verdict == "PASS"
        assert summary.ac_results[1].rendered_verdict == "NOT_EVALUATED"
        assert summary.execution_completion_status == "failed"
        assert summary.final_approved is False
        assert summary.run_verdict == "FAIL"
        assert "missing verifier report for AC 2" in (summary.failure_reason or "")

    def test_seed_indices_are_required_even_when_mechanical_records_omit_them(self) -> None:
        """A mechanically reported subset cannot narrow formal Seed authority."""
        seed = SimpleNamespace(acceptance_criteria=("Create config", "Add docs"))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern="config",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Create config",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            outcome=VerificationOutcome.VERIFIED,
                        ),
                    ),
                ),
            )
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert [result.rendered_verdict for result in summary.ac_results] == [
            "PASS",
            "NOT_EVALUATED",
        ]
        assert summary.final_approved is False
        assert summary.run_verdict == "FAIL"
        assert "missing verifier report for AC 2" in (summary.failure_reason or "")

    @pytest.mark.parametrize(
        "ordered_outcomes",
        [
            (VerificationOutcome.DISCREPANCY, VerificationOutcome.VERIFIED),
            (VerificationOutcome.VERIFIED, VerificationOutcome.DISCREPANCY),
        ],
    )
    def test_duplicate_report_order_cannot_mint_formal_authority(
        self,
        ordered_outcomes: tuple[VerificationOutcome, VerificationOutcome],
    ) -> None:
        """The adapter revalidates duplicate identity even for compatibility objects."""
        seed = SimpleNamespace(acceptance_criteria=("Create config",))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
        )
        reports = tuple(
            ACVerificationReport(
                ac_index=0,
                ac_text="Create config",
                results=(SpecVerificationResult(assertion=assertion, outcome=outcome),),
            )
            for outcome in ordered_outcomes
        )

        summary = _evaluation_summary_from_spec_verification(
            mechanical,
            SimpleNamespace(reports=reports),
            seed,
        )

        assert summary is not None
        assert summary.final_approved is False
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert "duplicate report ac_index=0" in (summary.failure_reason or "")

    @pytest.mark.parametrize("surface", ["report", "assertion"])
    @pytest.mark.parametrize("invalid_index", [True, False, "0", "1", 0.0, 1.0, 1.5, -1])
    def test_adapter_rejects_non_strict_raw_indices_after_model_bypass(
        self,
        surface: str,
        invalid_index: object,
    ) -> None:
        """Raw compatibility objects cannot exploit bool/int equality or coercion."""
        seed = SimpleNamespace(acceptance_criteria=("Create config",))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        raw_assertion = SimpleNamespace(
            ac_index=invalid_index if surface == "assertion" else 0,
            ac_text="Create config",
        )
        raw_report = SimpleNamespace(
            ac_index=invalid_index if surface == "report" else 0,
            ac_text="Create config",
            results=(SimpleNamespace(assertion=raw_assertion),),
        )

        summary = _evaluation_summary_from_spec_verification(
            mechanical,
            SimpleNamespace(reports=(raw_report,)),
            seed,
        )

        assert summary is not None
        assert summary.final_approved is False
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert "invalid" in (summary.failure_reason or "")

    @pytest.mark.parametrize(
        ("assertion_index", "assertion_text", "reason"),
        [
            (7, "Create config", "assertion ac_index=7"),
            (0, "Unrelated criterion", "assertion text"),
        ],
    )
    def test_adapter_rejects_unvalidated_nested_report_identity(
        self,
        assertion_index: int,
        assertion_text: str,
        reason: str,
    ) -> None:
        """Model validation bypasses cannot introduce misbound evidence authority."""
        seed = SimpleNamespace(acceptance_criteria=("Create config",))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=assertion_index,
            ac_text=assertion_text,
            tier=VerificationTier.T2_STRUCTURAL,
        )
        result = SpecVerificationResult(
            assertion=assertion,
            outcome=VerificationOutcome.VERIFIED,
        )
        unvalidated_report = ACVerificationReport.model_construct(
            ac_index=0,
            ac_text="Create config",
            results=(result,),
            agent_reported_pass=True,
        )

        summary = _evaluation_summary_from_spec_verification(
            mechanical,
            SimpleNamespace(reports=(unvalidated_report,)),
            seed,
        )

        assert summary is not None
        assert summary.final_approved is False
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert reason in (summary.failure_reason or "")

    def test_serialized_out_of_range_report_is_rejected_against_seed(self) -> None:
        seed = SimpleNamespace(acceptance_criteria=("Create config",))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=1,
            ac_text="Unexpected AC",
            tier=VerificationTier.T2_STRUCTURAL,
        )
        verification = SpecVerificationSummary.model_validate(
            {
                "reports": [
                    {
                        "ac_index": 1,
                        "ac_text": "Unexpected AC",
                        "results": [
                            {
                                "assertion": assertion.model_dump(mode="json"),
                                "outcome": "verified",
                            }
                        ],
                    }
                ]
            }
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert summary.final_approved is False
        assert "outside Seed AC coverage" in (summary.failure_reason or "")

    def test_serialized_seed_text_mismatch_is_rejected(self) -> None:
        seed = SimpleNamespace(
            acceptance_criteria=(
                AcceptanceCriterionSpec(
                    description="Create config",
                    semantic_ac_key="ac_0123456789abcdef",
                ),
            )
        )
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Unrelated criterion",
            tier=VerificationTier.T2_STRUCTURAL,
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Unrelated criterion",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            outcome=VerificationOutcome.VERIFIED,
                        ),
                    ),
                ),
            )
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert summary.final_approved is False
        assert summary.ac_results[0].semantic_ac_key == "ac_0123456789abcdef"
        assert "does not match the authoritative Seed AC" in (summary.failure_reason or "")

    def test_serialized_missing_report_remains_not_evaluated(self) -> None:
        seed = SimpleNamespace(acceptance_criteria=("Create config", "Add docs"))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=tuple(
                TaskResult(
                    task_index=index,
                    task_content=text,
                    status="completed",
                    completed=True,
                    source_ac_index=index,
                )
                for index, text in enumerate(seed.acceptance_criteria)
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
        )
        verification = SpecVerificationSummary.model_validate(
            {
                "reports": [
                    {
                        "ac_index": 0,
                        "ac_text": "Create config",
                        "results": [
                            {
                                "assertion": assertion.model_dump(mode="json"),
                                "outcome": "verified",
                            }
                        ],
                    }
                ]
            }
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert [result.rendered_verdict for result in summary.ac_results] == [
            "PASS",
            "NOT_EVALUATED",
        ]
        assert summary.final_approved is False

    def test_identity_consistent_legacy_payload_can_still_approve(self) -> None:
        seed = SimpleNamespace(acceptance_criteria=("Create config",))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
        )
        verification = SpecVerificationSummary.model_validate(
            {
                "reports": [
                    {
                        "ac_index": 0,
                        "ac_text": "Create config",
                        "results": [
                            {
                                "assertion": assertion.model_dump(mode="json"),
                                "verified": True,
                            }
                        ],
                    }
                ]
            }
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert summary.final_approved is True
        assert summary.run_verdict == "PASS"

    def test_serialized_mixed_outcomes_cannot_mint_formal_pass(self) -> None:
        seed = SimpleNamespace(acceptance_criteria=("Create config",))
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
        )
        serialized_assertion = assertion.model_dump(mode="json")
        verification = SpecVerificationSummary.model_validate(
            {
                "reports": [
                    {
                        "ac_index": 0,
                        "ac_text": "Create config",
                        "results": [
                            {"assertion": serialized_assertion, "outcome": "verified"},
                            {"assertion": serialized_assertion, "outcome": "discrepancy"},
                        ],
                    }
                ],
                "confirmed_discrepancy_count": 0,
            }
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification, seed)

        assert summary is not None
        assert verification.confirmed_discrepancy_count == 1
        assert summary.final_approved is False
        assert summary.ac_results[0].rendered_verdict == "FAIL"
        assert summary.run_verdict == "FAIL"

    def test_unavailable_spec_verification_result_does_not_approve_run(self) -> None:
        """Unavailable verifier evidence is a failed formal AC, not approval."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Set MAX_RETRIES to 5",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Set MAX_RETRIES to 5",
            tier=VerificationTier.T1_CONSTANT,
            pattern=r"MAX_RETRIES\s*=\s*",
            expected_value="5",
            file_hint="*.rs",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Set MAX_RETRIES to 5",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            outcome=VerificationOutcome.UNVERIFIABLE,
                            detail="No files matched hint: *.rs",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.final_approved is False
        assert summary.ac_results[0].passed is False
        assert summary.ac_results[0].ac_verdict_state == "not_evaluated"
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert summary.ac_results[0].evidence == "No files matched hint: *.rs"
        assert summary.approval_status == "rejected"
        assert summary.run_verdict == "FAIL"

    def test_contradictory_legacy_verification_flags_cannot_mint_formal_pass(self) -> None:
        """A stale legacy PASS bit cannot override an explicit discrepancy bit."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Set MAX_RETRIES to 5",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Set MAX_RETRIES to 5",
            tier=VerificationTier.T1_CONSTANT,
        )
        contradictory = SpecVerificationResult.model_validate(
            {
                "assertion": assertion.model_dump(mode="json"),
                "verified": True,
                "discrepancy": True,
                "detail": "Observed MAX_RETRIES=3",
            }
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Set MAX_RETRIES to 5",
                    results=(contradictory,),
                    agent_reported_pass=True,
                ),
            )
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert contradictory.outcome is VerificationOutcome.DISCREPANCY
        assert summary.ac_results[0].rendered_verdict == "FAIL"
        assert summary.final_approved is False
        assert summary.run_verdict == "FAIL"

    def test_skipped_spec_verification_result_does_not_approve_run(self) -> None:
        """A visible T3/T4 skip remains NOT_EVALUATED at the formal gate."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Interaction feels natural",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Interaction feels natural",
            tier=VerificationTier.T4_UNVERIFIABLE,
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Interaction feels natural",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            outcome=VerificationOutcome.SKIPPED,
                            detail="Subjective assertion is not independently verifiable",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.final_approved is False
        assert summary.ac_results[0].passed is False
        assert summary.ac_results[0].ac_verdict_state == "not_evaluated"
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert "source verification skipped for AC 1" in (summary.failure_reason or "")

    def test_spec_verification_promotes_checked_reports_to_formal_ac_results(self) -> None:
        """Verifier-checked reports become formal AC verdicts without synthetic drift."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern="config",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Create config",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            verified=True,
                            detail="Found file: config.py",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert len(summary.task_results) == 1
        assert len(summary.ac_results) == 1
        assert summary.ac_results[0].passed is True
        assert summary.ac_results[0].verification_method == "spec_verifier"
        assert summary.approval_status == "approved"
        assert summary.drift_score is None
        assert summary.run_verdict == "PASS"

    def test_spec_verification_plain_failure_reason_has_no_dangling_bracket(self) -> None:
        """Ordinary verifier failures should render a clean failure reason."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern="config",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Create config",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            verified=False,
                            detail="Structure 'config' not found",
                        ),
                    ),
                    agent_reported_pass=False,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.failure_reason == "1/1 ACs failed (AC 1)"

    def test_spec_verification_does_not_approve_failed_execution(self) -> None:
        """Passing verifier results must not approve a run whose execution failed.

        The AC itself is now `NOT_EVALUATED` rather than a passing result
        carried inside a rejected run. The worker reported this task
        incomplete, so the execution record says the agent did not claim this
        AC passed, and the report's own `agent_reported_pass=True` is the
        contradicting copy rather than the authority. Source-scan evidence
        cannot resolve that disagreement in favour of a pass — this is the
        shape #1835 describes, a grep result standing in for work a worker
        reported it never finished.
        """
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="failed",
                    completed=False,
                    source_ac_index=0,
                    evidence="Worker failed before completing the task",
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="failed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern="config",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Create config",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            verified=True,
                            detail="Found file: config.py",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.ac_results[0].passed is False
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert summary.execution_completion_status == "failed"
        assert summary.approval_status == "rejected"
        assert summary.final_approved is False
        assert summary.run_verdict == "FAIL"
        assert summary.failure_reason == (
            "unverifiable assertion evidence for AC 1 [execution_completion_status=failed]"
        )

    def test_spec_verification_discrepancy_becomes_formal_ac_failure(self) -> None:
        """False-positive legacy PASS claims remain catchable by spec verification."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern="config",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Create config",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            verified=False,
                            discrepancy=True,
                            detail="Structure 'config' not found",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.task_results[0].completed is True
        assert summary.ac_results[0].passed is False
        assert summary.ac_results[0].ac_verdict_state == "overridden"
        assert summary.ac_results[0].provisional_verdict == "pass"
        assert summary.ac_results[0].override_source == "spec_verifier"
        assert summary.ac_results[0].override_reason == "Structure 'config' not found"
        assert summary.approval_status == "rejected"
        assert summary.failure_reason == "1/1 ACs failed (AC 1) [1 spec verification override(s)]"
        assert summary.drift_score is None
        assert summary.run_verdict == "FAIL"

    @pytest.mark.parametrize(
        "tier",
        [VerificationTier.T1_CONSTANT, VerificationTier.T2_STRUCTURAL],
        ids=["T1", "T2"],
    )
    @pytest.mark.parametrize(
        ("content", "ac_text", "pattern"),
        [
            ("  \t\n", "marker.txt MUST be empty", r"\A\Z"),
            ("header\n\nbody\n", "marker.txt MUST be empty", r"(?m)^$"),
            ("header\n\nbody\n", "marker.txt MUST be empty", r"^$"),
            ("content", "marker.txt MUST be empty", r"\A.*\Z"),
            ("content", "marker.txt MUST be empty", r"\A\w*\Z"),
            ("content", "marker.txt MUST contain a header", r".*"),
            ("content", "marker.txt MUST contain a header", r"x?"),
        ],
        ids=[
            "whitespace-is-not-empty",
            "multiline-blank-line",
            "line-anchors",
            "dot-star-pinned",
            "word-star-pinned",
            "matches-anywhere",
            "optional-atom",
        ],
    )
    def test_a_pattern_that_is_not_evidence_cannot_be_approved_through_the_adapter(
        self, tmp_path: Any, tier: VerificationTier, content: str, ac_text: str, pattern: str
    ) -> None:
        """A pattern a file with content can satisfy must not reach formal approval.

        `marker.txt` holds content in every case here, so every one of these is a
        criterion the project has not met, and each pattern would report a match
        on it if the verifier admitted the pattern. `.*` and `x?` match anywhere
        in anything — the original blocker. The rest are the shapes that a guard
        with an exit for `\\A\\Z` can be talked into admitting: `\\A.*\\Z` and
        `\\A\\w*\\Z` are pinned to both ends and still swallow a whole file, and
        `(?m)^$` and `^$` are pinned to the ends of a *line*, which any blank line
        inside a full file provides.

        The first case is the opposite mistake and belongs at the same boundary:
        a whitespace-only file is blank and is not empty, `\\A\\Z` says so, and the
        verdict must survive the trip through the adapter rather than being
        rounded up to a pass.

        Each of these published `final_approved=True`, `score=1.0`,
        `final_verdict="pass"` at the adapter for a criterion the project
        violates or has not met.
        """
        (tmp_path / "marker.txt").write_text(content)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )
        verification = SpecVerifier(project_dir=str(tmp_path)).verify_all(
            (assertion,), agent_results={0: True}
        )
        mechanical = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content=ac_text,
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="approved",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.ac_results[0].passed is False, f"{ac_text!r} must not be approved"
        assert summary.ac_results[0].final_verdict != "pass"
        assert summary.final_approved is False
        assert summary.score == 0.0
        assert summary.run_verdict == "FAIL"

    @pytest.mark.parametrize(
        "tier",
        [VerificationTier.T1_CONSTANT, VerificationTier.T2_STRUCTURAL],
        ids=["T1", "T2"],
    )
    @pytest.mark.parametrize(
        ("content", "ac_text", "pattern"),
        [
            ("", "Please ensure marker.txt is empty", r"\A\Z"),
            ("", "Kindly make sure marker.txt is empty", r"\A\Z"),
            ("", "It is required that marker.txt be empty", r"\A\Z"),
            ("", "Check that marker.txt is empty", r"\A\Z"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(a)?\1"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(?P<x>a)?(?P=x)"),
            ("", "Marker.txt must be empty", r"\A\Z"),
            ("", "MARKER.TXT must be empty", r"\A\Z"),
            ("", "Please ensure Marker.txt is empty", r"\A\Z"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(a)?(?(1)|a)"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(a?)(?(1)a|)"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(?=())(?(1)a|)"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(?<=())(?(1)a|)"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(?!()x)(?(1)|a)"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(?!x())(?(1)|a)"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(?!x())\1|aa"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(?!)|aa"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"aa(?!)|aa"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"()(?(1)Impossible|)|aa"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"aa|()(?(1)Impossible|)"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"(?:()|x)(?(1)a|)"),
            ("aa\n", "marker.txt MUST contain a doubled letter", r"()(?(1)()|)(?(2)a|)"),
        ],
        ids=[
            "politeness-frame",
            "politeness-and-periphrasis",
            "impersonal-obligation",
            "checking-verb",
            "numbered-backreference",
            "named-backreference",
            "capitalized-mention",
            "shouted-mention",
            "capitalized-in-frame",
            "conditional-on-an-absent-group",
            "conditional-on-a-present-group",
            "conditional-on-a-capture-inside-a-lookahead",
            "conditional-on-a-capture-inside-a-lookbehind",
            "conditional-on-a-capture-inside-a-failed-negative-lookahead",
            "conditional-on-a-capture-after-a-consuming-atom",
            "backreference-to-a-capture-that-did-not-take-part",
            "branch-that-can-never-be-taken",
            "branch-that-can-never-be-taken-after-a-literal",
            "conditional-in-the-first-branch",
            "conditional-in-the-second-branch",
            "conditional-after-a-branch-only-one-of-which-can-be-empty",
            "capture-in-the-arm-the-conditional-selects",
        ],
    )
    def test_a_satisfied_criterion_is_not_converted_into_a_formal_failure(
        self, tmp_path: Any, tier: VerificationTier, content: str, ac_text: str, pattern: str
    ) -> None:
        """Over-rejection is the same blocker seen from the other side.

        Each case here is a project that *meets* its criterion, and each was
        published as `final_approved=False` / `score=0.0` / `run_verdict="FAIL"`
        — an authoritative failure manufactured by the guard rather than by the
        code under test.

        The first four were refused because a word that carries no claim at all
        was absent from the words known to carry none: `Please ensure marker.txt
        is empty`, with an empty `marker.txt`, failed on `please`. The last two
        were refused because every backreference was treated as zero-width, so
        `(a)?\\1` — which cannot match nothing and does match this file — was
        called unusable. Three differ from the hint only in the case of the
        filename, which the mention check ignored and the masking that follows it
        did not. The last two are conditionals whose arms disagree, refused
        because the reading required agreement instead of asking whether the
        group could have taken part. The last two are the same failure made by
        the interpreter rather than by the reading: from 3.13 the parser folds
        `(?!)` into a single opcode this walk did not know, so a pattern with an
        unreachable branch was evidence on 3.12 and an authoritative failure on
        3.13 and 3.14. The final two were refused for having an alternative at
        all: every branch was read as a path that may have been skipped, so a
        capture and the conditional reading it, written side by side in one
        branch, could no longer see each other. Both directions are driven
        through the real verifier
        and the real adapter, because the failure only becomes authoritative at
        this boundary.
        """
        (tmp_path / "marker.txt").write_text(content)
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )
        verification = SpecVerifier(project_dir=str(tmp_path)).verify_all(
            (assertion,), agent_results={0: True}
        )
        mechanical = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content=ac_text,
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="approved",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.ac_results[0].passed is True, f"{ac_text!r} must not be failed"
        assert summary.ac_results[0].final_verdict == "pass"
        assert summary.final_approved is True
        assert summary.score == 1.0
        assert summary.run_verdict != "FAIL"

    @pytest.mark.parametrize(
        "tier",
        [VerificationTier.T1_CONSTANT, VerificationTier.T2_STRUCTURAL],
        ids=["T1", "T2"],
    )
    @pytest.mark.parametrize(
        "pattern",
        [
            r"(?!(a)?(?(1)|c))",
            r"(?!(a)?(?(1)|x)b)",
            "(?!" + "(" * 45 + "x" + ")" * 45 + ")",
            r"(?!\b)",
            r"(?<!\b)",
            r"(?!()(?(1)a|))",
            r"(?!(?!(a?)(?(1)|a)))",
        ],
        ids=[
            "negated-conditional",
            "negated-conditional-with-tail",
            "negated-past-depth-limit",
            "negated-boundary",
            "negated-lookbehind-boundary",
            "negated-conditional-on-a-capture-beside-it",
            "twice-negated-conditional-on-a-nullable-capture",
        ],
    )
    def test_a_negated_guess_cannot_be_approved_through_the_adapter(
        self, tmp_path: Any, tier: VerificationTier, pattern: str
    ) -> None:
        """A pattern the analyzer did not understand must not reach formal approval.

        Each of these matches every file, so it verifies whatever it is pointed
        at — the original defect. Each got there through a negation: the reading
        sends its doubt to the side that is safe when the answer stands alone,
        `(?!…)` flips which side that is, and the guess came back out as
        confidence that the pattern discriminates. Published here as
        `final_approved=True`, `score=1.0`, `run_verdict="PASS"` for an AC the
        file does not satisfy.

        The last two arrive by a narrower road: the negation was also declaring
        the captures written inside its own body absent *while that body was
        still being read*, so a conditional standing beside such a capture took
        the arm the runtime never takes. The body is an attempt like any other,
        and what it captured is real to everything inside it.
        """
        (tmp_path / "marker.txt").write_text("hello\n")
        ac_text = "marker.txt MUST declare a CameraProvider"
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )
        verification = SpecVerifier(project_dir=str(tmp_path)).verify_all(
            (assertion,), agent_results={0: True}
        )
        mechanical = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content=ac_text,
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="approved",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.ac_results[0].passed is False, f"{pattern!r} must not be approved"
        assert summary.ac_results[0].final_verdict != "pass"
        assert summary.final_approved is False
        assert summary.score == 0.0
        assert summary.run_verdict == "FAIL"

    @pytest.mark.parametrize(
        "tier",
        [VerificationTier.T1_CONSTANT, VerificationTier.T2_STRUCTURAL],
        ids=["T1", "T2"],
    )
    @pytest.mark.parametrize(
        "pattern",
        [r"\B", r"(?!\B)", r"(?<!\B)", r"(?=\B)"],
        ids=["non-boundary", "negated", "negated-lookbehind", "asserted"],
    )
    def test_a_non_boundary_reaches_the_formal_verdict_this_interpreter_justifies(
        self, tmp_path: Any, tier: VerificationTier, pattern: str
    ) -> None:
        """Whether `\\B` holds on an empty subject changed in CPython 3.14.

        Written into the anchor table as a constant, it was right on the
        interpreter it was written on and wrong on 3.12, where `(?!\\B)` matches
        every file: an unrelated `marker.txt` was published here as
        `final_approved=True`, `score=1.0`, `run_verdict="PASS"` for a
        `CameraProvider` that does not exist. The table now asks the engine, so
        the assertion is that the formal verdict follows what this interpreter
        actually does — a pattern that matches nothing at all is refused and
        fails, one that discriminates is approved — and it pins every version CI
        runs without naming one.
        """
        matches_nothing = re.search(pattern, "") is not None
        (tmp_path / "marker.txt").write_text("hello\n")
        ac_text = "marker.txt MUST declare a CameraProvider"
        assertion = SpecAssertion(
            ac_index=0,
            ac_text=ac_text,
            tier=tier,
            pattern=pattern,
            file_hint="marker.txt",
        )
        verification = SpecVerifier(project_dir=str(tmp_path)).verify_all(
            (assertion,), agent_results={0: True}
        )
        mechanical = EvaluationSummary(
            final_approved=True,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content=ac_text,
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="approved",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.ac_results[0].passed is not matches_nothing, (
            f"{pattern!r} matches the empty string here: {matches_nothing}"
        )
        assert summary.final_approved is not matches_nothing
        assert summary.score == (0.0 if matches_nothing else 1.0)
        assert (summary.run_verdict == "FAIL") is matches_nothing

    def test_spec_verification_rejects_partial_ac_coverage(self) -> None:
        """A subset of verifier reports must not approve unverified ACs."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Create config",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
                TaskResult(
                    task_index=1,
                    task_content="Add docs",
                    status="completed",
                    completed=True,
                    source_ac_index=1,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        assertion = SpecAssertion(
            ac_index=0,
            ac_text="Create config",
            tier=VerificationTier.T2_STRUCTURAL,
            pattern="config",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Create config",
                    results=(
                        SpecVerificationResult(
                            assertion=assertion,
                            verified=True,
                            detail="Found file: config.py",
                        ),
                    ),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert len(summary.ac_results) == 2
        assert summary.ac_results[0].passed is True
        assert summary.ac_results[1].passed is False
        assert summary.ac_results[1].ac_verdict_state == "not_evaluated"
        assert summary.ac_results[1].rendered_verdict == "NOT_EVALUATED"
        assert summary.approval_status == "rejected"
        assert "missing verifier report for AC 2" in (summary.failure_reason or "")
        assert summary.run_verdict == "FAIL"

    def test_spec_verification_rejects_unverifiable_completed_task(self) -> None:
        """A completed task is not an AC approval when no assertions ran."""
        mechanical = EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            task_results=(
                TaskResult(
                    task_index=0,
                    task_content="Improve UX",
                    status="completed",
                    completed=True,
                    source_ac_index=0,
                    execution_method="legacy_parallel_report",
                ),
            ),
            execution_completion_status="completed",
            approval_status="not_evaluated",
        )
        verification = SpecVerificationSummary.from_reports(
            (
                ACVerificationReport(
                    ac_index=0,
                    ac_text="Improve UX",
                    results=(),
                    agent_reported_pass=True,
                ),
            ),
            project_dir="/tmp/project",
        )

        summary = _evaluation_summary_from_spec_verification(mechanical, verification)

        assert summary is not None
        assert summary.ac_results[0].passed is False
        assert summary.ac_results[0].ac_verdict_state == "not_evaluated"
        assert summary.ac_results[0].rendered_verdict == "NOT_EVALUATED"
        assert summary.approval_status == "rejected"
        assert "unverifiable assertion evidence for AC 1" in (summary.failure_reason or "")
        assert summary.run_verdict == "FAIL"

    def test_extract_feedback_metadata_from_artifact_parses_structured_warning(self) -> None:
        """Execution artifacts should expose structured evaluation feedback metadata."""
        artifact = """
Parallel Execution Verification Report
Success: 1/1

## Feedback Metadata
Feedback Metadata JSON: {"feedback_metadata": [{"code": "decomposition_depth_warning", "details": {"affected_ac_paths": ["1.1.1"], "affected_count": 1, "max_depth": 3}, "message": "Recursive decomposition reached the soft depth safety net; affected leaves were forced to atomic execution.", "severity": "warning", "source": "parallel_executor"}]}

## Task Results
### Task 1: [COMPLETED] Ship feature
""".strip()

        feedback = _extract_feedback_metadata_from_artifact(artifact)

        assert len(feedback) == 1
        assert feedback[0].code == "decomposition_depth_warning"
        assert feedback[0].severity == "warning"
        assert feedback[0].source == "parallel_executor"
        assert feedback[0].details["max_depth"] == 3
        assert feedback[0].details["affected_ac_paths"] == ["1.1.1"]


class TestMCPServerAdapterTools:
    """Test MCPServerAdapter tool operations."""

    def test_register_tool(self) -> None:
        """register_tool adds a tool handler."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler()

        adapter.register_tool(handler)

        assert adapter.info.capabilities.tools is True

    async def test_list_tools(self) -> None:
        """list_tools returns registered tools."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("my_tool")

        adapter.register_tool(handler)
        tools = await adapter.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "my_tool"

    async def test_call_tool_success(self) -> None:
        """call_tool invokes handler and returns result."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("my_tool")
        adapter.register_tool(handler)

        result = await adapter.call_tool("my_tool", {"input": "test"})

        assert result.is_ok
        assert result.value.text_content == "Success"
        handler.handle_mock.assert_called_once_with({"input": "test"})

    async def test_concurrent_calls_share_startup_before_handler_dispatch(self) -> None:
        """Owned startup runs once and gates every concurrent request."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("my_tool")
        adapter.register_tool(handler)
        initialize_started = asyncio.Event()
        release_initialize = asyncio.Event()

        class _StartupResource:
            initialize_calls = 0
            close_calls = 0

            async def initialize(self) -> None:
                self.initialize_calls += 1
                initialize_started.set()
                await release_initialize.wait()

            async def close(self) -> None:
                self.close_calls += 1

        resource = _StartupResource()
        adapter.register_owned_resource(resource, initialize_on_startup=True)

        calls = [
            asyncio.create_task(adapter.call_tool("my_tool", {"input": str(index)}))
            for index in range(2)
        ]
        await asyncio.wait_for(initialize_started.wait(), timeout=0.5)
        assert handler.handle_mock.await_count == 0
        release_initialize.set()

        results = await asyncio.gather(*calls)
        assert all(result.is_ok for result in results)
        assert resource.initialize_calls == 1
        assert handler.handle_mock.await_count == 2

        await asyncio.gather(adapter.shutdown(), adapter.shutdown())
        assert resource.close_calls == 1

    async def test_startup_failure_prevents_handler_work_and_remains_owned(self) -> None:
        """A failed initializer blocks dispatch but shutdown still releases resources."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("my_tool")
        adapter.register_tool(handler)
        close_order: list[str] = []

        class _Resource:
            def __init__(self, name: str, *, fail: bool = False) -> None:
                self.name = name
                self.fail = fail
                self.initialize_calls = 0

            async def initialize(self) -> None:
                self.initialize_calls += 1
                if self.fail:
                    raise RuntimeError("startup exploded")

            async def close(self) -> None:
                close_order.append(self.name)

        first = _Resource("first")
        failing = _Resource("failing", fail=True)
        adapter.register_owned_resource(first, initialize_on_startup=True)
        adapter.register_owned_resource(failing, initialize_on_startup=True)

        first_result = await adapter.call_tool("my_tool", {"input": "one"})
        second_result = await adapter.call_tool("my_tool", {"input": "two"})

        assert first_result.is_err and second_result.is_err
        assert "startup exploded" in str(first_result.error)
        assert "startup exploded" in str(second_result.error)
        assert first.initialize_calls == 1
        assert failing.initialize_calls == 1
        handler.handle_mock.assert_not_awaited()

        await adapter.shutdown()
        assert close_order == ["first", "failing"]

    async def test_call_tool_logs_lifecycle_without_argument_values(self) -> None:
        """call_tool emits boundary logs without leaking argument payloads."""
        adapter = MCPServerAdapter(name="test-server")
        handler = MockToolHandler("my_tool")
        adapter.register_tool(handler)

        with capture_logs() as logs:
            result = await adapter.call_tool("my_tool", {"input": "secret-value"})

        assert result.is_ok
        start = next(event for event in logs if event["event"] == "mcp.server.call_tool.start")
        returned = next(event for event in logs if event["event"] == "mcp.server.call_tool.return")
        assert start["tool"] == "my_tool"
        assert start["server_name"] == "test-server"
        assert start["argument_keys"] == ["input"]
        assert "secret-value" not in str(start)
        assert returned["tool"] == "my_tool"
        assert returned["ok"] is True
        assert isinstance(returned["duration_ms"], int)
        assert returned["duration_ms"] >= 0

    async def test_call_tool_job_wait_returns_capped_pollable_payload_through_adapter(
        self, tmp_path
    ) -> None:
        """Adapter routing exposes the job_wait timeout cap as observable MCP metadata."""
        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        await store.initialize()
        try:
            snapshot = JobSnapshot(
                job_id="job_wait_adapter_timeout_cap",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Running execute_seed",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=7,
                links=JobLinks(),
            )

            class StaticJobManager:
                async def wait_for_change(
                    self,
                    job_id: str,
                    *,
                    cursor: int,
                    timeout_seconds: int,
                ) -> tuple[JobSnapshot, bool]:
                    assert job_id == snapshot.job_id
                    assert cursor == 6
                    assert timeout_seconds == 5
                    return snapshot, False

            adapter = MCPServerAdapter(name="test-server")
            adapter.register_tool(JobWaitHandler(event_store=store, job_manager=StaticJobManager()))

            result = await adapter.call_tool(
                "ouroboros_job_wait",
                {
                    "job_id": snapshot.job_id,
                    "cursor": 6,
                    "timeout_seconds": 120,
                },
            )

            assert result.is_ok
            assert result.value.meta["job_id"] == snapshot.job_id
            assert result.value.meta["timeout_seconds"] == 5
            assert result.value.meta["timeout_seconds_requested"] == 120
            assert result.value.meta["timeout_seconds_capped"] is True
            assert result.value.meta["changed"] is False
            assert result.value.is_error is False
        finally:
            await store.close()

    async def test_call_tool_job_wait_delayed_zero_snapshot_then_result_recovers_terminal(
        self, tmp_path
    ) -> None:
        """A delayed zero-time snapshot still composes with a later terminal result."""
        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
        await store.initialize()
        try:
            running = JobSnapshot(
                job_id="job_wait_then_result",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Running execute_seed",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=4,
                links=JobLinks(),
            )
            terminal = JobSnapshot(
                job_id=running.job_id,
                job_type=running.job_type,
                status=JobStatus.COMPLETED,
                message="Job complete",
                created_at=running.created_at,
                updated_at=running.updated_at,
                cursor=5,
                links=running.links,
                result_text="terminal result",
            )

            class RecoveringJobManager:
                terminal_snapshot: JobSnapshot | None = None

                def get_cached_snapshot(self, job_id: str) -> JobSnapshot | None:
                    assert job_id == running.job_id
                    return self.terminal_snapshot

                async def wait_for_change(
                    self,
                    job_id: str,
                    *,
                    cursor: int,
                    timeout_seconds: int,
                ) -> tuple[JobSnapshot, bool]:
                    assert job_id == running.job_id
                    assert cursor == 4
                    assert timeout_seconds == 0
                    await asyncio.sleep(1.05)
                    return running, False

            manager = RecoveringJobManager()
            adapter = MCPServerAdapter(name="test-server")
            adapter.register_tool(JobWaitHandler(event_store=store, job_manager=manager))
            adapter.register_tool(JobResultHandler(event_store=store, job_manager=manager))

            wait_result = await adapter.call_tool(
                "ouroboros_job_wait",
                {"job_id": running.job_id, "cursor": 4, "timeout_seconds": 0},
            )
            manager.terminal_snapshot = terminal
            result = await adapter.call_tool("ouroboros_job_result", {"job_id": running.job_id})

            assert wait_result.is_ok
            assert "wait_timed_out" not in wait_result.value.meta
            assert wait_result.value.meta["lifecycle_status"] == "running"
            assert wait_result.value.meta["cursor"] == 4
            assert wait_result.value.meta["result_available"] is False
            assert result.is_ok
            assert result.value.text_content == "terminal result"
            assert result.value.meta["lifecycle_status"] == "completed"
            assert result.value.meta["result_available"] is True
        finally:
            await store.close()

    def test_fastmcp_tool_result_preserves_meta(self) -> None:
        """FastMCP boundary conversion must not drop MCPToolResult.meta."""
        result = MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="Success"),),
            meta={"internal_reasoning": ["phase: start"]},
            structured_content={"job_id": "job_123", "session_id": "orch_123"},
        )

        converted = _to_fastmcp_tool_result(result)

        assert converted.content[0].text == "Success"
        assert converted.meta == {"internal_reasoning": ["phase: start"]}
        assert converted.structured_content == {
            "job_id": "job_123",
            "session_id": "orch_123",
        }

    async def test_call_tool_scopes_io_journal_recorder_from_runtime_context(self) -> None:
        """MCP tool calls provide per-call journal identity to shared adapters."""

        class _RecorderProbeHandler(MockToolHandler):
            def __init__(self) -> None:
                super().__init__("probe_tool")
                self.recorder = None

            async def handle(
                self, arguments: dict[str, Any]
            ) -> Result[MCPToolResult, MCPServerError]:
                self.recorder = get_current_io_journal_recorder()
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                    )
                )

        adapter = MCPServerAdapter()
        adapter.set_runtime_context(
            AgentRuntimeContext(
                event_store=_FakeEventStore(),
                runtime_backend="codex",
                llm_backend="litellm",
            )
        )
        handler = _RecorderProbeHandler()
        adapter.register_tool(handler)

        result = await adapter.call_tool(
            "probe_tool",
            {
                "execution_id": "exec_123",
                "session_id": "sess_123",
                "phase": "reflect",
                "generation_number": 2,
            },
        )

        assert result.is_ok
        assert handler.recorder is not None
        assert handler.recorder.target_type == "execution"
        assert handler.recorder.target_id == "exec_123"
        assert handler.recorder.session_id == "sess_123"
        assert handler.recorder.execution_id == "exec_123"
        assert handler.recorder.phase == "reflect"
        assert handler.recorder.generation_number == 2
        assert get_current_io_journal_recorder() is None

    def test_io_recorder_for_tool_call_uses_lineage_identity(self) -> None:
        adapter = MCPServerAdapter()
        adapter.set_runtime_context(
            AgentRuntimeContext(
                event_store=_FakeEventStore(),
                runtime_backend="codex",
                llm_backend="litellm",
            )
        )

        recorder = adapter._io_recorder_for_tool_call(
            "ouroboros_evolve_step",
            {
                "lineage_id": "lin_123",
                "session_id": "sess_123",
                "generation": 3,
                "current_phase": "reflect",
            },
        )

        assert recorder is not None
        assert recorder.target_type == "lineage"
        assert recorder.target_id == "lin_123"
        assert recorder.lineage_id == "lin_123"
        assert recorder.session_id == "sess_123"
        assert recorder.generation_number == 3
        assert recorder.phase == "reflect"

    def test_io_recorder_for_tool_call_uses_session_identity(self) -> None:
        adapter = MCPServerAdapter()
        adapter.set_runtime_context(
            AgentRuntimeContext(
                event_store=_FakeEventStore(),
                runtime_backend="codex",
                llm_backend="litellm",
            )
        )

        recorder = adapter._io_recorder_for_tool_call(
            "ouroboros_qa",
            {"qa_session_id": "qa_123"},
        )

        assert recorder is not None
        assert recorder.target_type == "session"
        assert recorder.target_id == "qa_123"
        assert recorder.session_id == "qa_123"
        assert recorder.execution_id is None
        assert recorder.lineage_id is None

    def test_io_recorder_for_tool_call_uses_mcp_tool_fallback_identity(self) -> None:
        adapter = MCPServerAdapter()
        adapter.set_runtime_context(
            AgentRuntimeContext(
                event_store=_FakeEventStore(),
                runtime_backend="codex",
                llm_backend="litellm",
            )
        )

        recorder = adapter._io_recorder_for_tool_call("plain_tool", {})

        assert recorder is not None
        assert recorder.target_type == "mcp_tool"
        assert recorder.target_id.startswith("plain_tool:")
        assert recorder.session_id is None
        assert recorder.execution_id is None
        assert recorder.lineage_id is None

    async def test_call_tool_not_found(self) -> None:
        """call_tool returns error for unknown tool."""
        adapter = MCPServerAdapter()

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_unknown_tool", {})

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)
        capture.assert_called_once()
        assert capture.call_args.args[0] == "ouroboros_unknown_tool"
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "MCPResourceNotFoundError"

    async def test_call_tool_hostile_unregistered_name_is_sanitized(self) -> None:
        """A caller-controlled unregistered name never reaches telemetry verbatim."""
        adapter = MCPServerAdapter()
        hostile_name = "ouroboros_/home/alice/private-project"

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool(hostile_name, {})

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)
        capture.assert_called_once()
        assert capture.call_args.args[0] == "ouroboros_unknown_tool"
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "MCPResourceNotFoundError"
        for value in (*capture.call_args.args, *capture.call_args.kwargs.values()):
            assert "/home/alice" not in str(value)
            assert "private-project" not in str(value)

    async def test_call_tool_registered_name_still_captured_verbatim(self) -> None:
        """The sanitization gate does not clip a genuinely registered tool's name."""
        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler("ouroboros_registered_probe"))

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_registered_probe", {"input": "safe"})

        assert result.is_ok
        capture.assert_called_once()
        assert capture.call_args.args[0] == "ouroboros_registered_probe"
        assert capture.call_args.kwargs["ok"] is True

    async def test_call_tool_registered_extension_success_is_not_collected(self) -> None:
        """Successful non-product extension calls do not consume telemetry volume.

        The real telemetry sink is patched so this proves the canonical folding
        boundary drops successful non-lifecycle extension calls entirely.
        """
        adapter = MCPServerAdapter()
        extension_name = "custom_tool"
        adapter.register_tool(MockToolHandler(extension_name))

        with patch("ouroboros.telemetry.capture") as capture:
            result = await adapter.call_tool(extension_name, {"input": "safe"})

        assert result.is_ok
        capture.assert_called_once_with("service_active", {"service": "mcp"})

    async def test_call_tool_non_prefixed_extension_failure_is_folded(self) -> None:
        adapter = MCPServerAdapter()
        handler = MockToolHandler("custom_tool")
        handler.handle_mock.return_value = Result.err(MCPServerError("failed"))
        adapter.register_tool(handler)

        with patch("ouroboros.telemetry.capture") as capture:
            result = await adapter.call_tool("custom_tool", {"input": "safe"})

        assert result.is_err
        assert capture.call_args_list[0].args == ("service_active", {"service": "mcp"})
        assert capture.call_args_list[1].args == (
            "command_run",
            {
                "command": "extension_tool",
                "service": "mcp",
                "status": "failed",
                "error_type": "MCPServerError",
            },
        )

    async def test_call_tool_non_prefixed_extension_logical_block_is_folded(self) -> None:
        adapter = MCPServerAdapter()
        handler = MockToolHandler("custom_tool")
        handler.handle_mock.return_value = Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="input_required"),),
                is_error=True,
            )
        )
        adapter.register_tool(handler)

        with patch("ouroboros.telemetry.capture") as capture:
            result = await adapter.call_tool("custom_tool", {"input": "safe"})

        assert result.is_ok
        assert capture.call_args_list[0].args == ("service_active", {"service": "mcp"})
        assert capture.call_args_list[1].args == (
            "command_run",
            {
                "command": "extension_tool",
                "service": "mcp",
                "status": "blocked",
                "error_type": None,
            },
        )

    async def test_call_tool_logical_error_response_counts_as_not_ok(self) -> None:
        """A built-in handler returning Result.ok(MCPToolResult(is_error=True))
        (e.g. ouroboros_ralph without lineage_id, status=input_required) is a
        logical failure, not a success -- outer Result.is_ok alone is not
        enough to call it ok=True.
        """
        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_logical_error_probe")
        handler.handle_mock.return_value = Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="input_required"),),
                is_error=True,
                meta={"status": "input_required"},
            )
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_logical_error_probe", {"input": "safe"})

        assert result.is_ok
        assert result.value.is_error is True
        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        # No exception exists for a logical error -- absence is honest here,
        # not an invented "unknown" value.
        assert capture.call_args.kwargs["error_type"] is None

    async def test_call_tool_normal_success_still_counts_as_ok(self) -> None:
        """No-regression companion to the logical-error test above."""
        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler("ouroboros_success_probe"))

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_success_probe", {"input": "safe"})

        assert result.is_ok
        assert result.value.is_error is False
        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is True
        assert capture.call_args.kwargs["error_type"] is None

    async def test_call_tool_extension_error_class_is_folded_in_error_type(self) -> None:
        """A registered extension's handler returning Result.err(AcmePrivateProjectError(...))
        directly (not via raise -- _call_tool_impl only wraps raised exceptions,
        an err Result the handler builds itself flows through unwrapped) must
        never expose that class name through error_type.
        """
        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_extension_error_probe")
        handler.handle_mock.return_value = Result.err(
            AcmePrivateProjectError("acme private project failed")
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_extension_error_probe", {"input": "safe"})

        assert result.is_err
        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "AcmePrivateProject" not in full_event
        assert "acme" not in full_event.lower()

    async def test_call_tool_spoofed_builtins_module_is_still_folded(self) -> None:
        """__module__ == "builtins" does not earn a verbatim error_type.

        The pre-round-14 gate trusted this claim; the closed-vocabulary gate
        only trusts class *names* it enumerated itself from the real
        `builtins` module, so an extension lying about its module gains
        nothing.
        """
        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_spoofed_builtins_probe")
        handler.handle_mock.return_value = Result.err(
            SpoofedBuiltinModuleError("acme private project failed")
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_spoofed_builtins_probe", {"input": "safe"})

        assert result.is_err
        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "SpoofedBuiltinModule" not in full_event
        assert "acme" not in full_event.lower()

    async def test_call_tool_ouroboros_prefix_collision_is_still_folded(self) -> None:
        """A module merely starting with "ouroboros" is not the ouroboros package.

        Regression for the prefix-collision bug: a real package named e.g.
        ``ouroboros_acme_private`` would have passed the old
        ``module.startswith("ouroboros")`` check.
        """
        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_prefix_collision_probe")
        handler.handle_mock.return_value = Result.err(
            SpoofedOuroborosPrefixError("acme private project failed")
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_prefix_collision_probe", {"input": "safe"})

        assert result.is_err
        capture.assert_called_once()
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "SpoofedOuroborosPrefix" not in full_event
        assert "acme" not in full_event.lower()

    async def test_call_tool_malformed_module_metadata_never_crashes_the_real_result(
        self,
    ) -> None:
        """A non-string __module__ must not replace the real Result.err payload.

        This is the exact contract-violation the reviewer demonstrated: the
        old gate's ``module.partition(".")`` against a non-string __module__
        raised AttributeError *after* the handler had already produced a
        real result, silently swapping a legitimate error payload for a
        telemetry-internal crash.
        """
        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_malformed_module_probe")
        original_error = MalformedModuleMetadataError("acme private project failed")
        handler.handle_mock.return_value = Result.err(original_error)
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_malformed_module_probe", {"input": "safe"})

        # The real handler result survives untouched -- no AttributeError,
        # no substitute error, the caller gets exactly what the handler sent.
        assert result.is_err
        assert result.error is original_error
        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "MalformedModuleMetadata" not in full_event
        assert "acme" not in full_event.lower()

    async def test_call_tool_hostile_is_error_systemexit_never_crashes_the_real_result(
        self,
    ) -> None:
        """Round-16: a hostile is_error property raising SystemExit must not
        replace a genuinely successful handler result.

        Unlike the hostile-__name__/Result.err shape, this one is provably
        clean through the REAL adapter path: MCPServerAdapter._call_tool_impl's
        own logging only ever reads ``result.is_ok``/``type(result.error).__name__``,
        never ``result.value.is_error`` -- so this scenario never touches
        that unrelated pre-existing code and genuinely exercises
        observe_adapter_tool_call's own _is_logical_error call.
        """
        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_hostile_is_error_probe")
        original_value = _HostileIsErrorRaisesSystemExit()
        handler.handle_mock.return_value = Result.ok(original_value)
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_hostile_is_error_probe", {"input": "safe"})

        # The call completes normally and the real handler value survives
        # untouched -- no SystemExit escaped, no substitute result.
        assert result.is_ok
        assert result.value is original_value
        capture.assert_called_once()
        # _is_logical_error's total-isolation fallback is False (not a
        # logical error), so the outer Result.is_ok alone decides -- ok stays
        # True, matching this round's spec ("logical-error False").
        assert capture.call_args.kwargs["ok"] is True

    async def test_call_tool_raised_hostile_keyboardinterrupt_error_type_folds(self) -> None:
        """Round-16: a raised (not returned) BaseException-not-Exception
        subclass with a hostile __name__ reaches observe_adapter_tool_call's
        own except BaseException clause with the original object intact --
        _call_tool_impl's ``except Exception`` never matches a
        KeyboardInterrupt subclass, so its own logging never runs and this
        genuinely exercises _safe_error_type() through the real adapter path
        (the Result.err(...) shape used for the analogous err-variant test
        is intercepted by that unrelated pre-existing logging first; see
        TestSafeErrorTypeDirect's docstring and the round-14 report).

        The exception legitimately propagating to the caller is expected,
        unchanged behavior -- Result reserves raised exceptions for
        programming errors, not Result.err's expected-failure channel. What
        must not happen is a SECOND, different crash while telemetry
        computes error_type for it.
        """
        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_hostile_raised_probe")
        handler.handle_mock.side_effect = HostileRaisedKeyboardInterrupt(
            "acme private project failed"
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            with pytest.raises(HostileRaisedKeyboardInterrupt):
                await adapter.call_tool("ouroboros_hostile_raised_probe", {"input": "safe"})

        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "acme" not in full_event.lower()

    async def test_call_tool_builtin_error_class_stays_verbatim(self) -> None:
        """No-regression companion: our own/builtin error classes are unaffected."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_builtin_error_probe")
        handler.handle_mock.return_value = Result.err(MCPToolError("boom", tool_name="probe"))
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_builtin_error_probe", {"input": "safe"})

        assert result.is_err
        capture.assert_called_once()
        assert capture.call_args.kwargs["error_type"] == "MCPToolError"

    async def test_call_tool_security_denial_is_captured_once(self) -> None:
        """A pre-handler security return is a visible failed invocation."""
        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler("ouroboros_secure_tool"))
        denial = MCPServerError("denied")
        adapter._security.check_request = AsyncMock(return_value=Result.err(denial))

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            result = await adapter.call_tool("ouroboros_secure_tool", {"input": "safe"})

        assert result.is_err
        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "MCPServerError"

    async def test_call_tool_handler_error(self) -> None:
        """call_tool handles handler errors."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler()
        handler.handle_mock.side_effect = RuntimeError("Handler failed")
        adapter.register_tool(handler)

        with capture_logs() as logs:
            result = await adapter.call_tool("test_tool", {})

        assert result.is_err
        assert "Handler failed" in str(result.error)
        assert any(event["event"] == "mcp.server.call_tool.error" for event in logs)


class TestCanonicalToolNameSyncGuard:
    """Guard the SSOT pairing between the shipped tool registry and telemetry.

    telemetry.py's capture_tool_call folds any non-canonical (including a
    genuinely registered but unaudited extension) tool name to the fixed
    ``ouroboros_extension_tool`` literal. If a future built-in tool ships
    without a matching entry in ``_CANONICAL_TOOL_NAMES``, it would silently
    report as an anonymous extension tool -- losing its real funnel step --
    instead of failing loudly. This test makes that omission fail CI.
    """

    def test_every_shipped_tool_is_in_the_canonical_set(self) -> None:
        from ouroboros.mcp.tools.definitions import get_ouroboros_tools
        from ouroboros.telemetry import _CANONICAL_TOOL_NAMES

        shipped_names = {tool.definition.name for tool in get_ouroboros_tools()}
        missing = shipped_names - _CANONICAL_TOOL_NAMES

        assert not missing, (
            f"Shipped tool(s) missing from telemetry._CANONICAL_TOOL_NAMES: "
            f"{sorted(missing)} -- add them or they will silently report as "
            f"ouroboros_extension_tool in telemetry."
        )


class TestSafeErrorTypeDirect:
    """Direct unit coverage of _safe_error_type()'s closed-vocabulary gate.

    These call the helper directly rather than through the adapter, because
    HostileNameError's hostile __name__ metaclass also trips an unrelated,
    pre-existing bug in MCPServerAdapter._call_tool_impl's own local
    structlog calls (adapter.py ~line 1020 does an un-isolated
    ``type(result.error).__name__`` for its "mcp.server.call_tool.return"
    log line) -- that crash happens before the telemetry boundary even
    runs, so it can't be used to prove _safe_error_type's own isolation
    through the full adapter path. See the round report for that finding;
    it is a local-logging-only issue (never reaches PostHog) and out of
    this round's owned scope.
    """

    def test_hostile_name_metaclass_never_raises(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert _safe_error_type(HostileNameError("acme private project failed")) == (
            "ExtensionError"
        )

    def test_spoofed_builtins_module_still_folds(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert _safe_error_type(SpoofedBuiltinModuleError("x")) == "ExtensionError"

    def test_ouroboros_prefix_collision_still_folds(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert _safe_error_type(SpoofedOuroborosPrefixError("x")) == "ExtensionError"

    def test_malformed_module_metadata_never_raises(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert _safe_error_type(MalformedModuleMetadataError("x")) == "ExtensionError"

    def test_ouroboros_error_class_stays_verbatim(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert _safe_error_type(MCPToolError("boom", tool_name="probe")) == "MCPToolError"

    def test_builtin_error_class_stays_verbatim(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert _safe_error_type(ValueError("boom")) == "ValueError"

    def test_third_party_error_class_folds(self) -> None:
        """jsonschema.ValidationError is genuinely third-party -- confirms
        round-13's outcome is unchanged under the round-14 rewrite.
        """
        from jsonschema.exceptions import ValidationError

        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert _safe_error_type(ValidationError("boom")) == "ExtensionError"

    def test_hostile_name_metaclass_raising_keyboardinterrupt_never_escapes(self) -> None:
        """Round-16: except Exception alone would let this KeyboardInterrupt
        through -- it must be caught and folded like any other hostile name.
        """
        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert (
            _safe_error_type(HostileKeyboardInterruptNameError("acme private project failed"))
            == "ExtensionError"
        )

    def test_hostile_name_metaclass_raising_systemexit_never_escapes(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _safe_error_type

        assert (
            _safe_error_type(HostileSystemExitNameError("acme private project failed"))
            == "ExtensionError"
        )


class TestIsLogicalErrorDirect:
    """Direct unit coverage of _is_logical_error()'s total isolation.

    Same rationale as TestSafeErrorTypeDirect for testing directly rather
    than only through the adapter: it lets every hostile shape be proven
    independent of whether some other, unrelated code on the adapter path
    happens to touch the same attribute first.
    """

    def test_hostile_is_error_raising_systemexit_never_escapes(self) -> None:
        """Round-16: except Exception alone would let this SystemExit
        through -- it must be caught and folded to False (not a logical
        error), letting the outer Result.is_ok decide instead.
        """
        from ouroboros.mcp.telemetry_boundary import _is_logical_error

        assert _is_logical_error(_HostileIsErrorRaisesSystemExit()) is False

    def test_hostile_is_error_raising_keyboardinterrupt_never_escapes(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _is_logical_error

        assert _is_logical_error(_HostileIsErrorRaisesKeyboardInterrupt()) is False

    def test_normal_is_error_true_still_detected(self) -> None:
        """No-regression companion: a well-behaved is_error=True still counts."""
        from ouroboros.mcp.telemetry_boundary import _is_logical_error

        assert _is_logical_error(MCPToolResult(is_error=True)) is True

    def test_normal_is_error_false_still_detected(self) -> None:
        from ouroboros.mcp.telemetry_boundary import _is_logical_error

        assert _is_logical_error(MCPToolResult(is_error=False)) is False

    def test_missing_attribute_falls_back_false(self) -> None:
        """A value with no is_error attribute at all (not hostile, just a
        different shape) falls back to False via getattr's own default.
        """
        from ouroboros.mcp.telemetry_boundary import _is_logical_error

        assert _is_logical_error(object()) is False


class TestMCPServerAdapterResources:
    """Test MCPServerAdapter resource operations."""

    def test_register_resource(self) -> None:
        """register_resource adds a resource handler."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler()

        adapter.register_resource(handler)

        assert adapter.info.capabilities.resources is True

    async def test_list_resources(self) -> None:
        """list_resources returns registered resources."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler("test://my-resource")

        adapter.register_resource(handler)
        resources = await adapter.list_resources()

        assert len(resources) == 1
        assert resources[0].uri == "test://my-resource"

    async def test_read_resource_success(self) -> None:
        """read_resource invokes handler and returns content."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler("test://resource")
        adapter.register_resource(handler)

        result = await adapter.read_resource("test://resource")

        assert result.is_ok
        assert result.value.text == "Resource content"

    async def test_read_resource_routes_registered_base_uri_prefix(self) -> None:
        """read_resource routes child URIs to handlers registered at the base URI."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler("test://resource")
        adapter.register_resource(handler)

        result = await adapter.read_resource("test://resource/child")

        assert result.is_ok
        handler.handle_mock.assert_awaited_once_with("test://resource/child")

    async def test_read_resource_not_found(self) -> None:
        """read_resource returns error for unknown resource."""
        adapter = MCPServerAdapter()

        result = await adapter.read_resource("unknown://resource")

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)


class TestMCPServerAdapterInfo:
    """Test MCPServerAdapter info property."""

    def test_info_updates_with_registrations(self) -> None:
        """Server info reflects registered handlers."""
        adapter = MCPServerAdapter()

        # Initially no capabilities
        assert adapter.info.capabilities.tools is False
        assert adapter.info.capabilities.resources is False

        # After registering tool
        adapter.register_tool(MockToolHandler())
        assert adapter.info.capabilities.tools is True

        # After registering resource
        adapter.register_resource(MockResourceHandler())
        assert adapter.info.capabilities.resources is True

    def test_info_includes_tool_definitions(self) -> None:
        """Server info includes tool definitions."""
        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler("tool1"))
        adapter.register_tool(MockToolHandler("tool2"))

        info = adapter.info

        assert len(info.tools) == 2
        tool_names = {t.name for t in info.tools}
        assert "tool1" in tool_names
        assert "tool2" in tool_names


# ── Transport validation ────────────────────────────────────────────


class TestValidateTransport:
    """Tests for validate_transport()."""

    def test_valid_lowercase(self):
        assert validate_transport("stdio") == "stdio"
        assert validate_transport("sse") == "sse"
        assert validate_transport("streamable-http") == "streamable-http"

    def test_case_insensitive(self):
        assert validate_transport("SSE") == "sse"
        assert validate_transport("Stdio") == "stdio"
        assert validate_transport("sSe") == "sse"
        assert validate_transport("STREAMABLE-HTTP") == "streamable-http"
        assert validate_transport("streamable_http") == "streamable-http"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid transport"):
            validate_transport("http")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Invalid transport"):
            validate_transport("")

    def test_valid_transports_constant(self):
        assert "stdio" in VALID_TRANSPORTS
        assert "sse" in VALID_TRANSPORTS
        assert "streamable-http" in VALID_TRANSPORTS


class TestServeTransport:
    """Tests for MCPServerAdapter.serve() transport handling."""

    @pytest.mark.asyncio
    async def test_invalid_transport_raises(self):
        adapter = MCPServerAdapter()
        with pytest.raises(ValueError, match="Invalid transport"):
            await adapter.serve(transport="bogus")

    @pytest.mark.asyncio
    async def test_sse_passes_host_port_to_mcpserver_run_method(self):
        """Network binding belongs to the v2 run method, not construction."""
        from unittest.mock import MagicMock, patch

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_sse_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        adapter = MCPServerAdapter()

        with patch(
            "ouroboros.mcp.server.adapter._OuroborosSDKServer",
            mock_fastmcp_cls,
        ):
            await adapter.serve(transport="sse", host="127.0.0.1", port=9000)

        mock_fastmcp_cls.assert_called_once()
        assert mock_fastmcp_cls.call_args.args == (adapter,)
        assert mock_fastmcp_cls.call_args.kwargs["version"] == __version__
        extensions = mock_fastmcp_cls.call_args.kwargs["extensions"]
        assert len(extensions) == 1
        assert extensions[0].identifier == "io.ouroboros/subagents"
        assert extensions[0].settings()["undeclaredBehavior"] == (
            "parallel_preferred_sequential_fallback"
        )
        mock_instance.run_sse_async.assert_awaited_once()
        run_args = mock_instance.run_sse_async.await_args.kwargs
        assert run_args["host"] == "127.0.0.1"
        assert run_args["port"] == 9000
        assert run_args["transport_security"].allowed_hosts == [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]
        assert run_args["transport_security"].allowed_origins == []

    @pytest.mark.asyncio
    async def test_stdio_serve_logs_exit(self) -> None:
        """serve() logs transport lifecycle completion."""
        from unittest.mock import MagicMock, patch

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_stdio_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        adapter = MCPServerAdapter(name="test-server")

        with (
            patch(
                "ouroboros.mcp.server.adapter._OuroborosSDKServer",
                mock_fastmcp_cls,
            ),
            capture_logs() as logs,
        ):
            await adapter.serve(transport="stdio")

        exit_event = next(event for event in logs if event["event"] == "mcp.server.serve_exit")
        assert exit_event["name"] == "test-server"
        assert exit_event["transport"] == "stdio"
        assert isinstance(exit_event["duration_ms"], int)
        assert exit_event["duration_ms"] >= 0

    @pytest.mark.asyncio
    async def test_serve_initializes_owned_resources_before_transport(self) -> None:
        """No transport accepts work before the explicit startup boundary."""
        from unittest.mock import MagicMock, patch

        lifecycle: list[str] = []

        class _StartupResource:
            async def initialize(self) -> None:
                lifecycle.append("initialize")

            async def close(self) -> None:
                lifecycle.append("close")

        async def run_stdio() -> None:
            lifecycle.append("serve")

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_stdio_async = AsyncMock(side_effect=run_stdio)
        mock_fastmcp_cls.return_value = mock_instance
        adapter = MCPServerAdapter()
        adapter.register_owned_resource(_StartupResource(), initialize_on_startup=True)

        with patch(
            "ouroboros.mcp.server.adapter._OuroborosSDKServer",
            mock_fastmcp_cls,
        ):
            await adapter.serve(transport="stdio")
        await adapter.shutdown()

        assert lifecycle == ["initialize", "serve", "close"]

    @pytest.mark.asyncio
    async def test_sse_ephemeral_port_zero(self):
        """port=0 must reach MCPServer's run method without being rewritten."""
        from unittest.mock import MagicMock, patch

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_sse_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        adapter = MCPServerAdapter()

        with patch(
            "ouroboros.mcp.server.adapter._OuroborosSDKServer",
            mock_fastmcp_cls,
        ):
            await adapter.serve(transport="sse", host="localhost", port=0)

        mock_instance.run_sse_async.assert_awaited_once()
        run_args = mock_instance.run_sse_async.await_args.kwargs
        assert run_args["host"] == "localhost"
        assert run_args["port"] == 0
        assert run_args["transport_security"].allowed_hosts == [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]
        assert run_args["transport_security"].allowed_origins == []

    @pytest.mark.asyncio
    async def test_streamable_http_uses_modern_stateless_run_options(self):
        """Streamable HTTP v2 is bound and configured at run time."""
        from unittest.mock import MagicMock, patch

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_streamable_http_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        adapter = MCPServerAdapter()

        with patch(
            "ouroboros.mcp.server.adapter._OuroborosSDKServer",
            mock_fastmcp_cls,
        ):
            await adapter.serve(transport="streamable-http", host="127.0.0.1", port=9100)

        mock_fastmcp_cls.assert_called_once()
        assert mock_fastmcp_cls.call_args.args == (adapter,)
        assert mock_fastmcp_cls.call_args.kwargs["version"] == __version__
        mock_instance.run_streamable_http_async.assert_awaited_once()
        run_args = mock_instance.run_streamable_http_async.await_args.kwargs
        assert run_args["host"] == "127.0.0.1"
        assert run_args["port"] == 9100
        assert run_args["stateless_http"] is True
        assert run_args["transport_security"].allowed_hosts == [
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
        ]
        assert run_args["transport_security"].allowed_origins == []

    @pytest.mark.asyncio
    async def test_streamable_http_real_mcpserver_exposes_mcp_path(self) -> None:
        """Real MCPServer streamable HTTP serving exposes the advertised /mcp path."""
        from unittest.mock import patch

        pytest.importorskip("mcp.server")
        pytest.importorskip("uvicorn")

        served = SimpleNamespace(config=None)

        async def capture_serve(server, *args, **kwargs) -> None:
            served.config = server.config

        adapter = MCPServerAdapter()

        with patch("uvicorn.Server.serve", new=capture_serve):
            await adapter.serve(transport="streamable-http", host="127.0.0.1", port=9100)

        assert served.config is not None
        assert served.config.host == "127.0.0.1"
        assert served.config.port == 9100

        route_paths = {getattr(route, "path", None) for route in served.config.app.routes}
        assert "/mcp" in route_paths

    @pytest.mark.asyncio
    async def test_real_fastmcp_tools_list_preserves_parameter_descriptions(self) -> None:
        from unittest.mock import patch

        mcp_server_module = pytest.importorskip("mcp.server")
        from ouroboros.mcp.tools.definitions import StartEvolveStepHandler

        adapter = MCPServerAdapter()
        adapter.register_tool(StartEvolveStepHandler())

        with patch.object(
            mcp_server_module.MCPServer,
            "run_stdio_async",
            new=AsyncMock(),
        ):
            await adapter.serve(transport="stdio")

        tools = await adapter._mcp_server.list_tools()
        tool = next(item for item in tools if item.name == "ouroboros_start_evolve_step")
        description = tool.input_schema["properties"]["seed_content"]["description"]

        assert "YAML-formatted string" in description
        assert "not JSON-shaped text or an object literal" in description
        assert "before Ouroboros receives it" in description

    @pytest.mark.asyncio
    async def test_real_fastmcp_tools_list_preserves_parameter_schema_metadata(self) -> None:
        from unittest.mock import patch

        mcp_server_module = pytest.importorskip("mcp.server")
        from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler
        from ouroboros.mcp.tools.brownfield_handler import BrownfieldHandler
        from ouroboros.mcp.tools.evolution_handlers import StartEvolveStepHandler

        adapter = MCPServerAdapter()
        adapter.register_tool(BrownfieldHandler())
        adapter.register_tool(GenerateSeedHandler())
        adapter.register_tool(StartEvolveStepHandler())

        with patch.object(
            mcp_server_module.MCPServer,
            "run_stdio_async",
            new=AsyncMock(),
        ):
            await adapter.serve(transport="stdio")

        tools = {tool.name: tool for tool in await adapter._mcp_server.list_tools()}

        brownfield_action = tools["ouroboros_brownfield"].input_schema["properties"]["action"]
        assert brownfield_action["enum"] == [
            "scan",
            "register",
            "query",
            "set_default",
            "set_defaults",
        ]

        authoring_client_gates = tools["ouroboros_generate_seed"].input_schema["properties"][
            "client_gates"
        ]
        assert authoring_client_gates["items"] == {"type": "string"}

        evolve_tool = tools["ouroboros_start_evolve_step"]
        execute_schema = evolve_tool.input_schema["properties"]["execute"]
        assert execute_schema["type"] == "boolean"
        assert "null" not in execute_schema.get("type", [])
        seed_schema = evolve_tool.input_schema["properties"]["seed_content"]
        assert "default" not in seed_schema
        assert "seed_content" not in evolve_tool.input_schema.get("required", [])

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_required_parameter_default_survives_into_fastmcp_schema(self) -> None:
        """A required parameter may still carry a `default` annotation.

        `MCPToolDefinition.to_input_schema()` emits `default` for required
        parameters, and JSON Schema permits it. Pydantic discards the value along
        with `Field(default=...)`, so the two surfaces must be pinned against each
        other or they silently describe different tools.
        """
        mcp_server_module = pytest.importorskip("mcp.server")

        class DefaultedRequiredHandler(MockToolHandler):
            @property
            def definition(self) -> MCPToolDefinition:
                return MCPToolDefinition(
                    name="defaulted_tool",
                    description="A required parameter carrying a default",
                    parameters=(
                        MCPToolParameter(
                            name="mode",
                            type=ToolInputType.STRING,
                            required=True,
                            default="safe",
                            description="Execution mode",
                        ),
                    ),
                )

        adapter = MCPServerAdapter()
        handler = DefaultedRequiredHandler(name="defaulted_tool")
        adapter.register_tool(handler)
        with patch.object(mcp_server_module.MCPServer, "run_stdio_async", new=AsyncMock()):
            await adapter.serve(transport="stdio")

        tools = await adapter._mcp_server.list_tools()
        tool = next(t for t in tools if t.name == "defaulted_tool")
        mode_schema = tool.input_schema["properties"]["mode"]

        canonical = handler.definition.to_input_schema()["properties"]["mode"]
        assert canonical["default"] == "safe"
        assert mode_schema["default"] == "safe", "FastMCP schema dropped the canonical default"
        assert "mode" in tool.input_schema["required"], "the default must not make it optional"

    def test_integer_array_items_accept_integral_floats(self) -> None:
        """JSON Schema `type: integer` matches any number with no fractional part.

        The advertised schema accepts `[1.0]`, so runtime validation must not
        reject it on Python `int` identity alone.
        """
        parameter = MCPToolParameter(
            name="nums",
            type=ToolInputType.ARRAY,
            required=False,
            items={"type": "integer"},
        )
        for accepted in ([1], [1.0], [2.0, 3]):
            _validate_parameter_constraints((parameter,), {"nums": accepted})

        for rejected in ([1.5], [True], ["1"]):
            with pytest.raises(ValueError, match="Invalid items for nums"):
                _validate_parameter_constraints((parameter,), {"nums": rejected})

    async def test_real_fastmcp_invocation_omits_unset_optional_parameters(self) -> None:
        """Omitted optionals must not reach the handler as explicit `None`.

        #1538 classified forwarding an unset optional as `None` a bug — in-process
        callers see a missing key while plugin-MCP callers saw `key present, value
        None`, which crashed handlers doing `.get(k, [])`. #1726 normalized that at
        the wrapper chokepoint, so the contract asserted here is omission, not None.
        """
        mcp_server_module = pytest.importorskip("mcp.server")

        class OptionalParameterHandler(MockToolHandler):
            @property
            def definition(self) -> MCPToolDefinition:
                return MCPToolDefinition(
                    name="optional_tool",
                    description="A tool with an omitted optional parameter",
                    parameters=(
                        MCPToolParameter(
                            name="required_input",
                            type=ToolInputType.STRING,
                            required=True,
                        ),
                        # `MCPToolParameter.required` defaults to True, so an
                        # optional parameter must say so explicitly — `default=None`
                        # alone does not make it optional.
                        MCPToolParameter(
                            name="optional_input",
                            type=ToolInputType.STRING,
                            required=False,
                            default=None,
                            description="Optional input",
                        ),
                        MCPToolParameter(
                            name="optional_mode",
                            type=ToolInputType.STRING,
                            required=False,
                            enum=("fast", "safe"),
                            default=None,
                        ),
                        MCPToolParameter(
                            name="scores",
                            type=ToolInputType.ARRAY,
                            required=False,
                            items={"type": "number"},
                        ),
                    ),
                )

        from unittest.mock import patch

        adapter = MCPServerAdapter()
        handler = OptionalParameterHandler(name="optional_tool")
        adapter.register_tool(handler)
        with (
            patch.object(
                mcp_server_module.MCPServer,
                "run_stdio_async",
                new=AsyncMock(),
            ),
        ):
            await adapter.serve(transport="stdio")

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            await adapter._mcp_server.call_tool(
                "optional_tool",
                {"required_input": "provided", "scores": [1.5]},
            )

            with pytest.raises(Exception, match="Invalid value for optional_mode"):
                await adapter._mcp_server.call_tool(
                    "optional_tool",
                    {
                        "required_input": "provided",
                        "optional_mode": "unsafe",
                        "scores": [1.5],
                    },
                )
            with pytest.raises(Exception, match="Invalid items for scores"):
                await adapter._mcp_server.call_tool(
                    "optional_tool",
                    {"required_input": "provided", "scores": [True]},
                )

        handler.handle_mock.assert_awaited_once_with(
            {
                "required_input": "provided",
                "scores": [1.5],
            }
        )
        forwarded = handler.handle_mock.await_args.args[0]
        assert "optional_input" not in forwarded
        assert "optional_mode" not in forwarded

        assert capture.call_count == 3
        assert [call.kwargs["ok"] for call in capture.call_args_list] == [True, False, False]

    async def test_sdk_call_preserves_normalized_host_context_for_handler(self) -> None:
        """The SDK boundary must not discard client identity/capability facts."""
        mcp_server_module = pytest.importorskip("mcp.server")
        from ouroboros.mcp.host_context import current_mcp_host_context

        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_sdk_host_context_probe")
        observed = []

        async def handle(arguments: dict[str, Any]):
            observed.append(current_mcp_host_context())
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                )
            )

        handler.handle_mock.side_effect = handle
        adapter.register_tool(handler)
        with patch.object(
            mcp_server_module.MCPServer,
            "run_stdio_async",
            new=AsyncMock(),
        ):
            await adapter.serve(transport="stdio")
        sdk_context = SimpleNamespace(
            session=SimpleNamespace(
                client_params=SimpleNamespace(client_info=SimpleNamespace(name="claude-code")),
                client_capabilities=SimpleNamespace(
                    extensions={"io.ouroboros/subagents": {"mode": "parallel"}},
                    experimental=None,
                ),
            )
        )

        await adapter._mcp_server.call_tool(
            "ouroboros_sdk_host_context_probe",
            {"input": "safe"},
            sdk_context,
        )

        assert len(observed) == 1
        assert observed[0].host_family.value == "claude_code"
        assert observed[0].subagent_capability.value == "parallel"
        assert observed[0].dispatch_authority.value == "mcp_host"

    async def test_sdk_failure_boundaries_are_captured_exactly_once(self) -> None:
        """Adapter, output-validation, and conversion failures are not double-counted."""
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        class OutputHandler(MockToolHandler):
            @property
            def definition(self) -> MCPToolDefinition:
                return MCPToolDefinition(
                    name="ouroboros_sdk_boundary",
                    description="SDK telemetry boundary probe",
                    parameters=(MCPToolParameter(name="input", type=ToolInputType.STRING),),
                    output_schema={
                        "type": "object",
                        "properties": {"approved": {"type": "boolean"}},
                        "required": ["approved"],
                        "additionalProperties": False,
                    },
                )

        adapter = MCPServerAdapter()
        handler = OutputHandler(name="ouroboros_sdk_boundary")
        adapter.register_tool(handler)
        success = MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
            structured_content={"approved": True},
        )

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            handler.handle_mock.return_value = Result.err(MCPServerError("adapter failed"))
            with pytest.raises(RuntimeError, match="adapter failed"):
                await call_sdk_tool(adapter, "ouroboros_sdk_boundary", {"input": "safe"})
            capture.assert_called_once()
            assert capture.call_args.kwargs["ok"] is False
            assert capture.call_args.kwargs["error_type"] == "MCPServerError"

            capture.reset_mock()
            handler.handle_mock.return_value = Result.ok(
                MCPToolResult(
                    content=success.content,
                    structured_content={"approved": "yes"},
                )
            )
            with pytest.raises(Exception, match="is not of type 'boolean'"):
                await call_sdk_tool(adapter, "ouroboros_sdk_boundary", {"input": "safe"})
            capture.assert_called_once()
            assert capture.call_args.kwargs["ok"] is False
            # jsonschema.ValidationError is third-party (module "jsonschema.*",
            # not builtins/stdlib/ouroboros), so the audited error_type taxonomy
            # folds it to the fixed extension literal rather than exposing the
            # dependency's class name verbatim.
            assert capture.call_args.kwargs["error_type"] == "ExtensionError"

            capture.reset_mock()
            handler.handle_mock.return_value = Result.ok(success)
            with (
                patch(
                    "ouroboros.mcp.sdk_mapping.tool_result_to_sdk",
                    side_effect=ValueError("conversion failed"),
                ),
                pytest.raises(ValueError, match="conversion failed"),
            ):
                await call_sdk_tool(adapter, "ouroboros_sdk_boundary", {"input": "safe"})
            capture.assert_called_once()
            assert capture.call_args.kwargs["ok"] is False
            assert capture.call_args.kwargs["error_type"] == "ValueError"

    async def test_sdk_call_tool_hostile_unregistered_name_is_sanitized(self) -> None:
        """The SDK entry path never queues a caller-controlled unregistered name."""
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        hostile_name = "ouroboros_/home/alice/private-project"

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            with pytest.raises(RuntimeError, match="Tool not found"):
                await call_sdk_tool(adapter, hostile_name, {})

        capture.assert_called_once()
        assert capture.call_args.args[0] == "ouroboros_unknown_tool"
        assert capture.call_args.kwargs["ok"] is False
        for value in (*capture.call_args.args, *capture.call_args.kwargs.values()):
            assert "/home/alice" not in str(value)
            assert "private-project" not in str(value)

    async def test_sdk_call_tool_registered_extension_success_is_not_collected(self) -> None:
        """Successful non-product extension calls emit service activity only."""
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        extension_name = "custom_tool"
        adapter.register_tool(MockToolHandler(extension_name))

        with patch("ouroboros.telemetry.capture") as capture:
            await call_sdk_tool(adapter, extension_name, {"input": "safe"})

        capture.assert_called_once_with("service_active", {"service": "mcp"})

    async def test_sdk_non_prefixed_extension_failure_is_folded(self) -> None:
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        handler = MockToolHandler("custom_tool")
        handler.handle_mock.return_value = Result.err(MCPServerError("failed"))
        adapter.register_tool(handler)

        with patch("ouroboros.telemetry.capture") as capture:
            with pytest.raises(RuntimeError, match="failed"):
                await call_sdk_tool(adapter, "custom_tool", {"input": "safe"})

        assert capture.call_args_list[0].args == ("service_active", {"service": "mcp"})
        assert capture.call_args_list[1].args == (
            "command_run",
            {
                "command": "extension_tool",
                "service": "mcp",
                "status": "failed",
                "error_type": "MCPServerError",
            },
        )

    async def test_sdk_non_prefixed_extension_logical_block_is_folded(self) -> None:
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        handler = MockToolHandler("custom_tool")
        handler.handle_mock.return_value = Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="input_required"),),
                is_error=True,
            )
        )
        adapter.register_tool(handler)

        with patch("ouroboros.telemetry.capture") as capture:
            await call_sdk_tool(adapter, "custom_tool", {"input": "safe"})

        assert capture.call_args_list[0].args == ("service_active", {"service": "mcp"})
        assert capture.call_args_list[1].args == (
            "command_run",
            {
                "command": "extension_tool",
                "service": "mcp",
                "status": "blocked",
                "error_type": None,
            },
        )

    async def test_sdk_call_tool_logical_error_response_counts_as_not_ok(self) -> None:
        """SDK-path companion to the typed-adapter logical-error test."""
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_sdk_logical_error_probe")
        handler.handle_mock.return_value = Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="input_required"),),
                is_error=True,
                meta={"status": "input_required"},
            )
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            await call_sdk_tool(adapter, "ouroboros_sdk_logical_error_probe", {"input": "safe"})

        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        # The SDK-path success-side capture call never included error_type at
        # all (unlike the typed path, which always passes it explicitly) --
        # its absence here is the pre-existing convention, not a regression.
        assert capture.call_args.kwargs.get("error_type") is None

    async def test_sdk_call_tool_normal_success_still_counts_as_ok(self) -> None:
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler("ouroboros_sdk_success_probe"))

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            await call_sdk_tool(adapter, "ouroboros_sdk_success_probe", {"input": "safe"})

        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is True

    async def test_sdk_call_tool_extension_error_class_is_folded_in_error_type(self) -> None:
        """SDK-path companion to the typed-adapter extension-error test."""
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_sdk_extension_error_probe")
        handler.handle_mock.return_value = Result.err(
            AcmePrivateProjectError("acme private project failed")
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            with pytest.raises(RuntimeError, match="acme private project failed"):
                await call_sdk_tool(
                    adapter, "ouroboros_sdk_extension_error_probe", {"input": "safe"}
                )

        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "AcmePrivateProject" not in full_event
        assert "acme" not in full_event.lower()

    async def test_sdk_call_tool_spoofed_builtins_module_is_still_folded(self) -> None:
        """SDK-path companion to the typed-adapter spoofed-builtins test."""
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_sdk_spoofed_builtins_probe")
        handler.handle_mock.return_value = Result.err(
            SpoofedBuiltinModuleError("acme private project failed")
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            with pytest.raises(RuntimeError, match="acme private project failed"):
                await call_sdk_tool(
                    adapter, "ouroboros_sdk_spoofed_builtins_probe", {"input": "safe"}
                )

        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "SpoofedBuiltinModule" not in full_event
        assert "acme" not in full_event.lower()

    async def test_sdk_call_tool_ouroboros_prefix_collision_is_still_folded(self) -> None:
        """SDK-path companion to the typed-adapter prefix-collision test."""
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_sdk_prefix_collision_probe")
        handler.handle_mock.return_value = Result.err(
            SpoofedOuroborosPrefixError("acme private project failed")
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            with pytest.raises(RuntimeError, match="acme private project failed"):
                await call_sdk_tool(
                    adapter, "ouroboros_sdk_prefix_collision_probe", {"input": "safe"}
                )

        capture.assert_called_once()
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "SpoofedOuroborosPrefix" not in full_event
        assert "acme" not in full_event.lower()

    async def test_sdk_call_tool_malformed_module_metadata_never_crashes(self) -> None:
        """SDK-path companion: the RuntimeError still carries the real error
        message (str(result.error)) rather than being replaced by an
        AttributeError from a hostile __module__ read.
        """
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_sdk_malformed_module_probe")
        handler.handle_mock.return_value = Result.err(
            MalformedModuleMetadataError("acme private project failed")
        )
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            with pytest.raises(RuntimeError, match="acme private project failed"):
                await call_sdk_tool(
                    adapter, "ouroboros_sdk_malformed_module_probe", {"input": "safe"}
                )

        capture.assert_called_once()
        assert capture.call_args.kwargs["ok"] is False
        assert capture.call_args.kwargs["error_type"] == "ExtensionError"
        full_event = repr(capture.call_args.args) + repr(capture.call_args.kwargs)
        assert "MalformedModuleMetadata" not in full_event
        assert "acme" not in full_event.lower()

    async def test_sdk_call_tool_builtin_error_class_stays_verbatim(self) -> None:
        pytest.importorskip("mcp.server")
        from ouroboros.mcp.telemetry_boundary import call_sdk_tool

        adapter = MCPServerAdapter()
        handler = MockToolHandler("ouroboros_sdk_builtin_error_probe")
        handler.handle_mock.return_value = Result.err(MCPToolError("boom", tool_name="probe"))
        adapter.register_tool(handler)

        with patch("ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call") as capture:
            with pytest.raises(RuntimeError):
                await call_sdk_tool(adapter, "ouroboros_sdk_builtin_error_probe", {"input": "safe"})

        capture.assert_called_once()
        assert capture.call_args.kwargs["error_type"] == "MCPToolError"

    @pytest.mark.asyncio
    async def test_fastmcp_path_enforces_security(self):
        """FastMCP tool wrapper routes through call_tool to enforce security checks."""
        from unittest.mock import MagicMock, patch

        # Create adapter with no auth but input validation enabled (default)
        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler(name="secure_tool"))

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        captured_wrapper = None

        def capture_tool_decorator(name, description):
            """Capture the tool wrapper function."""

            def decorator(func):
                nonlocal captured_wrapper
                captured_wrapper = func
                return func

            return decorator

        mock_instance.tool = capture_tool_decorator
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_stdio_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        with (
            patch(
                "ouroboros.mcp.server.adapter._OuroborosSDKServer",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            await adapter.serve(transport="stdio")

        # Verify wrapper was captured
        assert captured_wrapper is not None

        # Test: Path traversal should be rejected by input validation
        with pytest.raises(RuntimeError, match="Path traversal detected"):
            await captured_wrapper(input="../../../etc/passwd")

    @pytest.mark.asyncio
    async def test_fastmcp_wrapper_logs_entry_and_return(self) -> None:
        """FastMCP wrapper logs whether requests cross the SDK boundary."""
        from unittest.mock import MagicMock, patch

        adapter = MCPServerAdapter(name="test-server")
        adapter.register_tool(MockToolHandler(name="wrapped_tool"))

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        captured_wrapper = None

        def capture_tool_decorator(name, description):
            def decorator(func):
                nonlocal captured_wrapper
                captured_wrapper = func
                return func

            return decorator

        mock_instance.tool = capture_tool_decorator
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_stdio_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        with (
            patch(
                "ouroboros.mcp.server.adapter._OuroborosSDKServer",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            await adapter.serve(transport="stdio")

        assert captured_wrapper is not None
        with capture_logs() as logs:
            converted = await captured_wrapper(input="secret-value")

        assert converted.content[0].text == "Success"
        entry = next(
            event for event in logs if event["event"] == "mcp.server.fastmcp_tool_wrapper.entry"
        )
        returned = next(
            event for event in logs if event["event"] == "mcp.server.fastmcp_tool_wrapper.return"
        )
        assert entry["tool"] == "wrapped_tool"
        assert entry["raw_argument_keys"] == ["input"]
        assert "secret-value" not in str(entry)
        assert returned["tool"] == "wrapped_tool"
        assert returned["ok"] is True
        assert isinstance(returned["duration_ms"], int)

    @pytest.mark.asyncio
    async def test_fastmcp_wrapper_omits_unset_optional_arguments(self) -> None:
        from unittest.mock import MagicMock, patch

        class OptionalToolHandler(MockToolHandler):
            @property
            def definition(self) -> MCPToolDefinition:
                return MCPToolDefinition(
                    name=self._name,
                    description="A test tool",
                    parameters=(
                        MCPToolParameter(name="input", type=ToolInputType.STRING),
                        MCPToolParameter(
                            name="optional-input",
                            type=ToolInputType.STRING,
                            required=False,
                        ),
                    ),
                )

        adapter = MCPServerAdapter()
        handler = OptionalToolHandler(name="optional_tool")
        adapter.register_tool(handler)

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        captured_wrapper = None

        def capture_tool_decorator(name, description):
            def decorator(func):
                nonlocal captured_wrapper
                captured_wrapper = func
                return func

            return decorator

        mock_instance.tool = capture_tool_decorator
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_stdio_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        with (
            patch(
                "ouroboros.mcp.server.adapter._OuroborosSDKServer",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            await adapter.serve(transport="stdio")

        assert captured_wrapper is not None
        await captured_wrapper(input="required", optional_input=None)

        handler.handle_mock.assert_awaited_once_with({"input": "required"})

        handler.handle_mock.reset_mock()
        await captured_wrapper(input="required", optional_input="provided")
        handler.handle_mock.assert_awaited_once_with(
            {"input": "required", "optional-input": "provided"}
        )

        handler.handle_mock.reset_mock()
        await captured_wrapper(input="required")
        handler.handle_mock.assert_awaited_once_with({"input": "required"})

        real_adapter = MCPServerAdapter()
        real_handler = OptionalToolHandler(name="optional_tool")
        real_adapter.register_tool(real_handler)
        mcp_server_module = pytest.importorskip("mcp.server")

        with patch.object(
            mcp_server_module.MCPServer,
            "run_stdio_async",
            new=AsyncMock(),
        ):
            await real_adapter.serve(transport="stdio")

        await real_adapter._mcp_server.call_tool("optional_tool", {"input": "required"})

        real_handler.handle_mock.assert_awaited_once_with({"input": "required"})

    @pytest.mark.asyncio
    async def test_fastmcp_registers_base_resource_uri_template(self) -> None:
        """FastMCP path exposes child URIs for base resource handlers."""
        from unittest.mock import MagicMock, patch

        adapter = MCPServerAdapter()
        handler = MockResourceHandler("test://resource")
        adapter.register_resource(handler)

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        captured_resources: dict[str, Any] = {}

        def capture_resource_decorator(uri: str, **_kwargs: Any):
            def decorator(func):
                captured_resources[uri] = func
                return func

            return decorator

        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = capture_resource_decorator
        mock_instance.run_stdio_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        with (
            patch(
                "ouroboros.mcp.server.adapter._OuroborosSDKServer",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            await adapter.serve(transport="stdio")

        assert "test://resource" in captured_resources
        assert "test://resource/{resource_id}" in captured_resources

        text = await captured_resources["test://resource/{resource_id}"]("child")
        assert text == "Resource content"
        handler.handle_mock.assert_awaited_with("test://resource/child")

    @pytest.mark.asyncio
    async def test_stdio_rejects_auth_config_at_startup(self):
        """stdio serve() rejects auth config upfront with a clear error.

        stdio has no header to carry a credential on, so this guard prevents
        the confusing failure mode where the server starts successfully but
        then rejects every tool call at runtime. Network transports do support
        authentication -- see the network security suite.
        """
        from ouroboros.mcp.server.security import AuthConfig, AuthMethod

        # Create adapter with auth required
        auth_config = AuthConfig(
            method=AuthMethod.API_KEY,
            api_keys=frozenset(["valid-key"]),
            required=True,
        )
        adapter = MCPServerAdapter(auth_config=auth_config)

        # serve() should reject the incompatible configuration immediately
        with pytest.raises(
            ValueError,
            match="stdio transport does not support authentication",
        ):
            await adapter.serve(transport="stdio")

    @pytest.mark.asyncio
    async def test_fastmcp_allows_none_auth_with_required_true(self):
        """FastMCP allows AuthMethod.NONE even with required=True.

        This edge case verifies that required=True with method=NONE doesn't
        trigger the guard, since NONE always allows access regardless of
        the required flag.
        """
        from unittest.mock import MagicMock, patch

        from ouroboros.mcp.server.security import AuthConfig, AuthMethod

        # required=True with method=NONE should not trigger guard
        auth_config = AuthConfig(
            method=AuthMethod.NONE,
            required=True,  # Has no effect when method is NONE
        )
        adapter = MCPServerAdapter(auth_config=auth_config)
        adapter.register_tool(MockToolHandler(name="test_tool"))

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_stdio_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        with (
            patch(
                "ouroboros.mcp.server.adapter._OuroborosSDKServer",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            # Should not raise - method is NONE so guard passes
            await adapter.serve(transport="stdio")

    @pytest.mark.asyncio
    async def test_rejects_rate_limit_config_without_auth(self):
        """serve() rejects rate limiting that has no client identity to bucket by.

        Only a credential supplies that identity, so rate limiting without an
        auth method would put every caller in one shared bucket -- a false
        sense of security. With auth configured it is supported.
        """
        from ouroboros.mcp.server.security import RateLimitConfig

        adapter = MCPServerAdapter(
            rate_limit_config=RateLimitConfig(
                enabled=True,
                requests_per_minute=100,
            )
        )

        with pytest.raises(
            ValueError,
            match="Rate limiting requires client identity",
        ):
            await adapter.serve(transport="stdio")


# ── _safe_cwd helper ──────────────────────────────────────────────────


class TestSafeCwd:
    """Tests for _safe_cwd() fallback logic (issue #400)."""

    def test_returns_cwd_when_writable_and_not_root(self, tmp_path, monkeypatch):
        """Normal writable directory is returned as-is."""
        monkeypatch.chdir(tmp_path)
        assert _safe_cwd() == tmp_path

    def test_falls_back_to_home_when_cwd_is_root(self, monkeypatch):
        """When cwd is /, _safe_cwd should return Path.home()."""
        from pathlib import Path
        from unittest.mock import patch

        with patch("ouroboros.mcp.server.adapter.Path.cwd", return_value=Path("/")):
            result = _safe_cwd()
        assert result == Path.home()

    def test_falls_back_to_home_when_cwd_not_writable(self, tmp_path, monkeypatch):
        """When cwd is not writable, _safe_cwd should return Path.home()."""
        from pathlib import Path
        from unittest.mock import patch

        monkeypatch.chdir(tmp_path)
        with patch("os.access", return_value=False):
            result = _safe_cwd()
        assert result == Path.home()


# ── Factory-level create_ouroboros_server test ───────────────────────


class TestCreateOuroborosServerCwdFallback:
    """Verify create_ouroboros_server() propagates _safe_cwd() fallback to components."""

    def test_cwd_root_propagates_fallback_to_all_components(self, tmp_path):
        """When cwd=/, runtime and LLM adapters receive the fallback directory.

        This is the factory-level complement to the unit-level TestSafeCwd tests.
        """
        import contextlib
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        expected_fallback = Path.home()

        # Track calls to key dependency factories
        mock_create_runtime = MagicMock(return_value=MagicMock())
        mock_create_llm = MagicMock(return_value=MagicMock())

        mock_event_store = MagicMock()
        mock_event_store.initialize = MagicMock()

        def _mock_handler(name: str) -> MagicMock:
            return MagicMock(return_value=MagicMock(definition=MagicMock(name=name)))

        patch_targets = {
            # Force _safe_cwd to see cwd=/
            "ouroboros.mcp.server.adapter.Path.cwd": MagicMock(return_value=Path("/")),
            # Intercept the two adapters that receive cwd=
            "ouroboros.orchestrator.create_agent_runtime": mock_create_runtime,
            "ouroboros.providers.create_llm_adapter": mock_create_llm,
            "ouroboros.orchestrator.resolve_agent_runtime_backend": MagicMock(
                return_value="claude"
            ),
            # Stub heavy service classes
            "ouroboros.bigbang.interview.InterviewEngine": MagicMock(),
            "ouroboros.bigbang.seed_generator.SeedGenerator": MagicMock(),
            "ouroboros.evaluation.EvaluationPipeline": MagicMock(),
            "ouroboros.evolution.loop.EvolutionaryLoop": MagicMock(),
            "ouroboros.evolution.wonder.WonderEngine": MagicMock(),
            "ouroboros.evolution.reflect.ReflectEngine": MagicMock(),
            "ouroboros.verification.extractor.AssertionExtractor": MagicMock(),
            "ouroboros.mcp.job_manager.JobManager": MagicMock(),
            # Stub all tool handler classes
            "ouroboros.mcp.tools.definitions.ExecuteSeedHandler": _mock_handler(
                "ouroboros_execute_seed"
            ),
            "ouroboros.mcp.tools.definitions.StartExecuteSeedHandler": _mock_handler(
                "ouroboros_start_execute_seed"
            ),
            "ouroboros.mcp.tools.definitions.SessionStatusHandler": _mock_handler(
                "ouroboros_session_status"
            ),
            "ouroboros.mcp.tools.definitions.JobStatusHandler": _mock_handler(
                "ouroboros_job_status"
            ),
            "ouroboros.mcp.tools.definitions.JobWaitHandler": _mock_handler("ouroboros_job_wait"),
            "ouroboros.mcp.tools.definitions.JobResultHandler": _mock_handler(
                "ouroboros_job_result"
            ),
            "ouroboros.mcp.tools.definitions.CancelJobHandler": _mock_handler(
                "ouroboros_cancel_job"
            ),
            "ouroboros.mcp.tools.definitions.QueryEventsHandler": _mock_handler(
                "ouroboros_query_events"
            ),
            "ouroboros.mcp.tools.definitions.GenerateSeedHandler": _mock_handler(
                "ouroboros_generate_seed"
            ),
            "ouroboros.mcp.tools.definitions.MeasureDriftHandler": _mock_handler(
                "ouroboros_measure_drift"
            ),
            "ouroboros.mcp.tools.definitions.InterviewHandler": _mock_handler(
                "ouroboros_interview"
            ),
            "ouroboros.mcp.tools.definitions.EvaluateHandler": _mock_handler("ouroboros_evaluate"),
            "ouroboros.mcp.tools.definitions.LateralThinkHandler": _mock_handler(
                "ouroboros_lateral_think"
            ),
            "ouroboros.mcp.tools.definitions.EvolveStepHandler": _mock_handler(
                "ouroboros_evolve_step"
            ),
            "ouroboros.mcp.tools.definitions.StartEvolveStepHandler": _mock_handler(
                "ouroboros_start_evolve_step"
            ),
            "ouroboros.mcp.tools.definitions.StartEvaluateHandler": _mock_handler(
                "ouroboros_start_evaluate"
            ),
            "ouroboros.mcp.tools.definitions.LineageStatusHandler": _mock_handler(
                "ouroboros_lineage_status"
            ),
            "ouroboros.mcp.tools.definitions.EvolveRewindHandler": _mock_handler(
                "ouroboros_evolve_rewind"
            ),
            "ouroboros.mcp.tools.definitions.ACDashboardHandler": _mock_handler(
                "ouroboros_ac_dashboard"
            ),
            "ouroboros.mcp.tools.definitions.ACTreeHUDHandler": _mock_handler(
                "ouroboros_ac_tree_hud"
            ),
            "ouroboros.mcp.tools.definitions.CancelExecutionHandler": _mock_handler(
                "ouroboros_cancel_execution"
            ),
            "ouroboros.mcp.tools.pm_handler.PMInterviewHandler": _mock_handler(
                "ouroboros_pm_interview"
            ),
            "ouroboros.mcp.tools.brownfield_handler.BrownfieldHandler": _mock_handler(
                "ouroboros_brownfield"
            ),
            "ouroboros.mcp.tools.qa.QAHandler": _mock_handler("ouroboros_qa"),
            "ouroboros.mcp.tools.registry.ToolRegistry": MagicMock(),
            "ouroboros.config.get_clarification_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_semantic_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_wonder_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_reflect_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_assertion_extraction_model": MagicMock(return_value="test-model"),
        }

        with contextlib.ExitStack() as stack:
            for target, mock_obj in patch_targets.items():
                stack.enter_context(patch(target, mock_obj))

            from ouroboros.mcp.server.adapter import create_ouroboros_server

            create_ouroboros_server(event_store=mock_event_store)

        # 1) Runtime adapter received the fallback directory
        runtime_call = mock_create_runtime.call_args_list[0]
        assert runtime_call.kwargs["cwd"] == expected_fallback, (
            f"create_agent_runtime should receive cwd={expected_fallback}, "
            f"got {runtime_call.kwargs['cwd']}"
        )

        # 2) LLM adapter received the fallback directory
        llm_call = mock_create_llm.call_args
        assert llm_call.kwargs["cwd"] == expected_fallback, (
            f"create_llm_adapter should receive cwd={expected_fallback}, "
            f"got {llm_call.kwargs['cwd']}"
        )


class TestCreateOuroborosServerUpdateNoticeBoundary:
    """The advisory update nudge must never fail server construction (#2066)."""

    def test_metadata_failure_does_not_break_server_construction(self, monkeypatch):
        """importlib.metadata errors beyond PackageNotFoundError — corrupt
        dist-info, backend failures — stay inside the advisory seam and
        never reach create_ouroboros_server()."""
        from unittest.mock import MagicMock, patch

        from ouroboros.mcp import update_notice

        def _raise(_name: str) -> str:
            raise OSError("corrupt dist-info")

        monkeypatch.setattr(update_notice.metadata, "version", _raise)

        mock_event_store = MagicMock()
        mock_event_store.initialize = MagicMock()
        with patch("ouroboros.mcp.tools.registry.ToolRegistry", MagicMock()):
            from ouroboros.mcp.server.adapter import create_ouroboros_server

            server = create_ouroboros_server(event_store=mock_event_store)

        assert server is not None


class TestCreateOuroborosServerOpenCodeMode:
    """Verify create_ouroboros_server() threads opencode_mode to handlers."""

    def test_config_resolves_to_opencode_threads_mode_to_subagent_handlers(self):
        """Config-resolved OpenCode plugin mode reaches subagent-aware handlers."""
        import contextlib
        from unittest.mock import MagicMock, patch

        captured_modes: dict[str, list[tuple[str | None, str | None]]] = {}

        def _capture_handler(name: str) -> type:
            """Factory that records runtime backend and opencode mode."""

            class _Handler:
                def __init__(self, **kwargs):
                    mode = kwargs.get("opencode_mode")
                    backend = kwargs.get("agent_runtime_backend")
                    captured_modes.setdefault(name, []).append((backend, mode))
                    self.opencode_mode = mode
                    self.agent_runtime_backend = backend
                    self.definition = MagicMock(name=name)

            return _Handler

        mock_event_store = MagicMock()
        mock_event_store.initialize = MagicMock()

        gated_handlers = {
            "ouroboros_execute_seed": "ouroboros.mcp.tools.definitions.ExecuteSeedHandler",
            "ouroboros_start_execute_seed": "ouroboros.mcp.tools.definitions.StartExecuteSeedHandler",
            "ouroboros_generate_seed": "ouroboros.mcp.tools.definitions.GenerateSeedHandler",
            "ouroboros_interview": "ouroboros.mcp.tools.definitions.InterviewHandler",
            "ouroboros_evaluate": "ouroboros.mcp.tools.definitions.EvaluateHandler",
            "ouroboros_lateral_think": "ouroboros.mcp.tools.definitions.LateralThinkHandler",
            "ouroboros_evolve_step": "ouroboros.mcp.tools.definitions.EvolveStepHandler",
            "ouroboros_start_evolve_step": "ouroboros.mcp.tools.definitions.StartEvolveStepHandler",
            "ouroboros_ralph": "ouroboros.mcp.tools.definitions.RalphHandler",
            "ouroboros_start_ralph": "ouroboros.mcp.tools.definitions.StartRalphHandler",
            "ouroboros_pm_interview": "ouroboros.mcp.tools.fanout_composition.PMInterviewHandler",
            "ouroboros_qa": "ouroboros.mcp.tools.qa.QAHandler",
        }

        def _simple_mock_handler(name: str) -> type:
            """Non-gated handler mock."""

            class _H:
                def __init__(self, **kwargs):
                    self.definition = MagicMock(name=name)

            return _H

        patch_targets = {
            # Config resolves to opencode without runtime_backend arg
            "ouroboros.orchestrator.resolve_agent_runtime_backend": MagicMock(
                return_value="opencode"
            ),
            "ouroboros.config.get_opencode_mode": MagicMock(return_value="plugin"),
            "ouroboros.orchestrator.create_agent_runtime": MagicMock(return_value=MagicMock()),
            "ouroboros.providers.create_llm_adapter": MagicMock(return_value=MagicMock()),
            "ouroboros.bigbang.interview.InterviewEngine": MagicMock(),
            "ouroboros.bigbang.seed_generator.SeedGenerator": MagicMock(),
            "ouroboros.evaluation.EvaluationPipeline": MagicMock(),
            "ouroboros.evolution.loop.EvolutionaryLoop": MagicMock(),
            "ouroboros.evolution.wonder.WonderEngine": MagicMock(),
            "ouroboros.evolution.reflect.ReflectEngine": MagicMock(),
            "ouroboros.verification.extractor.AssertionExtractor": MagicMock(),
            "ouroboros.mcp.job_manager.JobManager": MagicMock(),
            "ouroboros.mcp.tools.definitions.SessionStatusHandler": _simple_mock_handler(
                "ouroboros_session_status"
            ),
            "ouroboros.mcp.tools.definitions.JobStatusHandler": _simple_mock_handler(
                "ouroboros_job_status"
            ),
            "ouroboros.mcp.tools.definitions.JobWaitHandler": _simple_mock_handler(
                "ouroboros_job_wait"
            ),
            "ouroboros.mcp.tools.definitions.JobResultHandler": _simple_mock_handler(
                "ouroboros_job_result"
            ),
            "ouroboros.mcp.tools.definitions.CancelJobHandler": _simple_mock_handler(
                "ouroboros_cancel_job"
            ),
            "ouroboros.mcp.tools.definitions.QueryEventsHandler": _simple_mock_handler(
                "ouroboros_query_events"
            ),
            "ouroboros.mcp.tools.definitions.MeasureDriftHandler": _simple_mock_handler(
                "ouroboros_measure_drift"
            ),
            "ouroboros.mcp.tools.definitions.LineageStatusHandler": _simple_mock_handler(
                "ouroboros_lineage_status"
            ),
            "ouroboros.mcp.tools.definitions.EvolveRewindHandler": _simple_mock_handler(
                "ouroboros_evolve_rewind"
            ),
            "ouroboros.mcp.tools.definitions.ACDashboardHandler": _simple_mock_handler(
                "ouroboros_ac_dashboard"
            ),
            "ouroboros.mcp.tools.definitions.ACTreeHUDHandler": _simple_mock_handler(
                "ouroboros_ac_tree_hud"
            ),
            "ouroboros.mcp.tools.definitions.CancelExecutionHandler": _simple_mock_handler(
                "ouroboros_cancel_execution"
            ),
            "ouroboros.mcp.tools.brownfield_handler.BrownfieldHandler": _simple_mock_handler(
                "ouroboros_brownfield"
            ),
            "ouroboros.mcp.tools.registry.ToolRegistry": MagicMock(),
            "ouroboros.config.get_clarification_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_semantic_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_wonder_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_reflect_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_assertion_extraction_model": MagicMock(return_value="test-model"),
        }

        for handler_name, patch_path in gated_handlers.items():
            patch_targets[patch_path] = _capture_handler(handler_name)

        with contextlib.ExitStack() as stack:
            for target, mock_obj in patch_targets.items():
                stack.enter_context(patch(target, mock_obj))

            from ouroboros.mcp.server.adapter import create_ouroboros_server

            create_ouroboros_server(event_store=mock_event_store)

        for name in gated_handlers:
            assert captured_modes.get(name), f"{name} was not constructed"
            assert all(backend == "opencode" for backend, _mode in captured_modes[name])
            modes = {mode for _backend, mode in captured_modes[name]}
            if name in {"ouroboros_evolve_step", "ouroboros_start_ralph"}:
                assert modes == {"plugin", None}
            else:
                assert modes == {"plugin"}


class TestCreateOuroborosServerBrownfieldStore:
    """Verify create_ouroboros_server() can share a brownfield store."""

    def test_injected_store_is_shared_with_handler_and_owned_by_server(self):
        """Shared brownfield stores should be injected and closed with the server."""
        import contextlib
        from unittest.mock import MagicMock, patch

        captured_handler_kwargs: dict[str, object] = {}

        class _BrownfieldHandler:
            def __init__(self, **kwargs):
                captured_handler_kwargs.update(kwargs)
                self.definition = MagicMock(name="ouroboros_brownfield")

        def _simple_mock_handler(name: str) -> type:
            class _H:
                def __init__(self, **kwargs):
                    self.definition = MagicMock(name=name)

            return _H

        mock_event_store = MagicMock()
        mock_event_store.initialize = MagicMock()
        mock_brownfield_store = MagicMock()

        patch_targets = {
            "ouroboros.orchestrator.resolve_agent_runtime_backend": MagicMock(
                return_value="claude"
            ),
            "ouroboros.orchestrator.create_agent_runtime": MagicMock(return_value=MagicMock()),
            "ouroboros.providers.create_llm_adapter": MagicMock(return_value=MagicMock()),
            "ouroboros.bigbang.interview.InterviewEngine": MagicMock(),
            "ouroboros.bigbang.seed_generator.SeedGenerator": MagicMock(),
            "ouroboros.evaluation.EvaluationPipeline": MagicMock(),
            "ouroboros.evolution.loop.EvolutionaryLoop": MagicMock(),
            "ouroboros.evolution.wonder.WonderEngine": MagicMock(),
            "ouroboros.evolution.reflect.ReflectEngine": MagicMock(),
            "ouroboros.verification.extractor.AssertionExtractor": MagicMock(),
            "ouroboros.mcp.job_manager.JobManager": MagicMock(),
            "ouroboros.mcp.tools.definitions.ExecuteSeedHandler": _simple_mock_handler(
                "ouroboros_execute_seed"
            ),
            "ouroboros.mcp.tools.definitions.StartExecuteSeedHandler": _simple_mock_handler(
                "ouroboros_start_execute_seed"
            ),
            "ouroboros.mcp.tools.definitions.SessionStatusHandler": _simple_mock_handler(
                "ouroboros_session_status"
            ),
            "ouroboros.mcp.tools.definitions.JobStatusHandler": _simple_mock_handler(
                "ouroboros_job_status"
            ),
            "ouroboros.mcp.tools.definitions.JobWaitHandler": _simple_mock_handler(
                "ouroboros_job_wait"
            ),
            "ouroboros.mcp.tools.definitions.JobResultHandler": _simple_mock_handler(
                "ouroboros_job_result"
            ),
            "ouroboros.mcp.tools.definitions.CancelJobHandler": _simple_mock_handler(
                "ouroboros_cancel_job"
            ),
            "ouroboros.mcp.tools.definitions.QueryEventsHandler": _simple_mock_handler(
                "ouroboros_query_events"
            ),
            "ouroboros.mcp.tools.definitions.GenerateSeedHandler": _simple_mock_handler(
                "ouroboros_generate_seed"
            ),
            "ouroboros.mcp.tools.definitions.MeasureDriftHandler": _simple_mock_handler(
                "ouroboros_measure_drift"
            ),
            "ouroboros.mcp.tools.definitions.InterviewHandler": _simple_mock_handler(
                "ouroboros_interview"
            ),
            "ouroboros.mcp.tools.definitions.EvaluateHandler": _simple_mock_handler(
                "ouroboros_evaluate"
            ),
            "ouroboros.mcp.tools.definitions.LateralThinkHandler": _simple_mock_handler(
                "ouroboros_lateral_think"
            ),
            "ouroboros.mcp.tools.definitions.EvolveStepHandler": _simple_mock_handler(
                "ouroboros_evolve_step"
            ),
            "ouroboros.mcp.tools.definitions.StartEvolveStepHandler": _simple_mock_handler(
                "ouroboros_start_evolve_step"
            ),
            "ouroboros.mcp.tools.definitions.StartEvaluateHandler": _simple_mock_handler(
                "ouroboros_start_evaluate"
            ),
            "ouroboros.mcp.tools.definitions.LineageStatusHandler": _simple_mock_handler(
                "ouroboros_lineage_status"
            ),
            "ouroboros.mcp.tools.definitions.EvolveRewindHandler": _simple_mock_handler(
                "ouroboros_evolve_rewind"
            ),
            "ouroboros.mcp.tools.definitions.ACDashboardHandler": _simple_mock_handler(
                "ouroboros_ac_dashboard"
            ),
            "ouroboros.mcp.tools.definitions.ACTreeHUDHandler": _simple_mock_handler(
                "ouroboros_ac_tree_hud"
            ),
            "ouroboros.mcp.tools.definitions.CancelExecutionHandler": _simple_mock_handler(
                "ouroboros_cancel_execution"
            ),
            "ouroboros.mcp.tools.pm_handler.PMInterviewHandler": _simple_mock_handler(
                "ouroboros_pm_interview"
            ),
            "ouroboros.mcp.tools.brownfield_handler.BrownfieldHandler": _BrownfieldHandler,
            "ouroboros.mcp.tools.qa.QAHandler": _simple_mock_handler("ouroboros_qa"),
            "ouroboros.mcp.tools.registry.ToolRegistry": MagicMock(),
            "ouroboros.config.get_opencode_mode": MagicMock(return_value="subprocess"),
            "ouroboros.config.get_clarification_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_semantic_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_wonder_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_reflect_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_assertion_extraction_model": MagicMock(return_value="test-model"),
        }

        with contextlib.ExitStack() as stack:
            for target, mock_obj in patch_targets.items():
                stack.enter_context(patch(target, mock_obj))

            from ouroboros.mcp.server.adapter import create_ouroboros_server

            server = create_ouroboros_server(
                event_store=mock_event_store,
                brownfield_store=mock_brownfield_store,
            )

        assert captured_handler_kwargs["_store"] is mock_brownfield_store
        assert isinstance(server._owned_resources[0], ControlBus)
        assert server._owned_resources[1:] == [mock_event_store, mock_brownfield_store]


def test_create_ouroboros_server_retains_runtime_context() -> None:
    """The composition root must keep AgentRuntimeContext reachable after return."""
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(runtime_backend="codex", llm_backend="claude_code")

    assert server.runtime_context is not None
    assert server.runtime_context.runtime_backend == "codex"
    assert server.runtime_context.llm_backend == "claude_code"
    assert server.runtime_context.control is not None


@pytest.mark.asyncio
async def test_server_shutdown_drains_runtime_control_bus() -> None:
    """Server-owned ControlBus must not leave subscriber tasks behind."""
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(runtime_backend="codex", llm_backend="claude_code")
    assert server.runtime_context is not None
    bus = server.runtime_context.control
    assert bus is not None
    bus._close_timeout_s = 0.01

    started = asyncio.Event()

    async def blocked(_event: BaseEvent) -> None:
        started.set()
        await asyncio.sleep(60)

    bus.subscribe(lambda _event: True, blocked)
    tasks = bus.publish(
        BaseEvent(
            type="control.directive.emitted",
            aggregate_type="lineage",
            aggregate_id="lin_shutdown_probe",
            data={"directive": "cancel"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    await server.shutdown()

    assert tasks[0].cancelled()
    assert bus._tasks == set()
    assert server._owned_resources == []


@pytest.mark.asyncio
async def test_server_shutdown_stops_before_dependents_when_control_bus_refuses_drain() -> None:
    """Do not close dependent resources while control subscribers are still live."""
    server = MCPServerAdapter()
    bus = ControlBus(_close_timeout_s=0.01, _cancel_timeout_s=0.01)
    started = asyncio.Event()
    release = asyncio.Event()

    class _DependentResource:
        closed = False

        async def close(self) -> None:
            self.closed = True

    resource = _DependentResource()

    async def stubborn(_event: BaseEvent) -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await release.wait()

    server.register_owned_resource(bus)
    server.register_owned_resource(resource)
    bus.subscribe(lambda _event: True, stubborn)
    tasks = bus.publish(
        BaseEvent(
            type="control.directive.emitted",
            aggregate_type="lineage",
            aggregate_id="lin_shutdown_probe",
            data={"directive": "cancel"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    with pytest.raises(ControlBusDrainError):
        await asyncio.wait_for(server.shutdown(), timeout=0.5)

    assert resource.closed is False

    release.set()
    await asyncio.wait_for(tasks[0], timeout=0.5)


# --------------------------------------------------------------------------- #
# Fan-out re-entry reachability on the shipped server (#1754)
# --------------------------------------------------------------------------- #


def test_composition_root_registers_fanout_reentry_tool() -> None:
    """The re-entry tool must exist on the server the CLI actually builds.

    This asserts against ``create_ouroboros_server`` rather than
    ``get_ouroboros_tools`` deliberately. Both factories build tool sets, only
    the first one ships, and for a long time only the second one wired the
    fan-out — so a full unit suite passed while the primary MCP surface had no
    ``ouroboros_submit_fanout_results`` at all and stamped no ``fanout_id``.
    A test aimed at the correct factory could never have caught that.
    """
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(name="fanout-reentry-probe")

    assert "ouroboros_submit_fanout_results" in {tool.name for tool in server.info.tools}


def test_composition_root_shares_one_fanout_registry() -> None:
    """Producers and the re-entry tool must observe the same registry.

    A fan-out registered by the interview handler is redeemed through the
    submit handler; separate registry instances would make every submission
    report ``unknown_fanout_id`` while every individual handler looked fine in
    isolation.
    """
    from ouroboros.mcp.server.adapter import create_ouroboros_server
    from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
    from ouroboros.mcp.tools.evaluation_handlers import (
        LateralThinkHandler,
        SubmitFanoutResultsHandler,
    )

    server = create_ouroboros_server(name="fanout-registry-probe")
    handlers = list(server._tool_handlers.values())

    registries = {
        id(handler.fanout_registry)
        for handler in handlers
        if isinstance(handler, (InterviewHandler, LateralThinkHandler, SubmitFanoutResultsHandler))
        and handler.fanout_registry is not None
    }
    assert len(registries) == 1, "producers and the submit tool must share one registry"


def test_composition_root_builds_the_registry_at_its_final_directory(tmp_path) -> None:
    """No mutable re-root on the path that ships.

    This root resolves the state dir long before it builds the registry, so the
    registry can be constructed where its records will live. Leaving it default
    -located and re-rooting later is what let a producer register into one
    directory and have lookups moved to another.
    """
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(name="fanout-dir-probe", state_dir=tmp_path)
    handler = server._tool_handlers["ouroboros_submit_fanout_results"]

    assert handler.fanout_registry is not None
    assert handler.fanout_registry.directory == tmp_path / "fanout"


@pytest.mark.asyncio
async def test_production_fanout_returns_only_disposable_envelope(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay is input-exact while every terminal child body stays artifact-only."""
    from ouroboros.core.disposable_memory import DisposableResultEnvelope
    from ouroboros.mcp.server import adapter as adapter_module
    from ouroboros.mcp.tools import fanout_handler
    from ouroboros.mcp.tools.fanout import FANOUT_KIND_QUESTION_ADVISORY

    launcher = tmp_path / "launcher"
    project = tmp_path / "runtime-project"
    launcher.mkdir()
    project.mkdir()
    monkeypatch.chdir(launcher)
    event_store = EventStore(f"sqlite+aiosqlite:///{project / 'events.db'}")
    server = adapter_module.create_ouroboros_server(
        name="fanout-disposable-probe",
        event_store=event_store,
        state_dir=project / "state",
        project_dir=project,
    )
    handler = server._tool_handlers["ouroboros_submit_fanout_results"]
    fetch_handler = server._tool_handlers["ouroboros_fetch_artifact"]
    assert handler.disposable_memory is not None
    assert handler.disposable_memory.artifact_store.root == (
        project.resolve() / ".ouroboros" / "artifacts"
    )
    registry = handler.fanout_registry
    assert registry is not None
    fanout_id = registry.register(
        kind=FANOUT_KIND_QUESTION_ADVISORY,
        session_id="session-disposable",
        correlation_key="context.lane_id",
        expected_keys=["code_context"],
        synthesizer_input={"lane_ids": ["code_context"]},
        required_keys=["code_context"],
    )
    assert fanout_id is not None
    marker = "large-child-body:" + ("x" * 900_000)
    arguments = {
        "session_id": "session-disposable",
        "fanout_id": fanout_id,
        "correlation_key": "context.lane_id",
        "results": [{"key": "code_context", "content": marker}],
    }
    synthesis_calls = 0
    original_synthesize = fanout_handler.synthesize_fanout_results

    def tracked_synthesize(prepared):
        nonlocal synthesis_calls
        synthesis_calls += 1
        return original_synthesize(prepared)

    monkeypatch.setattr(fanout_handler, "synthesize_fanout_results", tracked_synthesize)
    try:
        first = await handler.handle(arguments)
        second = await handler.handle(arguments)
        assert first.is_ok and second.is_ok
        first_result = first.unwrap()
        second_result = second.unwrap()
        envelope = DisposableResultEnvelope.model_validate(first_result.meta)

        assert second_result.meta == first_result.meta
        assert synthesis_calls == 1
        assert len(json.dumps(first_result.meta).encode("utf-8")) < 4 * 1024
        assert marker not in first_result.content[0].text
        assert marker not in json.dumps(first_result.meta)

        fetched = await fetch_handler.handle({"contract_id": envelope.contract_id})
        assert fetched.is_ok
        fetched_body = fetched.unwrap().meta["body"]
        assert marker in json.dumps(fetched_body)
        assert fetched_body["status"] == "complete"
        events = await event_store.replay("contract", envelope.contract_id)
        assert len(events) == 1
        assert marker not in json.dumps(events[0].data)

        changed_marker = "changed-child-body:" + ("y" * 900_000)
        changed = await handler.handle(
            {
                **arguments,
                "results": [{"key": "code_context", "content": changed_marker}],
            }
        )
        assert changed.is_ok
        changed_result = changed.unwrap()
        changed_envelope = DisposableResultEnvelope.model_validate(changed_result.meta)

        assert changed_envelope.contract_id != envelope.contract_id
        assert synthesis_calls == 2
        assert len(json.dumps(changed_result.meta).encode("utf-8")) < 4 * 1024
        assert changed_marker not in changed_result.content[0].text
        assert changed_marker not in json.dumps(changed_result.meta)

        changed_fetched = handler.disposable_memory.fetch(changed_envelope.contract_id)
        assert changed_marker in json.dumps(changed_fetched.body)
        assert marker not in json.dumps(changed_fetched.body)
        assert marker in json.dumps(handler.disposable_memory.fetch(envelope.contract_id).body)
        changed_events = await event_store.replay("contract", changed_envelope.contract_id)
        assert len(changed_events) == 1
        assert changed_marker not in json.dumps(changed_events[0].data)
    finally:
        await server.shutdown()


@pytest.mark.asyncio
async def test_production_fanout_surfaces_owned_store_startup_failure_before_work(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production readiness boundary fails before synthesis or publication."""
    from ouroboros.mcp.server import adapter as adapter_module
    from ouroboros.mcp.tools import fanout_handler
    from ouroboros.mcp.tools.fanout import FANOUT_KIND_QUESTION_ADVISORY

    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    server = adapter_module.create_ouroboros_server(
        name="fanout-startup-failure-probe",
        event_store=event_store,
        state_dir=tmp_path / "state",
        project_dir=tmp_path,
    )
    handler = server._tool_handlers["ouroboros_submit_fanout_results"]
    registry = handler.fanout_registry
    assert registry is not None
    fanout_id = registry.register(
        kind=FANOUT_KIND_QUESTION_ADVISORY,
        session_id="session-startup-failure",
        correlation_key="context.lane_id",
        expected_keys=["code_context"],
        synthesizer_input={"lane_ids": ["code_context"]},
        required_keys=["code_context"],
    )
    assert fanout_id is not None
    synthesize = AsyncMock()
    monkeypatch.setattr(fanout_handler, "synthesize_fanout_results", synthesize)
    initialize = AsyncMock(side_effect=RuntimeError("event store startup failed"))
    monkeypatch.setattr(event_store, "initialize", initialize)

    try:
        result = await handler.handle(
            {
                "session_id": "session-startup-failure",
                "fanout_id": fanout_id,
                "correlation_key": "context.lane_id",
                "results": [{"key": "code_context", "content": "child output"}],
            }
        )

        assert result.is_err
        assert "event store startup failed" in str(result.error)
        initialize.assert_awaited_once()
        synthesize.assert_not_awaited()
    finally:
        await server.shutdown()


@pytest.mark.asyncio
async def test_production_fanout_does_not_initialize_custom_store(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production ownership must not weaken custom-store durable fail-fast."""
    from ouroboros.mcp.server import adapter as adapter_module
    from ouroboros.mcp.tools import fanout_handler
    from ouroboros.mcp.tools.fanout import FANOUT_KIND_QUESTION_ADVISORY

    class _CustomStore:
        initialize_calls = 0
        close_calls = 0

        async def initialize(self) -> None:
            self.initialize_calls += 1

        async def append_durable(self, _event: BaseEvent, *, timeout: float) -> None:
            del timeout
            raise RuntimeError("custom store remains uninitialized")

        async def close(self) -> None:
            self.close_calls += 1

    custom_store = _CustomStore()
    server = adapter_module.create_ouroboros_server(
        name="fanout-custom-store-probe",
        event_store=custom_store,
        state_dir=tmp_path / "state",
        project_dir=tmp_path,
    )
    handler = server._tool_handlers["ouroboros_submit_fanout_results"]
    registry = handler.fanout_registry
    assert registry is not None
    fanout_id = registry.register(
        kind=FANOUT_KIND_QUESTION_ADVISORY,
        session_id="session-custom-store",
        correlation_key="context.lane_id",
        expected_keys=["code_context"],
        synthesizer_input={"lane_ids": ["code_context"]},
        required_keys=["code_context"],
    )
    assert fanout_id is not None
    synthesize = AsyncMock()
    monkeypatch.setattr(fanout_handler, "synthesize_fanout_results", synthesize)

    try:
        result = await handler.handle(
            {
                "session_id": "session-custom-store",
                "fanout_id": fanout_id,
                "correlation_key": "context.lane_id",
                "results": [{"key": "code_context", "content": "child output"}],
            }
        )

        assert result.is_err
        assert "custom store remains uninitialized" in str(result.error)
        assert custom_store.initialize_calls == 0
        synthesize.assert_not_awaited()
    finally:
        await server.shutdown()

    assert custom_store.close_calls == 1
