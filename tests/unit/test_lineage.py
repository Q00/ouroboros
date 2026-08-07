def test_rewind_to_retains_directives_for_discarded_generation_audit() -> None:
    from datetime import UTC, datetime

    from ouroboros.core.lineage import ControlDirectiveEmission, GenerationRecord, OntologyLineage
    from ouroboros.core.seed import OntologyField, OntologySchema

    ontology = OntologySchema(
        name="Test",
        description="test",
        fields=(OntologyField(name="x", field_type="string", description="x"),),
    )
    lineage = OntologyLineage(
        lineage_id="lin_rewind_directives",
        goal="test",
        generations=(
            GenerationRecord(generation_number=1, seed_id="s1", ontology_snapshot=ontology),
            GenerationRecord(generation_number=2, seed_id="s2", ontology_snapshot=ontology),
        ),
        directive_emissions=(
            ControlDirectiveEmission(
                directive="evolve",
                reason="gen1",
                emitted_by="test",
                timestamp=datetime.now(UTC),
                generation_number=1,
            ),
            ControlDirectiveEmission(
                directive="retry",
                reason="gen2",
                emitted_by="test",
                timestamp=datetime.now(UTC),
                generation_number=2,
            ),
        ),
    )

    rewound = lineage.rewind_to(1)

    assert [e.directive for e in rewound.directive_emissions] == ["evolve", "retry"]


def test_latest_ontology_stable_action_requests_verification_handoff() -> None:
    from datetime import UTC, datetime, timedelta

    from ouroboros.core.lineage import ControlDirectiveEmission, GenerationRecord, OntologyLineage
    from ouroboros.core.seed import OntologySchema

    completed_at = datetime.now(UTC)
    lineage = OntologyLineage(
        goal="test",
        generations=(
            GenerationRecord(
                generation_number=1,
                seed_id="s1",
                ontology_snapshot=OntologySchema(name="o", description="o"),
                created_at=completed_at,
            ),
        ),
        directive_emissions=(
            ControlDirectiveEmission(
                directive="evaluate",
                reason="ontology stable",
                emitted_by="evolver",
                timestamp=completed_at + timedelta(microseconds=1),
                generation_number=1,
                step_action="ontology_stable",
            ),
        ),
    )

    assert lineage.verification_handoff_pending is True


def test_replaced_completion_does_not_inherit_stale_verification_handoff() -> None:
    from datetime import UTC, datetime, timedelta

    from ouroboros.core.lineage import ControlDirectiveEmission, GenerationRecord, OntologyLineage
    from ouroboros.core.seed import OntologySchema

    completed_at = datetime.now(UTC)
    lineage = OntologyLineage(
        goal="test",
        generations=(
            GenerationRecord(
                generation_number=1,
                seed_id="replacement",
                ontology_snapshot=OntologySchema(name="o", description="o"),
                created_at=completed_at,
            ),
        ),
        directive_emissions=(
            ControlDirectiveEmission(
                directive="evaluate",
                reason="old attempt",
                emitted_by="evolver",
                timestamp=completed_at - timedelta(microseconds=1),
                generation_number=1,
                step_action="ontology_stable",
            ),
        ),
    )

    assert lineage.verification_handoff_pending is False
