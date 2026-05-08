"""Conservative source-tagged auto answers for Socratic interview prompts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import re

from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger


class AutoAnswerSource(StrEnum):
    """Source categories for generated auto answers."""

    USER_GOAL = "user_goal"
    REPO_FACT = "repo_fact"
    EXISTING_CONVENTION = "existing_convention"
    USER_PREFERENCE = "user_preference"
    CONSERVATIVE_DEFAULT = "conservative_default"
    ASSUMPTION = "assumption"
    NON_GOAL = "non_goal"
    BLOCKER = "blocker"


class QuestionIntent(StrEnum):
    """Ledger-level intent inferred from an interview question."""

    NON_GOALS = "non_goals"
    VERIFICATION = "verification"
    ACCEPTANCE_CRITERIA = "acceptance_criteria"
    ACTOR_IO = "actor_io"
    RUNTIME_CONTEXT = "runtime_context"
    PRODUCT_BEHAVIOR = "product_behavior"


@dataclass(frozen=True, slots=True)
class AutoAnswerContext:
    """Bounded facts supplied by a caller before answering interview questions.

    The answerer remains deterministic and does not inspect the repository on its
    own; callers can pass already-collected facts with optional evidence labels.

    ``user_preferences`` carries caller-supplied preferences keyed by ledger
    section name (e.g. ``runtime_context``, ``constraints``, ``non_goals``).
    Only the Driver/MCP layer is allowed to populate this — the deterministic
    answerer just reads it. Matching answers are tagged
    :attr:`AutoAnswerSource.USER_PREFERENCE` so provenance survives in the
    ledger.
    """

    repo_facts: Mapping[str, str] = field(default_factory=dict)
    evidence: Mapping[str, Sequence[str]] = field(default_factory=dict)
    user_preferences: Mapping[str, str] = field(default_factory=dict)

    def runtime_fact(self) -> tuple[str, Sequence[str]] | None:
        """Return a complete runtime/project fact when one was supplied.

        Narrow facts such as ``framework`` or ``package_manager`` are useful
        evidence, but they do not by themselves answer the stronger
        ``runtime_context`` ledger contract.
        """
        for key in ("runtime_context", "project_runtime"):
            value = self.repo_facts.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), self.evidence.get(key, ())
        return None

    def partial_runtime_facts(self) -> tuple[tuple[str, str, Sequence[str]], ...]:
        """Return bounded runtime-adjacent facts that are not complete context."""
        facts: list[tuple[str, str, Sequence[str]]] = []
        for key in ("framework", "package_manager", "project_structure"):
            value = self.repo_facts.get(key)
            if isinstance(value, str) and value.strip():
                facts.append((key, value.strip(), self.evidence.get(key, ())))
        return tuple(facts)


@dataclass(frozen=True, slots=True)
class AutoBlocker:
    """A hard blocker that should stop auto convergence."""

    reason: str
    question: str


@dataclass(frozen=True, slots=True)
class AutoAnswer:
    """Answer plus structured ledger updates."""

    text: str
    source: AutoAnswerSource
    confidence: float
    ledger_updates: list[tuple[str, LedgerEntry]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    blocker: AutoBlocker | None = None
    # True only when the answer came from the catch-all generic-default route
    # (``_default_answer``). Feature-specific helpers
    # (e.g. ``_feature_acceptance_answer``, ``_runtime_answer``) leave this
    # ``False`` even when their ``source`` is ``CONSERVATIVE_DEFAULT``, so the
    # interview driver can preserve repeated specific follow-ups instead of
    # treating them as repeated generic fallbacks.
    generic_default: bool = False

    @property
    def prefixed_text(self) -> str:
        """Return the text sent back to the interview handler."""
        return f"[from-auto][{self.source.value}] {self.text}"


class AutoAnswerer:
    """Policy engine for bounded auto interview answers.

    This class is deterministic and performs no unbounded repository or network
    exploration.  Later integrations may pass bounded repo facts into it.
    """

    def answer(
        self,
        question: str,
        ledger: SeedDraftLedger,
        context: AutoAnswerContext | None = None,
    ) -> AutoAnswer:
        """Answer ``question`` using a conservative policy and optional bounded facts."""
        context = context or AutoAnswerContext()
        lowered = _normalize_question(question)
        intents = _classify_question_intents(question)
        blocker = _blocker_for(question)
        if blocker is not None:
            return AutoAnswer(
                text=f"Cannot safely decide automatically: {blocker.reason}",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=blocker,
            )

        if QuestionIntent.NON_GOALS in intents:
            # NON_GOAL source is grounded (explicit non-goal) and is NOT
            # upgradable to USER_PREFERENCE — observed in priority order.
            return self._non_goal_answer(question, ledger)
        # When PRODUCT_BEHAVIOR is also inferred, prefer it over VERIFICATION
        # and ACCEPTANCE_CRITERIA so feature questions like
        # ``"Can users verify their email?"`` (and the multilingual siblings
        # ``"Les utilisateurs peuvent-ils vérifier leur e-mail ?"``,
        # ``"用户可以验证他们的电子邮件吗？"``, ``"사용자가 이메일을
        # 확인할 수 있나요?"``) preserve the product feature contract instead
        # of being collapsed into a generic verification-plan template.
        # Demote VERIFICATION / ACCEPTANCE_CRITERIA in favour of
        # PRODUCT_BEHAVIOR ONLY when the question is a user-facing
        # verification feature (actor + permission modal + verify-verb,
        # without a first-person-plural meta subject).  Other
        # product-behavior matchers (English ``should…delete``, Spanish
        # ``pueden…eliminar``, etc.) match the INNER permission clause of
        # meta-verify questions like ``"Should we verify users can delete
        # branches?"`` — those should still route to ``_verification_answer``,
        # not ``_product_behavior_answer``.
        demote_for_user_verify = (
            QuestionIntent.PRODUCT_BEHAVIOR in intents and _has_user_verify_feature_shape(lowered)
        )
        if QuestionIntent.VERIFICATION in intents and not demote_for_user_verify:
            return _maybe_apply_user_preference(
                self._verification_answer(question),
                _INTENT_TO_SECTIONS[QuestionIntent.VERIFICATION],
                context,
            )
        if QuestionIntent.ACCEPTANCE_CRITERIA in intents and not demote_for_user_verify:
            return _maybe_apply_user_preference(
                self._feature_acceptance_answer(question),
                _INTENT_TO_SECTIONS[QuestionIntent.ACCEPTANCE_CRITERIA],
                context,
            )
        if QuestionIntent.RUNTIME_CONTEXT in intents and _should_preserve_runtime_route(lowered):
            answer = self._runtime_answer(question, context)
        elif QuestionIntent.PRODUCT_BEHAVIOR in intents:
            answer = self._product_behavior_answer(question)
        elif QuestionIntent.ACTOR_IO in intents:
            answer = self._io_actor_answer(question)
        elif QuestionIntent.RUNTIME_CONTEXT in intents:
            answer = self._runtime_answer(question, context)
        else:
            answer = self._default_answer(question, ledger)

        # Try a USER_PREFERENCE upgrade for upgradable answers (CONSERVATIVE_DEFAULT
        # / ASSUMPTION sources only). Stronger sources (REPO_FACT,
        # EXISTING_CONVENTION, NON_GOAL, USER_GOAL) are preserved as-is.
        answer = _maybe_apply_user_preference(answer, _section_hints_for_intents(intents), context)

        # When the chosen route produced a non-grounded fallback (ASSUMPTION,
        # EXISTING_CONVENTION, CONSERVATIVE_DEFAULT) for a question that the
        # safe-allowlist recognises as a regulated-product question, re-route
        # to _product_behavior_answer() so the regulated-feature semantics
        # (regulated noun, subject-specific constraints) are preserved in the
        # ledger instead of being replaced by a generic IO/runtime/default
        # template. Grounded answers (REPO_FACT etc.) are NOT in
        # _RISKY_FALLBACK_SOURCES and so are left untouched — preserving the
        # existing runtime/IO contract that "concrete repo fact wins".
        if answer.source in _RISKY_FALLBACK_SOURCES and _is_safe_product_regulated_question(
            lowered
        ):
            answer = self._product_behavior_answer(question)

        if answer.source in _RISKY_FALLBACK_SOURCES:
            risky_blocker = _risky_fallback_blocker_for(question, lowered)
            if risky_blocker is not None:
                return AutoAnswer(
                    text=(
                        "Cannot safely decide automatically with a generic default: "
                        f"{risky_blocker.reason}"
                    ),
                    source=AutoAnswerSource.BLOCKER,
                    confidence=1.0,
                    blocker=risky_blocker,
                )
        return answer

    def answer_gap(
        self,
        section: str,
        ledger: SeedDraftLedger,
        context: AutoAnswerContext | None = None,
    ) -> AutoAnswer:
        """Return an answer targeted at an unresolved required ledger section.

        This path is used when the backend keeps asking broad prompts and the
        normal question-shaped answer would repeat or fail to close any open
        required gap.  Keep it domain-neutral: it only fills Seed contract
        sections with conservative, reversible defaults.
        """
        context = context or AutoAnswerContext()
        if section == "goal":
            blocker = AutoBlocker(reason="goal is unresolved", question="Clarify the primary goal.")
            return AutoAnswer(
                text="Cannot safely decide automatically: goal is unresolved",
                source=AutoAnswerSource.BLOCKER,
                confidence=1.0,
                blocker=blocker,
            )
        if section in {"actors", "inputs", "outputs"}:
            return _maybe_apply_user_preference(
                self._io_actor_answer("Who are the actors, inputs, and outputs for this task?"),
                (section,),
                context,
            )
        if section in {"constraints", "failure_modes"}:
            return _maybe_apply_user_preference(
                self._default_answer(
                    "What conservative constraints and failure modes should bound this MVP?",
                    ledger,
                ),
                (section,),
                context,
            )
        if section == "non_goals":
            # NON_GOAL source is not upgradable; preference will be ignored here
            # by design (grounded sources beat caller preferences).
            return self._non_goal_answer(
                "What non-goals should explicitly remain out of scope?", ledger
            )
        if section in {"acceptance_criteria", "verification_plan"}:
            return _maybe_apply_user_preference(
                self._verification_answer("Which command output verifies the acceptance criteria?"),
                (section,),
                context,
            )
        if section == "runtime_context":
            return _maybe_apply_user_preference(
                self._runtime_answer(
                    "Which runtime stack, repo, and project patterns should be used?", context
                ),
                ("runtime_context",),
                context,
            )
        blocker = AutoBlocker(
            reason=f"unsupported ledger gap section: {section}",
            question=f"Clarify {section}.",
        )
        return AutoAnswer(
            text=f"Cannot safely decide automatically: unsupported ledger gap section: {section}",
            source=AutoAnswerSource.BLOCKER,
            confidence=1.0,
            blocker=blocker,
        )

    def apply(self, answer: AutoAnswer, ledger: SeedDraftLedger, *, question: str) -> None:
        """Apply answer updates to ``ledger``."""
        ledger.record_qa(question, answer.prefixed_text)
        if answer.blocker is not None:
            ledger.add_entry(
                "constraints",
                LedgerEntry(
                    key="blocker.auto_answer",
                    value=answer.blocker.reason,
                    source=LedgerSource.BLOCKER,
                    confidence=1.0,
                    status=LedgerStatus.BLOCKED,
                    reversible=False,
                    rationale=f"Auto mode cannot safely answer: {answer.blocker.question}",
                ),
            )
        for section, entry in answer.ledger_updates:
            ledger.add_entry(section, entry)

    def _non_goal_answer(self, question: str, ledger: SeedDraftLedger) -> AutoAnswer:  # noqa: ARG002
        goal_text = _latest_resolved_goal(ledger).lower()
        excluded = ["cloud sync", "paid services"]
        identity_terms = (
            r"auth|authentication|authorization|authorize|login|sign[- ]?in|signup|"
            r"password|sso|single sign[- ]?on|oauth|oidc|saml|identity|"
            r"role[- ]?based|roles?|permissions?|access control"
        )
        if not re.search(rf"\b({identity_terms})\b", goal_text):
            excluded.append("authentication")
        if not re.search(r"\b(production|prod|deploy|deployment|release|publish)\b", goal_text):
            excluded.append("production deployment")
        value = (
            f"For auto MVP scope, {', '.join(excluded)} are non-goals unless explicitly requested."
        )
        entry = LedgerEntry(
            key="non_goals.mvp_scope",
            value=value,
            source=LedgerSource.NON_GOAL,
            confidence=0.86,
            status=LedgerStatus.DEFAULTED,
            rationale="Conservative auto policy bounds MVP scope.",
        )
        return AutoAnswer(
            value, AutoAnswerSource.NON_GOAL, 0.86, [("non_goals", entry)], non_goals=[value]
        )

    def _verification_answer(self, question: str) -> AutoAnswer:  # noqa: ARG002
        value = "Success must be verified with observable behavior: commands or tests should produce stable output, non-zero failures for invalid input, and reproducible artifacts where applicable."
        updates = [
            (
                "verification_plan",
                LedgerEntry(
                    key="verification.observable",
                    value=value,
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.84,
                    status=LedgerStatus.DEFAULTED,
                    rationale="A-grade Seeds require testable acceptance criteria.",
                ),
            ),
            (
                "acceptance_criteria",
                LedgerEntry(
                    key="acceptance.observable_behavior",
                    value="A command-level check returns exit code 0 and stdout contains stable output or writes a reproducible artifact for each acceptance criterion.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.82,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Converts vague completion into testable behavior.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.84, updates)

    def _feature_acceptance_answer(self, question: str) -> AutoAnswer:
        subject = _acceptance_subject(question)
        value = (
            f"Acceptance for {subject} must cover the requested behavior directly: "
            "a successful operation returns an observable status/output, invalid input fails "
            "with a non-zero/error status, and any persisted artifact or state change can be verified."
        )
        updates = [
            (
                "acceptance_criteria",
                LedgerEntry(
                    key=f"acceptance.{_slug_key(subject)}",
                    value=value,
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.82,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Preserves feature-specific acceptance semantics from the interview question.",
                ),
            ),
            (
                "verification_plan",
                LedgerEntry(
                    key=f"verification.{_slug_key(subject)}",
                    value=f"Verify {subject} with command/API checks for success, failure, and persisted state or output.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.8,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Feature-specific acceptance requires observable verification.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.82, updates)

    def _runtime_answer(self, question: str, context: AutoAnswerContext) -> AutoAnswer:  # noqa: ARG002
        supplied_fact = context.runtime_fact()
        partial_facts = context.partial_runtime_facts()
        partial_evidence = [
            evidence for _, _, evidence_items in partial_facts for evidence in evidence_items
        ]
        partial_summary = "; ".join(f"{key}: {value}" for key, value, _ in partial_facts)
        partial_entries = [
            (
                "runtime_context",
                LedgerEntry(
                    key=f"runtime.partial.{key}",
                    value=value,
                    source=LedgerSource.REPO_FACT,
                    confidence=0.72,
                    status=LedgerStatus.WEAK,
                    rationale=(
                        "Bounded repository fact informs runtime selection but does not "
                        "fully confirm the runtime_context contract."
                    ),
                    evidence=list(evidence_items),
                ),
            )
            for key, value, evidence_items in partial_facts
        ]
        if supplied_fact is not None:
            value, evidence = supplied_fact
            runtime_entry = LedgerEntry(
                key="runtime.repo_fact",
                value=value,
                source=LedgerSource.REPO_FACT,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
                rationale="Bounded repository context was supplied to auto answerer.",
                evidence=list(evidence),
            )
            answer_source = AutoAnswerSource.REPO_FACT
            confidence = 0.9
        else:
            value = "Use the existing repository runtime, package manager, and architectural patterns; avoid new dependencies unless required by acceptance criteria."
            if partial_summary:
                value = f"{value} Supplied repo facts: {partial_summary}."
            runtime_entry = LedgerEntry(
                key="runtime.existing_project",
                value=value,
                source=LedgerSource.EXISTING_CONVENTION,
                confidence=0.8 if partial_facts else 0.78,
                status=LedgerStatus.DEFAULTED,
                rationale=(
                    "Auto mode should avoid unnecessary stack choices; supplied partial "
                    "repo facts are recorded separately and do not confirm full runtime context."
                    if partial_facts
                    else "Auto mode should avoid unnecessary stack choices."
                ),
                evidence=partial_evidence,
            )
            answer_source = AutoAnswerSource.EXISTING_CONVENTION
            confidence = 0.8 if partial_facts else 0.78
        updates = [
            (
                "runtime_context",
                runtime_entry,
            ),
            *partial_entries,
            (
                "constraints",
                LedgerEntry(
                    key="constraints.no_unnecessary_dependencies",
                    value="Do not add new dependencies unless they are necessary to satisfy explicit acceptance criteria.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.86,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Reduces execution risk and review scope.",
                ),
            ),
        ]
        return AutoAnswer(value, answer_source, confidence, updates)

    def _io_actor_answer(self, question: str) -> AutoAnswer:  # noqa: ARG002
        value = "Assume a single local user operating through the requested interface; inputs and outputs should be explicit command/API arguments and stable returned text or artifacts."
        updates = [
            (
                "actors",
                LedgerEntry(
                    key="actors.single_local_user",
                    value="Single local user",
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.76,
                    status=LedgerStatus.DEFAULTED,
                    rationale="No multi-user requirement was provided.",
                ),
            ),
            (
                "inputs",
                LedgerEntry(
                    key="inputs.explicit_arguments",
                    value="Explicit command/API arguments derived from the task goal",
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.74,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Auto mode needs concrete IO to generate testable Seeds.",
                ),
            ),
            (
                "outputs",
                LedgerEntry(
                    key="outputs.stable_text_or_artifacts",
                    value="Stable text output or generated artifacts suitable for verification",
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.74,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Outputs must be observable for A-grade testability.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.ASSUMPTION, 0.76, updates, assumptions=[value])

    def _product_behavior_answer(self, question: str) -> AutoAnswer:
        subject = _acceptance_subject(question)
        value = (
            f"Treat this requested product behavior as in scope for the MVP: {subject}. "
            "Implement it directly and make the resulting state, output, or API response observable."
        )
        key = _slug_key(subject)
        updates = [
            (
                "constraints",
                LedgerEntry(
                    key=f"constraints.behavior.{key}",
                    value=f"Preserve the product behavior requested by the interview question: {subject}",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.8,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Safe product-semantics questions should not be collapsed into a generic MVP policy.",
                ),
            ),
            (
                "acceptance_criteria",
                LedgerEntry(
                    key=f"acceptance.behavior.{key}",
                    value=f"A command or API check for {subject} returns exit code 0 or HTTP 2xx status, and stdout, response body, or a persisted file contains evidence of the requested behavior.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.78,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Feature semantics from the interview question must remain visible in the Seed contract.",
                ),
            ),
        ]
        return AutoAnswer(value, AutoAnswerSource.CONSERVATIVE_DEFAULT, 0.8, updates)

    def _default_answer(self, question: str, ledger: SeedDraftLedger) -> AutoAnswer:  # noqa: ARG002
        value = "Proceed with a conservative MVP: keep scope small, prefer existing project patterns, document assumptions, and make completion verifiable with observable acceptance criteria."
        updates = [
            (
                "constraints",
                LedgerEntry(
                    key="constraints.conservative_mvp",
                    value="Keep the implementation to the smallest safe MVP that satisfies the task goal.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.82,
                    status=LedgerStatus.DEFAULTED,
                    rationale="Default auto policy favors safe convergence.",
                ),
            ),
            (
                "failure_modes",
                LedgerEntry(
                    key="failure_modes.unverified_or_scope_creep",
                    value="Failure includes unverified behavior, non-reproducible output, or scope expansion beyond the MVP.",
                    source=LedgerSource.CONSERVATIVE_DEFAULT,
                    confidence=0.8,
                    status=LedgerStatus.DEFAULTED,
                    rationale="A-grade Seeds need explicit failure boundaries.",
                ),
            ),
        ]
        return AutoAnswer(
            value,
            AutoAnswerSource.CONSERVATIVE_DEFAULT,
            0.82,
            updates,
            generic_default=True,
        )


_INTENT_CUES: Mapping[QuestionIntent, tuple[str, ...]] = {
    QuestionIntent.NON_GOALS: (
        "non-goal",
        "non goal",
        "out of scope",
        "scope boundary",
        "exclude",
        "not do",
        "won't do",
        "will not do",
        "no hacer",
        "fuera de alcance",
        "hors périmètre",
        "hors perimetre",
        "nicht ziel",
        "fuera del alcance",
        "범위 제외",
        "하지 않을",
        "비목표",
        "対象外",
        "不在范围",
        "范围外",
        "不做",
        "非目标",
    ),
    QuestionIntent.VERIFICATION: (
        "verify",
        "verification",
        "validate",
        "validation",
        # Spanish / German verify-verb infinitives so meta-verify questions
        # like ``"¿Deberíamos verificar que los usuarios pueden eliminar
        # ramas?"`` and ``"Sollten wir verifizieren, ob Benutzer Branches
        # löschen können?"`` still classify as VERIFICATION (the routing
        # layer then preserves the verification path because the question
        # has a first-person-plural meta subject).
        "verificar",
        "comprobar",
        "confirmar",
        "verifizieren",
        "validieren",
        "bestätigen",
        "bestaetigen",
        # NB: ``"test"`` is intentionally NOT in this list — bare substring
        # matching of ``"test"`` would silently route unrelated questions
        # like ``"Should users contest charges?"`` or ``"What is the latest
        # output path?"`` into ``_verification_answer()``.  ``\btests?\b``
        # already lives in ``_is_verification_question`` and uses regex
        # word boundaries, so the verification path keeps full coverage of
        # genuine "test"/"tests" questions without the false-positive risk.
        "definition of done",
        "done criteria",
        "how know it works",
        "cómo verific",
        "como verific",
        "validar",
        "vérifier",
        "verifier",
        "vérification",
        "verifikation",
        "검증",
        "테스트",
        "확인",
        "検証",
        "测试",
        "驗證",
        "验证",
    ),
    QuestionIntent.ACCEPTANCE_CRITERIA: (
        "acceptance criteria",
        "acceptance criterion",
        # NB: bare ``"acceptance"`` is intentionally NOT a cue — it would
        # silently match property/status questions like ``"What is the
        # acceptance status?"`` and route them through
        # ``_feature_acceptance_answer()``.  ``_is_feature_acceptance_question``
        # already covers genuine English acceptance-criteria questions via a
        # ``\b(acceptance|criteria)\b`` pre-filter combined with stronger
        # shape checks, so we don't need a bare cue for English coverage.
        "success criteria",
        "completion criteria",
        "criterios de aceptación",
        "criterios de aceptacion",
        "critères d'acceptation",
        "criteres d'acceptation",
        "akzeptanzkriterien",
        "인수 조건",
        "수락 기준",
        "허용 기준",
        "受け入れ基準",
        "验收标准",
        "驗收標準",
    ),
    QuestionIntent.ACTOR_IO: (
        "actor",
        "actors",
        "user",
        "users",
        "persona",
        "stakeholder",
        "input",
        "inputs",
        "output",
        "outputs",
        "argument",
        "arguments",
        "usuario",
        "usuarios",
        "entrada",
        "entradas",
        "salida",
        "salidas",
        "utilisateur",
        "utilisateurs",
        "entrée",
        "entree",
        "sortie",
        "benutzer",
        "eingabe",
        "ausgabe",
        "사용자",
        "입력",
        "출력",
        "利用者",
        "ユーザー",
        "入力",
        "出力",
        "用户",
        "使用者",
        "输入",
        "輸入",
        "输出",
        "輸出",
    ),
    # Runtime cues must be repository-/runtime-specific.  Broad design
    # nouns like ``architecture`` / ``estructura`` / ``cadre`` were removed
    # because they leak into product/design questions
    # (e.g. ``¿Qué estructura usamos para los datos?``) once paired with a
    # generic selection verb and silently mutate ``runtime_context`` /
    # ``constraints`` ledger entries.  Phrase-level variants like
    # ``estructura del proyecto`` and ``project structure`` stay because
    # the phrase itself is anchored to the project's runtime contract.
    QuestionIntent.RUNTIME_CONTEXT: (
        "runtime",
        "stack",
        "repo",
        "repository",
        "framework",
        "package manager",
        "project structure",
        "project runtime",
        "estructura del proyecto",
        "repositorio",
        "référentiel",
        "referentiel",
        "gestionnaire de paquets",
        "projektstruktur",
        "laufzeit",
        "저장소",
        "레포",
        "런타임",
        "프레임워크",
        "패키지 매니저",
        "프로젝트 구조",
        "リポジトリ",
        "ランタイム",
        "フレームワーク",
        "项目结构",
        "專案結構",
        "运行时",
        "執行環境",
        "框架",
    ),
}


def _normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question.casefold()).strip()


def _classify_question_intents(question: str) -> frozenset[QuestionIntent]:
    """Classify interview text by ledger intent, not only English phrasing.

    The classifier intentionally maps broad concept cues to ledger sections and
    then lets the existing handlers produce conservative answers.  Unknown
    questions return no intent and keep the existing default fallback.
    """
    lowered = _normalize_question(question)
    intents: set[QuestionIntent] = set()

    if _matches_any(
        lowered, (r"\bnon-goals?\b", r"\bout of scope\b", r"\bexclude\b", r"\bnot do\b")
    ) or _contains_intent_cue(lowered, QuestionIntent.NON_GOALS):
        intents.add(QuestionIntent.NON_GOALS)
    if _is_verification_question(lowered) or _contains_intent_cue(
        lowered, QuestionIntent.VERIFICATION
    ):
        intents.add(QuestionIntent.VERIFICATION)
    if _is_feature_acceptance_question(lowered) or _contains_intent_cue(
        lowered, QuestionIntent.ACCEPTANCE_CRITERIA
    ):
        intents.add(QuestionIntent.ACCEPTANCE_CRITERIA)
    if _has_actor_io_intent(lowered):
        intents.add(QuestionIntent.ACTOR_IO)
    if _has_runtime_context_intent(lowered):
        intents.add(QuestionIntent.RUNTIME_CONTEXT)
    if _has_product_behavior_intent(lowered):
        intents.add(QuestionIntent.PRODUCT_BEHAVIOR)

    return frozenset(intents)


# Regex for cues composed only of plain ASCII letters (a–z), spaces, hyphens,
# or apostrophes.  ASCII-letter cues are matched with regex word boundaries so
# that broad nouns/verbs like ``"test"`` or ``"verify"`` cannot silently
# substring-match unrelated words like ``"contest"``, ``"latest"``,
# ``"protest"``, ``"attestations"``, or ``"overify"``.  Cues that contain
# accented Latin or CJK characters fall through to plain substring matching
# because Python's ``\b`` is undefined around CJK characters and the
# multilingual cues we use are distinctive enough phrases (e.g. ``"cómo
# verific"``, ``"검증"``).
_ASCII_LATIN_CUE_RE = re.compile(r"^[a-z'\- ]+$")


def _cue_matches(cue: str, lowered: str) -> bool:
    if not _ASCII_LATIN_CUE_RE.match(cue):
        return cue in lowered
    if " " in cue or "-" in cue:
        # Multi-word / hyphenated phrases: substring is safe because the
        # phrase shape itself acts as the boundary (e.g. ``"out of scope"``,
        # ``"definition of done"``).
        return cue in lowered
    return bool(re.search(rf"\b{re.escape(cue)}\b", lowered))


def _contains_intent_cue(lowered: str, intent: QuestionIntent) -> bool:
    return any(_cue_matches(cue, lowered) for cue in _INTENT_CUES[intent])


_IO_NOUN_CUES: tuple[str, ...] = (
    "input",
    "inputs",
    "output",
    "outputs",
    "argument",
    "arguments",
    "entrada",
    "entradas",
    "salida",
    "salidas",
    "entrée",
    "entrées",
    "entree",
    "entrees",
    "sortie",
    "sorties",
    "eingabe",
    "eingaben",
    "ausgabe",
    "ausgaben",
    "입력",
    "출력",
    "入力",
    "出力",
    "输入",
    "輸入",
    "输出",
    "輸出",
)


# Cross-lingual flow-verb / interrogative anchors that indicate the question is
# really asking about the IO contract (what flows in/out, what is produced or
# returned, what schema/structure to expect).  English shape coverage lives in
# ``_is_actor_or_io_question``; the patterns below add multilingual signals so
# that broad nouns like "input" or "output" alone never authoritatively classify
# a question — there must also be an action-/contract-asking shape.  This is a
# direct response to ouroboros-agent's design note that broad noun substrings
# were being treated as authoritative intent without question-shape validation.
_IO_FLOW_SHAPE_PATTERNS: tuple[str, ...] = (
    # Spanish flow verbs
    r"\b(produce|produzca|produzcan|devuelve|devuelven|devolver|emite|emitir|"
    r"escribe|escribir|recibe|recibir|genera|generan|generar|retorna|retornar)\b",
    # Spanish interrogative shape: qué/cuál/cuáles + entradas/salidas
    r"\b(qu[éeè]|cu[áa]l(?:es)?)\b.+\b(entradas?|salidas?)\b",
    # French flow verbs
    r"\b(produire|produit|produits|retourner|retourne|[ée]mettre|renvoyer|"
    r"renvoie|[ée]crire|recevoir|re[çc]oit|g[ée]n[ée]rer|g[ée]n[ée]re)\b",
    # French interrogative shape
    r"\b(quels?|quelles?|que)\b.+\b(entr[ée]es?|sorties?)\b",
    # German flow verbs
    r"\b(produzieren|produziert|zur[üu]ckgeben|emittieren|schreiben|"
    r"empfangen|erzeugen|generieren)\b",
    # German interrogative shape
    r"\bwelche\b.+\b(eingaben?|ausgaben?)\b",
    # Korean: 입력/출력 followed by an action/asking particle, or asking words
    # paired with 입력/출력.
    r"(입력|출력)(?:은|는|을|를|이|가|에)?[^\?]*?(무엇|뭐|어떤|어떻게|어떠|"
    r"반환|생성|내보|쓰|받|보내|만들|돌려)",
    r"(어떤|무엇|뭐|어떠|어떠한)[^\?]*?(입력|출력)",
    # Japanese: 入力/出力 paired with 何/どの or generation/return verbs.
    r"(入力|出力)[^\?]*?(何|どの|どんな|生成|返|書|出す|出力|作)",
    r"(何|どの|どんな)[^\?]*?(入力|出力)",
    # Chinese (simplified + traditional)
    r"(输入|输出|輸入|輸出)[^\?]*?(是什么|是甚麼|有哪些|有什么|生成|返回|"
    r"产生|產生|寫|写|输出|輸出)",
    r"(什么|甚麼|哪些|哪個|哪个)[^\?]*?(输入|输出|輸入|輸出)",
)


_ACTOR_NOUN_CUES: tuple[str, ...] = (
    "actor",
    "actors",
    "user",
    "users",
    "persona",
    "personas",
    "stakeholder",
    "stakeholders",
    "usuario",
    "usuarios",
    "utilisateur",
    "utilisateurs",
    "benutzer",
    "사용자",
    "유저",
    "利用者",
    "ユーザー",
    "ユーザ",
    "用户",
    "使用者",
)


_ACTOR_QUESTION_CUES: tuple[str, ...] = (
    "who",
    # NB: bare ``"which user"`` and ``"what user"`` are intentionally NOT
    # in this list — combined with the ``user`` / ``users`` actor noun
    # cues they would silently misroute product-behavior questions like
    # ``"What user settings should be displayed?"`` and ``"Which user
    # fields should be editable?"`` into ``_io_actor_answer()``.  Specific
    # actor phrases (``"primary user"`` / ``"end user"``) stay because
    # they are unambiguous "who is the user" questions.
    "primary user",
    "end user",
    "quién",
    "quien",
    "quiénes",
    "quienes",
    "qui ",
    "quel utilisateur",
    "quels utilisateurs",
    "welche benutzer",
    "wer ",
    "누구",
    "어떤 사용자",
    "어떤 유저",
    "誰",
    "どのユーザー",
    "どのユーザ",
    "谁",
    "哪些用户",
    "哪个用户",
    "哪些使用者",
    "哪個使用者",
)


def _has_io_cue_with_flow_shape(lowered: str) -> bool:
    if not any(_cue_matches(cue, lowered) for cue in _IO_NOUN_CUES):
        return False
    return any(re.search(pattern, lowered) for pattern in _IO_FLOW_SHAPE_PATTERNS)


def _has_actor_cue_with_question_shape(lowered: str) -> bool:
    return any(_cue_matches(cue, lowered) for cue in _ACTOR_NOUN_CUES) and any(
        _cue_matches(cue, lowered) for cue in _ACTOR_QUESTION_CUES
    )


def _contains_actor_io_intent_cue(lowered: str) -> bool:
    """Cue-based actor/IO classifier with question-shape validation.

    The previous implementation triggered ``True`` for *any* string containing
    broad nouns like ``"input"`` or ``"output"``, which silently misrouted
    questions such as ``"What is the output directory?"`` (a property lookup,
    not an IO contract question) to the actor/IO answerer.  IO cues now require
    a flow-verb or interrogative shape; actor cues continue to require an
    interrogative subject paired with the actor noun.
    """
    return _has_io_cue_with_flow_shape(lowered) or _has_actor_cue_with_question_shape(lowered)


def _has_actor_io_intent(lowered: str) -> bool:
    return _is_actor_or_io_question(lowered) or _contains_actor_io_intent_cue(lowered)


# Cross-lingual selection/decision shape for runtime questions.  Cue words like
# ``"runtime"``, ``"repo"``, or ``"architecture"`` are concept anchors but
# product/property questions can mention them too ("What is the repository
# status?", "What architecture decisions are documented?").  We require a
# selection/decision verb in addition to the cue before the runtime intent is
# inferred.  English shape selection is also handled by the stricter
# ``_is_runtime_context_question`` regex.
_RUNTIME_SELECTION_SHAPE_PATTERNS: tuple[str, ...] = (
    # English selection/decision verbs not always covered by the strict shape regex
    r"\b(use|using|uses|used|choose|chose|chosen|select|selected|adopt|adopted|"
    r"configure|configured|set up|setup|target|targets|run on|run in|deploy on|"
    r"build on|switch to|migrate to|standardize on|standardise on)\b",
    # Spanish
    r"\b(usar|usamos|usa|usan|elegir|elegimos|elegido|seleccionar|seleccionamos|"
    r"configurar|configuramos|adoptar|adoptamos|migrar|escoger)\b",
    # French
    r"\b(utiliser|utilise|utilisons|utilis[ée]|choisir|choisissons|choisi|"
    r"s[ée]lectionner|configurer|adopter|adopt[ée]|migrer)\b",
    # German
    r"\b(verwenden|verwendet|nutzen|nutzt|w[äa]hlen|ausw[äa]hlen|"
    r"konfigurieren|konfiguriert|adoptieren|migrieren|einrichten)\b",
    # Korean: only verb-distinctive selection cues.  ``구성`` (composition)
    # and ``설정`` (settings) are dropped because they also appear in
    # ordinary status / display questions like ``런타임 설정은 어디에
    # 표시되나요?`` and would silently misroute property lookups into
    # ``_runtime_answer()``.
    r"사용|선택|채택|도입",
    # Japanese: same rule — drop ``構成`` and ``設定`` (which surface in
    # ``ランタイム設定はどこに表示されますか?``-style status questions).
    r"使う|使い|使用|選ぶ|選択|採用|導入",
    # Chinese (simplified + traditional): drop ``配置`` / ``設定`` /
    # ``设定`` / ``設置`` / ``设置`` for the same reason — those surface
    # in display/status questions like ``运行时配置显示在哪里？``.
    r"使用|选择|選擇|选用|選用|采用|採用|採納|采纳",
)


def _has_runtime_selection_shape(lowered: str) -> bool:
    return any(re.search(pattern, lowered) for pattern in _RUNTIME_SELECTION_SHAPE_PATTERNS)


# Bare direct-lookup runtime shapes for non-English languages.  English
# direct-lookup ("What runtime?", "Which framework?") is already handled by
# ``_is_runtime_context_question``; without an equivalent multilingual layer
# bare lookups like ``"¿Qué framework?"``, ``"Quel framework ?"``,
# ``"Welches Framework?"``, ``"ランタイムは何ですか?"``, ``"框架是什么？"``,
# and ``"런타임은 무엇인가요?"`` would fall through to ``_default_answer()``.
# Each pattern is anchored on a runtime cue plus a direct-lookup
# interrogative ("what is X" / "which X" / "X 무엇" / "X は何" / "X 是什么")
# so design / property questions like ``"¿Qué estructura usamos para los
# datos?"`` (which we already drop ``estructura`` from runtime cues for) and
# ``"What is the repository status?"`` (English, doesn't match the
# multilingual interrogatives) keep their conservative-default routing.
_RUNTIME_DIRECT_LOOKUP_PATTERNS: tuple[str, ...] = (
    # Spanish / French / German: interrogative + optional copula/article +
    # runtime cue.  The pattern is anchored to the start of the question
    # and ends shortly after the cue (only ``?`` and whitespace allowed
    # at the tail), so longer status questions don't match.
    r"^\s*¿?\s*(qu[éeè]|cu[áa]l(?:es)?|quel(?:le|s|les)?|welche[ar]?|welches)\b"
    r"\s*(es|son|ist|sind)?\s*"
    r"(el|la|los|las|das|der|die|den|le|les|un|une|una)?\s*"
    r"(runtime|stack|repo|repository|framework|package\s+manager|"
    r"project\s+structure|project\s+runtime|repositorio|"
    r"r[ée]f[ée]rentiel|gestionnaire\s+de\s+paquets|projektstruktur|laufzeit|"
    r"estructura\s+del\s+proyecto)"
    r"\s*\??\s*$",
    # Korean: cue + optional topic/subject particle + 무엇/뭐/어떠한.
    r"(런타임|저장소|레포|프레임워크|패키지\s*매니저|프로젝트\s*구조|스택)"
    r"(?:은|는|이|가)?\s*(무엇|뭐|어떠한)",
    # Japanese: cue + は/の + 何/どの/どんな/なに.
    r"(ランタイム|リポジトリ|レポ|フレームワーク|パッケージマネージャ|"
    r"プロジェクト構造|スタック)"
    r"(?:は|の)?\s*(何|どの|どんな|なに)",
    # Chinese (simplified + traditional): cue + (是|有)? + 什么/什麼/哪个/哪個/哪些/啥.
    r"(运行时|執行環境|栈|堆栈|仓库|倉庫|框架|包管理器|项目结构|專案結構)"
    r"\s*(是|有)?\s*(什么|什麼|哪个|哪個|哪些|啥)",
)


def _has_runtime_direct_lookup_shape(lowered: str) -> bool:
    return any(re.search(pattern, lowered) for pattern in _RUNTIME_DIRECT_LOOKUP_PATTERNS)


def _has_runtime_context_intent(lowered: str) -> bool:
    """Classify runtime intent with question-shape validation for cue matches.

    The previous implementation accepted *any* string containing broad nouns
    like ``"repository"`` or ``"architecture"`` as runtime intent, which
    silently misrouted property/status questions ("What is the repository
    status?") into ``_runtime_answer()``.  We keep the strict English shape
    selector as the authoritative trigger and let cross-lingual cues add the
    intent only when paired with either a selection/decision verb (``사용`` /
    ``使う`` / ``adopter`` / etc.) or a direct-lookup interrogative
    (``¿Qué …?`` / ``X は何ですか?`` / ``X 是什么?`` / ``X 무엇?``).
    """
    if _is_runtime_context_question(lowered):
        return True
    if not _contains_intent_cue(lowered, QuestionIntent.RUNTIME_CONTEXT):
        return False
    return _has_runtime_selection_shape(lowered) or _has_runtime_direct_lookup_shape(lowered)


# Cross-lingual permission/action shape for product-behavior questions.  The
# strict English ``_is_product_behavior_question()`` covers
# ``can|should|must|...`` paired with mutation/visibility verbs.  Without an
# equivalent multilingual layer, non-English permission questions like
# ``Quels utilisateurs peuvent supprimer des branches?`` or
# ``哪些用户可以删除分支?`` collide with the new actor cues — they get the
# ACTOR_IO intent only, and the answerer injects ``actors``/``inputs``/
# ``outputs`` assumptions instead of preserving the requested authorization
# behavior in the ledger contract.  These patterns add the missing
# multilingual coverage so PRODUCT_BEHAVIOR wins routing precedence as
# intended.  This is a direct response to ouroboros-agent's design note that
# the classifier and the route recognizers were asymmetric.
_MULTILINGUAL_PRODUCT_BEHAVIOR_PATTERNS: tuple[str, ...] = (
    # Spanish: pueden/puede/deben/debe/podrán/podrían + mutation/visibility verb
    r"\b(pueden|puede|podr[áa]n?|podr[íi]an|deben|debe|deber[áa]n?|deber[íi]an?)\b"
    r"[^?]*?\b(eliminar|borrar|crear|editar|modificar|actualizar|enviar|generar|"
    r"exportar|descargar|ver|acceder|aprobar|rechazar|cancelar|asignar|notificar|"
    r"configurar|mostrar|guardar|almacenar|leer)\b",
    # French: peut/peuvent/doit/doivent/pourra/devraient + mutation/visibility verb.
    # Hyphenated subject pronouns ("doit-il", "peut-on") are split by the
    # ``\b`` word boundary.
    r"\b(peut|peuvent|doit|doivent|pourra|pourront|pourraient|"
    r"devrait|devraient|peut[- ]on)\b"
    r"[^?]*?\b(supprimer|effacer|cr[ée]er|modifier|mettre[- ]?[àa][- ]?jour|"
    r"envoyer|exporter|t[ée]l[ée]charger|voir|consulter|acc[ée]der|approuver|"
    r"rejeter|annuler|assigner|notifier|configurer|afficher|stocker|enregistrer|"
    r"lire)\b",
    # German: können/kann/dürfen/darf/sollen/soll/müssen/muss + mutation verb.
    # Accepts both umlauted (``dürfen``, ``löschen``) and the conventional
    # ASCII transliterations (``duerfen``, ``loeschen``) so questions written
    # without umlauts (common when typed on non-DE keyboards) still classify
    # as product behavior.  Without the ASCII alternates,
    # ``"Welche Benutzer duerfen Branches loeschen?"`` would silently fall to
    # ACTOR_IO and inject ``actors``/``inputs``/``outputs``.
    r"\b(k(?:[öo]|oe)nnen|kann|d(?:[üu]|ue)rfen|darf|sollen|soll|sollte|sollten|"
    r"m(?:[üu]|ue)ssen|muss|m(?:[üu]|ue)sste|m(?:[üu]|ue)ssten)\b"
    r"[^?]*?\b(l(?:[öo]|oe)schen|entfernen|erstellen|anlegen|bearbeiten|"
    r"aktualisieren|senden|exportieren|herunterladen|anzeigen|sehen|zugreifen|"
    r"genehmigen|ablehnen|stornieren|zuweisen|benachrichtigen|konfigurieren|"
    r"generieren|speichern|lesen)\b",
    # Korean: action noun + Korean verb-formation morpheme (하|할|되|돼|됨|
    # 됩|됐|할까|하나|하지|되나|되어야) + (later) a permission/modal cue.
    # Anchoring on the verb morpheme prevents noun substrings like ``저장``
    # inside ``저장소`` (repository) or ``읽`` inside ``읽기`` from falsely
    # triggering the pattern on runtime/IO questions.
    r"(삭제|제거|생성|편집|수정|업데이트|전송|다운로드|표시|보기|접근|"
    r"승인|거부|취소|할당|알림|구성|설정|저장|읽기|보내기|만들기|받기|"
    r"내보내기|내려받기|로그인|로그아웃|업로드)"
    r"(?:하|할|함|되|됨|돼|됩|됐|할까|하나|하지|되나|되어야)"
    r"[^?]*?(수\s*있|수\s*없|해야|해도|가능|있나|있을까|할까|허용|허락)",
    r"(수\s*있|수\s*없|해야|해도|가능|있나|있을까|할까|허용|허락)"
    r"[^?]*?(삭제|제거|생성|편집|수정|업데이트|전송|다운로드|표시|보기|"
    r"접근|승인|거부|취소|할당|알림|구성|설정|저장|읽기|보내기|만들기|"
    r"받기|내보내기|내려받기|로그인|로그아웃|업로드)"
    r"(?:하|할|함|되|됨|돼|됩|됐)",
    # Japanese: action verb + できる/できます/してもよい/可能/していい (or reverse).
    r"(削除|消去|作成|作る|追加|編集|更新|送信|送る|エクスポート|表示|見|"
    r"アクセス|承認|却下|キャンセル|割り当て|通知|設定|構成|生成|"
    r"ダウンロード|保存|読)"
    r"[^?]*?(できる|できます|できますか|してもよい|してよい|可能|していい|"
    r"してください|すべき|すべきか)",
    r"(できる|できます|できますか|してもよい|してよい|可能|していい|"
    r"すべき|すべきか)"
    r"[^?]*?(削除|消去|作成|作る|追加|編集|更新|送信|送る|エクスポート|"
    r"表示|見|アクセス|承認|却下|キャンセル|割り当て|通知|設定|構成|"
    r"生成|ダウンロード|保存|読)",
    # Chinese (simplified + traditional): permission modal + mutation verb,
    # or mutation verb + permission modal.
    r"(可以|可|应该|應該|必须|必須|能|能否|应當|應當|該|应|须|須)"
    r"[^?]*?(删除|刪除|创建|建立|创|建|添加|编辑|編輯|更新|修改|发送|"
    r"發送|发|導出|导出|匯出|下载|下載|查看|访问|訪問|批准|拒绝|拒絕|"
    r"取消|分配|通知|配置|生成|存储|存儲|读取|讀取|显示|顯示)",
    r"(删除|刪除|创建|建立|创|建|添加|编辑|編輯|更新|修改|发送|發送|发|"
    r"導出|导出|匯出|下载|下載|查看|访问|訪問|批准|拒绝|拒絕|取消|分配|"
    r"通知|配置|生成|存储|存儲|读取|讀取|显示|顯示)"
    r"[^?]*?(可以|可|应该|應該|必须|必須|能|能否|应當|應當|該|应|须|須)",
)


# Cross-lingual "user-verifies-X" feature shape.  The bare verbs
# ``verify`` / ``vérifier`` / ``verificar`` / ``验证`` / ``확인`` / ``検証``
# are also classified as VERIFICATION cues; the routing layer prefers
# PRODUCT_BEHAVIOR when both are inferred so feature questions like
# ``"Can users verify their email?"`` (and the multilingual siblings) are
# not collapsed into a generic verification-plan template.
#
# We require an explicit actor noun (users / usuarios / utilisateurs /
# 사용자 / 用户 / ユーザー / etc.) PLUS a permission modal PLUS a verify-style
# verb.  Without the actor requirement, engineering-side questions like
# ``"How should we verify the HIPAA worker tests pass?"`` would also match
# and get demoted from VERIFICATION (which is wrong — those are not product
# feature questions).
_USER_VERIFY_ACTOR_RE = re.compile(
    r"\b(users?|usuarios?|utilisateurs?|benutzer|"
    r"clients?|kunden?|persons?|accounts?|admins?|owners?|members?|recipients?)\b"
    r"|사용자|유저|用户|使用者|ユーザー|ユーザ|利用者"
)
_USER_VERIFY_MODAL_RE = re.compile(
    r"\b(can|should|must|will|do|does|may|might|able to|allowed to|"
    r"pueden|puede|podr[áa]n?|deben|debe|"
    r"peuvent|peut|peut[- ]on|doivent|doit|pourra|pourront|"
    r"k(?:[öo]|oe)nnen|kann|d(?:[üu]|ue)rfen|sollen|soll)\b"
    r"|수\s*있|수\s*없|해야|가능|허용|허락"
    r"|できる|できます|してもよい|可能|していい|してください"
    r"|可以|应该|應該|必须|必須|能否|是否"
)
_USER_VERIFY_VERB_RE = re.compile(
    r"\b(verify|verifies|validate|validates|confirm|confirms|approve|approves|"
    r"verificar|validar|confirmar|aprobar|"
    r"v[ée]rifier|valider|confirmer|approuver|"
    r"verifizieren|validieren|best[äa]tigen|bestaetigen|genehmigen)\b"
    r"|확인|검증|승인"
    r"|確認|検証|承認"
    r"|验证|驗證|确认|確認|核实|核實|审核|審核|批准"
)


# Meta-verification questions ("Should WE verify users can reset passwords?",
# "How should WE validate admins can log in?") share the same actor-noun +
# permission-modal + verify-verb tokens as user-facing feature questions but
# the OUTER subject is engineering / first-person-plural ("we", "nous",
# "wir", "我们", etc.).  When that meta-subject is present the question is
# asking about QA, not a product feature, so we defer back to VERIFICATION
# instead of demoting it.
#
# Possessive determiners (``our``/``ours``/``notre``/``nos``/``unser*``/
# ``nuestro*``) are intentionally excluded: they routinely modify the actor
# noun in product-behavior questions (``Can our users verify their email?``)
# and treating them as engineering meta-subjects silently misroutes
# user-facing feature questions to the verification handler.  The CJK
# pronoun forms (``우리``/``我们``) are similarly ambiguous between possessive
# and subject usage in unanchored matching, so they are matched only with a
# trailing topic/subject particle that disambiguates an outer 1pp subject.
_FIRST_PERSON_META_RE = re.compile(
    r"\b(we|us|"
    r"nous|"
    r"wir|uns|"
    r"nosotros|nosotras|"
    # Spanish first-person plural verb forms.  ``-mos`` is unambiguously
    # 1pp in standard usage, so these are safe meta signals.  Listing the
    # specific common verbs keeps the matcher precise.
    r"deber[íi]amos|debemos|deb[íi]amos|"
    r"podr[íi]amos|podemos|pod[íi]amos|"
    r"verificamos|verificar[íi]amos|validamos|comprobamos|confirmamos|"
    r"necesitamos|necesitar[íi]amos|hacemos|haremos|queremos|"
    # French first-person plural ``-ons`` verb forms.
    r"devrions|devons|devions|"
    r"pourrions|pouvons|pouvions|"
    r"v[ée]rifions|v[ée]rifierions|validons|confirmons|approuvons|"
    r"voulons|voudrions)\b"
    # Japanese 1pp pronouns are unambiguously subject (no possessive overlap
    # without an explicit ``の`` particle).
    r"|私たち|私達"
    # Chinese / Korean 1pp pronouns require a topic/subject particle so a
    # possessive use ("我们的产品", "우리 사용자") does not trip the meta path.
    r"|我们(?:是|应|应当|是否|要|可|可以)|我們(?:是|應|應當|是否|要|可|可以)"
    r"|우리(?:는|가|들이|들은)|저희(?:는|가|들이|들은)"
)


def _has_user_verify_feature_shape(lowered: str) -> bool:
    """Detect ``ACTOR + permission-modal + verify-verb`` feature questions.

    Required for routing precedence: when this pattern matches the question
    is asking about a user-facing verification feature (e.g. ``"Can users
    verify their email?"``) and PRODUCT_BEHAVIOR should win over the
    VERIFICATION cue path.  An actor noun is mandatory so engineering-side
    QA questions like ``"How should we verify the HIPAA worker tests
    pass?"`` continue to route to ``_verification_answer()`` and through
    the existing regulated-data blocker.

    A first-person-plural subject (``we``/``nous``/``wir``/``我们``/``우리``…)
    is also disqualifying — those questions ask about engineering-side QA
    even when they mention an actor, e.g. ``"Should we verify users can
    reset passwords?"``.
    """
    if not _USER_VERIFY_ACTOR_RE.search(lowered):
        return False
    if not _USER_VERIFY_MODAL_RE.search(lowered):
        return False
    if not _USER_VERIFY_VERB_RE.search(lowered):
        return False
    return not _FIRST_PERSON_META_RE.search(lowered)


def _has_product_behavior_intent(lowered: str) -> bool:
    """Classify product-behavior intent across English and other languages.

    Without multilingual coverage the classifier becomes asymmetric: actor and
    runtime cues recognise non-English wording but PRODUCT_BEHAVIOR does not,
    so non-English permission / behavior questions get misrouted to ACTOR_IO
    or RUNTIME_CONTEXT (e.g. ``"哪些用户可以删除分支?"`` writing
    ``actors``/``inputs``/``outputs`` instead of preserving the requested
    authorization behavior).  This helper restores symmetry.
    """
    if _is_product_behavior_question(lowered):
        return True
    if _has_user_verify_feature_shape(lowered):
        return True
    return any(re.search(pattern, lowered) for pattern in _MULTILINGUAL_PRODUCT_BEHAVIOR_PATTERNS)


def _is_verification_question(lowered: str) -> bool:
    return bool(
        _matches_any(
            lowered,
            (
                r"\btests?\b",
                r"\bverify\b",
                r"\bverifies\b",
                r"\bverification\b",
                r"\bvalidation\b",
                r"\bdefinition of done\b",
            ),
        )
        or re.search(r"\b(command output|output)\b.+\b(verifies|verify|proves?)\b", lowered)
        or re.search(r"\b(verifies|verify|proves?)\b.+\b(acceptance|criteria)\b", lowered)
    )


def _is_feature_acceptance_question(lowered: str) -> bool:
    if not re.search(r"\b(acceptance|criteria)\b", lowered):
        return False
    if re.search(
        r"\b(general|overall|test strategy|verification plan|definition of done|verify|verifies|verification|validation)\b",
        lowered,
    ):
        return False
    return bool(
        re.search(
            r"\b(for|when|where|should|must|feature|flow|integration|endpoint|api|command|report|webhook|billing|search|generator|users?|user)\b",
            lowered,
        )
    )


def _acceptance_subject(question: str) -> str:
    cleaned = re.sub(r"\s+", " ", question.strip().rstrip("?"))
    patterns = (
        r"acceptance criteria should (?P<subject>.+?) satisfy$",
        r"criteria should (?P<subject>.+?) satisfy$",
        r"should (?P<subject>.+?) do$",
        r"for (?P<subject>.+)$",
    )
    lowered = cleaned.lower()
    for pattern in patterns:
        match = re.search(pattern, lowered)
        if match:
            return match.group("subject").strip() or "the requested behavior"
    return cleaned or "the requested behavior"


def _slug_key(value: str) -> str:
    """Build a stable, language-aware ledger key fragment from arbitrary text.

    Earlier this stripped everything outside ``[a-z0-9]``, which meant CJK
    questions like ``"哪些用户可以删除分支?"`` and
    ``"用户可以验证他们的电子邮件吗？"`` both collapsed to the
    ``requested_behavior`` fallback and silently merged into the same
    ledger slot.  Now we keep Unicode letters / digits (``\\w`` is
    Unicode-aware in Python 3 by default) so non-English questions each
    produce a distinct, descriptive key.
    """
    slug = re.sub(r"[^\w]+", "_", value.casefold(), flags=re.UNICODE).strip("_")
    return slug[:64] or "requested_behavior"


def _is_runtime_context_question(lowered: str) -> bool:
    runtime_terms = (
        r"runtime",
        r"stack",
        r"repo",
        r"repository",
        r"repository runtime",
        r"framework",
        r"package manager",
        r"project structure",
        r"project runtime",
    )
    runtime_term = r"(?:" + "|".join(runtime_terms) + r")"
    selection_verbs = (
        r"(?:use|used|using|uses|choose|select|configure|adopt|manage|managed|structure|organize)"
    )

    return bool(
        re.search(rf"^\s*(which|what)\s+{runtime_term}\s*\??\s*$", lowered)
        or re.search(rf"\b(which|what)\b.+\b{runtime_term}\b.+\b{selection_verbs}\b", lowered)
        or re.search(rf"\b{runtime_term}\b.+\b{selection_verbs}\b", lowered)
        or re.search(rf"\b{selection_verbs}\b.+\b{runtime_term}\b", lowered)
    )


def _should_preserve_runtime_route(lowered: str) -> bool:
    """Prefer runtime context only for stack/repo selection questions.

    Broad intent cues intentionally recognise words like "runtime" and "repo"
    across languages.  Product questions can also contain those words ("runtime
    status", "repo integration"), so preserve the runtime route only when the
    established English selector recognises an actual stack/repository choice.
    Regulated runtime fallback questions still pass through the later safe
    product reroute unless a grounded repo fact was supplied.
    """
    return _is_runtime_context_question(lowered) and not re.search(
        r"\bruntime\s+status\b|\bstatus\b.+\bruntime\b", lowered
    )


def _is_product_behavior_question(lowered: str) -> bool:
    return bool(
        re.search(
            r"\b(should|must|can|will|do|does|is|are)\b.+\b(mark|marked|show|display|write|return|create|update|edit|delete|remove|rotate|store|save|send|generate|filter|sort|search|export|import|notify|report|use|configure)\b",
            lowered,
        )
        or re.search(r"\bwhat\s+(output|input)\b.+\b(should|does|do|format|write|use)\b", lowered)
        or re.search(
            r"\bwhat\s+should\b.+\b(write|return|display|show|create|store|generate|edit|delete)\b",
            lowered,
        )
        or re.search(
            r"\bwhat\b.+\b(fields?|settings?)\b.+\b(should|does|do)\b.+\b(display|show|store|use)\b",
            lowered,
        )
        or re.search(
            r"\bhow\s+should\b.+\b(behave|work|display|return|write|store|mark)\b", lowered
        )
        or re.search(
            r"\b(which|what)\b.+\b(can|should)\b.+\b(edit|delete|remove|update|create|view|access)\b",
            lowered,
        )
        or re.search(
            r"\b(should|must|can|will|do|does|is|are)\b.+\b(be|become)\s+"
            r"(editable|edited|deleted|removed|trackable|tracked|enforced|"
            r"configurable|visible|searchable|exportable|importable|"
            # Past-participle forms of common product/UI verbs so questions
            # like ``"What user settings should be displayed?"`` and
            # ``"Which fields should be shown?"`` route to PRODUCT_BEHAVIOR
            # instead of falling through to ``_default_answer`` once the
            # actor cue path is no longer matched.
            r"displayed|shown|rendered|hidden|stored|saved|sent|received|"
            r"returned|generated|created|updated|imported|exported|"
            r"validated|verified|confirmed|approved|notified)\b",
            lowered,
        )
        or re.search(
            r"\b(should|must|can|will|do|does)\b.+\b(subscribe|track|enforce)\b",
            lowered,
        )
        or re.search(
            r"\b(which|what)\b.+\b(rules?|polic(?:y|ies)|workflows?|documents?|tiers?)\b.+"
            r"\b(should|must|can|will|do|does|enforce|track|edit|subscribe)\b",
            lowered,
        )
        # Covers every product-semantics verb that
        # ``_is_safe_product_regulated_question()`` allows (export, download,
        # render, display, show, expose, support, enable, allow, view, access)
        # and the "be able to <verb>" phrasing gap for ``view`` / ``access`` /
        # ``download``.  Some of these verbs are already matched by the broader
        # patterns above; listing the full set here keeps the safe-allowlist
        # vocabulary explicitly aligned with the router so the two never drift
        # silently. Any question allowed past the risky-fallback gate must also
        # route through ``_product_behavior_answer()`` rather than silently
        # falling to ``_default_answer()``.
        or re.search(
            r"\b(should|must|can|will|do|does|is|are)\b.+\b(be able to\s+)?"
            r"(export|download|render|display|show|expose|support|enable|allow|view|access)\b",
            lowered,
        )
    )


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, value) for pattern in patterns)


def _is_actor_or_io_question(lowered: str) -> bool:
    if re.search(
        r"\b(what|which)\s+(are|inputs? are|outputs? are)\s+.+\b(inputs|outputs)\b", lowered
    ):
        return True
    if re.search(
        r"\b(what|which)\s+(inputs|outputs)\s+(are|should be|does|do|will|can|must)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(what|which)\s+(inputs|outputs)\b.+\b(take|produce|return|emit|write|read|accept|receive)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(what|which)\s+.+\b(inputs|outputs)\b.+\b(take|produce|return|emit|write|read|accept|receive)\b",
        lowered,
    ):
        return True
    if re.search(
        r"\b(who|which|what)\s+(is|are)\s+.+\b(actors?|personas?|stakeholders?)\b", lowered
    ):
        return True
    return bool(re.search(r"\b(who|which)\s+(is|are)\s+the\s+users?\b", lowered))


def _latest_resolved_goal(ledger: SeedDraftLedger) -> str:
    section = ledger.sections.get("goal")
    if section is None:
        return ""
    inactive = {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    for entry in reversed(section.entries):
        if entry.status not in inactive and entry.value.strip():
            return entry.value
    return ""


def _is_safe_product_branch_question(lowered: str) -> bool:
    return bool(
        _matches_any(
            lowered,
            (
                r"\b(users?|customers?|admins?|maintainers?|owners?)\b.+\b(delete|remove)\b.+\b(branch|branches)\b",
                r"\b(app|application|tool|system|service|cli|workflow|feature)\b.+\b(delete|remove)\b.+\b(branch|branches)\b",
            ),
        )
        and _is_product_behavior_question(lowered)
        and not re.search(
            r"\b(current|this|production|prod|live|external|remote|local)\b.+\b(branch|branches)\b",
            lowered,
        )
    )


def _asks_for_sensitive_value_or_authority(lowered: str) -> bool:
    """Return True when the question asks auto mode to choose/use real secrets."""
    return bool(
        _matches_any(
            lowered,
            (
                r"\b(provide|enter|paste|supply)\b.+\b(credential|credentials|secret|token|key|password)\b",
                r"\b(credential|credentials|secret|token|key|password)\b.+\b(value|secret)\b",
                r"\b(which|what)\b.+\b(credential|credentials|access token|auth token|private key|api key|password|secret)\b.+\b(use|configure|set|env|environment|workflow|ci)\b",
                r"\b(which|what)\b.+\b(value|secret)\b.+\b(credential|credentials|access token|auth token|private key|api key|password)\b",
                r"\b(use|configure|set)\b.+\b(production|prod|live|external)\b.+\b(credential|credentials|secret|api key|private key|access token|auth token)\b",
                r"\b(use|configure|set)\b.+\b(credential|credentials|secret|api key|private key|access token|auth token)\b.+\b(production|prod|live|external)\b",
            ),
        )
    )


def _is_safe_product_sensitive_question(lowered: str) -> bool:
    """Allow product-semantics questions that mention sensitive-domain nouns.

    Auto mode must not invent real credential values or production authority,
    but it can answer bounded requirements questions about product-managed
    credential/token/key/secret features.  These questions are routed to the
    product-behavior answerer so the Seed keeps the requested semantics.
    """
    if not _is_product_behavior_question(lowered):
        return False
    if _asks_for_sensitive_value_or_authority(lowered):
        return False
    return bool(
        _matches_any(
            lowered,
            (
                r"\b(users?|customers?|admins?|maintainers?|owners?|the app|app|system|settings form)\b.+\b(credential|credentials|secret|token|tokens|api keys?|private keys?|passwords?)\b",
                r"\b(credential|credentials|secret|token|tokens|api keys?|private keys?|passwords?)\b.+\b(fields?|settings?|form|login|authentication|rotation|display|store|save|delete|remove)\b",
            ),
        )
    )


_RISKY_FALLBACK_SOURCES: frozenset[AutoAnswerSource] = frozenset(
    {
        AutoAnswerSource.CONSERVATIVE_DEFAULT,
        AutoAnswerSource.ASSUMPTION,
        # ``_runtime_answer`` returns EXISTING_CONVENTION when no concrete
        # repo fact was supplied. The text is still a generic
        # "use the existing repository runtime" template, so for regulated
        # topics it must be gated like any other fallback. A REPO_FACT-backed
        # runtime answer (full ``runtime_context`` supplied) is unaffected.
        AutoAnswerSource.EXISTING_CONVENTION,
        # USER_PREFERENCE answers carry user-supplied text verbatim. A
        # caller-supplied preference must not bypass the safety gate for
        # regulated topics (credentials, payments, legal/medical, destructive
        # production actions). For those questions the gate either re-routes
        # to ``_product_behavior_answer`` or raises a BLOCKER, the same
        # treatment EXISTING_CONVENTION receives. Concretely: a user
        # preference of "store passwords in plaintext" for a credential
        # question must NOT silently land in the ledger as a grounded answer.
        AutoAnswerSource.USER_PREFERENCE,
    }
)


# Sources that may be replaced by a caller-supplied USER_PREFERENCE when one
# matches the question's intent section. Grounded sources (USER_GOAL,
# REPO_FACT, NON_GOAL) are intentionally NOT upgradable — explicit grounded
# facts beat caller-supplied generic preferences.
#
# EXISTING_CONVENTION is included because the auto answerer's existing
# fallback paths (notably ``_runtime_answer``) tag generic template responses
# as EXISTING_CONVENTION even when no actual repo convention was observed —
# the same labelling inflation called out in the comment block on
# :data:`_RISKY_FALLBACK_SOURCES`. A caller-supplied preference is more
# specific than a generic "use the existing repository runtime" template, so
# the upgrade is allowed here. Phase 4 ledger-level conflict resolution will
# distinguish evidence-grounded EXISTING_CONVENTION entries from the template
# variant.
_USER_PREFERENCE_UPGRADABLE_SOURCES: frozenset[AutoAnswerSource] = frozenset(
    {
        AutoAnswerSource.CONSERVATIVE_DEFAULT,
        AutoAnswerSource.ASSUMPTION,
        AutoAnswerSource.EXISTING_CONVENTION,
    }
)


# Mapping from question intent to candidate ledger sections that a
# user_preference key may target. Order within each tuple is the search order
# when multiple sections match the same intent.
_INTENT_TO_SECTIONS: dict[QuestionIntent, tuple[str, ...]] = {
    QuestionIntent.NON_GOALS: ("non_goals",),
    QuestionIntent.VERIFICATION: ("verification_plan",),
    QuestionIntent.ACCEPTANCE_CRITERIA: ("acceptance_criteria",),
    QuestionIntent.ACTOR_IO: ("actors", "inputs", "outputs"),
    QuestionIntent.RUNTIME_CONTEXT: ("runtime_context",),
    QuestionIntent.PRODUCT_BEHAVIOR: ("constraints",),
}


def _section_hints_for_intents(intents: frozenset[QuestionIntent]) -> tuple[str, ...]:
    """Return ordered, deduplicated ledger sections to consult for ``intents``.

    Iterates ``_INTENT_TO_SECTIONS`` in dict-insertion order (Python 3.7+
    guarantee) rather than the input ``frozenset`` — set iteration is
    hash-randomized across processes, which would let the "first matching
    section" vary between runs and break the answerer's determinism contract.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for intent, sections in _INTENT_TO_SECTIONS.items():
        if intent not in intents:
            continue
        for section in sections:
            if section not in seen:
                seen.add(section)
                ordered.append(section)
    return tuple(ordered)


def _maybe_apply_user_preference(
    answer: AutoAnswer,
    section_hints: tuple[str, ...],
    context: AutoAnswerContext,
) -> AutoAnswer:
    """Replace an upgradable answer with a USER_PREFERENCE-tagged answer.

    The answer is only replaced when:

    - ``answer.source`` is in :data:`_USER_PREFERENCE_UPGRADABLE_SOURCES`
      (REPO_FACT, EXISTING_CONVENTION, NON_GOAL etc. are stronger than
      caller-supplied preferences and are preserved as-is)
    - ``context.user_preferences`` has a non-empty value for one of the
      provided ``section_hints``
    - ``answer.blocker`` is None

    The first matching section wins. The new answer carries the user's value
    verbatim and tags the corresponding ledger entry with
    :attr:`LedgerSource.USER_PREFERENCE`. Updates for sections OTHER than the
    upgraded one are preserved so multi-section answers (e.g. verification +
    acceptance pair) still seed their other sections.
    """
    if answer.blocker is not None:
        return answer
    if answer.source not in _USER_PREFERENCE_UPGRADABLE_SOURCES:
        return answer
    if not context.user_preferences:
        return answer
    for section in section_hints:
        raw_value = context.user_preferences.get(section)
        if not isinstance(raw_value, str):
            continue
        value = raw_value.strip()
        if not value:
            continue
        preserved = [
            (other_section, other_entry)
            for other_section, other_entry in answer.ledger_updates
            if other_section != section
        ]
        new_entry = LedgerEntry(
            key=f"{section}.user_preference",
            value=value,
            source=LedgerSource.USER_PREFERENCE,
            confidence=0.83,
            status=LedgerStatus.CONFIRMED,
            rationale="Caller supplied an explicit user preference for this section.",
        )
        preserved.append((section, new_entry))
        non_goals = list(answer.non_goals)
        if section == "non_goals" and value not in non_goals:
            non_goals.append(value)
        return AutoAnswer(
            text=value,
            source=AutoAnswerSource.USER_PREFERENCE,
            confidence=0.83,
            ledger_updates=preserved,
            assumptions=list(answer.assumptions),
            non_goals=non_goals,
            generic_default=False,
        )
    return answer


_DESTRUCTIVE_BULK_VERBS = (
    r"truncate|truncates|truncating|truncated|"
    r"purge|purges|purging|purged|"
    r"wipe|wipes|wiping|wiped|"
    r"drop|drops|dropping|dropped|"
    r"erase|erases|erasing|erased"
)
# Strong data-object nouns that unambiguously indicate schema/data destruction.
_DESTRUCTIVE_BULK_NOUNS = (
    r"table|tables|schema|schemas|"
    r"database|databases|"
    r"record|records|row|rows|"
    r"audit log|audit logs|audit trail|audit trails|"
    r"index|indexes|indices|"
    r"migration|migrations"
)
# When the question contains one of these non-data qualifier phrases the
# destructive-bulk match is referring to a process artefact (release plan, docs,
# roadmap, …) rather than schema/data destruction — skip the gate for those.
#
# The qualifier is strictly phrase-scoped: bare tokens like ``documentation`` or
# ``release plan`` anywhere in the sentence would let an actual destructive
# operation slip past the gate (e.g. "Which tables should we drop according to
# the documentation before redeploying?"). The exemption fires only when the
# artefact is the explicit object of the drop/wipe — introduced by
# ``from the …`` or ``in the …`` — which is the phrasing that signals
# "remove/edit an entry inside a process artefact" rather than "delete data from
# a system". Authority/reference phrasings (``according to the documentation``,
# ``per the release plan``) do NOT match this pattern and therefore do NOT
# suppress the destructive-bulk gate.
_DESTRUCTIVE_BULK_NON_DATA_QUALIFIERS = re.compile(
    r"\b(?:from|in)\s+the\s+"
    r"(?:release\s+plan|docs|documentation|roadmap|backlog|changelog|spec)"
    r"\b"
)


_RISKY_FALLBACK_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"\b(pii|personally identifiable information)\b",
        "regulated personal data handling",
    ),
    (
        r"\b(gdpr|hipaa|sox|pci[- ]?dss)\b",
        "regulated data handling",
    ),
    # Verb-then-noun, e.g. "How should the migration purge tables for old users?"
    (
        rf"\b(?:{_DESTRUCTIVE_BULK_VERBS})\b.+\b(?:{_DESTRUCTIVE_BULK_NOUNS})\b",
        "destructive bulk data operation",
    ),
    # Noun-then-verb, e.g. "Which tables should the migration truncate?"
    (
        rf"\b(?:{_DESTRUCTIVE_BULK_NOUNS})\b.+\b(?:{_DESTRUCTIVE_BULK_VERBS})\b",
        "destructive bulk data operation",
    ),
)


_REGULATED_NOUNS_RE = re.compile(
    r"\b(pii|personally identifiable information|gdpr|hipaa|sox|pci[- ]?dss)\b"
)
_PRODUCT_SEMANTICS_REGULATED_VERBS_RE = re.compile(
    r"\b(export|exports|exporting|exported|"
    r"download|downloads|downloading|downloaded|"
    r"render|renders|rendering|rendered|"
    r"display|displays|displaying|displayed|"
    r"show|shows|showing|shown|"
    r"expose|exposes|exposing|exposed|"
    r"support|supports|supporting|supported|"
    r"enable|enables|enabling|enabled|"
    r"allow|allows|allowing|allowed|"
    r"view|views|viewing|viewed|"
    r"access|accesses|accessing|accessed)\b"
)
# Compliance-policy verbs in *active* form only (base / -s / -ing).
# Past-participle forms (``stored``, ``encrypted``, ``retained``, …) are
# deliberately excluded because they routinely act as adjectives modifying a
# regulated noun (``view stored PII``, ``display encrypted HIPAA files``) — the
# main verb of those sentences is the product-semantics one, not a request for
# a compliance-policy decision.
#
# When an active-form compliance verb appears, the question is asking the
# pipeline to decide regulated-data handling (``How should the system store …?``,
# ``Should we retain and export PII records?``) and must remain blocked even if
# the same sentence also mentions a product-semantics verb.
_COMPLIANCE_POLICY_ACTIVE_VERBS_RE = re.compile(
    r"\b(store|stores|storing|"
    r"handle|handles|handling|"
    r"retain|retains|retaining|"
    r"collect|collects|collecting|"
    r"encrypt|encrypts|encrypting|"
    r"process|processes|processing|"
    r"transmit|transmits|transmitting|"
    r"disclose|discloses|disclosing|"
    r"share|shares|sharing|"
    r"manage|manages|managing|"
    r"govern|governs|governing)\b"
)
# Broad product-question indicator: contains a modal/question word.  Looser than
# ``_is_product_behavior_question`` so that phrasings like "Should users be able
# to download …" are captured even when ``download`` is not in that helper's verb list.
_PRODUCT_QUESTION_MODAL_RE = re.compile(r"\b(should|must|can|will|do|does|is|are)\b")
# Reject "compliance-scope-as-feature-flag" phrasings: a wide-coverage
# enablement verb (``support`` / ``enable`` / ``allow``) followed by a
# regulated noun used as a policy scope rather than a concrete feature.
# Two shapes are rejected:
#
#   1. Bare regulated noun with no further qualifying noun
#      (``Should the platform support HIPAA?``, ``Should the app enable GDPR?``).
#      The trailing negative lookahead ``(?!\s+[a-z])`` fires when the
#      regulated noun is the last lexical token of the clause.
#
#   2. Regulated noun followed (optionally bridged by ``data``) by a
#      compliance-policy noun that names the policy regime itself —
#      ``retention``, ``storage``, ``encryption``, ``handling``, ``processing``,
#      ``collection``, ``disclosure``, ``governance``, ``compliance``,
#      ``transmission``, ``redaction``.  Phrasings such as
#      ``support HIPAA data retention`` and ``enable GDPR data storage`` frame
#      the entire compliance policy as a toggle and are still
#      regulated-policy decisions, not bounded product behaviour, so they
#      remain blocked.  A trailing word boundary keeps concrete-feature
#      variants ("support HIPAA retention reports", "enable GDPR consent
#      banners") off this path because the policy noun is then followed by a
#      qualifying feature noun rather than ending the clause.
#
# Concrete-feature qualifiers that follow the regulated noun directly
# ("HIPAA audit logs", "GDPR consent banners", "PII redaction in exports",
# "GDPR data exports") still describe bounded product features and are not
# rejected.
_BARE_COMPLIANCE_SCOPE_RE = re.compile(
    r"\b(?:support|supports|supporting|supported|"
    r"enable|enables|enabling|enabled|"
    r"allow|allows|allowing|allowed)\s+"
    r"(?:pii|personally identifiable information|gdpr|hipaa|sox|pci[- ]?dss)\b"
    r"(?:"
    r"(?!\s+[a-z])"
    r"|"
    r"(?:\s+data)?\s+"
    r"(?:retention|storage|encryption|handling|processing|collection|"
    r"disclosure|governance|compliance|transmission|redaction)"
    r"(?!\s+[a-z])"
    r")"
)


def _is_safe_product_regulated_question(lowered: str) -> bool:
    """Allow product-semantics questions that mention regulated-data nouns.

    Auto mode must not decide compliance policy (how to store/handle/retain PII,
    which fields are HIPAA-regulated, etc.), but it can answer bounded product
    requirements questions such as "Should the app export PII reports?" or
    "Should users be able to download GDPR exports?".  Those are asking for
    feature-level behavior, not compliance-policy decisions.

    Strategy: pass through when the question
      1. mentions a regulated noun (PII/GDPR/HIPAA/SOX/PCI-DSS),
      2. contains a product-question modal (should/can/will/must/do/does/is/are),
      3. does NOT use an *active*-form compliance-policy verb (``store``,
         ``stores``, ``storing``, ``handle``, ``encrypt``, ``share``, …) — those
         signal a regulated-data handling decision and must stay blocked even
         when mixed with product-semantics verbs (``How should the system store
         and display HIPAA files?``, ``Should we retain and export PII
         records?``),
      4. is NOT a bare compliance-scope-as-feature-flag phrasing
         (``support|enable|allow`` + bare regulated noun with no qualifying
         feature noun). ``Should the platform support HIPAA?`` and
         ``Should the app enable GDPR?`` are framing the entire regulatory
         regime as a toggle, which remains a compliance-policy decision.
      5. uses a product-semantics verb (export, download, display, show, view …).

    Past-participle compliance forms (``stored``, ``encrypted``, ``retained``,
    …) are intentionally NOT in the negative list: in product-behavior questions
    they routinely act as adjectives modifying a regulated noun (``view stored
    PII``, ``display encrypted HIPAA files``), and the sentence's main action is
    the product-semantics verb. Pure-compliance phrasings using past-participle
    forms (``Should PII be stored …?``) lack a product-semantics verb and are
    rejected by step (4) instead.
    """
    if not _REGULATED_NOUNS_RE.search(lowered):
        return False
    if not _PRODUCT_QUESTION_MODAL_RE.search(lowered):
        return False
    if _COMPLIANCE_POLICY_ACTIVE_VERBS_RE.search(lowered):
        return False
    if _BARE_COMPLIANCE_SCOPE_RE.search(lowered):
        return False
    return bool(_PRODUCT_SEMANTICS_REGULATED_VERBS_RE.search(lowered))


def _risky_fallback_blocker_for(question: str, lowered: str) -> AutoBlocker | None:
    """Return a blocker when a generative fallback answer would touch a high-risk topic.

    The gate only fires for *generative* answer routes (actor/IO, runtime,
    product behavior, default). Meta-question routes — non-goal listing,
    verification policy, feature acceptance criteria — are checked earlier in
    ``answer`` and never reach this function, because phrasing such as
    "What acceptance criteria should the HIPAA worker satisfy?" is asking
    about a generic acceptance template, not asking the auto pipeline to
    decide regulated-data handling.

    Targeted topics: regulated personal data (PII/GDPR/HIPAA/SOX/PCI-DSS) and
    destructive bulk schema/table operations.  Production-deployment and
    credential authority are already gated by the explicit ``_blocker_for``
    allow/deny lists.

    Product-feature questions covered by existing safe-allowlists — such as
    "should users be able to configure production credentials?" or
    "should the app export PII reports?" — are skipped so the auto pipeline
    keeps answering them with feature semantics.
    """
    if (
        _is_safe_product_branch_question(lowered)
        or _is_safe_product_sensitive_question(lowered)
        or _is_safe_product_regulated_question(lowered)
    ):
        return None
    for pattern, reason in _RISKY_FALLBACK_PATTERNS:
        if re.search(pattern, lowered):
            # For destructive-bulk matches, skip when the question context
            # indicates a non-data artefact (release plan, docs, etc.) rather
            # than actual schema/data destruction.
            if (
                reason == "destructive bulk data operation"
                and _DESTRUCTIVE_BULK_NON_DATA_QUALIFIERS.search(lowered)
            ):
                continue
            return AutoBlocker(reason=reason, question=question)
    return None


def _blocker_for(question: str) -> AutoBlocker | None:
    lowered = question.lower()
    if _is_safe_product_branch_question(lowered) or _is_safe_product_sensitive_question(lowered):
        return None

    external_action_patterns = (
        (
            r"\b(credential value|credential secret)\b",
            "credential or secret value required",
        ),
        (
            r"\b(provide|enter|paste|supply|configure|set)\b.+\b(access token|auth token|private key)\b",
            "credential or secret value required",
        ),
        (
            r"\b(which|what)\b.+\b(access token|auth token|private key)\b.+\b(use|configure|set|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(provide|enter|paste|supply|configure|set)\b.+\b(credentials?)\b.+\b(value|secret|token|key|password|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(which|what)\s+credentials?\b.+\b(use|configure|set|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(provide|enter|use|configure|set)\b.+\b(api keys?|passwords?)\b.+\b(value|secret|token|credential|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(which|what)\b.+\b(api keys?|passwords?)\b.+\b(value|secret|token|credential|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(credential|credentials)\b.+\b(value|secret|token|key|password|env|environment|workflow|ci)\b",
            "credential or secret value required",
        ),
        (
            r"\b(charge|purchase|subscribe|provide|enter|use|configure|set)\b.+\b(payment|billing|paid service|credit card|bank account|invoice)\b.+\b(account|provider|key|secret|production|live)\b",
            "paid service or financial decision required",
        ),
        (
            r"\b(payment|billing|paid service|credit card|bank account|invoice)\b.+\b(account|provider|key|secret|production|live)\b.+\b(charge|purchase|subscribe|pay)\b",
            "paid service or financial decision required",
        ),
        (
            r"\b(which|what|provide|obtain|get|use|choose|select)\b.+\b(legal|compliance|license|contract)\b.+\b(advice|judgment|review|approval|liability|risk|interpretation)\b",
            "legal judgment required",
        ),
        (
            r"\b(which|what|provide|use|choose|select)\b.+\b(medical|clinical|diagnosis|treatment|health)\b.+\b(advice|judgment|diagnose|prescribe|triage|recommendation)\b",
            "medical judgment required",
        ),
        (
            r"\b(should|can|may|will|do we|should we)\b.+\b(deploy|release|publish)\b.+\b(to|against|on)\s+\b(production|prod|live|external)\b",
            "deployment target requires human authority",
        ),
        (
            r"\b(which|what|choose|select|use|configure|set)\b.+\b(production|prod|live|external)\b.+\b(environment|target|account|project|cluster|region)\b",
            "deployment target requires human authority",
        ),
        (
            r"\b(which|what|choose|select|use|configure|set)\b.+\b(environment|target|account|project|cluster|region)\b.+\b(deploy|release|publish)\b.+\b(production|prod|live|external)\b",
            "deployment target requires human authority",
        ),
        (
            r"\b(provide|enter|paste|supply|use|configure|set)\b.+\b(production|prod|live|external)\b.+\b(credential|secret|api key)\b",
            "production deployment or irreversible external action required",
        ),
        (
            r"\b(delete|drop|erase|wipe|remove)\b.+\b(database|db|branch|production|prod)\b",
            "destructive external operation requires human authority",
        ),
        (
            r"\b(provide|enter|paste|supply|use|configure|set)\b.+\bsecret\b.+\b(value|key|token|credential|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
        (
            r"\b(which|what)\s+secret\b.+\b(use|configure|set|env|environment|workflow|ci|production|prod)\b",
            "credential or secret value required",
        ),
    )
    for pattern, reason in external_action_patterns:
        if re.search(pattern, lowered):
            return AutoBlocker(reason=reason, question=question)
    return None
