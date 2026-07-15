"""Tests for one-shot installed-plugin rewind catalog selection."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from ouroboros.plugin.hooks import HOOK_REWIND_OBSERVE_SCOPE
from ouroboros.plugin.lockfile import LockEntry
from ouroboros.plugin.manifest import (
    CommandSpec,
    Entrypoint,
    HookSpec,
    Permission,
    PluginManifest,
    SourceSpec,
)
from ouroboros.plugin.rewind import RewindCatalog


def _manifest(name: str, *, schema_version: str = "0.6", hooks: int = 1) -> PluginManifest:
    return PluginManifest(
        schema_version=schema_version,
        name=name,
        version="1.0.0",
        source=SourceSpec(type="plugin_home", path=name),
        commands=(
            CommandSpec(
                namespace=name,
                name="status",
                summary="Status",
                usage=f"ooo {name} status",
                risk="read_only",
            ),
        ),
        capabilities=(),
        permissions=(
            Permission(
                scope=HOOK_REWIND_OBSERVE_SCOPE,
                risk="read_only",
                required=True,
            ),
        ),
        entrypoint=Entrypoint(type="command", command="python -m plugin"),
        hooks=tuple(
            HookSpec(
                name="on_rewind",
                entrypoint=Entrypoint(type="command", command=f"python -m hook {index}"),
                failure_policy="fail_open",
                timeout_seconds=5,
                permissions=(HOOK_REWIND_OBSERVE_SCOPE,),
            )
            for index in range(hooks)
        ),
    )


def _entry(name: str) -> LockEntry:
    return LockEntry(
        name=name,
        version="1.0.0",
        source_kind="git",
        repository=f"https://example.invalid/{name}",
        git_sha="abc123",
        manifest_checksum="sha256:manifest",
        installed_at="2026-07-13T06:00:00Z",
        plugin_home=f"/plugins/{name}",
        source_type="plugin_home",
        source_identity=f"https://example.invalid/{name}",
        artifact_digest="sha256:digest",
    )


class _Lockfile:
    def __init__(self, entries: dict[str, LockEntry]) -> None:
        self.entries = entries
        self.read_count = 0

    def read(self) -> dict[str, LockEntry]:
        self.read_count += 1
        return self.entries


def test_catalog_reads_lockfile_once_and_orders_name_then_declaration() -> None:
    lockfile = _Lockfile({"zulu": _entry("zulu"), "alpha": _entry("alpha")})
    manifests = {"alpha": _manifest("alpha", hooks=2), "zulu": _manifest("zulu")}
    catalog = RewindCatalog(
        lockfile,  # type: ignore[arg-type]
        manifest_loader=lambda path: manifests[Path(path).parent.name],
    )

    snapshot = catalog.snapshot()

    assert lockfile.read_count == 1
    assert [candidate.manifest.name for candidate in snapshot.candidates] == [
        "alpha",
        "alpha",
        "zulu",
    ]
    assert [candidate.hook_index for candidate in snapshot.candidates] == [0, 1, 0]


def test_catalog_skips_corrupt_manifest_and_continues() -> None:
    lockfile = _Lockfile({"alpha": _entry("alpha"), "zulu": _entry("zulu")})

    def _load(path: str | Path) -> PluginManifest:
        if Path(path).parent.name == "alpha":
            raise ValueError("corrupt manifest")
        return _manifest("zulu")

    snapshot = RewindCatalog(lockfile, manifest_loader=_load).snapshot()  # type: ignore[arg-type]

    assert [candidate.manifest.name for candidate in snapshot.candidates] == ["zulu"]
    assert snapshot.issues[0].plugin_name == "alpha"
    assert snapshot.issues[0].reason == "manifest_unreadable:ValueError"


def test_catalog_omits_archived_schema_and_missing_lockfile_entries() -> None:
    empty = _Lockfile({})
    assert RewindCatalog(empty).snapshot().candidates == ()  # type: ignore[arg-type]

    old = _Lockfile({"alpha": _entry("alpha")})
    snapshot = RewindCatalog(
        old,  # type: ignore[arg-type]
        manifest_loader=lambda _path: _manifest("alpha", schema_version="0.4"),
    ).snapshot()
    assert snapshot.candidates == ()


def test_candidate_is_immutable() -> None:
    lockfile = _Lockfile({"alpha": _entry("alpha")})
    candidate = (
        RewindCatalog(
            lockfile,  # type: ignore[arg-type]
            manifest_loader=lambda _path: _manifest("alpha"),
        )
        .snapshot()
        .candidates[0]
    )

    with pytest.raises(FrozenInstanceError):
        candidate.hook_index = 9  # type: ignore[misc]
