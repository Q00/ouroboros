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
from ouroboros.auto.state import AutoCommitPolicy, AutoWorktreePolicy

DEFAULT_COMMIT_POLICY = AutoCommitPolicy.AC_CHECKPOINT
DEFAULT_WORKTREE_POLICY = AutoWorktreePolicy.AUTO

# Keep these lightweight constants local so importing/registering the built-in
# coding profile does not import ``ouroboros.auto.grading`` and its pydantic
# transitive dependencies. The auto answerer reads them through the profile.
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
    """Delegate to the observable-criterion check only when predicate matching runs."""
    from ouroboros.auto.grading import _is_observable  # noqa: PLC2701

    return _is_observable(criterion)


# How each predicate's criterion is checked, in the criterion's language. A
# repaired criterion keeps the requester's text first and verbatim and gains
# one clause; a generic English prefix would bury it and change its language.
CHECK_SEPARATOR = " \u2014 "
_CHECK_CLAUSES: dict[str, dict[str, str]] = {
    "exit_code": {
        "en": "the command exits 0 on success and non-zero on failure",
        "ko": "성공하면 종료 코드 0으로, 실패하면 0이 아닌 종료 코드로 끝난다",
        "ja": "成功時は終了コード 0、失敗時は 0 以外の終了コードで終わる",
        "zh": "成功时以退出码 0 结束，失败时以非零退出码结束",
        "ru": "при успехе команда завершается с кодом 0, при ошибке с ненулевым кодом",
    },
    "test_pass": {
        "en": "the test suite runs and all tests pass",
        "ko": "테스트를 실행하면 모두 통과한다",
        "ja": "テストスイートを実行するとすべて合格する",
        "zh": "运行测试套件时所有测试都通过",
        "ru": "набор тестов запускается, и все тесты проходят",
    },
    "lint_clean": {
        "en": "the linter reports zero errors",
        "ko": "린터가 오류를 하나도 보고하지 않는다",
        "ja": "リンターのエラーが 0 件である",
        "zh": "代码检查工具报告零个错误",
        "ru": "линтер не сообщает ни об одной ошибке",
    },
    "type_check_clean": {
        "en": "the type checker reports zero errors",
        "ko": "타입 검사기가 오류를 하나도 보고하지 않는다",
        "ja": "型チェッカーのエラーが 0 件である",
        "zh": "类型检查器报告零个错误",
        "ru": "проверка типов не сообщает ни об одной ошибке",
    },
    "observable_behavior": {
        "en": (
            "checked by its command output, a written file or artifact, "
            "an API response, or a test result"
        ),
        "ko": "명령 출력, 작성된 파일이나 산출물, API 응답, 테스트 결과로 확인한다",
        "ja": "コマンド出力、書き出されたファイルや成果物、API 応答、テスト結果で確認する",
        "zh": "通过命令输出、写入的文件或产物、API 响应或测试结果来检查",
        "ru": "проверяется по выводу команды, записанному файлу, ответу API или результату теста",
    },
}


def _with_check(criterion: str, code: str) -> str:
    """``criterion`` verbatim, then how it is checked, in its own language."""
    from ouroboros.auto.answer_text import answer_language

    text = criterion.strip()
    clause = _CHECK_CLAUSES[code][answer_language(text)]
    return f"{text}{CHECK_SEPARATOR}{clause}" if text else clause


def _matches_observable_pattern(criterion: str, *patterns: str) -> bool:
    """Return True only for criteria the observable-criterion check accepts."""
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
        return _with_check(criterion, self.code)


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
        return _with_check(criterion, self.code)


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
        return _with_check(criterion, self.code)


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
        return _with_check(criterion, self.code)


class _ObservableBehaviorPredicate:
    """Fallback predicate that mirrors grading._is_observable()."""

    code = "observable_behavior"

    def matches(self, criterion: str) -> bool:
        return _is_observable_criterion(criterion)

    def repair_template(self, criterion: str) -> str:
        return _with_check(criterion, self.code)


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
    """Return confidence that *cwd* is a coding project.

    Manifest files are strong coding signals.  Generic source-layout markers
    are weaker: they should select coding for plain source checkouts, but not
    dominate more specific profiles.  ``.git`` alone is intentionally ignored
    because Git-backed non-coding repositories are common.
    """
    if any((cwd / marker).is_file() for marker in _CODING_FILE_MARKERS):
        return 1.0
    has_dir_marker = any((cwd / marker).is_dir() for marker in _CODING_DIR_MARKERS)
    return 0.1 if has_dir_marker else 0.0


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
