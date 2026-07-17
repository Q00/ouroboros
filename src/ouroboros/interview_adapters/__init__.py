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
from ouroboros.interview_adapters.presentation import (
    QUESTION_PRESENTATION_CONTRACT_VERSION,
    QUESTION_PRESENTATION_PROMPT,
    QUESTION_PRESENTATION_RULES,
    QUESTION_PRESENTATION_SCHEMA,
    QUESTION_TARGET_DIMENSIONS,
    InterviewQuestionChoice,
    InterviewQuestionPresentation,
    InterviewQuestionRecommendation,
    QuestionChoiceProvenance,
    build_fallback_question_presentation,
    expand_selected_choice_answer,
    generated_question_contract_failures,
    infer_question_locale,
    parse_question_presentation,
    render_question_presentation,
    rendered_question_contract_failures,
    repair_question_presentation,
)
from ouroboros.interview_adapters.reference_contrast import (
    build_reference_contrast_presentation,
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
    "InterviewQuestionChoice",
    "InterviewQuestionPresentation",
    "InterviewQuestionRecommendation",
    "ManifestError",
    "ReferenceContrastResolution",
    "ReferenceCue",
    "ReferenceOrigin",
    "ReferenceResolutionStatus",
    "QuestionChoiceProvenance",
    "QUESTION_PRESENTATION_CONTRACT_VERSION",
    "QUESTION_PRESENTATION_PROMPT",
    "QUESTION_PRESENTATION_RULES",
    "QUESTION_PRESENTATION_SCHEMA",
    "QUESTION_TARGET_DIMENSIONS",
    "build_fallback_question_presentation",
    "build_reference_contrast_question",
    "build_reference_contrast_presentation",
    "builtin_registry",
    "candidates_from_contrast_answer",
    "detect_explicit_confusion_terms",
    "expand_selected_choice_answer",
    "generated_question_contract_failures",
    "load_builtin_manifest",
    "load_manifest_resource",
    "next_unresolved_reference",
    "infer_question_locale",
    "parse_question_presentation",
    "repair_question_presentation",
    "rendered_question_contract_failures",
    "render_question_presentation",
    "select_glossary_injection",
]
