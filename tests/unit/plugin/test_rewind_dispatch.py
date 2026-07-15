"""Tests for bounded post-commit rewind plugin dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import time

from jsonschema import Draft202012Validator
import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.evolution.rewind import RewindObservationSnapshot
from ouroboros.plugin.digest import canonical_tree_hash
from ouroboros.plugin.firewall import dispatch_rewind_hook, emit_rewind_budget_exhausted
from ouroboros.plugin.hooks import HOOK_REWIND_OBSERVE_SCOPE
from ouroboros.plugin.manifest import (
    CommandSpec,
    Entrypoint,
    HookSpec,
    Permission,
    PluginManifest,
    SourceSpec,
)
from ouroboros.plugin.rewind import (
    REWIND_PAYLOAD_MAX_BYTES,
    LockfileRewindObserver,
    RewindCatalogSnapshot,
    RewindHookCandidate,
    build_rewind_payload,
)
from ouroboros.plugin.trust_store import TrustStore

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures/plugin/rewind"
AUDIT_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "src/ouroboros/plugin/schemas/0.6/audit-event.schema.json"
    ).read_text()
)
AUDIT_VALIDATOR = Draft202012Validator(AUDIT_SCHEMA)


def _snapshot(*, lineage_id: str = "lin-1") -> RewindObservationSnapshot:
    return RewindObservationSnapshot(
        lineage_id=lineage_id,
        from_generation=3,
        to_generation=1,
        rewind_event_id="event-1",
        rewind_occurred_at=datetime(2026, 7, 13, 6, 0, tzinfo=UTC),
    )


def _manifest(
    name: str = "rewind-observer",
    *,
    command: str = "python -m hook rewind",
    timeout_seconds: int = 10,
) -> PluginManifest:
    return PluginManifest(
        schema_version="0.6",
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
        hooks=(
            HookSpec(
                name="on_rewind",
                entrypoint=Entrypoint(type="command", command=command),
                failure_policy="fail_open",
                timeout_seconds=timeout_seconds,
                permissions=(HOOK_REWIND_OBSERVE_SCOPE,),
            ),
        ),
    )


def _installed_subject(
    tmp_path: Path,
    manifest: PluginManifest,
) -> tuple[Path, str, str, str, TrustStore]:
    plugin_home = tmp_path / manifest.name
    plugin_home.mkdir()
    (plugin_home / "hook.py").write_text("print('hook')\n")
    source_type = "plugin_home"
    source_identity = f"https://example.invalid/{manifest.name}"
    artifact_digest = canonical_tree_hash(plugin_home)
    trust_store = TrustStore(root=tmp_path / "trust")
    trust_store.grant(
        plugin=manifest.name,
        version=manifest.version,
        scope=HOOK_REWIND_OBSERVE_SCOPE,
        granted_by="user:test",
        source_type=source_type,
        source_identity=source_identity,
        artifact_digest=artifact_digest,
    )
    return plugin_home, source_type, source_identity, artifact_digest, trust_store


def _dispatch(
    tmp_path: Path,
    *,
    manifest: PluginManifest | None = None,
    runner=None,
    sink=None,
    source_identity_override: str | None = None,
    digest_override: str | None = None,
):
    manifest = manifest or _manifest()
    plugin_home, source_type, source_identity, artifact_digest, trust_store = _installed_subject(
        tmp_path, manifest
    )
    events: list[dict] = []
    result = dispatch_rewind_hook(
        manifest=manifest,
        hook_index=0,
        plugin_home=plugin_home,
        source_type=source_type,
        source_identity=(
            source_identity if source_identity_override is None else source_identity_override
        ),
        artifact_digest=artifact_digest if digest_override is None else digest_override,
        trust_store=trust_store,
        rewind_event_id="event-1",
        lineage_id="lin-1",
        payload_json=build_rewind_payload(_snapshot()),
        timeout_seconds=5.0,
        event_sink=sink or events.append,
        subprocess_runner=runner,
    )
    return result, events, trust_store, plugin_home, source_identity


def test_payload_matches_golden_and_contains_exact_fields() -> None:
    payload = build_rewind_payload(_snapshot())
    expected = (FIXTURE_ROOT / "payload-v1.json").read_text().strip()

    assert payload == expected
    assert set(json.loads(payload)) == {
        "rewind_contract_version",
        "rewind_event_id",
        "rewind_occurred_at",
        "lineage_id",
        "from_generation",
        "to_generation",
        "correlation_id",
    }
    assert not any(
        forbidden in payload
        for forbidden in (
            "seed_json",
            "workspace",
            "checkout",
            "raw_event",
            "event_store",
            "stdout",
            "stderr",
            "token",
            "credentials",
        )
    )


def test_payload_accepts_exact_byte_limit_and_rejects_one_more() -> None:
    empty_size = len(build_rewind_payload(_snapshot(lineage_id="")).encode("utf-8"))
    exact_lineage_id = "x" * (REWIND_PAYLOAD_MAX_BYTES - empty_size)

    exact = build_rewind_payload(_snapshot(lineage_id=exact_lineage_id))
    assert len(exact.encode("utf-8")) == REWIND_PAYLOAD_MAX_BYTES

    with pytest.raises(ValueError, match="maximum is 2048"):
        build_rewind_payload(_snapshot(lineage_id=exact_lineage_id + "x"))


def test_happy_path_launches_with_bounded_payload_and_truthful_audit(tmp_path: Path) -> None:
    calls: list[dict] = []

    def _runner(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="RAW_STDOUT_SENTINEL",
            stderr="",
        )

    result, events, _, plugin_home, _ = _dispatch(tmp_path, runner=_runner)

    assert result.status == "completed"
    assert len(calls) == 1
    assert calls[0]["cwd"] == str(plugin_home)
    assert calls[0]["timeout"] == 5.0
    assert calls[0]["env"]["OUROBOROS_PLUGIN_REWIND_PAYLOAD"] == build_rewind_payload(_snapshot())
    assert [event["event_type"] for event in events] == [
        "plugin.hook.invoked",
        "plugin.hook.completed",
    ]
    assert all("observation" in event and "command" not in event for event in events)
    assert all(not list(AUDIT_VALIDATOR.iter_errors(event)) for event in events)
    assert "RAW_STDOUT_SENTINEL" not in json.dumps(events)
    assert len(events[-1]["provenance"]["stdout_sha256"]) == 64


@pytest.mark.parametrize(
    ("source_identity", "digest", "reason"),
    [
        ("", None, "incomplete_install_metadata"),
        (None, "", "incomplete_install_metadata"),
        ("https://example.invalid/other", None, "trust_subject_mismatch"),
        (None, "sha256:" + "0" * 64, "artifact_digest_mismatch"),
    ],
)
def test_strict_install_subject_blocks_before_launch(
    tmp_path: Path,
    source_identity: str | None,
    digest: str | None,
    reason: str,
) -> None:
    calls: list[list[str]] = []

    def _runner(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result, events, _, _, _ = _dispatch(
        tmp_path,
        runner=_runner,
        source_identity_override=source_identity,
        digest_override=digest,
    )

    assert result.status == "blocked"
    assert calls == []
    assert events[-1]["provenance"]["reason"] == reason


def test_disabled_subject_blocks_before_launch(tmp_path: Path) -> None:
    manifest = _manifest()
    plugin_home, source_type, source_identity, digest, trust_store = _installed_subject(
        tmp_path, manifest
    )
    trust_store.write_disable(
        manifest.name,
        source_type=source_type,
        source_identity=source_identity,
    )
    calls: list[list[str]] = []
    events: list[dict] = []

    result = dispatch_rewind_hook(
        manifest=manifest,
        hook_index=0,
        plugin_home=plugin_home,
        source_type=source_type,
        source_identity=source_identity,
        artifact_digest=digest,
        trust_store=trust_store,
        rewind_event_id="event-1",
        lineage_id="lin-1",
        payload_json=build_rewind_payload(_snapshot()),
        timeout_seconds=5.0,
        event_sink=events.append,
        subprocess_runner=lambda argv, **_kwargs: calls.append(argv),
    )

    assert result.status == "blocked"
    assert calls == []
    assert events[-1]["trust_state"] == "disabled"


@pytest.mark.parametrize("failure", ["nonzero", "startup", "timeout"])
def test_hook_process_failures_are_fail_open_and_digest_only(tmp_path: Path, failure: str) -> None:
    if failure == "nonzero":

        def runner(argv, **_kwargs):
            return subprocess.CompletedProcess(
                argv,
                7,
                stdout="secret stdout",
                stderr="secret stderr",
            )
    elif failure == "startup":

        def runner(argv, **_kwargs):
            raise OSError("missing interpreter")
    else:

        def runner(argv, **_kwargs):
            raise subprocess.TimeoutExpired(argv, 5, output=b"secret", stderr=b"private")

    result, events, _, _, _ = _dispatch(tmp_path, runner=runner)

    assert result.status == "failed"
    assert events[-1]["event_type"] == "plugin.hook.failed"
    serialized = json.dumps(events)
    assert "secret stdout" not in serialized
    assert "secret stderr" not in serialized
    assert "private" not in serialized


def test_audit_sink_failure_does_not_stop_hook(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _runner(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    result, _, _, _, _ = _dispatch(
        tmp_path,
        runner=_runner,
        sink=lambda _event: (_ for _ in ()).throw(RuntimeError("sink down")),
    )

    assert result.status == "completed"
    assert len(calls) == 1


def test_programmatic_fail_closed_hook_is_defense_in_depth_blocked(tmp_path: Path) -> None:
    manifest = _manifest()
    invalid_hook = replace(manifest.hooks[0], failure_policy="fail_closed")
    invalid_manifest = replace(manifest, hooks=(invalid_hook,))

    result, events, _, _, _ = _dispatch(tmp_path, manifest=invalid_manifest)

    assert result.status == "blocked"
    assert events[-1]["provenance"]["reason"] == "invalid_hook_contract"


class _Store:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.batches: list[list[BaseEvent]] = []

    async def initialize(self) -> None:
        return None

    async def append_batch(self, events: list[BaseEvent]) -> None:
        if self.fail:
            raise RuntimeError("audit store down")
        self.batches.append(list(events))


class _Catalog:
    def __init__(self, candidates: tuple[RewindHookCandidate, ...]) -> None:
        self.candidates = candidates
        self.calls = 0

    def snapshot(self) -> RewindCatalogSnapshot:
        self.calls += 1
        return RewindCatalogSnapshot(candidates=self.candidates)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _candidate(tmp_path: Path, name: str) -> tuple[RewindHookCandidate, TrustStore]:
    manifest = _manifest(name)
    plugin_home, source_type, source_identity, digest, trust_store = _installed_subject(
        tmp_path, manifest
    )
    return (
        RewindHookCandidate(
            manifest=manifest,
            plugin_home=plugin_home,
            source_type=source_type,
            source_identity=source_identity,
            artifact_digest=digest,
            hook_index=0,
        ),
        trust_store,
    )


@pytest.mark.asyncio
async def test_global_budget_skips_remaining_candidates_deterministically(
    tmp_path: Path,
) -> None:
    first, trust_store = _candidate(tmp_path, "alpha-observer")
    second_manifest = _manifest("zulu-observer")
    second_home = tmp_path / second_manifest.name
    second_home.mkdir()
    (second_home / "hook.py").write_text("print('hook')\n")
    second_identity = "https://example.invalid/zulu-observer"
    second_digest = canonical_tree_hash(second_home)
    trust_store.grant(
        plugin=second_manifest.name,
        version=second_manifest.version,
        scope=HOOK_REWIND_OBSERVE_SCOPE,
        granted_by="user:test",
        source_type="plugin_home",
        source_identity=second_identity,
        artifact_digest=second_digest,
    )
    second = RewindHookCandidate(
        manifest=second_manifest,
        plugin_home=second_home,
        source_type="plugin_home",
        source_identity=second_identity,
        artifact_digest=second_digest,
        hook_index=0,
    )
    clock = _Clock()
    calls: list[str] = []

    def _runner(argv, **kwargs):
        calls.append(Path(kwargs["cwd"]).name)
        clock.value = 5.0
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    store = _Store()
    catalog = _Catalog((first, second))
    observer = LockfileRewindObserver(
        store,
        catalog=catalog,  # type: ignore[arg-type]
        trust_store=trust_store,
        subprocess_runner=_runner,
        monotonic=clock,
    )

    await observer.observe(_snapshot())

    assert catalog.calls == 1
    assert calls == ["alpha-observer"]
    assert store.batches == []


def test_budget_exhaustion_event_records_total_skipped_count() -> None:
    events: list[dict] = []

    result = emit_rewind_budget_exhausted(
        manifest=_manifest(),
        rewind_event_id="event-1",
        lineage_id="lin-1",
        skipped_count=3,
        event_sink=events.append,
    )

    assert result.reason == "dispatch_budget_exhausted"
    assert events[-1]["provenance"] == {
        "correlation_id": "event-1",
        "hook_name": "on_rewind",
        "failure_policy": "fail_open",
        "reason": "dispatch_budget_exhausted",
        "skipped_count": "3",
    }


@pytest.mark.asyncio
async def test_remaining_timeout_is_clamped_after_prior_dispatch(tmp_path: Path) -> None:
    first, trust_store = _candidate(tmp_path, "alpha-observer")
    second_manifest = _manifest("zulu-observer")
    second_home = tmp_path / second_manifest.name
    second_home.mkdir()
    (second_home / "hook.py").write_text("print('hook')\n")
    second_identity = "https://example.invalid/zulu-observer"
    second_digest = canonical_tree_hash(second_home)
    trust_store.grant(
        plugin=second_manifest.name,
        version=second_manifest.version,
        scope=HOOK_REWIND_OBSERVE_SCOPE,
        granted_by="user:test",
        source_type="plugin_home",
        source_identity=second_identity,
        artifact_digest=second_digest,
    )
    second = RewindHookCandidate(
        manifest=second_manifest,
        plugin_home=second_home,
        source_type="plugin_home",
        source_identity=second_identity,
        artifact_digest=second_digest,
        hook_index=0,
    )
    clock = _Clock()
    timeouts: list[float] = []

    def _runner(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        if len(timeouts) == 1:
            clock.value = 4.9
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    observer = LockfileRewindObserver(
        _Store(),
        catalog=_Catalog((first, second)),  # type: ignore[arg-type]
        trust_store=trust_store,
        subprocess_runner=_runner,
        monotonic=clock,
    )

    await observer.observe(_snapshot())

    assert timeouts[0] == 5.0
    assert timeouts[1] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_slow_catalog_cannot_delay_observer_past_global_budget() -> None:
    class _SlowCatalog:
        def snapshot(self) -> RewindCatalogSnapshot:
            time.sleep(0.25)
            return RewindCatalogSnapshot(candidates=())

    observer = LockfileRewindObserver(
        _Store(),
        catalog=_SlowCatalog(),  # type: ignore[arg-type]
        trust_store=TrustStore(),
        dispatch_budget_seconds=0.03,
    )

    started = time.monotonic()
    await observer.observe(_snapshot())
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    await asyncio.sleep(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_stage", ["digest", "trust"])
async def test_slow_prelaunch_work_cannot_start_hook_after_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slow_stage: str,
) -> None:
    candidate, trust_store = _candidate(tmp_path, "alpha-observer")
    calls: list[list[str]] = []

    if slow_stage == "digest":

        def _slow_digest(_plugin_home: Path) -> str:
            time.sleep(0.25)
            return candidate.artifact_digest

        monkeypatch.setattr("ouroboros.plugin.firewall.canonical_tree_hash", _slow_digest)
    else:
        original_is_disabled = trust_store.is_disabled_for_subject

        def _slow_is_disabled(*args, **kwargs) -> bool:
            time.sleep(0.25)
            return original_is_disabled(*args, **kwargs)

        monkeypatch.setattr(trust_store, "is_disabled_for_subject", _slow_is_disabled)

    def _runner(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    observer = LockfileRewindObserver(
        _Store(),
        catalog=_Catalog((candidate,)),  # type: ignore[arg-type]
        trust_store=trust_store,
        subprocess_runner=_runner,
        dispatch_budget_seconds=0.03,
    )

    started = time.monotonic()
    await observer.observe(_snapshot())
    elapsed = time.monotonic() - started
    await asyncio.sleep(0.25)

    assert elapsed < 0.2
    assert calls == []


@pytest.mark.asyncio
async def test_audit_flush_failure_isolated_after_dispatch(tmp_path: Path) -> None:
    candidate, trust_store = _candidate(tmp_path, "alpha-observer")
    observer = LockfileRewindObserver(
        _Store(fail=True),
        catalog=_Catalog((candidate,)),  # type: ignore[arg-type]
        trust_store=trust_store,
        subprocess_runner=lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="", stderr=""
        ),
    )

    await observer.observe(_snapshot())


def test_golden_audit_fixture_validates_independently() -> None:
    fixture = json.loads((FIXTURE_ROOT / "audit-event-v0.6.json").read_text())
    assert not list(AUDIT_VALIDATOR.iter_errors(fixture))
