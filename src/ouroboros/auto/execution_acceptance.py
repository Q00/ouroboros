"""Execution-facing acceptance criteria normalization for auto-generated Seeds."""

from __future__ import annotations

from ouroboros.auto.task_class_application import (
    has_explicit_verification_contract,
    has_explicit_verification_text,
)
from ouroboros.core.seed import ExitCondition, Seed

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


def normalize_execution_acceptance(seed: Seed) -> Seed:
    """Remove auto-observation/reporting criteria from execution Seeds.

    Auto observation prompts can include wrapper/reporting duties such as
    dispatch confirmation and final auto-session metadata. Those should not be
    handed to the execution worker as implementation ACs. To avoid mutating
    product requirements, only normalize the known hello_auto observation
    context.
    """
    criteria = tuple(ac for ac in seed.acceptance_criteria if ac and ac.strip())
    if not criteria:
        return seed

    seed = _relax_minimality_constraints_for_exact_verification(seed)
    seed = _drop_repaired_fragments_when_exact_verification_exists(seed, criteria)
    criteria = tuple(seed.acceptance_criteria)

    if not _has_auto_wrapper_context(seed.goal, criteria):
        return seed

    filtered = normalize_observation_execution_criteria(criteria, context_text=seed.goal)
    if not filtered or filtered == criteria:
        return seed
    return seed.model_copy(update={"acceptance_criteria": filtered})


_MINIMAL_VERIFICATION_METADATA_CONSTRAINT = (
    "Do not add unrelated files, dependencies, or behavior; minimal project metadata "
    "needed to run the exact verification command is allowed."
)

_REAL_VERIFICATION_TOOL_CONSTRAINT = (
    "Use the real verification tool named by the exact command; do not satisfy it "
    "with a local shim or replacement executable. Minimal project metadata and "
    "dependencies needed for that tool are allowed."
)


def _relax_minimality_constraints_for_exact_verification(seed: Seed) -> Seed:
    """Allow the smallest runnable test metadata for exact verification commands.

    In a clean greenfield cwd, an exact command such as ``uv run pytest ...`` may
    require project metadata declaring pytest. Seed repair/generation sometimes
    adds broad "no extra files/dependencies" constraints that make that command
    impossible. Keep the scope guard, but phrase it so verification metadata is
    permitted.
    """
    if not has_explicit_verification_contract(seed):
        return seed

    filtered: list[str] = []
    relaxed = False
    for constraint in seed.constraints:
        if _is_overstrict_exact_verification_constraint(constraint):
            relaxed = True
            continue
        filtered.append(constraint)

    exit_conditions, exit_relaxed = _relax_exit_conditions_for_exact_verification(
        seed.exit_conditions
    )
    if not relaxed and not exit_relaxed:
        return seed
    if _REAL_VERIFICATION_TOOL_CONSTRAINT not in filtered:
        filtered.append(_REAL_VERIFICATION_TOOL_CONSTRAINT)
    if _MINIMAL_VERIFICATION_METADATA_CONSTRAINT not in filtered:
        filtered.append(_MINIMAL_VERIFICATION_METADATA_CONSTRAINT)
    return seed.model_copy(
        update={"constraints": tuple(filtered), "exit_conditions": exit_conditions}
    )


def _drop_repaired_fragments_when_exact_verification_exists(
    seed: Seed,
    criteria: tuple[str, ...],
) -> Seed:
    """Remove repairer-split requirement fragments behind an exact test command.

    The Seed repairer can turn one concrete criterion such as "create these
    files and run this exact pytest command" into multiple generic
    "command/API check proves original requirement" ACs. When a Seed already
    contains a non-repaired executable verification command, those fragments
    only expand runtime work without adding a stronger contract.
    """
    if not has_explicit_verification_contract(seed):
        return seed
    if not any(
        _contains_explicit_verification_command(criterion)
        and not _is_seed_repairer_original_requirement_line(criterion)
        for criterion in criteria
    ):
        return seed

    filtered = tuple(
        criterion
        for criterion in criteria
        if not _is_seed_repairer_original_requirement_line(criterion)
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


def _contains_explicit_verification_command(criterion: str) -> bool:
    return has_explicit_verification_text(criterion)


def _is_overstrict_exact_verification_constraint(constraint: str) -> bool:
    key = _criterion_key(constraint)
    return (
        ("no extra files" in key)
        or ("no additional files" in key)
        or ("avoid new dependencies" in key)
        or ("no new dependencies" in key)
        or ("do not add new dependencies" in key)
        or ("no additional" in key and "dependencies" in key)
        or ("implementation limited to" in key and "test" in key)
    )


def _relax_exit_conditions_for_exact_verification(
    exit_conditions: tuple[ExitCondition, ...],
) -> tuple[tuple[ExitCondition, ...], bool]:
    updated: list[ExitCondition] = []
    relaxed = False
    for condition in exit_conditions:
        if _is_overstrict_exact_verification_constraint(condition.evaluation_criteria):
            relaxed = True
            updated.append(
                condition.model_copy(
                    update={
                        "evaluation_criteria": (
                            "No unrelated dependencies, frameworks, deployment targets, "
                            "credentials, or external side effects are introduced; "
                            "minimal project metadata and dependencies needed to run the "
                            "exact verification command are allowed."
                        )
                    }
                )
            )
            continue
        updated.append(condition)
    return tuple(updated), relaxed


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
    if not _is_seed_repairer_original_requirement_key(key):
        return False
    return "observation report" in key or "plain chat summary" in key


def _unwrap_seed_repairer_original_requirement(criterion: str) -> str:
    key = _criterion_key(criterion)
    if not _is_seed_repairer_original_requirement_key(key):
        return criterion
    return key.removeprefix(_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX)


def _is_seed_repairer_original_requirement_line(criterion: str) -> bool:
    return _is_seed_repairer_original_requirement_key(_criterion_key(criterion))


def _is_seed_repairer_original_requirement_key(key: str) -> bool:
    return key.startswith(_SEED_REPAIRER_ORIGINAL_REQUIREMENT_PREFIX)
