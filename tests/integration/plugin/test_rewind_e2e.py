"""End-to-end rewind observation through lockfile, firewall, and ledger."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shlex
import sys

import pytest

from ouroboros.core.lineage import GenerationRecord, OntologyLineage
from ouroboros.core.seed import OntologyField, OntologySchema
from ouroboros.evolution.loop import EvolutionaryLoop
from ouroboros.persistence.event_store import EventStore
from ouroboros.plugin.digest import canonical_tree_hash
from ouroboros.plugin.hooks import HOOK_REWIND_OBSERVE_SCOPE
from ouroboros.plugin.lockfile import LockEntry, Lockfile
from ouroboros.plugin.rewind import build_lockfile_rewind_observer
from ouroboros.plugin.trust_store import TrustStore
from tests.unit.plugin.test_manifest import REFERENCE_MANIFEST


def _lineage() -> OntologyLineage:
    ontology = OntologySchema(
        name="Rewind",
        description="Rewind E2E ontology",
        fields=(OntologyField(name="id", field_type="string", description="ID"),),
    )
    return OntologyLineage(
        lineage_id="lin-rewind-e2e",
        goal="Exercise rewind observer",
        generations=tuple(
            GenerationRecord(
                generation_number=number,
                seed_id=f"seed-{number}",
                ontology_snapshot=ontology,
            )
            for number in (1, 2, 3)
        ),
    )


def _install_observer(tmp_path: Path) -> tuple[Path, Path, Path]:
    plugin_home = tmp_path / "plugins" / "rewind-observer"
    plugin_home.mkdir(parents=True)
    payload_path = tmp_path / "observed-payload.json"
    hook_path = plugin_home / "hook.py"
    hook_path.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "Path(sys.argv[1]).write_text(os.environ['OUROBOROS_PLUGIN_REWIND_PAYLOAD'])\n"
    )

    manifest = deepcopy(REFERENCE_MANIFEST)
    manifest.update(
        {
            "schema_version": "0.6",
            "name": "rewind-observer",
            "version": "1.0.0",
            "source": {"type": "plugin_home", "path": "rewind-observer"},
            "entrypoint": {"type": "command", "command": f"{sys.executable} hook.py"},
            "hooks": [
                {
                    "name": "on_rewind",
                    "entrypoint": {
                        "type": "command",
                        "command": (
                            f"{shlex.quote(sys.executable)} hook.py "
                            f"{shlex.quote(str(payload_path))}"
                        ),
                    },
                    "permissions": [HOOK_REWIND_OBSERVE_SCOPE],
                    "failure_policy": "fail_open",
                    "timeout_seconds": 5,
                }
            ],
        }
    )
    manifest["permissions"].append(
        {
            "scope": HOOK_REWIND_OBSERVE_SCOPE,
            "risk": "read_only",
            "required": True,
            "reason": "Observe committed rewinds.",
        }
    )
    (plugin_home / "ouroboros.plugin.json").write_text(json.dumps(manifest))

    digest = canonical_tree_hash(plugin_home)
    source_identity = "https://example.invalid/rewind-observer"
    lockfile_path = tmp_path / "plugins.lock"
    Lockfile(lockfile_path).add(
        LockEntry(
            name="rewind-observer",
            version="1.0.0",
            source_kind="git",
            repository=source_identity,
            git_sha="abc123",
            manifest_checksum="sha256:manifest",
            installed_at="2026-07-13T06:00:00Z",
            plugin_home=str(plugin_home),
            source_type="plugin_home",
            source_identity=source_identity,
            artifact_digest=digest,
        )
    )
    trust_root = tmp_path / "trust"
    TrustStore(root=trust_root).grant(
        plugin="rewind-observer",
        version="1.0.0",
        scope=HOOK_REWIND_OBSERVE_SCOPE,
        granted_by="user:test",
        source_type="plugin_home",
        source_identity=source_identity,
        artifact_digest=digest,
    )
    return lockfile_path, trust_root, payload_path


@pytest.mark.asyncio
async def test_no_plugin_installed_preserves_rewind_baseline(tmp_path: Path) -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    observer = build_lockfile_rewind_observer(
        store,
        lockfile_path=tmp_path / "missing.lock",
        trust_root=tmp_path / "trust",
    )

    result = await EvolutionaryLoop(store, rewind_observer=observer).rewind_to(_lineage(), 1)

    assert result.is_ok
    assert result.value.lineage.current_generation == 1
    assert [event.type for event in await store.query_events(limit=10)] == ["lineage.rewound"]


@pytest.mark.asyncio
async def test_trusted_observer_runs_after_commit_and_persists_audit(tmp_path: Path) -> None:
    lockfile_path, trust_root, payload_path = _install_observer(tmp_path)
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    observer = build_lockfile_rewind_observer(
        store,
        lockfile_path=lockfile_path,
        trust_root=trust_root,
    )

    result = await EvolutionaryLoop(store, rewind_observer=observer).rewind_to(_lineage(), 1)

    assert result.is_ok
    payload = json.loads(payload_path.read_text())
    assert payload == {
        "rewind_contract_version": "rewind.v1",
        "rewind_event_id": result.value.rewind_event_id,
        "rewind_occurred_at": result.value.rewind_occurred_at.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        "lineage_id": "lin-rewind-e2e",
        "from_generation": 3,
        "to_generation": 1,
        "correlation_id": result.value.rewind_event_id,
    }

    persisted = await store.query_events(limit=10)
    assert [event.type for event in persisted] == [
        "plugin.hook.completed",
        "plugin.hook.invoked",
        "lineage.rewound",
    ]
    assert persisted[0].data["observation"] == {
        "kind": "rewind",
        "id": result.value.rewind_event_id,
        "aggregate_type": "lineage",
        "aggregate_id": "lin-rewind-e2e",
    }
    assert all("command" not in event.data for event in persisted[:2])
