"""Lockfile-backed post-commit rewind observation adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time

from ouroboros.evolution.rewind import RewindObservationSnapshot
from ouroboros.plugin.firewall import (
    DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS,
    dispatch_rewind_hook,
    emit_rewind_budget_exhausted,
)
from ouroboros.plugin.hooks import HookKind
from ouroboros.plugin.ledger_adapter import EventStoreLike, PluginLedgerAdapter
from ouroboros.plugin.lockfile import DEFAULT_LOCKFILE_PATH, Lockfile
from ouroboros.plugin.manifest import PluginManifest, PluginManifestError, load_manifest
from ouroboros.plugin.trust_store import DEFAULT_TRUST_ROOT, TrustStore

logger = logging.getLogger(__name__)

REWIND_CONTRACT_VERSION = "rewind.v1"
REWIND_HOOK_SCHEMA_VERSION = "0.6"
REWIND_PAYLOAD_ENV = "OUROBOROS_PLUGIN_REWIND_PAYLOAD"
REWIND_PAYLOAD_MAX_BYTES = 2048
REWIND_DISPATCH_BUDGET_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class RewindHookCandidate:
    """Immutable installed-hook candidate selected from one lockfile snapshot."""

    manifest: PluginManifest
    plugin_home: Path
    source_type: str
    source_identity: str
    artifact_digest: str
    hook_index: int


@dataclass(frozen=True, slots=True)
class RewindCatalogIssue:
    plugin_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class RewindCatalogSnapshot:
    candidates: tuple[RewindHookCandidate, ...]
    issues: tuple[RewindCatalogIssue, ...] = ()


def build_rewind_payload(snapshot: RewindObservationSnapshot) -> str:
    """Serialize exactly the seven public rewind.v1 payload fields."""
    occurred_at = snapshot.rewind_occurred_at
    if occurred_at.tzinfo is None:
        raise ValueError("rewind_occurred_at must be timezone-aware")
    occurred_at_text = (
        occurred_at.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )
    payload = {
        "rewind_contract_version": REWIND_CONTRACT_VERSION,
        "rewind_event_id": snapshot.rewind_event_id,
        "rewind_occurred_at": occurred_at_text,
        "lineage_id": snapshot.lineage_id,
        "from_generation": snapshot.from_generation,
        "to_generation": snapshot.to_generation,
        "correlation_id": snapshot.rewind_event_id,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > REWIND_PAYLOAD_MAX_BYTES:
        raise ValueError(
            f"rewind payload is {len(encoded)} bytes; maximum is {REWIND_PAYLOAD_MAX_BYTES}"
        )
    return encoded.decode("utf-8")


class RewindCatalog:
    """Select v0.6 on_rewind declarations from one lockfile read."""

    def __init__(
        self,
        lockfile: Lockfile,
        *,
        manifest_loader: Callable[[str | Path], PluginManifest] = load_manifest,
    ) -> None:
        self._lockfile = lockfile
        self._manifest_loader = manifest_loader

    def snapshot(self) -> RewindCatalogSnapshot:
        entries = self._lockfile.read()
        candidates: list[RewindHookCandidate] = []
        issues: list[RewindCatalogIssue] = []
        for plugin_name in sorted(entries):
            entry = entries[plugin_name]
            plugin_home = Path(entry.plugin_home).expanduser()
            try:
                manifest = self._manifest_loader(plugin_home / "ouroboros.plugin.json")
            except (PluginManifestError, OSError, ValueError) as exc:
                issues.append(
                    RewindCatalogIssue(
                        plugin_name=plugin_name,
                        reason=f"manifest_unreadable:{type(exc).__name__}",
                    )
                )
                continue
            if manifest.name != plugin_name or manifest.version != entry.version:
                issues.append(
                    RewindCatalogIssue(
                        plugin_name=plugin_name,
                        reason="manifest_identity_mismatch",
                    )
                )
                continue
            if manifest.schema_version != REWIND_HOOK_SCHEMA_VERSION:
                continue
            for hook_index, hook in enumerate(manifest.hooks):
                if hook.name != HookKind.ON_REWIND.value:
                    continue
                candidates.append(
                    RewindHookCandidate(
                        manifest=manifest,
                        plugin_home=plugin_home,
                        source_type=entry.source_type,
                        source_identity=entry.source_identity,
                        artifact_digest=entry.artifact_digest,
                        hook_index=hook_index,
                    )
                )
        return RewindCatalogSnapshot(
            candidates=tuple(candidates),
            issues=tuple(issues),
        )


class LockfileRewindObserver:
    """Post-commit observer using installed manifests and the plugin firewall."""

    def __init__(
        self,
        event_store: EventStoreLike,
        *,
        catalog: RewindCatalog,
        trust_store: TrustStore,
        subprocess_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        dispatch_budget_seconds: float = REWIND_DISPATCH_BUDGET_SECONDS,
    ) -> None:
        self._event_store = event_store
        self._catalog = catalog
        self._trust_store = trust_store
        self._subprocess_runner = subprocess_runner
        self._monotonic = monotonic
        self._dispatch_budget_seconds = dispatch_budget_seconds

    async def observe(self, snapshot: RewindObservationSnapshot) -> None:
        deadline = self._monotonic() + self._dispatch_budget_seconds
        try:
            payload_json = build_rewind_payload(snapshot)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                logger.warning(
                    "plugin.rewind_dispatch_budget_exhausted",
                    extra={"rewind_event_id": snapshot.rewind_event_id},
                )
                return
            catalog_snapshot = await asyncio.wait_for(
                asyncio.to_thread(self._catalog.snapshot),
                timeout=remaining,
            )
        except TimeoutError:
            logger.warning(
                "plugin.rewind_catalog_budget_exhausted",
                extra={"rewind_event_id": snapshot.rewind_event_id},
            )
            return
        except Exception as exc:
            logger.warning(
                "plugin.rewind_observer_setup_failed",
                extra={
                    "rewind_event_id": snapshot.rewind_event_id,
                    "error": str(exc),
                },
            )
            return

        for issue in catalog_snapshot.issues:
            logger.warning(
                "plugin.rewind_catalog_entry_skipped",
                extra={"plugin": issue.plugin_name, "reason": issue.reason},
            )

        adapter = PluginLedgerAdapter(
            self._event_store,
            correlation_id=snapshot.rewind_event_id,
        )
        accept_audit = threading.Event()
        accept_audit.set()
        audit_lock = threading.Lock()

        def _audit_sink(event: dict) -> None:
            with audit_lock:
                if not accept_audit.is_set():
                    return
                try:
                    adapter.audit_sink(event)
                except Exception as exc:
                    logger.warning(
                        "plugin.rewind_audit_buffer_failed",
                        extra={
                            "rewind_event_id": snapshot.rewind_event_id,
                            "error": str(exc),
                        },
                    )

        candidates = catalog_snapshot.candidates
        for index, candidate in enumerate(candidates):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                emit_rewind_budget_exhausted(
                    manifest=candidate.manifest,
                    rewind_event_id=snapshot.rewind_event_id,
                    lineage_id=snapshot.lineage_id,
                    skipped_count=len(candidates) - index,
                    event_sink=_audit_sink,
                )
                break
            hook = candidate.manifest.hooks[candidate.hook_index]
            hook_timeout = float(hook.timeout_seconds or DEFAULT_PLUGIN_INVOCATION_TIMEOUT_SECONDS)
            timeout_seconds = min(hook_timeout, remaining)
            try:
                dispatch_result = await asyncio.wait_for(
                    asyncio.to_thread(
                        dispatch_rewind_hook,
                        manifest=candidate.manifest,
                        hook_index=candidate.hook_index,
                        plugin_home=candidate.plugin_home,
                        source_type=candidate.source_type,
                        source_identity=candidate.source_identity,
                        artifact_digest=candidate.artifact_digest,
                        trust_store=self._trust_store,
                        rewind_event_id=snapshot.rewind_event_id,
                        lineage_id=snapshot.lineage_id,
                        payload_json=payload_json,
                        timeout_seconds=timeout_seconds,
                        event_sink=_audit_sink,
                        subprocess_runner=self._subprocess_runner,
                        deadline=deadline,
                        monotonic=self._monotonic,
                        skipped_count=len(candidates) - index,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                with audit_lock:
                    accept_audit.clear()
                    emit_rewind_budget_exhausted(
                        manifest=candidate.manifest,
                        rewind_event_id=snapshot.rewind_event_id,
                        lineage_id=snapshot.lineage_id,
                        skipped_count=len(candidates) - index,
                        event_sink=adapter.audit_sink,
                    )
                logger.warning(
                    "plugin.rewind_dispatch_budget_exhausted",
                    extra={
                        "plugin": candidate.manifest.name,
                        "rewind_event_id": snapshot.rewind_event_id,
                    },
                )
                break
            except Exception as exc:
                logger.warning(
                    "plugin.rewind_dispatch_failed",
                    extra={
                        "plugin": candidate.manifest.name,
                        "rewind_event_id": snapshot.rewind_event_id,
                        "error": str(exc),
                    },
                )
            else:
                if dispatch_result.reason == "dispatch_budget_exhausted":
                    break

        remaining = deadline - self._monotonic()
        if remaining <= 0:
            if adapter.pending_events:
                logger.warning(
                    "plugin.rewind_audit_flush_budget_exhausted",
                    extra={
                        "rewind_event_id": snapshot.rewind_event_id,
                        "pending_events": len(adapter.pending_events),
                    },
                )
            return
        try:
            await asyncio.wait_for(adapter.flush(), timeout=remaining)
        except TimeoutError:
            logger.warning(
                "plugin.rewind_audit_flush_budget_exhausted",
                extra={
                    "rewind_event_id": snapshot.rewind_event_id,
                    "pending_events": len(adapter.pending_events),
                },
            )
        except Exception as exc:
            logger.warning(
                "plugin.rewind_audit_flush_failed",
                extra={
                    "rewind_event_id": snapshot.rewind_event_id,
                    "error": str(exc),
                },
            )


def build_lockfile_rewind_observer(
    event_store: EventStoreLike,
    *,
    lockfile_path: Path | None = None,
    trust_root: Path | None = None,
) -> LockfileRewindObserver:
    """Build the production observer from environment-aware plugin paths."""
    resolved_lockfile = (
        lockfile_path
        or Path(
            os.environ.get("OUROBOROS_PLUGIN_LOCKFILE", str(DEFAULT_LOCKFILE_PATH))
        ).expanduser()
    )
    resolved_trust_root = (
        trust_root
        or Path(os.environ.get("OUROBOROS_PLUGIN_TRUST_ROOT", str(DEFAULT_TRUST_ROOT))).expanduser()
    )
    return LockfileRewindObserver(
        event_store,
        catalog=RewindCatalog(Lockfile(resolved_lockfile)),
        trust_store=TrustStore(root=resolved_trust_root),
    )


__all__ = [
    "LockfileRewindObserver",
    "REWIND_CONTRACT_VERSION",
    "REWIND_DISPATCH_BUDGET_SECONDS",
    "REWIND_HOOK_SCHEMA_VERSION",
    "REWIND_PAYLOAD_ENV",
    "REWIND_PAYLOAD_MAX_BYTES",
    "RewindCatalog",
    "RewindCatalogIssue",
    "RewindCatalogSnapshot",
    "RewindHookCandidate",
    "build_lockfile_rewind_observer",
    "build_rewind_payload",
]
