"""Tests for the numeric-free ambiguity evidence ledger."""

import json

import pytest

from ouroboros.bigbang.ambiguity_evidence import (
    AmbiguityEvidenceEntry,
    AmbiguityEvidenceLedger,
    AmbiguityEvidenceSourceKind,
    AmbiguityEvidenceStatus,
    EvidenceConflict,
    EvidenceSource,
    parse_ambiguity_evidence_ledger,
)


def _source(quote: str = "The command must run offline.") -> EvidenceSource:
    return EvidenceSource(
        kind=AmbiguityEvidenceSourceKind.INTERVIEW_ANSWER,
        quote=quote,
    )


def _entry(
    *,
    dimension: str = "constraints",
    field: str = "network_access",
    status: AmbiguityEvidenceStatus = AmbiguityEvidenceStatus.CONFIRMED,
) -> AmbiguityEvidenceEntry:
    return AmbiguityEvidenceEntry(
        dimension=dimension,
        field=field,
        status=status,
        source=None if status is AmbiguityEvidenceStatus.MISSING else _source(),
    )


class TestAmbiguityEvidenceLedger:
    def test_canonical_json_is_stable_when_model_entries_arrive_in_different_order(self) -> None:
        goal = _entry(dimension="goal", field="deliverable")
        constraint = _entry()

        first = AmbiguityEvidenceLedger(entries=(constraint, goal))
        second = AmbiguityEvidenceLedger(entries=(goal, constraint))

        assert first.entries == (constraint, goal)
        assert first.canonical_json() == second.canonical_json()
        assert first.canonical_json() == (
            '{"entries":[{"conflict":null,"dimension":"constraints",'
            '"field":"network_access","source":{"kind":"interview_answer",'
            '"quote":"The command must run offline."},"status":"confirmed"},'
            '{"conflict":null,"dimension":"goal","field":"deliverable",'
            '"source":{"kind":"interview_answer","quote":"The command must run offline."},'
            '"status":"confirmed"}]}'
        )

    def test_conflicting_evidence_records_both_quotes(self) -> None:
        entry = AmbiguityEvidenceEntry(
            dimension="scope",
            field="platform",
            status=AmbiguityEvidenceStatus.CONFLICTING,
            source=_source("Ship macOS only."),
            conflict=EvidenceConflict(
                source=_source("Ship Windows and macOS."),
                explanation="The supported platforms differ.",
            ),
        )

        assert entry.conflict is not None
        assert entry.conflict.source.quote == "Ship Windows and macOS."


class TestAmbiguityEvidenceProvenance:
    @pytest.mark.parametrize(
        ("status", "source"),
        [
            (AmbiguityEvidenceStatus.CONFIRMED, None),
            (AmbiguityEvidenceStatus.INFERRED, None),
            (AmbiguityEvidenceStatus.MISSING, _source()),
        ],
    )
    def test_rejects_missing_or_invalid_provenance(
        self,
        status: AmbiguityEvidenceStatus,
        source: EvidenceSource | None,
    ) -> None:
        with pytest.raises(ValueError):
            AmbiguityEvidenceEntry(
                dimension="goal",
                field="outcome",
                status=status,
                source=source,
            )

    def test_rejects_blank_source_quote(self) -> None:
        with pytest.raises(ValueError, match="source quote"):
            EvidenceSource(
                kind=AmbiguityEvidenceSourceKind.INTERVIEW_ANSWER,
                quote="   ",
            )

    def test_rejects_conflict_without_distinct_conflicting_source(self) -> None:
        source = _source()
        with pytest.raises(ValueError, match="conflict source"):
            AmbiguityEvidenceEntry(
                dimension="scope",
                field="platform",
                status=AmbiguityEvidenceStatus.CONFLICTING,
                source=source,
                conflict=EvidenceConflict(
                    source=source,
                    explanation="Contradiction.",
                ),
            )


class TestAmbiguityEvidenceParser:
    def test_rejects_model_supplied_final_numeric_ambiguity(self) -> None:
        payload = {
            "entries": [
                {
                    "dimension": "goal",
                    "field": "outcome",
                    "status": "confirmed",
                    "source": {
                        "kind": "interview_answer",
                        "quote": "Generate a release report.",
                    },
                }
            ],
            "ambiguity_score": 0.01,
        }

        with pytest.raises(ValueError, match="ambiguity_score"):
            parse_ambiguity_evidence_ledger(json.dumps(payload))

    def test_rejects_invalid_source_kind(self) -> None:
        payload = {
            "entries": [
                {
                    "dimension": "goal",
                    "field": "outcome",
                    "status": "confirmed",
                    "source": {
                        "kind": "model_assertion",
                        "quote": "Generate a release report.",
                    },
                }
            ]
        }

        with pytest.raises(ValueError, match="model_assertion"):
            parse_ambiguity_evidence_ledger(json.dumps(payload))
