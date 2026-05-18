"""Execution-facing acceptance criteria normalization for auto-generated Seeds."""

from __future__ import annotations

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
    "`hello_auto() -> str` returns exactly `hello from ooo auto`, "
    "the test imports `hello_auto` and asserts that exact value, and "
    "the exact command `uv run pytest tests/test_hello_auto.py` passes."
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
    if not criteria or not _has_auto_wrapper_context(seed.goal, criteria):
        return seed

    filtered = normalize_observation_execution_criteria(criteria, context_text=seed.goal)
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
        return tuple(dict.fromkeys((_CANONICAL_HELLO_AUTO_OBSERVATION_AC, *passthrough)))

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


def has_auto_wrapper_context(text: str) -> bool:
    """Return true only for the known hello_auto observation prompt shape."""
    lowered = text.casefold()
    return all(marker in lowered for marker in _OBSERVATION_CONTEXT_REQUIRED) and any(
        marker in lowered for marker in _OBSERVATION_CONTEXT_ALTERNATES
    )


def _has_auto_wrapper_context(goal: str, criteria: tuple[str, ...]) -> bool:
    return has_auto_wrapper_context("\n".join((goal, *criteria)))


def _criterion_key(criterion: str) -> str:
    return " ".join(criterion.casefold().strip().rstrip(".").split())


def _normalize_known_observation_execution_line(criterion: str) -> str:
    """Canonicalize only known-equivalent hello_auto execution AC phrasings."""
    key = _criterion_key(criterion)
    hello_return_equivalents = {
        "`hello_auto.py` defines `hello_auto()` returning exactly `hello from ooo auto`",
        "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`",
        "hello_auto.py defines hello_auto() returning exactly hello from ooo auto",
        "hello_auto.py defines hello_auto() -> str returning exactly hello from ooo auto",
    }
    test_file_equivalents = {
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value",
        "tests/test_hello_auto.py imports hello_auto and asserts the exact return value",
    }
    pytest_equivalents = {
        "`uv run pytest tests/test_hello_auto.py` passes",
        "uv run pytest tests/test_hello_auto.py passes",
        "the exact command `uv run pytest tests/test_hello_auto.py` passes",
        "the targeted test command `uv run pytest tests/test_hello_auto.py` passes",
    }
    if key in hello_return_equivalents:
        return (
            "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`."
        )
    if key in test_file_equivalents:
        return "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value."
    if key in pytest_equivalents:
        return "The exact command `uv run pytest tests/test_hello_auto.py` passes."
    return criterion


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
    key = _criterion_key(criterion)
    if key.startswith(
        "a command/api check returns stable observable output or artifacts proving "
        "the original requirement for "
    ):
        subject = key.rsplit("the original requirement for ", 1)[1]
    else:
        subject = key

    if "final observation report" in subject:
        return False
    if "observation report" in subject and "plain chat summary" in subject:
        return False

    return any(
        marker in subject
        for marker in (
            "hello_auto.py",
            "tests/test_hello_auto.py",
            "uv run pytest tests/test_hello_auto.py",
            "hello_auto()",
            "hello from ooo auto",
        )
    )


def _is_observation_report_wrapper(criterion: str) -> bool:
    """Return true for repairer-wrapped observation report requirements."""
    key = _criterion_key(criterion)
    if not key.startswith(
        "a command/api check returns stable observable output or artifacts proving "
        "the original requirement for "
    ):
        return False
    return "observation report" in key or "plain chat summary" in key
