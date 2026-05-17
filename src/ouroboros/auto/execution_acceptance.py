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

_OBSERVATION_CONTEXT_REQUIRED = (
    "hello_auto.py",
    "tests/test_hello_auto.py",
)

_OBSERVATION_CONTEXT_ALTERNATES = (
    "ooo auto",
    "ouroboros_auto",
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

    keep_return = False
    keep_test_file = False
    keep_pytest = False
    passthrough: list[str] = []
    for criterion in criteria:
        stripped = criterion.strip()
        if not stripped or is_auto_reporting_acceptance_criterion(stripped):
            continue
        lowered = stripped.casefold()
        if _is_observation_report_only_line(lowered):
            continue
        if "uv run pytest" in lowered and "tests/test_hello_auto.py" in lowered:
            keep_pytest = True
            continue
        if "tests/test_hello_auto.py" in lowered:
            keep_test_file = True
            continue
        if "hello_auto.py" in lowered:
            keep_return = True
            continue
        passthrough.append(stripped)

    canonical: list[str] = []
    if keep_return:
        canonical.append(
            "`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`."
        )
    if keep_test_file:
        canonical.append(
            "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value."
        )
    if keep_pytest:
        canonical.append("The exact command `uv run pytest tests/test_hello_auto.py` passes.")

    return tuple(dict.fromkeys((*canonical, *passthrough)))


def is_auto_reporting_acceptance_criterion(criterion: str) -> bool:
    """Return true for known auto wrapper/report-only criteria."""
    key = _criterion_key(criterion)
    return key in _AUTO_WRAPPER_CRITERIA or _is_observation_report_only_line(key)


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


def _is_observation_report_only_line(lowered: str) -> bool:
    """Classify observation metadata lines that belong to the parent report."""
    report_markers = (
        "mcp dispatch",
        "dispatched through the installed ouroboros mcp tool",
        "dispatched to the mcp tool",
        "handled by ouroboros auto/mcp",
        "manual fallback",
        "auto session id",
        "seed id",
        "seed path",
        "seed grade",
        "seed reaches grade",
        "execution is handed off",
        "execution job",
        "execution id",
        "run session id",
        "run projection id",
        "terminal status",
        "non-terminal",
        "progress accounting",
        "recursive auto",
        "previous blocker",
        "last_question blocker",
        "interview open-gaps",
        "files changed",
        "exact test command",
        "test result",
        "final report",
        "after auto finishes",
    )
    return any(marker in lowered for marker in report_markers)
