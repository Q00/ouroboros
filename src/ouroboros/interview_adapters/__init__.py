"""Reference-aware interview adapter primitives.

The adapter package is intentionally independent from interview persistence and
Seed generation. It provides bounded, validated inputs and deterministic prompt
helpers that higher-level interview code can consume without treating glossary
or reference material as requirements.
"""

from ouroboros.interview_adapters.manifest import (
    GlossaryManifest,
    GlossaryTerm,
    ManifestError,
    load_builtin_manifest,
    load_manifest_resource,
)
from ouroboros.interview_adapters.models import (
    CONFUSED_TERM_LIMIT,
    EXCERPT_LENGTH_LIMIT,
    LABEL_LENGTH_LIMIT,
    REFERENCE_LIMIT,
    TERM_LENGTH_LIMIT,
    URL_LENGTH_LIMIT,
    InterviewTurnContext,
    ReferenceContrastResolution,
    ReferenceCue,
    ReferenceOrigin,
    ReferenceResolutionStatus,
)
from ouroboros.interview_adapters.question_batches import (
    QUESTION_BATCH_CONTRACT_VERSION,
    PlainTextAnswerAttempt,
    PlainTextAnswerCollector,
    QuestionAnswer,
    QuestionAnswerParseError,
    QuestionBatch,
    QuestionBatchAnswers,
    QuestionBatchOption,
    QuestionBatchPage,
    QuestionBatchQuestion,
    QuestionHostCapabilities,
    QuestionRenderMode,
    RenderedQuestionBatchPage,
    parse_native_question_answers,
    parse_plain_text_question_answers,
    render_plain_text_question_page,
    render_question_batch,
)
from ouroboros.interview_adapters.reference_contrast import (
    build_reference_contrast_question,
    candidates_from_contrast_answer,
    next_unresolved_reference,
)
from ouroboros.interview_adapters.registry import BuiltinGlossaryRegistry, builtin_registry
from ouroboros.interview_adapters.triggers import (
    GlossaryInjection,
    detect_explicit_confusion_terms,
    select_glossary_injection,
)

__all__ = [
    "CONFUSED_TERM_LIMIT",
    "EXCERPT_LENGTH_LIMIT",
    "LABEL_LENGTH_LIMIT",
    "REFERENCE_LIMIT",
    "TERM_LENGTH_LIMIT",
    "URL_LENGTH_LIMIT",
    "BuiltinGlossaryRegistry",
    "GlossaryInjection",
    "GlossaryManifest",
    "GlossaryTerm",
    "InterviewTurnContext",
    "ManifestError",
    "PlainTextAnswerAttempt",
    "PlainTextAnswerCollector",
    "QUESTION_BATCH_CONTRACT_VERSION",
    "QuestionAnswer",
    "QuestionAnswerParseError",
    "QuestionBatch",
    "QuestionBatchAnswers",
    "QuestionBatchOption",
    "QuestionBatchPage",
    "QuestionBatchQuestion",
    "QuestionHostCapabilities",
    "QuestionRenderMode",
    "ReferenceContrastResolution",
    "ReferenceCue",
    "ReferenceOrigin",
    "ReferenceResolutionStatus",
    "RenderedQuestionBatchPage",
    "build_reference_contrast_question",
    "builtin_registry",
    "candidates_from_contrast_answer",
    "detect_explicit_confusion_terms",
    "load_builtin_manifest",
    "load_manifest_resource",
    "next_unresolved_reference",
    "parse_native_question_answers",
    "parse_plain_text_question_answers",
    "render_plain_text_question_page",
    "render_question_batch",
    "select_glossary_injection",
]
