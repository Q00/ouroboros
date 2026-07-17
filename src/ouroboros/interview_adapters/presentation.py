"""Structured presentation contract for answerable interview turns."""

from __future__ import annotations

from enum import StrEnum
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

QUESTION_PRESENTATION_CONTRACT_VERSION = "answerable_interview_turn.v1"
QUESTION_TARGET_DIMENSIONS: tuple[str, ...] = (
    "goal_clarity",
    "constraint_clarity",
    "success_criteria_clarity",
    "context_clarity",
)

QUESTION_PRESENTATION_RULES = """## Answerable Interview Turn Contract
- Target one primary unresolved ambiguity and one clear reply objective.
- Use plain language and explain unavoidable specialized terms inline.
- When choices reduce effort, provide two to four concise choices that answer the same decision.
- If safe choices cannot be derived without inventing intent, ask for a direct answer without choices.
- Mark every provider-generated choice as `generated_hypothesis`; generated
  choices are not user intent until the user selects or edits one.
- A recommendation is optional and must reference one choice with a short reason.
- Always allow a free-text answer.
- Use the same human language as the conversation.
- Do not expose internal ambiguity, ontology, scoring, or workflow terminology.
"""

QUESTION_PRESENTATION_SCHEMA = f"""Use this JSON object shape:
{{
  "contract_version": "{QUESTION_PRESENTATION_CONTRACT_VERSION}",
  "decision_id": "short_snake_case_identifier",
  "target_dimension": "goal_clarity|constraint_clarity|success_criteria_clarity|context_clarity",
  "locale": "conversation language code, for example en or ko",
  "question": "one plain-language question",
  "context": ["optional short context line"],
  "choices": [] or [
    {{
      "choice_id": 1,
      "meaning_key": "short_snake_case_meaning",
      "label": "choice wording",
      "provenance": "generated_hypothesis"
    }}
  ],
  "choices_are_mutually_exclusive": true,
  "recommendation": {{
    "choice_id": 1,
    "label": "localized recommendation label",
    "reason": "short reason"
  }} or null,
  "allow_free_text": true,
  "free_text_prompt": "localized instruction allowing a number or direct answer"
}}
"""

QUESTION_PRESENTATION_PROMPT = (
    f"{QUESTION_PRESENTATION_RULES}\n\n"
    f"Respond ONLY with one JSON object. {QUESTION_PRESENTATION_SCHEMA}"
)

_DECISION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_CHOICE_LINE_PATTERN = re.compile(r"^\s*([1-4])[\).:]\s+(.+?)\s*$")
_RECOMMENDATION_LINE_PATTERN = re.compile(
    r"^\s*(?P<label>[^:：]{1,80})[:：]\s*(?P<choice_id>[1-4])\s*[,，]\s*"
    r"(?P<reason>.+?)\s*$"
)
_PRIMARY_DECISION_SPLIT_PATTERN = re.compile(
    r"\s+(?:and|,\s*)\s*(?=(?:what|which|who|when|where|why|how|should|"
    r"do\s+you|does\s+this|is\s+this|are\s+these|would\s+you|can\s+you|"
    r"could\s+you)\b)",
    re.IGNORECASE,
)
_QUESTION_MARKS = ("?", "？", "؟")
_INTERNAL_INTERVIEW_TERMS = (
    "ambiguity",
    "ambiguity score",
    "ontology",
    "ontological",
    "epistemic",
    "seed readiness",
    "seed-ready",
    "clarity score",
    "dimension",
)


class QuestionChoiceProvenance(StrEnum):
    """Where an answer choice came from before user confirmation."""

    GENERATED_HYPOTHESIS = "generated_hypothesis"
    USER_GOAL = "user_goal"
    REPO_FACT = "repo_fact"
    REFERENCE_DERIVED = "reference_derived"
    SAFETY_ASSUMPTION = "safety_assumption"


class _StrictFrozenModel(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")


class InterviewQuestionChoice(_StrictFrozenModel):
    """One bounded choice in a user-facing interview turn."""

    choice_id: int = Field(ge=1, le=4)
    meaning_key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=500)
    provenance: QuestionChoiceProvenance = QuestionChoiceProvenance.GENERATED_HYPOTHESIS

    @field_validator("meaning_key")
    @classmethod
    def _validate_meaning_key(cls, value: str) -> str:
        value = value.strip().casefold().replace(" ", "_")
        if not _DECISION_ID_PATTERN.fullmatch(value):
            raise ValueError("meaning_key must be a short machine-readable identifier")
        return value

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("choice label must not be blank")
        return value


