"""Durable contract for the orchestrator-run AC success-contract gate.

Extracted from ``parallel_executor`` per Q00/ouroboros#1797: the verify gate's
outcome type, its checkpoint encoding, and the filesystem oracle that judges
``expected_artifacts`` are one concern with one invariant — a cached gate
result may only stay a pass while the evidence it was based on still holds.

Nothing here executes a verify command; that stays with the executor. This
module owns what a gate outcome *is* and how it survives a checkpoint.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    expected_artifact_workspace_path_error,
)

# How much verify-command output to attach to a durable outcome.
_VERIFY_OUTPUT_TAIL_CHARS = 2000
_WORKSPACE_DIGEST_CHARS = 64


def _mapping_has_exact_keys(value: object, expected: frozenset[str]) -> bool:
    """Inspect at most one key beyond a finite durable-contract schema."""

    if not isinstance(value, Mapping):
        return False
    try:
        iterator = iter(value)
    except Exception:
        return False
    seen: set[str] = set()
    for index in range(len(expected) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return len(seen) == len(expected)
        except Exception:
            return False
        if index >= len(expected) or type(key) is not str or key not in expected or key in seen:
            return False
        seen.add(key)
    return False


@dataclass(frozen=True)
class _VerifyGateOutcome:
    """Outcome of the orchestrator-run AC success-contract gate (PR-V V1)."""

    passed: bool
    reason: str | None
    output_tail: str
    missing_artifacts: tuple[str, ...] = ()
    workspace_mutated: bool = False
    workspace_digest: str | None = None
    # True when the machine has no POSIX shell to run verify_command through.
    # Distinct from a failing command: nothing was judged, so the AC is
    # quarantined as unverifiable rather than reported as a worker failure.
    environment_unverifiable: bool = False


def _serialize_verify_gate_outcome(outcome: object) -> dict[str, object] | None:
    """Encode verify evidence into the JSON-safe checkpoint state."""
    if not isinstance(outcome, _VerifyGateOutcome):
        return None
    if (
        (outcome.reason is not None and not isinstance(outcome.reason, str))
        or not isinstance(outcome.output_tail, str)
        or len(outcome.output_tail) > _VERIFY_OUTPUT_TAIL_CHARS
        or not isinstance(outcome.missing_artifacts, tuple)
        or any(not isinstance(item, str) for item in outcome.missing_artifacts)
        or not isinstance(outcome.workspace_mutated, bool)
        or not isinstance(outcome.environment_unverifiable, bool)
        or (
            outcome.workspace_digest is not None
            and (
                not isinstance(outcome.workspace_digest, str)
                or len(outcome.workspace_digest) != _WORKSPACE_DIGEST_CHARS
                or any(char not in "0123456789abcdef" for char in outcome.workspace_digest)
            )
        )
    ):
        raise RuntimeError("verify gate outcome exceeds its durable evidence bounds")
    return {
        "passed": outcome.passed,
        "reason": outcome.reason,
        "output_tail": outcome.output_tail,
        "missing_artifacts": list(outcome.missing_artifacts),
        "workspace_mutated": outcome.workspace_mutated,
        "workspace_digest": outcome.workspace_digest,
        "environment_unverifiable": outcome.environment_unverifiable,
    }


def _deserialize_verify_gate_outcome(value: object) -> _VerifyGateOutcome | None:
    """Decode checkpointed verify evidence, rejecting malformed payloads."""
    legacy_keys = frozenset(
        {
            "passed",
            "reason",
            "output_tail",
            "missing_artifacts",
            "workspace_mutated",
            "workspace_digest",
        }
    )
    # Checkpoints written before the quarantine flag existed stay readable:
    # re-running a non-idempotent verify_command is worse than defaulting one
    # boolean that only ever suppressed a pass.
    expected_keys = legacy_keys | {"environment_unverifiable"}
    if not (
        _mapping_has_exact_keys(value, expected_keys) or _mapping_has_exact_keys(value, legacy_keys)
    ):
        return None
    assert isinstance(value, Mapping)
    passed = value.get("passed")
    reason = value.get("reason")
    output_tail = value.get("output_tail")
    raw_missing = value.get("missing_artifacts")
    workspace_mutated = value.get("workspace_mutated")
    workspace_digest = value.get("workspace_digest")
    environment_unverifiable = value.get("environment_unverifiable", False)
    if not isinstance(environment_unverifiable, bool):
        return None
    if not isinstance(passed, bool) or not isinstance(output_tail, str):
        return None
    if reason is not None and not isinstance(reason, str):
        return None
    if len(output_tail) > _VERIFY_OUTPUT_TAIL_CHARS:
        return None
    if not isinstance(raw_missing, list) or not all(isinstance(item, str) for item in raw_missing):
        return None
    if not isinstance(workspace_mutated, bool):
        return None
    if workspace_digest is not None:
        if (
            not isinstance(workspace_digest, str)
            or len(workspace_digest) != _WORKSPACE_DIGEST_CHARS
            or any(char not in "0123456789abcdef" for char in workspace_digest)
        ):
            return None
    return _VerifyGateOutcome(
        passed=passed,
        reason=reason,
        output_tail=output_tail,
        missing_artifacts=tuple(raw_missing),
        workspace_mutated=workspace_mutated,
        workspace_digest=workspace_digest,
        environment_unverifiable=environment_unverifiable,
    )


def _missing_expected_artifacts(artifacts: tuple[str, ...], cwd: str) -> tuple[str, ...]:
    """Return the expected artifacts absent relative to ``cwd``.

    Each entry must resolve to an existing file or directory under ``cwd``.
    Absolute paths and ``..`` escapes are rejected — treated as missing with the
    escape named — so a contract cannot be satisfied by files outside the run
    workspace.
    """
    root = Path(cwd).resolve()
    missing: list[str] = []
    for artifact in artifacts:
        path_error = expected_artifact_workspace_path_error(artifact, str(root))
        if path_error is not None:
            missing.append(f"{artifact!r} ({path_error})")
            continue
        try:
            candidate = (root / artifact).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            missing.append(f"{artifact!r} (invalid path: {exc})")
            continue
        if not candidate.is_relative_to(root):
            missing.append(f"{artifact} (escapes workspace)")
            continue
        if not candidate.exists():
            missing.append(artifact)
    return tuple(missing)


def _revalidate_cached_verify_gate_outcome(
    *,
    spec: AcceptanceCriterionSpec,
    cwd: str,
    outcome: _VerifyGateOutcome,
) -> _VerifyGateOutcome:
    """Refresh filesystem evidence without replaying a cached command.

    Verify commands may be non-idempotent, so an atomic result caches their
    outcome for finalization. Expected artifacts live in the shared workspace,
    however, and sibling ACs can delete or replace them after the atomic gate.
    A cached success is therefore valid only while its artifact leg still
    passes at the final acceptance boundary.
    """
    if not outcome.passed or not spec.expected_artifacts:
        return outcome
    missing_artifacts = _missing_expected_artifacts(spec.expected_artifacts, cwd)
    if not missing_artifacts:
        return outcome
    return _VerifyGateOutcome(
        passed=False,
        reason="expected_artifacts missing: " + ", ".join(missing_artifacts),
        output_tail=outcome.output_tail,
        missing_artifacts=missing_artifacts,
        workspace_mutated=outcome.workspace_mutated,
        workspace_digest=outcome.workspace_digest,
        environment_unverifiable=outcome.environment_unverifiable,
    )
