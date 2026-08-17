"""CLI safety contract for Disposable Memory inspection and pruning."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands.artifacts import parse_ttl
from ouroboros.cli.main import app
from ouroboros.persistence.artifact_store import (
    ArtifactStore,
    ArtifactTombstonedError,
)

runner = CliRunner()


def _put(project_dir: Path):
    store = ArtifactStore.for_project(project_dir)
    envelope = store.put_for_contract(
        contract_id="CONTRACT1",
        body={"answer": 42},
        runtime_id="cli-test",
        duration_ms=1,
        events_emitted_count=0,
    )
    return store, envelope


def test_artifacts_group_is_registered() -> None:
    result = runner.invoke(app, ["artifacts", "--help"])
    assert result.exit_code == 0
    assert "prune" in result.output
    assert "fetch" in result.output
    assert "replay" in result.output


def test_parse_ttl_uses_explicit_units() -> None:
    assert parse_ttl("30d") == timedelta(days=30)
    assert parse_ttl("12h") == timedelta(hours=12)


def test_parse_ttl_rejects_values_larger_than_timedelta_supports() -> None:
    with pytest.raises(ValueError, match="too large"):
        parse_ttl(f"{10**100}w")


def test_fetch_and_replay_are_explicit_read_only_commands(tmp_path: Path) -> None:
    _, envelope = _put(tmp_path)
    for command in ("fetch", "replay"):
        result = runner.invoke(
            app,
            ["artifacts", command, envelope.contract_id, "--project-dir", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert '"answer": 42' in result.output


def test_prune_is_dry_run_until_apply_and_replay_surfaces_tombstone(tmp_path: Path) -> None:
    store, envelope = _put(tmp_path)

    # The just-published contract sits inside replay retention, so pruning it
    # takes the explicit operator override on top of the zero TTL.
    dry_run = runner.invoke(
        app,
        [
            "artifacts",
            "prune",
            "--project-dir",
            str(tmp_path),
            "--ttl",
            "0d",
            "--allow-replay-tombstone",
        ],
    )
    assert dry_run.exit_code == 0
    assert "would remove" in dry_run.output
    assert "Dry run only" in dry_run.output
    assert store.fetch(envelope.contract_id).body == {"answer": 42}

    applied = runner.invoke(
        app,
        [
            "artifacts",
            "prune",
            "--project-dir",
            str(tmp_path),
            "--ttl",
            "0d",
            "--allow-replay-tombstone",
            "--apply",
        ],
    )
    assert applied.exit_code == 0
    assert "Removed 1 artifact body" in applied.output
    with pytest.raises(ArtifactTombstonedError):
        store.fetch(envelope.contract_id)

    replay = runner.invoke(
        app,
        ["artifacts", "replay", envelope.contract_id, "--project-dir", str(tmp_path)],
    )
    assert replay.exit_code == 1
    assert "force-rerun" in replay.output