class InterviewQuestionRecommendation(_StrictFrozenModel):
    """Optional recommendation over one of the presented choices."""

    choice_id: int = Field(ge=1, le=4)
    label: str = Field(default="Recommended", min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("label", "reason")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("recommendation text must not be blank")
        return value


class InterviewQuestionPresentation(_StrictFrozenModel):
    """Locale-aware, structurally validated interview question presentation."""

    contract_version: Literal["answerable_interview_turn.v1"] = (
        QUESTION_PRESENTATION_CONTRACT_VERSION
    )
    decision_id: str = Field(min_length=1, max_length=128)
    target_dimension: str
    locale: str = Field(default="en", min_length=2, max_length=32)
    question: str = Field(min_length=1, max_length=1000)
    context: tuple[str, ...] = Field(default_factory=tuple, max_length=4)
    choices: tuple[InterviewQuestionChoice, ...] = Field(default_factory=tuple, max_length=4)
    choices_are_mutually_exclusive: bool = True
    recommendation: InterviewQuestionRecommendation | None = None
    allow_free_text: bool = True
    free_text_prompt: str = Field(min_length=1, max_length=500)

    @field_validator("decision_id")
    @classmethod
    def _validate_decision_id(cls, value: str) -> str:
        value = value.strip().casefold().replace(" ", "_")
        if not _DECISION_ID_PATTERN.fullmatch(value):
            raise ValueError("decision_id must be a short machine-readable identifier")
        return value

    @field_validator("target_dimension")
    @classmethod
    def _validate_target_dimension(cls, value: str) -> str:
        value = value.strip()
        if value not in QUESTION_TARGET_DIMENSIONS:
            raise ValueError("target_dimension is not a supported ambiguity dimension")
        return value

    @field_validator("locale")
    @classmethod
    def _normalize_locale(cls, value: str) -> str:
        return value.strip().replace("_", "-").casefold()

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("question must be one nonblank line")
        question_mark_count = sum(value.count(mark) for mark in _QUESTION_MARKS)
        if question_mark_count != 1 or not value.endswith(_QUESTION_MARKS):
            raise ValueError("question must contain one reply objective")
        return value

    @field_validator("context")
    @classmethod
    def _validate_context(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value if item.strip())
        if any(len(item) > 2000 for item in normalized):
            raise ValueError("context lines must be at most 2000 characters")
        return normalized

    @field_validator("free_text_prompt")
    @classmethod
    def _validate_free_text_prompt(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("free_text_prompt must be one nonblank line")
        return value

    @model_validator(mode="after")
    def _validate_choice_contract(self) -> InterviewQuestionPresentation:
        if not self.choices_are_mutually_exclusive:
            raise ValueError("choices must be declared mutually exclusive")
        if not self.allow_free_text:
            raise ValueError("free-text answers must remain available")
        if len(self.choices) == 1:
            raise ValueError("choices must be omitted or contain two to four options")

        choice_ids = tuple(choice.choice_id for choice in self.choices)
        expected_ids = tuple(range(1, len(self.choices) + 1))
        if choice_ids != expected_ids:
            raise ValueError("choice_id values must be sequential starting at 1")

        meaning_keys = tuple(choice.meaning_key for choice in self.choices)
        if len(set(meaning_keys)) != len(meaning_keys):
            raise ValueError("choices must use distinct meaning_key values")

        labels = tuple(choice.label.casefold() for choice in self.choices)
        if len(set(labels)) != len(labels):
            raise ValueError("choices must use distinct labels")

        if self.recommendation is not None and self.recommendation.choice_id not in choice_ids:
            raise ValueError("recommendation must reference a presented choice")

        authored_text = "\n".join(
            (
                self.question,
                *(choice.label for choice in self.choices),
                *(
                    (self.recommendation.label, self.recommendation.reason)
                    if self.recommendation is not None
                    else ()
                ),
                self.free_text_prompt,
            )
        ).casefold()
        leaked_terms = tuple(
            term for term in _INTERNAL_INTERVIEW_TERMS if _contains_phrase(authored_text, term)
        )
        if leaked_terms:
            raise ValueError("user-facing question contains internal interview terminology")
        return self


def question_presentation_contract_metadata() -> dict[str, Any]:
    """Return the machine-readable contract used by host-managed fallbacks."""
    return {
        "contract_version": QUESTION_PRESENTATION_CONTRACT_VERSION,
        "one_reply_objective": True,
        "choice_count": {"minimum": 2, "maximum": 4, "optional": True},
        "allow_free_text": True,
        "generated_choice_provenance": QuestionChoiceProvenance.GENERATED_HYPOTHESIS.value,
        "numeric_answer_transport": (
            "retain_raw_answer_and_expand_selected_label_in_requirement_context"
        ),
        "forbid_internal_process_terms": True,
    }


def generated_question_contract_failures(
    presentation: InterviewQuestionPresentation,
) -> tuple[str, ...]:
    """Return provider-output failures that are not intrinsic model errors."""
    failures: list[str] = []
    if any(
        choice.provenance is not QuestionChoiceProvenance.GENERATED_HYPOTHESIS
        for choice in presentation.choices
    ):
        failures.append("provider-generated choices must remain generated hypotheses")

    supporting_text = (
        *presentation.context,
        *(choice.label for choice in presentation.choices),
        *(
            (presentation.recommendation.label, presentation.recommendation.reason)
            if presentation.recommendation is not None
            else ()
        ),
        presentation.free_text_prompt,
    )
    if any(_ends_with_question_mark(value) for value in supporting_text):
        failures.append("only the question field may contain a reply objective")

    context_text = "\n".join(presentation.context).casefold()
    if any(_contains_phrase(context_text, term) for term in _INTERNAL_INTERVIEW_TERMS):
        failures.append("provider-generated context uses internal interview terminology")

    return tuple(failures)


def expand_selected_choice_answer(
    presentation: InterviewQuestionPresentation,
    answer: str,
) -> str:
    """Expand a numeric reply only after the user selected a presented choice."""
    normalized_answer = answer.strip()
    selected_id = parse_numeric_choice_answer(normalized_answer)
    if selected_id is None:
        return answer

    selected = next(
        (choice for choice in presentation.choices if choice.choice_id == selected_id),
        None,
    )
    if selected is None:
        return answer
    return (
        f"{normalized_answer} - {selected.label} "
        f"[user selected; source={selected.provenance.value}]"
    )


def parse_numeric_choice_answer(answer: str) -> int | None:
    """Return a bounded choice id when an answer is only a numeric selection."""
    numeric_answer = answer.strip()
    if numeric_answer.casefold().startswith("pm answer:"):
        numeric_answer = numeric_answer.split(":", 1)[1].strip()
    numeric_answer = numeric_answer.rstrip(".)")
    if not numeric_answer.isdigit():
        return None
    selected_id = int(numeric_answer)
    return selected_id if 1 <= selected_id <= 4 else None


def repair_question_presentation(
    text: str,
    *,
    target_dimension: str,
    locale: str,
) -> InterviewQuestionPresentation | None:
    """Repair malformed output without inventing a different decision topic."""
    payload = _extract_json_mapping(text)
    if isinstance(payload, dict) and isinstance(payload.get("presentation"), dict):
        payload = payload["presentation"]

    raw_question = ""
    raw_choices: list[str] = []
    context: tuple[str, ...] = ()
    recommendation_parts: tuple[int, str, str] | None = None
    free_text_prompt = ""
    if isinstance(payload, dict):
        raw_question = str(payload.get("question") or "").strip()
        raw_context = payload.get("context")
        if isinstance(raw_context, list):
            context = tuple(
                value
                for item in raw_context[:4]
                if (value := str(item).strip()[:2000]) and _is_safe_generated_context_line(value)
            )
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices[:4]:
                if not isinstance(choice, dict):
                    continue
                label = str(choice.get("label") or "").strip()
                if label:
                    raw_choices.append(label[:500])
        raw_recommendation = payload.get("recommendation")
        if isinstance(raw_recommendation, dict):
            try:
                recommendation_choice_id = int(raw_recommendation.get("choice_id"))
            except (TypeError, ValueError):
                recommendation_choice_id = 0
            recommendation_label = str(raw_recommendation.get("label") or "").strip()
            recommendation_reason = str(raw_recommendation.get("reason") or "").strip()
            if recommendation_label and recommendation_reason:
                recommendation_parts = (
                    recommendation_choice_id,
                    recommendation_label[:80],
                    recommendation_reason[:500],
                )
        free_text_prompt = str(payload.get("free_text_prompt") or "").strip()
    else:
        lines = tuple(line.strip() for line in text.splitlines() if line.strip())
        raw_question = next(
            (
                line
                for line in lines
                if not _CHOICE_LINE_PATTERN.fullmatch(line)
                and not _looks_like_answer_instruction(line)
                and not _looks_like_recommendation(line)
            ),
            "",
        )
        raw_choices = [
            match.group(2).strip()[:500]
            for line in lines
            if (match := _CHOICE_LINE_PATTERN.fullmatch(line)) is not None
        ][:4]
        free_text_prompt = lines[-1] if lines and _looks_like_answer_instruction(lines[-1]) else ""
        recommendation_line = lines[-2] if free_text_prompt and len(lines) >= 2 else ""
        recommendation_match = _RECOMMENDATION_LINE_PATTERN.fullmatch(recommendation_line)
        if recommendation_match is not None:
            recommendation_parts = (
                int(recommendation_match.group("choice_id")),
                recommendation_match.group("label").strip(),
                _trim_terminal_punctuation(recommendation_match.group("reason")),
            )
        excluded_lines = {
            raw_question,
            free_text_prompt,
            recommendation_line if recommendation_match is not None else "",
        }
        context = tuple(
            line[:2000]
            for line in lines[1:]
            if line not in excluded_lines
            and _CHOICE_LINE_PATTERN.fullmatch(line) is None
            and _is_safe_generated_context_line(line)
        )[:4]

    question = _repair_primary_question(raw_question)
    if question is None:
        return None

    distinct_choices: list[str] = []
    seen_choices: set[str] = set()
    for label in raw_choices:
        normalized = label.casefold()
        if normalized in seen_choices:
            continue
        seen_choices.add(normalized)
        distinct_choices.append(label)
    if len(distinct_choices) not in (2, 3, 4):
        distinct_choices = []

    normalized_dimension = (
        target_dimension if target_dimension in QUESTION_TARGET_DIMENSIONS else "goal_clarity"
    )
    normalized_locale = _normalize_supported_locale(locale)
    recommendation = None
    if recommendation_parts is not None:
        choice_id, label, reason = recommendation_parts
        if 1 <= choice_id <= len(distinct_choices):
            recommendation = InterviewQuestionRecommendation(
                choice_id=choice_id,
                label=label,
                reason=reason,
            )
    try:
        return InterviewQuestionPresentation(
            decision_id=f"repaired_{normalized_dimension}",
            target_dimension=normalized_dimension,
            locale=normalized_locale,
            question=question,
            context=context,
            choices=tuple(
                InterviewQuestionChoice(
                    choice_id=index,
                    meaning_key=f"repaired_choice_{index}",
                    label=label,
                )
                for index, label in enumerate(distinct_choices, start=1)
            ),
            recommendation=recommendation,
            free_text_prompt=(
                free_text_prompt[:500]
                if free_text_prompt
                and _looks_like_answer_instruction(free_text_prompt)
                and not _ends_with_question_mark(free_text_prompt)
                else _free_text_prompt(normalized_locale, bool(distinct_choices))
            ),
        )
    except ValidationError:
        return None


def rendered_question_contract_failures(text: str) -> tuple[str, ...]:
    """Validate rendered shape without requiring English decision words."""
    lines = tuple(line.strip() for line in text.splitlines() if line.strip())
    if not lines:
        return ("missing question",)

    failures: list[str] = []
    first_line = lines[0]
    question_mark_count = sum(first_line.count(mark) for mark in _QUESTION_MARKS)
    if question_mark_count != 1 or not first_line.endswith(_QUESTION_MARKS):
        failures.append("first line must contain one reply objective")
    if any(line.endswith(_QUESTION_MARKS) for line in lines[1:]):
        failures.append("only the first line may ask a question")

    authored_lines = [first_line]
    choice_rows: list[tuple[int, str]] = []
    for line in lines[1:]:
        match = _CHOICE_LINE_PATTERN.fullmatch(line)
        if match is None:
            continue
        choice_rows.append((int(match.group(1)), match.group(2).strip()))
        authored_lines.append(match.group(2).strip())

    if len(choice_rows) not in (0, 2, 3, 4):
        failures.append("choices must be omitted or contain two to four options")
    if choice_rows:
        choice_ids = tuple(choice_id for choice_id, _ in choice_rows)
        if choice_ids != tuple(range(1, len(choice_rows) + 1)):
            failures.append("numbered choices must use sequential labels")
        labels = tuple(label.casefold() for _, label in choice_rows)
        if len(set(labels)) != len(labels):
            failures.append("numbered choices must be distinct")

    if not _looks_like_answer_instruction(lines[-1]):
        failures.append("must explicitly preserve a direct-input answer path")

    authored_text = "\n".join(lines).casefold()
    if any(_contains_phrase(authored_text, term) for term in _INTERNAL_INTERVIEW_TERMS):
        failures.append("uses internal interview terminology")

    return tuple(failures)


def parse_question_presentation(text: str) -> InterviewQuestionPresentation | None:
    """Parse a structured presentation from raw provider output."""
    payload = _extract_json_mapping(text)
    if payload is None:
        return None
    nested = payload.get("presentation")
    if isinstance(nested, dict):
        payload = nested
    try:
        return InterviewQuestionPresentation.model_validate(payload)
    except ValidationError:
        return None


def recover_relayed_question_presentation(
    text: str,
    *,
    target_dimension: str = "goal_clarity",
    expected_presentation: InterviewQuestionPresentation | None = None,
) -> InterviewQuestionPresentation | None:
    """Recover a plugin-relayed question without trusting generated provenance."""
    presentation = parse_question_presentation(text)
    if expected_presentation is not None and (
        presentation == expected_presentation
        or (
            presentation is not None
            and render_question_presentation(presentation)
            == render_question_presentation(expected_presentation)
        )
        or text.strip() == render_question_presentation(expected_presentation)
    ):
        return expected_presentation
    if presentation is not None:
        if not generated_question_contract_failures(presentation):
            return presentation
    elif "\n" not in text and "\r" not in text:
        return None

    return repair_question_presentation(
        text,
        target_dimension=(
            presentation.target_dimension if presentation is not None else target_dimension
        ),
        locale=presentation.locale if presentation is not None else infer_question_locale(text),
    )


def render_question_presentation(presentation: InterviewQuestionPresentation) -> str:
    """Render a structured presentation without re-interpreting its semantics."""
    lines = [presentation.question, *presentation.context]
    lines.extend(f"{choice.choice_id}. {choice.label}" for choice in presentation.choices)

    if presentation.recommendation is not None:
        reason = _trim_terminal_punctuation(presentation.recommendation.reason)
        choice_id = presentation.recommendation.choice_id
        label = presentation.recommendation.label.rstrip(":：")
        lines.append(f"{label}: {choice_id}, {reason}.")

    lines.append(presentation.free_text_prompt)
    return "\n".join(lines)


def build_fallback_question_presentation(
    target_dimension: str,
    *,
    locale: str = "en",
) -> InterviewQuestionPresentation:
    """Build a deterministic direct-answer turn for invalid provider output."""
    normalized_dimension = (
        target_dimension if target_dimension in QUESTION_TARGET_DIMENSIONS else "goal_clarity"
    )
    language = _normalize_supported_locale(locale)

    if language == "ko":
        questions = {
            "goal_clarity": "이 프로젝트가 첫 사용자에게 어떤 결과를 제공해야 하나요?",
            "constraint_clarity": "이 프로젝트가 반드시 지켜야 할 제한이나 요구사항은 무엇인가요?",
            "success_criteria_clarity": (
                "이 프로젝트가 작동한다고 판단할 수 있는 관찰 가능한 결과는 무엇인가요?"
            ),
            "context_clarity": (
                "이 프로젝트가 반드시 고려해야 할 기존 상황이나 참고 자료는 무엇인가요?"
            ),
        }
    elif language == "zh":
        questions = {
            "goal_clarity": "这个项目首先应该为用户带来什么结果？",
            "constraint_clarity": "这个项目必须遵守哪项限制或要求？",
            "success_criteria_clarity": "什么可观察的结果能证明这个项目有效？",
            "context_clarity": "这个项目必须考虑哪种现有情况或参考资料？",
        }
    else:
        questions = {
            "goal_clarity": "What result should this project deliver for its first user?",
            "constraint_clarity": "Which limit or requirement must this project follow?",
            "success_criteria_clarity": (
                "What observable result would show that this project works?"
            ),
            "context_clarity": (
                "Which existing situation or reference must this project account for?"
            ),
        }

    return InterviewQuestionPresentation(
        decision_id=f"fallback_{normalized_dimension}",
        target_dimension=normalized_dimension,
        locale=language,
        question=questions[normalized_dimension],
        free_text_prompt=_free_text_prompt(language, False),
    )


def infer_question_locale(text: str) -> str:
    """Infer only the locale needed for deterministic fallback rendering."""
    if re.search(r"[\uac00-\ud7a3]", text):
        return "ko"
    if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text):
        return "zh"
    return "en"


def _repair_primary_question(value: str) -> str | None:
    line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    if not line:
        return None

    mark_positions = [line.find(mark) for mark in _QUESTION_MARKS if mark in line]
    if mark_positions:
        line = line[: min(mark_positions) + 1]
    else:
        line = line.rstrip(".!。！") + "?"

    split = _PRIMARY_DECISION_SPLIT_PATTERN.search(line)
    if split is not None:
        line = line[: split.start()].rstrip(" ,;:") + "?"
    return line if len(line) > 1 else None


def _normalize_supported_locale(locale: str) -> str:
    language = locale.strip().replace("_", "-").casefold().split("-", 1)[0]
    return language if language in {"en", "ko", "zh"} else "en"


def _free_text_prompt(locale: str, has_choices: bool) -> str:
    if locale == "ko":
        return (
            "번호로 답하거나 직접 답변을 작성하세요." if has_choices else "직접 답변을 작성하세요."
        )
    if locale == "zh":
        return "可回复编号，或直接写出你的答案。" if has_choices else "请直接写出你的答案。"
    return (
        "Reply with the number, or write your own answer."
        if has_choices
        else "Write your answer in your own words."
    )


def _looks_like_answer_instruction(line: str) -> bool:
    lowered = line.casefold()
    return any(
        marker in lowered
        for marker in (
            "own answer",
            "own words",
            "direct answer",
            "직접 답변",
            "직접 작성",
            "答案",
        )
    )


def _looks_like_recommendation(line: str) -> bool:
    lowered = line.casefold()
    return lowered.startswith(("recommended:", "권장:", "建议:", "建议："))


def _ends_with_question_mark(value: str) -> bool:
    return value.rstrip().endswith(_QUESTION_MARKS)


def _is_safe_generated_context_line(value: str) -> bool:
    lowered = value.casefold()
    return not _ends_with_question_mark(value) and not any(
        _contains_phrase(lowered, term) for term in _INTERNAL_INTERVIEW_TERMS
    )


def _extract_json_mapping(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    elif "{" in stripped and "}" in stripped:
        stripped = stripped[stripped.find("{") : stripped.rfind("}") + 1]
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _trim_terminal_punctuation(value: str) -> str:
    return value.strip().rstrip(".。!！?？")


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase.casefold())}(?!\w)", text) is not None


__all__ = [
    "QUESTION_PRESENTATION_CONTRACT_VERSION",
    "QUESTION_PRESENTATION_PROMPT",
    "QUESTION_PRESENTATION_RULES",
    "QUESTION_PRESENTATION_SCHEMA",
    "QUESTION_TARGET_DIMENSIONS",
    "InterviewQuestionChoice",
    "InterviewQuestionPresentation",
    "InterviewQuestionRecommendation",
    "QuestionChoiceProvenance",
    "build_fallback_question_presentation",
    "expand_selected_choice_answer",
    "generated_question_contract_failures",
    "infer_question_locale",
    "parse_numeric_choice_answer",
    "parse_question_presentation",
    "question_presentation_contract_metadata",
    "recover_relayed_question_presentation",
    "repair_question_presentation",
    "rendered_question_contract_failures",
    "render_question_presentation",
]
