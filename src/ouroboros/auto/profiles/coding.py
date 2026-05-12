"""Built-in ``coding`` DomainProfile (#809 P3, PR 2/6).

Packages the existing Python/coding logic from ``answerer.py``,
``grading.py``, and ``safe_defaults.py`` as an immutable ``DomainProfile``
without modifying those modules.  PR-4 and PR-5 will route the callers
through this profile; for now the profile acts as a read-only adapter so
the parity tests can pin equality.
"""

from __future__ import annotations

from pathlib import Path
import re
from types import MappingProxyType
from typing import Any

from ouroboros.auto.domain_profile import DomainProfile

# Keep these lightweight constants local so importing/registering the built-in
# coding profile does not import ``ouroboros.auto.grading`` and its pydantic
# transitive dependencies. Parity tests pin these values against grading.py.
VAGUE_TERMS = (
    "easy",
    "intuitive",
    "robust",
    "scalable",
    "better",
    "improve",
    "optimized",
    "user-friendly",
    "seamless",
)

# ---------------------------------------------------------------------------
# VerifiablePredicate implementations
# ---------------------------------------------------------------------------


def _is_observable_criterion(criterion: str) -> bool:
    """Delegate to the existing grading gate only when predicate matching runs."""
    from ouroboros.auto.grading import _is_observable  # noqa: PLC2701

    return _is_observable(criterion)


def _matches_observable_pattern(criterion: str, *patterns: str) -> bool:
    """Return True only for criteria accepted by the existing grading contract."""
    if not _is_observable_criterion(criterion):
        return False
    lowered = criterion.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


class _ExitCodePredicate:
    """Matches AC text that references exit codes or command success/failure."""

    code = "exit_code"

    def matches(self, criterion: str) -> bool:
        return _matches_observable_pattern(
            criterion,
            r"\bexit\s*code\b",
            r"\bexit(?:s)?\s+(?:0|with|non-zero)\b",
            r"\breturns?\s+(?:0|non-zero|exit)\b",
            r"exit_code",
        )

    def repair_template(self, criterion: str) -> str:
        return f"Command exits 0 on success and non-zero on failure: {criterion}"


class _TestPassPredicate:
    """Matches AC text that references test suites passing."""

    code = "test_pass"

    def matches(self, criterion: str) -> bool:
        return _matches_observable_pattern(
            criterion,
            r"\btests?\s+pass\b",
            r"\ball\s+tests?\b",
            r"\btest\s+suite\b",
            r"\bpytest\b",
            r"\bjest\b",
        )

    def repair_template(self, criterion: str) -> str:
        return f"Run test suite and all tests pass: {criterion}"


class _LintCleanPredicate:
    """Matches AC text that references linting passing."""

    code = "lint_clean"

    def matches(self, criterion: str) -> bool:
        return _matches_observable_pattern(
            criterion,
            r"\blinters?\b",
            r"\blint(?:s|ed|ing)\b",
            r"\bruff\b",
            r"\bflake8\b",
            r"\beslint\b",
            r"\bno\s+lint\s+errors?\b",
        )

    def repair_template(self, criterion: str) -> str:
        return f"Linter reports zero errors: {criterion}"


class _TypeCheckCleanPredicate:
    """Matches AC text that references type checking passing."""

    code = "type_check_clean"

    def matches(self, criterion: str) -> bool:
        return _matches_observable_pattern(
            criterion,
            r"\btype\s*check\b",
            r"\bmypy\b",
            r"\bpyright\b",
            r"\btsc\b",
            r"\bno\s+type\s+errors?\b",
        )

    def repair_template(self, criterion: str) -> str:
        return f"Type checker reports zero errors: {criterion}"


class _ObservableBehaviorPredicate:
    """Fallback predicate that mirrors grading._is_observable()."""

    code = "observable_behavior"

    def matches(self, criterion: str) -> bool:
        return _is_observable_criterion(criterion)

    def repair_template(self, criterion: str) -> str:
        return f"Mention command output, file/artifact, API response, or test result: {criterion}"


# ---------------------------------------------------------------------------
# IntentClassifier adapter
# ---------------------------------------------------------------------------


