"""Unit tests for the projection record vocabulary.

Covers the contract from issue #946 PR-1a:

* Record models are immutable (Pydantic ``frozen=True``).
* ID factories produce prefixed identifiers.
* ``StepRecord`` rejects empty ``source_event_ids`` unless
  ``legacy_inferred`` is True.
* ``VerdictRecord`` enforces the ``scope`` ↔ ``ac_id`` invariant.
* Timestamp invariants reject ``ended_at < started_at``.
* Schema-version field defaults to ``PROJECTION_SCHEMA_VERSION``.
* ``metadata`` is read-only at runtime (blocks
  ``record.metadata[key] = value`` mutation).
* Identifier-tuple fields reject blank or whitespace-only entries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, cast

from pydantic import ValidationError
import pytest

from ouroboros.harness.projection import (
    PROJECTION_SCHEMA_VERSION,
    ArtifactRecord,
    RunRecord,
    StageKind,
    StageRecord,
    StepKind,
    StepRecord,
    VerdictOutcome,
    VerdictRecord,
)


def _now() -> datetime:
    return datetime.now(UTC)


class TestProjectionSchemaVersion:
    """Sanity checks for the version constant exported by the module."""

    def test_initial_version_is_one(self) -> None:
        assert PROJECTION_SCHEMA_VERSION == 1


class TestRunRecord:
    """Tests for ``RunRecord``."""

    def test_generates_prefixed_id(self) -> None:
        record = RunRecord(seed_id="seed_abc")
        assert record.run_id.startswith("run_")
        assert len(record.run_id) > len("run_")

    def test_default_schema_version(self) -> None:
        record = RunRecord(seed_id="seed_abc")
        assert record.schema_version == PROJECTION_SCHEMA_VERSION

    def test_rejects_unsupported_schema_version(self) -> None:
        with pytest.raises(ValidationError):
            RunRecord(seed_id="seed_abc", schema_version=2)

    def test_is_frozen(self) -> None:
        record = RunRecord(seed_id="seed_abc")
        with pytest.raises(ValidationError):
            record.seed_id = "seed_other"  # type: ignore[misc]

    def test_rejects_ended_before_started(self) -> None:
        start = _now()
        with pytest.raises(ValidationError):
            RunRecord(
                seed_id="seed_abc",
                started_at=start,
                ended_at=start - timedelta(seconds=1),
            )

    def test_seed_id_is_required(self) -> None:
        with pytest.raises(ValidationError):
            RunRecord()  # type: ignore[call-arg]

    def test_stage_ids_default_empty_tuple(self) -> None:
        record = RunRecord(seed_id="seed_abc")
        assert record.stage_ids == ()


class TestStageRecord:
    """Tests for ``StageRecord``."""

    def test_generates_prefixed_id(self) -> None:
        record = StageRecord(run_id="run_1", kind=StageKind.EXECUTE)
        assert record.stage_id.startswith("stage_")

    def test_kind_round_trips_through_enum(self) -> None:
        record = StageRecord(run_id="run_1", kind="evaluate")  # type: ignore[arg-type]
        assert record.kind is StageKind.EVALUATE

    def test_step_ids_default_empty(self) -> None:
        record = StageRecord(run_id="run_1", kind=StageKind.EXECUTE)
        assert record.step_ids == ()

    def test_rejects_ended_before_started(self) -> None:
        start = _now()
        with pytest.raises(ValidationError):
            StageRecord(
                run_id="run_1",
                kind=StageKind.EXECUTE,
                started_at=start,
                ended_at=start - timedelta(seconds=1),
            )


class TestStepRecord:
    """Tests for ``StepRecord`` including the source-event invariant."""

    def test_requires_source_event_or_legacy_flag(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(run_id="run_1", stage_id="stage_1", kind=StepKind.TOOL_CALL)

    def test_accepts_with_source_event_ids(self) -> None:
        record = StepRecord(
            run_id="run_1",
            stage_id="stage_1",
            kind=StepKind.TOOL_CALL,
            source_event_ids=("evt_1",),
        )
        assert record.source_event_ids == ("evt_1",)
        assert record.legacy_inferred is False

    def test_accepts_when_legacy_inferred(self) -> None:
        record = StepRecord(
            run_id="run_1",
            stage_id="stage_1",
            kind=StepKind.SHELL_COMMAND,
            legacy_inferred=True,
        )
        assert record.source_event_ids == ()
        assert record.legacy_inferred is True

    def test_rejects_ended_before_started(self) -> None:
        start = _now()
        with pytest.raises(ValidationError):
            StepRecord(
                run_id="run_1",
                stage_id="stage_1",
                kind=StepKind.TOOL_CALL,
                source_event_ids=("evt_1",),
                started_at=start,
                ended_at=start - timedelta(seconds=1),
            )

    def test_is_frozen(self) -> None:
        record = StepRecord(
            run_id="run_1",
            stage_id="stage_1",
            kind=StepKind.TOOL_CALL,
            source_event_ids=("evt_1",),
        )
        with pytest.raises(ValidationError):
            record.ok = True  # type: ignore[misc]


class TestArtifactRecord:
    """Tests for ``ArtifactRecord``."""

    def test_generates_prefixed_id(self) -> None:
        record = ArtifactRecord(step_id="step_1", kind="file", path="/tmp/x.txt")
        assert record.artifact_id.startswith("artifact_")

    def test_kind_must_not_be_blank(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactRecord(step_id="step_1", kind="   ", path="/tmp/x.txt")

    def test_kind_is_stripped(self) -> None:
        record = ArtifactRecord(step_id="step_1", kind="  patch  ")
        assert record.kind == "patch"

    def test_optional_fields_default_none(self) -> None:
        record = ArtifactRecord(step_id="step_1", kind="file")
        assert record.path is None
        assert record.media_type is None
        assert record.size_bytes is None
        assert record.digest is None
        assert record.summary == ""

    def test_size_bytes_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactRecord(step_id="step_1", kind="file", size_bytes=-1)


class TestVerdictRecord:
    """Tests for ``VerdictRecord`` scope invariants."""

    def test_run_scope_rejects_ac_id(self) -> None:
        with pytest.raises(ValidationError):
            VerdictRecord(
                run_id="run_1",
                scope="run",
                ac_id="ac_1",
                outcome=VerdictOutcome.PASS,
            )

    def test_ac_scope_requires_ac_id(self) -> None:
        with pytest.raises(ValidationError):
            VerdictRecord(
                run_id="run_1",
                scope="ac",
                outcome=VerdictOutcome.PASS,
            )

    def test_ac_scope_with_ac_id_accepted(self) -> None:
        record = VerdictRecord(
            run_id="run_1",
            scope="ac",
            ac_id="ac_1",
            outcome=VerdictOutcome.PASS,
            evidence_event_ids=("evt_1", "evt_2"),
        )
        assert record.scope == "ac"
        assert record.outcome is VerdictOutcome.PASS
        assert record.evidence_event_ids == ("evt_1", "evt_2")

    def test_run_scope_without_ac_id_accepted(self) -> None:
        record = VerdictRecord(
            run_id="run_1",
            scope="run",
            outcome=VerdictOutcome.FAIL,
        )
        assert record.ac_id is None

    def test_is_frozen(self) -> None:
        record = VerdictRecord(
            run_id="run_1",
            scope="run",
            outcome=VerdictOutcome.PASS,
        )
        with pytest.raises(ValidationError):
            record.outcome = VerdictOutcome.FAIL  # type: ignore[misc]


class TestEnumerations:
    """Quick coverage to lock the publicly exported enum values."""

    def test_stage_kind_values(self) -> None:
        assert {kind.value for kind in StageKind} == {
            "interview",
            "seed",
            "execute",
            "evaluate",
            "evolve",
            "plugin",
            "hitl",
        }

    def test_step_kind_values(self) -> None:
        assert {kind.value for kind in StepKind} == {
            "model_call",
            "tool_call",
            "shell_command",
            "subagent_dispatch",
            "plugin_command",
            "evaluation_check",
            "evidence_submission",
            "harness_internal",
        }

    def test_verdict_outcome_values(self) -> None:
        assert {outcome.value for outcome in VerdictOutcome} == {
            "pass",
            "fail",
            "escalate_human",
            "cancelled",
            "unknown",
        }


class TestMetadataIsRuntimeImmutable:
    """``metadata`` mapping fields must reject in-place mutation.

    ``frozen=True`` only blocks attribute reassignment on the model
    itself; we additionally wrap ``metadata`` in a ``MappingProxyType``
    view so consumers cannot quietly poison cached projections through
    ``record.metadata[key] = value``.
    """

    def test_run_record_metadata_is_mapping_proxy(self) -> None:
        record = RunRecord(seed_id="seed_abc", metadata={"k": "v"})
        assert isinstance(record.metadata, MappingProxyType)

    def test_run_record_metadata_blocks_setitem(self) -> None:
        record = RunRecord(seed_id="seed_abc", metadata={"k": "v"})
        with pytest.raises(TypeError):
            record.metadata["new"] = "value"  # type: ignore[index]

    def test_run_record_metadata_blocks_pop(self) -> None:
        record = RunRecord(seed_id="seed_abc", metadata={"k": "v"})
        with pytest.raises((AttributeError, TypeError)):
            record.metadata.pop("k")  # type: ignore[attr-defined]

    def test_stage_record_metadata_blocks_setitem(self) -> None:
        record = StageRecord(run_id="run_1", kind=StageKind.EXECUTE)
        with pytest.raises(TypeError):
            record.metadata["new"] = "value"  # type: ignore[index]

    def test_step_record_metadata_blocks_setitem(self) -> None:
        record = StepRecord(
            run_id="run_1",
            stage_id="stage_1",
            kind=StepKind.TOOL_CALL,
            source_event_ids=("evt_1",),
        )
        with pytest.raises(TypeError):
            record.metadata["new"] = "value"  # type: ignore[index]

    def test_artifact_record_metadata_blocks_setitem(self) -> None:
        record = ArtifactRecord(step_id="step_1", kind="file")
        with pytest.raises(TypeError):
            record.metadata["new"] = "value"  # type: ignore[index]

    def test_verdict_record_metadata_blocks_setitem(self) -> None:
        record = VerdictRecord(
            run_id="run_1",
            scope="run",
            outcome=VerdictOutcome.PASS,
        )
        with pytest.raises(TypeError):
            record.metadata["new"] = "value"  # type: ignore[index]

    def test_metadata_round_trips_through_model_dump(self) -> None:
        record = RunRecord(seed_id="seed_abc", metadata={"k": "v", "n": 1})
        dumped = record.model_dump()
        assert isinstance(dumped["metadata"], dict)
        assert dumped["metadata"] == {"k": "v", "n": 1}

    def test_metadata_copies_existing_mapping_proxy(self) -> None:
        backing = {"k": "v"}
        record = RunRecord(seed_id="seed_abc", metadata=MappingProxyType(backing))

        backing["k"] = "tampered"

        assert record.metadata["k"] == "v"

    def test_nested_metadata_is_copied_and_frozen(self) -> None:
        backing: dict[str, Any] = {"nested": {"k": "v"}, "items": ["a"]}
        record = RunRecord(seed_id="seed_abc", metadata=backing)

        backing["nested"]["k"] = "tampered"
        backing["items"].append("b")

        nested = cast(dict[str, Any], record.metadata["nested"])
        assert nested["k"] == "v"
        with pytest.raises(TypeError):
            nested["k"] = "tampered"
        assert record.metadata["items"] == ("a",)
        assert record.model_dump()["metadata"] == {"nested": {"k": "v"}, "items": ["a"]}

    def test_metadata_rejects_non_mapping_input(self) -> None:
        with pytest.raises(ValidationError):
            RunRecord(seed_id="seed_abc", metadata=[("k", "v")])  # type: ignore[arg-type]


class TestIdentifierTupleRejectsBlanks:
    """Tuple-of-identifier fields must reject empty or whitespace-only
    entries so cross-record references stay usable.
    """

    def test_step_record_rejects_blank_source_event_id(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(
                run_id="run_1",
                stage_id="stage_1",
                kind=StepKind.TOOL_CALL,
                source_event_ids=("   ",),
            )

    def test_step_record_rejects_empty_source_event_id(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(
                run_id="run_1",
                stage_id="stage_1",
                kind=StepKind.TOOL_CALL,
                source_event_ids=("",),
            )

    def test_step_record_rejects_blank_artifact_id(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(
                run_id="run_1",
                stage_id="stage_1",
                kind=StepKind.TOOL_CALL,
                source_event_ids=("evt_1",),
                artifact_ids=("   ",),
            )

    def test_step_record_trims_identifier_whitespace(self) -> None:
        record = StepRecord(
            run_id="run_1",
            stage_id="stage_1",
            kind=StepKind.TOOL_CALL,
            source_event_ids=("  evt_1  ",),
        )
        assert record.source_event_ids == ("evt_1",)

    def test_stage_record_rejects_blank_step_id(self) -> None:
        with pytest.raises(ValidationError):
            StageRecord(
                run_id="run_1",
                kind=StageKind.EXECUTE,
                step_ids=("",),
            )

    def test_run_record_rejects_blank_stage_id(self) -> None:
        with pytest.raises(ValidationError):
            RunRecord(seed_id="seed_abc", stage_ids=("   ",))

    def test_run_record_rejects_blank_verdict_id(self) -> None:
        with pytest.raises(ValidationError):
            RunRecord(seed_id="seed_abc", verdict_id="   ")

    def test_verdict_rejects_blank_evidence_event_id(self) -> None:
        with pytest.raises(ValidationError):
            VerdictRecord(
                run_id="run_1",
                scope="run",
                outcome=VerdictOutcome.PASS,
                evidence_event_ids=("",),
            )

    def test_verdict_rejects_blank_ac_id_when_scope_ac(self) -> None:
        # Blank ``ac_id`` should be rejected even though scope='ac' would
        # otherwise allow a non-None value.
        with pytest.raises(ValidationError):
            VerdictRecord(
                run_id="run_1",
                scope="ac",
                ac_id="   ",
                outcome=VerdictOutcome.PASS,
            )

    def test_step_record_rejects_blank_ac_id(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(
                run_id="run_1",
                stage_id="stage_1",
                kind=StepKind.TOOL_CALL,
                source_event_ids=("evt_1",),
                ac_id="   ",
            )


class TestScalarIdentifiersRejectBlanks:
    """Required scalar IDs must follow the same hygiene as identifier tuples."""

    def test_artifact_record_rejects_blank_step_id(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactRecord(step_id="   ", kind="file")

    def test_artifact_record_rejects_blank_overridden_artifact_id(self) -> None:
        with pytest.raises(ValidationError):
            ArtifactRecord(artifact_id="   ", step_id="step_1", kind="file")

    def test_step_record_rejects_blank_run_id(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(
                run_id="   ",
                stage_id="stage_1",
                kind=StepKind.TOOL_CALL,
                source_event_ids=("evt_1",),
            )

    def test_step_record_rejects_blank_stage_id(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(
                run_id="run_1",
                stage_id="   ",
                kind=StepKind.TOOL_CALL,
                source_event_ids=("evt_1",),
            )

    def test_step_record_rejects_blank_overridden_step_id(self) -> None:
        with pytest.raises(ValidationError):
            StepRecord(
                step_id="   ",
                run_id="run_1",
                stage_id="stage_1",
                kind=StepKind.TOOL_CALL,
                source_event_ids=("evt_1",),
            )

    def test_stage_record_rejects_blank_run_id(self) -> None:
        with pytest.raises(ValidationError):
            StageRecord(run_id="   ", kind=StageKind.EXECUTE)

    def test_stage_record_rejects_blank_overridden_stage_id(self) -> None:
        with pytest.raises(ValidationError):
            StageRecord(stage_id="   ", run_id="run_1", kind=StageKind.EXECUTE)

    def test_verdict_record_rejects_blank_run_id(self) -> None:
        with pytest.raises(ValidationError):
            VerdictRecord(run_id="   ", scope="run", outcome=VerdictOutcome.PASS)

    def test_verdict_record_rejects_blank_overridden_verdict_id(self) -> None:
        with pytest.raises(ValidationError):
            VerdictRecord(
                verdict_id="   ",
                run_id="run_1",
                scope="run",
                outcome=VerdictOutcome.PASS,
            )

    def test_run_record_rejects_blank_seed_id(self) -> None:
        with pytest.raises(ValidationError):
            RunRecord(seed_id="   ")

    def test_run_record_rejects_blank_overridden_run_id(self) -> None:
        with pytest.raises(ValidationError):
            RunRecord(run_id="   ", seed_id="seed_abc")

    def test_scalar_identifiers_are_trimmed(self) -> None:
        record = StepRecord(
            run_id="  run_1  ",
            stage_id="  stage_1  ",
            kind=StepKind.TOOL_CALL,
            source_event_ids=("evt_1",),
        )
        assert record.run_id == "run_1"
        assert record.stage_id == "stage_1"
