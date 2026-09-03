"""Strict JSON boundary for extracted acceptance criteria.

Extracted from ``seed_generator`` per Q00/ouroboros#1797. The seed-architect's
acceptance-criteria field is the one place a Seed's success contract is
authored, so its parsing is exact by design: unknown fields are refused rather
than dropped, and a criterion that claims an output assertion without a command
to produce it never becomes a Seed.
"""

from __future__ import annotations

import json

from pydantic import ValidationError as PydanticValidationError

from ouroboros.core.seed import AcceptanceCriterionSpec


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while rejecting keys that would otherwise be lost."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _parse_extracted_acceptance_criteria(raw_value: object) -> tuple[AcceptanceCriterionSpec, ...]:
    """Parse the lossless JSON acceptance-criteria extraction boundary."""
    # Deferred: these still live next to the POSIX lexer and JSON hardening
    # they share with the rest of seed_generator, which imports this module.
    from ouroboros.bigbang.seed_generator import (
        _bounded_json_int,
        _JsonNonFiniteToken,
        _unsupported_verify_command_reason,
    )

    field_label = "ACCEPTANCE_CRITERIA"
    if not isinstance(raw_value, str):
        raise ValueError(f"{field_label} must be a single-line JSON array of objects")
    text = raw_value.strip()
    if not text or "\n" in text or "\r" in text:
        raise ValueError(f"{field_label} must be a single-line JSON array of objects")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_int=_bounded_json_int,
            parse_constant=_JsonNonFiniteToken,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_label} must be a valid JSON array of objects: {exc}") from exc
    if not isinstance(decoded, list):
        raise ValueError(f"{field_label} must be a JSON array of objects")
    if not decoded:
        raise ValueError(f"{field_label} must contain at least one acceptance criterion")

    required_keys = {"description", "verify", "artifacts", "expect"}
    # Optional keys preserve older extraction responses while allowing the
    # generator to author the complete execution contract.
    optional_keys = {"cwd", "replay_safe", "exempt"}
    criteria: list[AcceptanceCriterionSpec] = []
    for index, entry in enumerate(decoded, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"{field_label} entry {index} must be a JSON object")
        keys = set(entry)
        missing = required_keys - keys
        extra = keys - required_keys - optional_keys
        if missing:
            raise ValueError(f"{field_label} entry {index} is missing fields: {sorted(missing)}")
        if extra:
            raise ValueError(f"{field_label} entry {index} has unknown fields: {sorted(extra)}")

        description = entry["description"]
        verify = entry["verify"]
        verify_cwd = entry.get("cwd")
        verify_replay_safe = entry.get("replay_safe", False)
        exempt = entry.get("exempt")
        if verify_cwd is not None and not isinstance(verify_cwd, str):
            raise ValueError(f"{field_label} entry {index} cwd must be a string or NONE")
        if not isinstance(verify_replay_safe, bool):
            raise ValueError(f"{field_label} entry {index} replay_safe must be a boolean")
        if exempt is not None and not isinstance(exempt, str):
            raise ValueError(f"{field_label} entry {index} exempt must be a string or NONE")
        artifacts = entry["artifacts"]
        expect = entry["expect"]
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"{field_label} entry {index} description must be a non-empty string")
        for key, value in (("verify", verify), ("expect", expect)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{field_label} entry {index} {key} must be a non-empty string or NONE"
                )
        if isinstance(artifacts, str):
            if artifacts.strip().upper() != "NONE":
                raise ValueError(
                    f"{field_label} entry {index} artifacts must be a JSON string array or NONE"
                )
            expected_artifacts: object = ()
        elif isinstance(artifacts, list):
            if any(not isinstance(item, str) or not item for item in artifacts):
                raise ValueError(
                    f"{field_label} entry {index} artifacts entries must be non-empty strings"
                )
            expected_artifacts = artifacts
        else:
            raise ValueError(
                f"{field_label} entry {index} artifacts must be a JSON string array or NONE"
            )
        try:
            criterion = AcceptanceCriterionSpec.model_validate(
                {
                    "description": description,
                    "verify_command": verify,
                    "verify_cwd": verify_cwd,
                    "verify_replay_safe": verify_replay_safe,
                    "expected_artifacts": expected_artifacts,
                    "output_assertion": expect,
                    "verify_exemption_reason": exempt,
                }
            )
        except PydanticValidationError as exc:
            raise ValueError(f"Invalid {field_label} entry {index}: {exc}") from exc
        reason = (
            _unsupported_verify_command_reason(criterion.verify_command)
            if criterion.verify_command
            else None
        )
        if reason:
            raise ValueError(
                f"Unsupported verify_command in acceptance criterion {index}: {reason}"
            )
        criteria.append(criterion)
    return tuple(criteria)
