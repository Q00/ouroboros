"""Helpers for resolving interview initial context from text or files."""

from __future__ import annotations

from pathlib import Path

import yaml

from ouroboros.bigbang.pm_seed import PMSeed
from ouroboros.core.errors import ValidationError
from ouroboros.core.security import InputValidator
from ouroboros.core.types import Result

_PM_SEED_SUFFIXES = {".json", ".yaml", ".yml"}
_TEXT_CONTEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
_MAX_PATH_CANDIDATE_LENGTH = 240


def _is_pm_seed_candidate(seed_path: Path, raw_data: object) -> bool:
    """Return True when a file should be treated as a PM-seed artifact."""
    if seed_path.stem.startswith("pm_seed_"):
        return True
    if isinstance(raw_data, dict):
        pm_id = raw_data.get("pm_id")
        if isinstance(pm_id, str) and pm_id.startswith("pm_seed_"):
            return True
    return False


def _validate_pm_seed_mapping(
    raw_data: dict[object, object], seed_path: Path
) -> Result[None, ValidationError]:
    """Validate the minimum PM-seed contract before conversion."""
    pm_id = raw_data.get("pm_id")
    product_name = raw_data.get("product_name")
    goal = raw_data.get("goal")

    has_pm_seed_filename = seed_path.stem.startswith("pm_seed_")
    has_pm_seed_id = isinstance(pm_id, str) and pm_id.startswith("pm_seed_")

    if (
        not isinstance(pm_id, str)
        or (not pm_id.strip())
        or (not has_pm_seed_id and not has_pm_seed_filename)
    ):
        return Result.err(
            ValidationError(
                "PM seed file must include a non-empty pm_id and either a 'pm_seed_' filename or pm_id",
                field="initial_context",
                value=str(seed_path),
            )
        )

    if not isinstance(product_name, str) or not product_name.strip():
        return Result.err(
            ValidationError(
                "PM seed file must include a non-empty product_name",
                field="initial_context",
                value=str(seed_path),
            )
        )

    if not isinstance(goal, str) or not goal.strip():
        return Result.err(
            ValidationError(
                "PM seed file must include a non-empty goal",
                field="initial_context",
                value=str(seed_path),
            )
        )

    return Result.ok(None)


def _resolve_path_candidate(
    context_candidate: str,
    *,
    cwd: str | Path | None = None,
) -> Path | None:
    """Return a plausible file-path candidate, or None for literal text."""
    if "\n" in context_candidate or len(context_candidate) > _MAX_PATH_CANDIDATE_LENGTH:
        return None

    try:
        candidate_path = Path(context_candidate).expanduser()
    except (OSError, ValueError):
        return None

    if not candidate_path.is_absolute() and cwd is not None:
        candidate_path = Path(cwd).expanduser() / candidate_path

    try:
        if candidate_path.exists() and candidate_path.is_file():
            return candidate_path
    except OSError:
        return None

    return None


def load_pm_seed_as_context(seed_path: Path) -> Result[str, ValidationError]:
    """Load a PMSeed artifact and convert it into dev-interview context."""
    try:
        raw_data = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return Result.err(
            ValidationError(
                f"Failed to load PM seed: {exc}",
                field="initial_context",
                value=str(seed_path),
            )
        )

    if not isinstance(raw_data, dict):
        return Result.err(
            ValidationError(
                "PM seed file must contain an object mapping",
                field="initial_context",
                value=str(seed_path),
            )
        )

    validation_result = _validate_pm_seed_mapping(raw_data, seed_path)
    if validation_result.is_err:
        return validation_result

    pm_seed = PMSeed.from_dict(raw_data)
    context = pm_seed.to_initial_context()
    is_valid, error_message = InputValidator.validate_initial_context(context)
    if not is_valid:
        return Result.err(
            ValidationError(
                error_message,
                field="initial_context",
                value=str(seed_path),
            )
        )

    return Result.ok(context)


def resolve_initial_context_input(
    initial_context: str,
    *,
    cwd: str | Path | None = None,
) -> Result[str, ValidationError]:
    """Resolve interview input from literal text or an existing file path."""
    context_candidate = initial_context.strip()
    if not context_candidate:
        return Result.err(
            ValidationError(
                "Initial context cannot be empty",
                field="initial_context",
                value=initial_context,
            )
        )

    candidate_path = _resolve_path_candidate(context_candidate, cwd=cwd)
    if candidate_path is not None:
        suffix = candidate_path.suffix.lower()
        if suffix in _PM_SEED_SUFFIXES:
            try:
                raw_data = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                return Result.err(
                    ValidationError(
                        f"Failed to read initial context file: {exc}",
                        field="initial_context",
                        value=str(candidate_path),
                    )
                )

            if _is_pm_seed_candidate(candidate_path, raw_data):
                return load_pm_seed_as_context(candidate_path)

        if suffix in _TEXT_CONTEXT_SUFFIXES or suffix in _PM_SEED_SUFFIXES:
            try:
                file_context = candidate_path.read_text(encoding="utf-8")
            except OSError as exc:
                return Result.err(
                    ValidationError(
                        f"Failed to read initial context file: {exc}",
                        field="initial_context",
                        value=str(candidate_path),
                    )
                )

            is_valid, error_message = InputValidator.validate_initial_context(file_context)
            if not is_valid:
                return Result.err(
                    ValidationError(
                        error_message,
                        field="initial_context",
                        value=str(candidate_path),
                    )
                )
            return Result.ok(file_context)

    is_valid, error_message = InputValidator.validate_initial_context(initial_context)
    if not is_valid:
        return Result.err(
            ValidationError(
                error_message,
                field="initial_context",
                value=initial_context,
            )
        )

    return Result.ok(initial_context)
