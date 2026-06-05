"""Execution-facing acceptance criteria normalization for auto-generated Seeds."""

from __future__ import annotations

import re

from ouroboros.core.seed import Seed

_AUTO_WRAPPER_CRITERIA = frozenset(
    {
        "`ooo auto` is dispatched to the mcp tool `ouroboros_auto`",
        "`ooo auto` is handled by ouroboros auto/mcp, not plain text",
        "final report includes auto session id, seed id, seed path, and test result",
        "final report includes auto session id, seed id, files changed, exact test command, and test result",
        "manual fallback is not used",
        "manual fallback was not used",
        "manual fallback used: no",
        "manual fallback used: false",
        "previous blocker recurrence is reported",
        "previous blocker recurrence: no",
        "previous last_question blocker did not recur",
        "previous seed grade c blocker did not recur",
        "previous interview closure blocker did not recur",
        "recursive auto invocation did not occur",
        "recursive auto invocation occurred: no",
        "report whether recursive auto invocation occurred",
    }
)

_OBSERVATION_REPORT_ONLY_CRITERIA = frozenset(
    {
        "`ooo auto` is dispatched through the installed ouroboros mcp tool, not interpreted as plain text",
        "`ooo auto` is dispatched to the mcp tool `ouroboros_auto`",
        "`ooo auto` is handled by ouroboros auto/mcp, not plain text",
        "whether mcp dispatch succeeded",
        "seed reaches grade a",
        "execution is handed off to the background execution job",
        "the execution job reaches a terminal status without manual cancellation",
        "whether progress accounting stalled at ac 0/n is reported",
        "execution job id",
        "final execution job terminal status",
        "whether manual fallback was used",
        "whether previous blockers recurred",
        "auto session id",
        "seed id and seed path",
        "files changed",
        "exact test command",
        "test result",
    }
)

_OBSERVATION_CONTEXT_REQUIRED = (
    "hello_auto.py",
    "tests/test_hello_auto.py",
)

_OBSERVATION_CONTEXT_ALTERNATES = (
    "ooo auto",
    "ouroboros_auto",
)

_CANONICAL_HELLO_AUTO_OBSERVATION_AC = (
    "Create `hello_auto.py` and `tests/test_hello_auto.py` so "
    "`hello_auto() -> str` returns exactly `{return_value}`, "
    "the test imports `hello_auto` and asserts that exact value, and "
    "the exact command `uv run pytest tests/test_hello_auto.py` passes."
)

_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX = (
    "a command/api check returns stable observable output or artifacts proving "
    "the original requirement for "
)

