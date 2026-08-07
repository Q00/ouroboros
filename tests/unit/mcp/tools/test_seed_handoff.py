"""Opaque Seed handoff and worker-safe rendering regressions."""

import json

import pytest
import yaml

from ouroboros.mcp.tools.evaluation_job import (
    resolve_auto_evolve_policy,
    restore_seed_handoff,
)
from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry, render_worker_safe_seed


def test_handoff_is_session_bound_and_bounded() -> None:
    registry = SeedHandoffRegistry(max_entries=1)
    first = registry.register(session_id="orch-first", seed_content="goal: first")
    second = registry.register(session_id="orch-second", seed_content="goal: second")

    assert registry.resolve(first, session_id="orch-first") is None
    assert registry.resolve(second, session_id="orch-wrong") is None
    assert registry.resolve("seed_handoff_unknown", session_id="orch-second") is None
    assert registry.resolve(second, session_id="orch-second") == "goal: second"


def test_worker_safe_seed_fails_closed_for_malformed_yaml() -> None:
    raw = "goal: [malformed SECRET_VALUE"

    rendered = render_worker_safe_seed(raw)

    assert "SECRET_VALUE" not in rendered
    assert "Seed omitted: invalid YAML" in rendered


@pytest.mark.parametrize(
    "repeat_value",
    [
        pytest.param(lambda value: value, id="raw"),
        pytest.param(repr, id="quoted"),
        pytest.param(lambda value: json.dumps(value)[1:-1], id="escaped"),
    ],
)
def test_worker_safe_seed_redacts_hidden_values_repeated_elsewhere(repeat_value) -> None:
    command = 'python hidden_grader.py --expect "PRIVATE_SENTINEL"'
    assertion = "PRIVATE_SENTINEL"
    raw = yaml.safe_dump(
        {
            "goal": "Build safely",
            "constraints": [
                f"Do not reveal {repeat_value(command)}",
                f"Do not reveal {repeat_value(assertion)}",
            ],
            "acceptance_criteria": [
                {
                    "description": "Produce output.json",
                    "expected_artifacts": ["output.json"],
                    "verify_command": command,
                    "output_assertion": assertion,
                }
            ],
            "ontology_schema": {
                "name": "HiddenContractArtifact",
                "description": "Artifact with parent-owned verification",
            },
            "metadata": {"ambiguity_score": 0.0},
        },
        sort_keys=False,
    )

    rendered = render_worker_safe_seed(raw)

    assert "hidden_grader.py" not in rendered
    assert "PRIVATE_SENTINEL" not in rendered
    assert "verify_command" not in rendered
    assert "output_assertion" not in rendered
    assert "Produce output.json" in rendered
    assert "output.json" in rendered


def test_restore_consumes_process_local_handoff_handle() -> None:
    registry = SeedHandoffRegistry()
    handoff_id = registry.register(session_id="orch-restore", seed_content="goal: private")
    original = {"session_id": "orch-restore", "seed_handoff_id": handoff_id}

    restored = restore_seed_handoff(
        original,
        session_id="orch-restore",
        registry=registry,
    )

    assert restored == {"session_id": "orch-restore", "seed_content": "goal: private"}
    assert original["seed_handoff_id"] == handoff_id


def test_disabled_auto_evolve_snapshot_survives_enabled_config_on_reentry() -> None:
    initial, enabled = resolve_auto_evolve_policy({}, configured_enabled=False)
    replayed, replay_enabled = resolve_auto_evolve_policy(initial, configured_enabled=True)

    assert enabled is False
    assert replay_enabled is False
    assert initial["auto_evolve"] is False
    assert replayed["auto_evolve"] is False
    assert "_auto_evolve_max_generations" not in replayed


def test_enabled_auto_evolve_snapshot_survives_disabled_config_on_reentry(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "ouroboros.mcp.tools.evaluation_job.get_auto_evolve_max_generations",
        lambda: 4,
    )
    initial, enabled = resolve_auto_evolve_policy({}, configured_enabled=True)
    replayed, replay_enabled = resolve_auto_evolve_policy(initial, configured_enabled=False)

    assert enabled is True
    assert replay_enabled is True
    assert initial["auto_evolve"] is True
    assert replayed["auto_evolve"] is True
    assert replayed["_auto_evolve_max_generations"] == 4
