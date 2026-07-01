from __future__ import annotations

import json

from ouroboros.auto.autoresearch_contract import apply_autoresearch_contract
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata


def _contract() -> dict[str, object]:
    return {
        "repository": "/tmp/autoresearch-demo",
        "program_path": "program.md",
        "handoff_brief_path": "/tmp/autoresearch-demo/.ouroboros/autoresearch/seed.md",
        "editable_files": ["train.py"],
        "fixed_files": ["program.md", "prepare.py"],
        "primary_metric": "val_bpb",
        "metric_direction": "lower_is_better",
        "experiment_budget": 2,
        "timeout_seconds": 60,
        "verification_command": "python3 train.py",
        "execution_command": {
            "command": "python3 train.py",
            "cwd": "/tmp/autoresearch-demo",
            "timeout_seconds": 60,
            "timeout_policy": "Apply timeout_seconds in the harness/orchestrator.",
        },
        "seed_artifact_policy": {
            "handoff_brief_path": "/tmp/autoresearch-demo/.ouroboros/autoresearch/seed.md",
            "handoff_brief_only": True,
            "saved_seed_path_runtime_owned": True,
            "repo_local_seed_output_required": False,
            "forbidden_repo_local_seed_outputs": [
                ".ouroboros/autoresearch/seed.yaml",
                ".ouroboros/autoresearch/generated-seed.yaml",
            ],
        },
        "candidate_sequence": [
            {"id": 1, "name": "baseline", "train_py_change": "none"},
            {"id": 2, "name": "additive-smoothing", "train_py_change": "tune alpha"},
        ],
        "non_goals": ["Do not edit prepare.py."],
        "runtime_context": {"cwd": "/tmp/autoresearch-demo", "artifacts_local": True},
        "metric_fallback": {"primary": "val_bpb", "legacy_json_fallback": "best_val_bpb"},
        "ledger": {"path": ".ouroboros/autoresearch/experiment-log.md"},
        "validity_rules": {"exit_code": 0, "metric_required": True},
        "verification_plan": {
            "seed_creation": ["Inspect the generated Seed artifact."],
            "experiment_execution": ["Run python3 train.py."],
        },
        "conflict_resolution": "The probability-model candidate_sequence wins conflicts.",
    }


def _seed(goal: str) -> Seed:
    return Seed(
        goal=goal,
        task_type="code",
        constraints=("Use existing patterns.",),
        acceptance_criteria=(
            "A headless simulation of N input ticks produces a state-change trace.",
            "The game's main loop terminates cleanly on a quit signal.",
            "The runnable build launches without missing-asset errors.",
            "Adopt this concrete implementation decision before execution: ## Persona: Hacker ...",
        ),
        ontology_schema=OntologySchema(
            name="Demo",
            description="Demo ontology.",
        ),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )


def test_apply_autoresearch_contract_promotes_json_to_top_level_seed_fields() -> None:
    contract_json = json.dumps(_contract(), indent=2)
    seed = _seed(
        "Improve val_bpb.\n\n```json\n"
        f"{contract_json}\n"
        "```"
    )

    normalized = apply_autoresearch_contract(seed)
    payload = normalized.to_dict()

    assert payload["task_type"] == "research"
    assert payload["repository"] == "/tmp/autoresearch-demo"
    assert payload["program_path"] == "program.md"
    assert payload["editable_files"] == ["train.py"]
    assert payload["fixed_files"] == ["program.md", "prepare.py"]
    assert payload["primary_metric"] == "val_bpb"
    assert payload["execution_command"]["command"] == "python3 train.py"
    assert payload["seed_artifact_policy"]["saved_seed_path_runtime_owned"] is True
    assert payload["candidate_sequence"][0]["name"] == "baseline"
    assert "runtime_context" in payload
    assert "metric_fallback" in payload
    assert "verification_plan" in payload
    assert any("top-level values" in item for item in payload["acceptance_criteria"])
    assert any("seed_artifact_policy" in item for item in payload["acceptance_criteria"])
    assert any("top-level verification_plan" in item for item in payload["acceptance_criteria"])
    assert any("baseline-only rerun" in item for item in payload["acceptance_criteria"])
    assert any("final best val_bpb" in item for item in payload["acceptance_criteria"])
    assert any("Candidate sequence contains exactly 2 experiments" in item for item in payload["acceptance_criteria"])
    assert any("baseline-only output is insufficient" in item for item in payload["constraints"])
    assert any("saved Seed artifact path is owned" in item for item in payload["constraints"])
    assert any("do not rewrite the command string" in item for item in payload["constraints"])
    assert not any("headless simulation" in item for item in payload["constraints"])
    assert not any("quit signal" in item for item in payload["acceptance_criteria"])
    assert not any("Persona" in item for item in payload["constraints"])


def test_apply_autoresearch_contract_removes_format_drift_constraints() -> None:
    seed = _seed("Improve val_bpb.").model_copy(
        update={
            **_contract(),
            "constraints": (
                "top-level `actors` must be [\"codex\"]",
                "top-level `inputs` must include repository",
                "top-level `outputs` must include seed.yaml",
                "top-level seed_artifact_path must be .ouroboros/autoresearch/generated-seed.yaml",
                "acceptance inspector prints SEED_VERIFICATION_OK",
            ),
        }
    )

    normalized = apply_autoresearch_contract(seed)
    constraints = normalized.to_dict()["constraints"]

    assert not any("actors" in item for item in constraints)
    assert not any("inputs" in item for item in constraints)
    assert not any("top-level `outputs`" in item for item in constraints)
    assert not any("seed_artifact_path" in item for item in constraints)
    assert not any("SEED_VERIFICATION_OK" in item for item in constraints)


def test_seed_extra_contract_fields_roundtrip() -> None:
    seed = _seed("Improve val_bpb.").model_copy(update=_contract())

    restored = Seed.from_dict(seed.to_dict())

    assert restored.to_dict()["repository"] == "/tmp/autoresearch-demo"
    assert restored.to_dict()["verification_command"] == "python3 train.py"
    assert restored.to_dict()["seed_artifact_policy"]["repo_local_seed_output_required"] is False
