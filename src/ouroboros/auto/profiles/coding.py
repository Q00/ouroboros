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

from ouroboros.auto.domain_profile import DEFAULT_REGISTRY, DomainProfile
from ouroboros.auto.grading import VAGUE_TERMS, _is_observable  # noqa: PLC2701
from ouroboros.auto.repo_context import repo_auto_answer_context
from ouroboros.auto.safe_defaults import _SAFE_DEFAULTS  # noqa: PLC2701

# ---------------------------------------------------------------------------
# VerifiablePredicate implementations
# ---------------------------------------------------------------------------


class _ExitCodePredicate:
    """Matches AC text that references exit codes or command success/failure."""

    code = "exit_code"

    def matches(self, criterion: str) -> bool:
        lowered = criterion.lower()
        return bool(
            re.search(r"\bexit\s*code\b", lowered)
            or re.search(r"\bexit(?:s)?\s+(?:0|with|non-zero)\b", lowered)
            or re.search(r"\breturns?\s+(?:0|non-zero|exit)\b", lowered)
            or "exit_code" in lowered
        )

    def repair_template(self, criterion: str) -> str:
        return f"Command exits 0 on success and non-zero on failure: {criterion}"


class _TestPassPredicate:
    """Matches AC text that references test suites passing."""

    code = "test_pass"

    def matches(self, criterion: str) -> bool:
        lowered = criterion.lower()
        return bool(
            re.search(r"\btests?\s+pass\b", lowered)
            or re.search(r"\ball\s+tests?\b", lowered)
            or re.search(r"\btest\s+suite\b", lowered)
            or re.search(r"\bpytest\b", lowered)
            or re.search(r"\bjest\b", lowered)
        )

    def repair_template(self, criterion: str) -> str:
        return f"Run test suite and all tests pass: {criterion}"


class _LintCleanPredicate:
    """Matches AC text that references linting passing."""

    code = "lint_clean"

    def matches(self, criterion: str) -> bool:
        lowered = criterion.lower()
        return bool(
            re.search(r"\blinters?\b", lowered)
            or re.search(r"\blint(?:s|ed|ing)\b", lowered)
            or re.search(r"\bruff\b", lowered)
            or re.search(r"\bflake8\b", lowered)
            or re.search(r"\beslint\b", lowered)
            or re.search(r"\bno\s+lint\s+errors?\b", lowered)
        )

    def repair_template(self, criterion: str) -> str:
        return f"Linter reports zero errors: {criterion}"


class _TypeCheckCleanPredicate:
    """Matches AC text that references type checking passing."""

    code = "type_check_clean"

    def matches(self, criterion: str) -> bool:
        lowered = criterion.lower()
        return bool(
            re.search(r"\btype\s*check\b", lowered)
            or re.search(r"\bmypy\b", lowered)
            or re.search(r"\bpyright\b", lowered)
            or re.search(r"\btsc\b", lowered)
            or re.search(r"\bno\s+type\s+errors?\b", lowered)
        )

    def repair_template(self, criterion: str) -> str:
        return f"Type checker reports zero errors: {criterion}"


class _ObservableBehaviorPredicate:
    """Fallback predicate that mirrors grading._is_observable()."""

    code = "observable_behavior"

    def matches(self, criterion: str) -> bool:
        return _is_observable(criterion)

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

    _SUPPORTED: frozenset[str] = frozenset(
        {
            "non_goals",
            "verification",
            "acceptance_criteria",
            "actor_io",
            "runtime_context",
            "product_behavior",
        }
    )

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
        return self._SUPPORTED


# ---------------------------------------------------------------------------
# RepoContextExtractor adapter
# ---------------------------------------------------------------------------


class _CodingRepoContextExtractor:
    """Thin adapter over ``repo_auto_answer_context`` from ``repo_context.py``."""

    def extract(self, cwd: Path) -> dict[str, Any]:
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
    return MappingProxyType(dict(_SAFE_DEFAULTS))


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


def _coding_detector(cwd: Path) -> float:
    """Return 1.0 if the directory looks like a coding project, else 0.0."""
    if (cwd / "pyproject.toml").is_file():
        return 1.0
    return 0.0


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

# Register with the module-level singleton so any caller that imports this
# module benefits from auto-registration.  The guard prevents double-
# registration on reimport (e.g. in pytest with importmode=importlib).
if DEFAULT_REGISTRY.get("coding") is None:
    DEFAULT_REGISTRY.register(CODING_PROFILE)