_HELLO_AUTO_RETURN_EQUIVALENTS = frozenset(
    {
        "`hello_auto.py` defines `hello_auto()` returning exactly `hello from ooo auto`",
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`",
        "hello_auto.py defines hello_auto() returning exactly hello from ooo auto",
        "hello_auto.py defines hello_auto() -> str returning exactly hello from ooo auto",
    }
)

_HELLO_AUTO_TEST_FILE_EQUIVALENTS = frozenset(
    {
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value",
        "tests/test_hello_auto.py imports hello_auto and asserts the exact return value",
        "tests/test_hello_auto.py imports hello_auto and asserts exact return value",
    }
)

_HELLO_AUTO_PYTEST_EQUIVALENTS = frozenset(
    {
        "`uv run pytest tests/test_hello_auto.py` passes",
        "uv run pytest tests/test_hello_auto.py passes",
        "the exact command `uv run pytest tests/test_hello_auto.py` passes",
        "the targeted test command `uv run pytest tests/test_hello_auto.py` passes",
    }
)

_HELLO_AUTO_EXISTENCE_EQUIVALENTS = frozenset(
    {
        "`hello_auto.py` exists",
        "hello_auto.py exists",
        "`tests/test_hello_auto.py` exists",
        "tests/test_hello_auto.py exists",
    }
)

_HELLO_AUTO_OBSERVATION_UNIT_EQUIVALENTS = (
    _HELLO_AUTO_RETURN_EQUIVALENTS
    | _HELLO_AUTO_TEST_FILE_EQUIVALENTS
    | _HELLO_AUTO_PYTEST_EQUIVALENTS
    | _HELLO_AUTO_EXISTENCE_EQUIVALENTS
)

_LIBRARY_DEFAULT_AC_EQUIVALENTS = frozenset(
    {
        "all public api symbols are importable from the documented module path",
        "all public api symbols importable from the documented module path",
        "unit tests cover every public function/method's primary success path",
        "`ruff check` and the project's type-check command exit 0",
        "ruff check and the project's type-check command exit 0",
    }
)

_LIBRARY_CONTEXT_SIGNALS = (
    "library",
    "package",
    "api surface",
    "public api",
    "sdk",
    "importable",
)

_FILE_ARTIFACT_SIGNALS = (
    " file ",
    " file named ",
    " exists",
    " content",
    " full content",
    " single line",
    " exact",
)


def normalize_execution_acceptance(seed: Seed) -> Seed:
    """Remove auto-observation/reporting criteria from execution Seeds.

    Auto observation prompts can include wrapper/reporting duties such as
    dispatch confirmation and final auto-session metadata. Those should not be
    handed to the execution worker as implementation ACs. To avoid mutating
    product requirements, only normalize the known hello_auto observation
    context.
    """
    criteria = tuple(ac for ac in seed.acceptance_criteria if ac and ac.strip())
    direction_context = "\n".join((seed.goal, *seed.constraints))
    if not criteria:
        return seed

    filtered = criteria
    if _has_auto_wrapper_context(seed.goal, criteria):
        filtered = normalize_observation_execution_criteria(
            filtered, context_text=direction_context
        )
    filtered = normalize_file_artifact_execution_criteria(
        filtered,
        context_text=direction_context,
    )
    if not filtered or filtered == criteria:
        return seed
    return seed.model_copy(update={"acceptance_criteria": filtered})


def normalize_observation_execution_criteria(
    criteria: tuple[str, ...],
    *,
    context_text: str = "",
) -> tuple[str, ...]:
    """Return concrete execution criteria for the hello_auto observation task.

    In the observation context, parent/reporting duties must not become worker
    ACs.  Keep only concrete local checks and canonicalize equivalent phrasings
    so the worker sees a small stable AC set.
    """
    if not _has_auto_wrapper_context(context_text, criteria):
        return criteria

    execution_lines: list[str] = []
    for criterion in criteria:
        stripped = criterion.strip()
        if not stripped:
            continue
        if is_auto_reporting_acceptance_criterion(stripped) or _is_observation_report_only_line(
            stripped
        ):
            continue
        if _is_observation_report_wrapper(stripped):
            continue
        execution_lines.append(stripped)

    if _has_complete_hello_auto_observation_unit(context_text, tuple(execution_lines)):
        passthrough = [
            line for line in execution_lines if not _is_hello_auto_observation_unit_line(line)
        ]
        canonical = _canonical_hello_auto_observation_ac(context_text, tuple(execution_lines))
        return tuple(dict.fromkeys((canonical, *passthrough)))

    normalized = [_normalize_known_observation_execution_line(line) for line in execution_lines]
    return tuple(dict.fromkeys(normalized))


def is_auto_reporting_acceptance_criterion(criterion: str) -> bool:
    """Return true only for exact known auto wrapper/report-only criteria.

    Broad observation-only report markers are intentionally handled behind the
    hello_auto observation context gate in ``normalize_observation_execution_criteria``.
    Keeping this standalone helper exact prevents unrelated product requirements
    such as execution-job or progress-accounting features from being classified
    as reporting metadata by a future caller that lacks the observation guard.
    """
    return _criterion_key(criterion) in _AUTO_WRAPPER_CRITERIA


def normalize_file_artifact_execution_criteria(
    criteria: tuple[str, ...],
    *,
    context_text: str = "",
) -> tuple[str, ...]:
    """Drop library defaults from direct file-artifact Seeds.

    When task-class inference falls back to ``library`` for a tiny file
    artifact, the catalog's import/unit-test/lint defaults are unrelated and
    can prevent ``ooo auto`` from reaching the requested runtime. Keep this
    scoped to file-state goals that do not explicitly ask for a library/API.
    """
    if not _has_file_artifact_context(context_text, criteria):
        return criteria

    filtered = tuple(
        criterion for criterion in criteria if not _is_library_default_acceptance(criterion)
    )
    return filtered or criteria


def has_auto_wrapper_context(text: str) -> bool:
    """Return true only for the known hello_auto observation prompt shape."""
    lowered = text.casefold()
    return all(marker in lowered for marker in _OBSERVATION_CONTEXT_REQUIRED) and any(
        marker in lowered for marker in _OBSERVATION_CONTEXT_ALTERNATES
    )


def _has_file_artifact_context(context_text: str, criteria: tuple[str, ...]) -> bool:
    context_lowered = f" {context_text} ".casefold()
    if any(signal in context_lowered for signal in _LIBRARY_CONTEXT_SIGNALS):
        return False
    text = f"{context_lowered}\n" + "\n".join(criteria).casefold()
    lowered = text.casefold()
    if not any(signal in lowered for signal in _FILE_ARTIFACT_SIGNALS):
        return False
    has_file_path = bool(re.search(r"\b[\w.-]+\.[A-Za-z0-9]{1,8}\b", lowered))
    has_file_check = any("exists" in criterion.casefold() for criterion in criteria)
    has_content_check = any(
        marker in criterion.casefold() for criterion in criteria for marker in ("content", "line")
    )
    return has_file_path and (has_file_check or has_content_check)


def _is_library_default_acceptance(criterion: str) -> bool:
    subject = _unwrap_seed_repairer_original_requirement(criterion)
    key = _criterion_key(subject)
    return key in _LIBRARY_DEFAULT_AC_EQUIVALENTS


def _has_auto_wrapper_context(goal: str, criteria: tuple[str, ...]) -> bool:
    return has_auto_wrapper_context("\n".join((goal, *criteria)))


def _criterion_key(criterion: str) -> str:
    return " ".join(criterion.casefold().strip().rstrip(".").split())


def _normalize_known_observation_execution_line(criterion: str) -> str:
    """Canonicalize only known-equivalent hello_auto execution AC phrasings."""
    key = _criterion_key(criterion)
    if key in _HELLO_AUTO_RETURN_EQUIVALENTS:
        return (
            "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`."
        )
    if key in _HELLO_AUTO_TEST_FILE_EQUIVALENTS:
        return "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value."
    if key in _HELLO_AUTO_PYTEST_EQUIVALENTS:
        return "The exact command `uv run pytest tests/test_hello_auto.py` passes."
    return criterion


def _canonical_hello_auto_observation_ac(context_text: str, criteria: tuple[str, ...]) -> str:
    return_value = _extract_hello_auto_return_value("\n".join((context_text, *criteria)))
    return _CANONICAL_HELLO_AUTO_OBSERVATION_AC.format(return_value=return_value)


def _extract_hello_auto_return_value(text: str) -> str:
    for pattern in (
        r"hello_auto\(\)(?:\s*->\s*str)?\s+returns?\s+exactly\s+[`'\"]([^`'\"]+)[`'\"]",
        r"must\s+return\s+exactly\s+[`'\"]([^`'\"]+)[`'\"]",
        r"returning\s+exactly\s+[`'\"]([^`'\"]+)[`'\"]",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return "hello from ooo auto"


def _is_observation_report_only_line(criterion: str) -> bool:
    """Classify exact known observation metadata lines from the parent report."""
    return _criterion_key(criterion) in _OBSERVATION_REPORT_ONLY_CRITERIA


def _has_complete_hello_auto_observation_unit(
    context_text: str,
    criteria: tuple[str, ...],
) -> bool:
    """Return true when the observation asks for the full proof+pytest unit."""
    text = "\n".join((context_text, *criteria)).casefold()
    return (
        "hello_auto.py" in text
        and "tests/test_hello_auto.py" in text
        and "hello from ooo auto" in text
        and "uv run pytest tests/test_hello_auto.py" in text
    )


def _is_hello_auto_observation_unit_line(criterion: str) -> bool:
    """Classify lines that are part of the canonical hello_auto smoke unit."""
    subject = _unwrap_seed_repairer_original_requirement(criterion)
    return _criterion_key(subject) in _HELLO_AUTO_OBSERVATION_UNIT_EQUIVALENTS


def _is_observation_report_wrapper(criterion: str) -> bool:
    """Return true for repairer-wrapped observation report requirements."""
    key = _criterion_key(criterion)
    if not key.startswith(_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX):
        return False
    return "observation report" in key or "plain chat summary" in key


def _unwrap_seed_repairer_original_requirement(criterion: str) -> str:
    key = _criterion_key(criterion)
    if not key.startswith(_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX):
        return criterion
    return key.removeprefix(_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX)
