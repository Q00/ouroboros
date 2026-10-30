"""Execution-bound test wrapper receipts backed by command artifact provenance."""

from __future__ import annotations

import ast
import base64
from collections.abc import Mapping
import inspect
import json
from pathlib import Path
import shlex
import sys
import zlib

from ouroboros.core import filesystem_capability
from ouroboros.harness import wrapper_runner
from ouroboros.harness.deliver_gate import (
    _is_canonical_command_artifact_path,
    _journal_command_artifact,
)
from ouroboros.harness.journal import EvidenceEntry, EvidenceManifest
from ouroboros.orchestrator import verify_shell

_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "kind",
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
_RUNNER_SOURCE = Path(wrapper_runner.__file__).read_text(encoding="utf-8")
_CAPABILITY_SOURCE = Path(filesystem_capability.__file__).read_text(encoding="utf-8")
_FROZEN_CAPABILITY = base64.b64encode(zlib.compress(_CAPABILITY_SOURCE.encode("utf-8"))).decode(
    "ascii"
)
_FROZEN_VERIFY_FUNCTION = ast.parse(
    inspect.getsource(verify_shell.sanitized_verify_environment)
).body[0]
if not isinstance(_FROZEN_VERIFY_FUNCTION, ast.FunctionDef):
    raise TypeError("verify environment builder source must be a function")
if (
    _FROZEN_VERIFY_FUNCTION.body
    and isinstance(_FROZEN_VERIFY_FUNCTION.body[0], ast.Expr)
    and isinstance(_FROZEN_VERIFY_FUNCTION.body[0].value, ast.Constant)
    and isinstance(_FROZEN_VERIFY_FUNCTION.body[0].value.value, str)
):
    _FROZEN_VERIFY_FUNCTION.body.pop(0)
_FROZEN_VERIFY_ENVIRONMENT = (
    "from __future__ import annotations\n"
    "from collections.abc import Mapping\n"
    "import os\n"
    "from types import SimpleNamespace\n"
    f"VERIFY_ENV_STRIPPED_KEYS = frozenset({tuple(sorted(verify_shell.VERIFY_ENV_STRIPPED_KEYS))!r})\n"
    f"VERIFY_ENV_STRIPPED_PREFIXES = {verify_shell.VERIFY_ENV_STRIPPED_PREFIXES!r}\n"
    f"{ast.unparse(_FROZEN_VERIFY_FUNCTION)}\n"
    "_WRAPPER_VERIFY_SHELL = SimpleNamespace(sanitized_verify_environment=sanitized_verify_environment)\n"
)
_RUNNER_PROGRAM = (
    "import base64,zlib;"
    f"exec(zlib.decompress(base64.b64decode({_FROZEN_CAPABILITY!r})));"
    "_WRAPPER_DIRECTORY_OPENER=open_nofollow_directory_chain;"
    f"exec({_FROZEN_VERIFY_ENVIRONMENT!r});"
    f"exec({_RUNNER_SOURCE!r})"
)


def trusted_wrapper_invocation(
    outer: str, task_cwd: str | None
) -> tuple[str, str, str, str] | None:
    """Parse the frozen runner command before leasing its output paths."""
    if not isinstance(outer, str) or task_cwd is None:
        return None
    if Path(wrapper_runner.__file__).resolve().is_relative_to(Path(task_cwd).resolve()):
        return None
    try:
        parts = shlex.split(outer)
    except ValueError:
        return None
    if (
        len(parts) < 12
        or parts[:4] != [sys.executable, "-I", "-c", _RUNNER_PROGRAM]
        or parts[4] != "--receipt"
        or parts[6] != "--stdout"
        or parts[8] != "--stderr"
        or parts[10] != "--"
        or not parts[11:]
        or shlex.join(parts) != outer
        or not _is_canonical_command_artifact_path(parts[5])
        or not _is_canonical_command_artifact_path(parts[7])
        or not _is_canonical_command_artifact_path(parts[9])
    ):
        return None
    return parts[5], parts[7], parts[9], shlex.join(parts[11:])


def _trusted_wrapper_paths(
    claim: Mapping[str, object], task_cwd: str | None
) -> tuple[str, str] | None:
    outer = claim["outer_command"]
    inner = claim["inner_command"]
    if not isinstance(outer, str) or not isinstance(inner, str):
        return None
    parsed = trusted_wrapper_invocation(outer, task_cwd)
    if parsed is None or parsed[0] != claim["receipt_path"] or parsed[3] != inner:
        return None
    return parsed[1], parsed[2]


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
    output_paths: tuple[str, str],
) -> bool:
    if not isinstance(receipt, dict) or frozenset(receipt) != _RECEIPT_KEYS:
        return False
    payload = entry.payload
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["kind"] != "ouroboros.test-wrapper-receipt"
        or payload.get("execution_id") != execution_id
        or payload.get("ac_id") != manifest.ac_id
        or len(entry.source_event_ids) != 2
        or claim["outer_command"] != payload.get("command")
        or receipt["inner_command"] != claim["inner_command"]
        or not isinstance(receipt["inner_command"], str)
        or not receipt["inner_command"].strip()
        or receipt["test_result"] != "passed"
        or (receipt["stdout_ref"], receipt["stderr_ref"]) != output_paths
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
        output_paths = _trusted_wrapper_paths(claim, task_cwd)
        if output_paths is None:
            return None
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
                output_paths=output_paths,
            ):
                matches.append(entry)
        if len(matches) != 1:
            return None
        result[inner] = matches[0]
    return result