class _CodingIntentClassifier:
    """Thin adapter over ``_classify_question_intents`` from ``answerer.py``.

    ``supported_intents`` returns the canonical label set drawn from
    ``QuestionIntent`` (defined in ``answerer.py``).  ``classify`` delegates
    to the existing private classifier so intent routing stays in a single
    place until PR-4 migrates callers.
    """

    def classify(self, question: str) -> str | None:
        # Import locally to avoid a circular import at module load time.
        from ouroboros.auto.answerer import (  # noqa: PLC2701
            QuestionIntent,
            _classify_question_intents,
            _has_user_verify_feature_shape,
            _normalize_question,
            _should_preserve_runtime_route,
        )

        intents = _classify_question_intents(question)
        if not intents:
            return None

        # Mirror AutoAnswerer.answer()'s route order, including the
        # user-facing verify-feature demotion that keeps questions like
        # "Can users verify their email?" on the product_behavior path.
        lowered = _normalize_question(question)
        demote_for_user_verify = (
            QuestionIntent.PRODUCT_BEHAVIOR in intents and _has_user_verify_feature_shape(lowered)
        )
        if QuestionIntent.NON_GOALS in intents:
            return QuestionIntent.NON_GOALS.value
        if QuestionIntent.VERIFICATION in intents and not demote_for_user_verify:
            return QuestionIntent.VERIFICATION.value
        if QuestionIntent.ACCEPTANCE_CRITERIA in intents and not demote_for_user_verify:
            return QuestionIntent.ACCEPTANCE_CRITERIA.value
        if QuestionIntent.RUNTIME_CONTEXT in intents and _should_preserve_runtime_route(lowered):
            return QuestionIntent.RUNTIME_CONTEXT.value
        if QuestionIntent.PRODUCT_BEHAVIOR in intents:
            return QuestionIntent.PRODUCT_BEHAVIOR.value
        if QuestionIntent.ACTOR_IO in intents:
            return QuestionIntent.ACTOR_IO.value
        if QuestionIntent.RUNTIME_CONTEXT in intents:
            return QuestionIntent.RUNTIME_CONTEXT.value
        return next(iter(intents)).value

    def supported_intents(self) -> frozenset[str]:
        # Import locally to avoid coupling this adapter's module import to
        # answerer.py while still deriving the supported set from the source
        # enum instead of hard-coding labels.
        from ouroboros.auto.answerer import QuestionIntent

        return frozenset(intent.value for intent in QuestionIntent)


# ---------------------------------------------------------------------------
# RepoContextExtractor adapter
# ---------------------------------------------------------------------------


class _CodingRepoContextExtractor:
    """Thin adapter over ``repo_auto_answer_context`` from ``repo_context.py``."""

    def extract(self, cwd: Path) -> dict[str, Any]:
        from ouroboros.auto.repo_context import repo_auto_answer_context

        ctx = repo_auto_answer_context(cwd)
        return dict(ctx.repo_facts)


# ---------------------------------------------------------------------------
# Safe defaults adapter
# ---------------------------------------------------------------------------


def _build_safe_defaults() -> MappingProxyType[str, Any]:
    """Return an immutable mapping mirroring ``_SAFE_DEFAULTS``.

    ``_SAFE_DEFAULTS`` is ``dict[str, _DefaultSpec]`` where ``_DefaultSpec``
    is a frozen dataclass with ``value`` and ``rationale`` fields.  We expose
    a read-only copy so callers cannot mutate shared defaults on the module
    singleton.  PR-5 will introduce a typed ``_DefaultSpec``-aware schema;
    ``Any`` is intentional here.
    """
    from ouroboros.auto.safe_defaults import _SAFE_DEFAULTS  # noqa: PLC2701

    return MappingProxyType(dict(_SAFE_DEFAULTS))


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


_CODING_FILE_MARKERS = (
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "composer.json",
    "Gemfile",
)
_CODING_DIR_MARKERS = ("src", "tests")


def _coding_detector(cwd: Path) -> float:
    """Return 1.0 if the directory looks like a coding project, else 0.0."""
    has_file_marker = any((cwd / marker).is_file() for marker in _CODING_FILE_MARKERS)
    has_git_marker = (cwd / ".git").exists()
    has_dir_marker = any((cwd / marker).is_dir() for marker in _CODING_DIR_MARKERS)
    return 1.0 if has_file_marker or has_git_marker or has_dir_marker else 0.0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

CODING_PROFILE: DomainProfile = DomainProfile(
    name="coding",
    repo_context_extractor=_CodingRepoContextExtractor(),
    verifiable_predicates=(
        _ExitCodePredicate(),
        _TestPassPredicate(),
        _LintCleanPredicate(),
        _TypeCheckCleanPredicate(),
        _ObservableBehaviorPredicate(),
    ),
    intent_classifier=_CodingIntentClassifier(),
    vague_terms=frozenset(VAGUE_TERMS),
    safe_defaults=_build_safe_defaults(),
    detector=_coding_detector,
)
