def test_rewind_to_prunes_directives_for_discarded_generations() -> None:
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

    assert [e.directive for e in rewound.directive_emissions] == ["evolve"]
