"""Execution-bound test wrapper receipts backed by command artifact provenance."""

from __future__ import annotations

from collections.abc import Mapping
import json

from ouroboros.harness.deliver_gate import (
    _is_canonical_command_artifact_path,
    _journal_command_artifact,
)
from ouroboros.harness.journal import EvidenceEntry, EvidenceManifest

_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "execution_id",
        "ac_id",
        "retry_attempt",
        "session_attempt_id",
        "command_call_id",
        "completion_event_id",
        "outer_command",
        "inner_command",
        "outer_exit_status",
        "inner_exit_status",
        "stdout_ref",
        "stderr_ref",
        "test_result",
    }
)
_CLAIM_KEYS = frozenset({"receipt_path", "outer_command", "inner_command"})
_MAX_RECEIPT_BYTES = 16384


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate receipt key")
        result[key] = value
    return result


def _receipt_matches_entry(
    receipt: object,
    *,
    entry: EvidenceEntry,
    manifest: EvidenceManifest,
    execution_id: str,
    claim: Mapping[str, object],
    task_cwd: str | None,
) -> bool:
    if not isinstance(receipt, dict) or frozenset(receipt) != _RECEIPT_KEYS:
        return False
    payload = entry.payload
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["kind"] != "ouroboros.test-wrapper-receipt"
        or receipt["execution_id"] != execution_id
        or receipt["ac_id"] != manifest.ac_id
        or type(receipt["retry_attempt"]) is not int
        or receipt["retry_attempt"] != payload.get("retry_attempt")
        or receipt["session_attempt_id"] != payload.get("session_attempt_id")
        or receipt["command_call_id"] != payload.get("tool_call_id")
        or len(entry.source_event_ids) != 2
        or receipt["completion_event_id"] != entry.source_event_ids[1]
        or receipt["outer_command"] != claim["outer_command"]
        or receipt["outer_command"] != payload.get("command")
        or receipt["inner_command"] != claim["inner_command"]
        or not isinstance(receipt["inner_command"], str)
        or not receipt["inner_command"].strip()
        or receipt["test_result"] != "passed"
    ):
        return False
    for key in ("inner_exit_status", "outer_exit_status"):
        value = receipt[key]
        if isinstance(value, bool) or not isinstance(value, int) or value != 0:
            return False
    for key in ("stdout_ref", "stderr_ref"):
        path = receipt[key]
        if not isinstance(path, str) or not _is_canonical_command_artifact_path(path):
            return False
        if path == claim["receipt_path"]:
            return False
        valid, _ = _journal_command_artifact(entry, relative_path=path, task_cwd=task_cwd)
        if not valid:
            return False
    return receipt["stdout_ref"] != receipt["stderr_ref"]


def matching_wrapper_receipts(
    manifest: EvidenceManifest,
    *,
    claims: object,
    execution_id: str,
    task_cwd: str | None,
) -> dict[str, EvidenceEntry] | None:
    """Return uniquely proven inner commands, or None on any malformed claim.

    The receipt and both output references must be newly captured by the same
    accepted Bash completion. Reading occurs through the Phase-1 no-follow
    capability while its file descriptor is held and postvalidated.
    """
    if not isinstance(claims, (list, tuple)) or not claims or len(claims) > 128:
        return None
    result: dict[str, EvidenceEntry] = {}
    seen_paths: set[str] = set()
    for claim in claims:
        if not isinstance(claim, Mapping) or frozenset(claim) != _CLAIM_KEYS:
            return None
        path = claim["receipt_path"]
        outer = claim["outer_command"]
        inner = claim["inner_command"]
        if (
            not isinstance(path, str)
            or not _is_canonical_command_artifact_path(path)
            or path in seen_paths
            or not isinstance(outer, str)
            or not outer.strip()
            or not isinstance(inner, str)
            or not inner.strip()
            or inner in result
        ):
            return None
        seen_paths.add(path)
        matches: list[EvidenceEntry] = []
        for entry in manifest.entries:
            if entry.ok is not True or entry.payload.get("tool_name") != "Bash":
                continue
            valid, content = _journal_command_artifact(
                entry, relative_path=path, task_cwd=task_cwd, read_limit=_MAX_RECEIPT_BYTES
            )
            if not valid or content is None:
                continue
            try:
                receipt = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
            except (UnicodeError, json.JSONDecodeError, ValueError):
                continue
            if _receipt_matches_entry(
                receipt,
                entry=entry,
                manifest=manifest,
                execution_id=execution_id,
                claim=claim,
                task_cwd=task_cwd,
            ):
                matches.append(entry)
        if len(matches) != 1:
            return None
        result[inner] = matches[0]
    return result
