"""Structured question metadata helpers for platform UI adapters.

These helpers keep the MCP-side interview contract stable even when different
clients render user prompts differently. The returned metadata is intentionally
tool-agnostic, but includes ready-to-use payloads for Cursor and Claude Code
so interview turns stay choice-oriented even for open-ended prompts.
"""

from __future__ import annotations

from collections.abc import Sequence
import os
import re
from typing import Any

_VALID_ANSWER_MODES = frozenset({"free_text", "single_select", "multi_select"})
_FALLBACK_OPEN_ENDED_OPTION = "Not sure yet"
_CURSOR_CUSTOM_OPTION = {"id": "custom", "label": "Other"}
_CLAUDE_CUSTOM_OPTION = {"label": "Other", "description": "Type a custom answer"}


def detect_preferred_question_ui_tool() -> str | None:
    """Infer the host UI tool name for structured question prompts.

    Detection is best-effort only. Callers should still rely on the generic
    `question_spec` payload because some environments do not expose a stable
    marker for the active host.
    """

    override = os.environ.get("OUROBOROS_STRUCTURED_QUESTION_TOOL", "").strip()
    if override in {"AskQuestion", "AskUserQuestion"}:
        return override

    if any(
        os.environ.get(key)
        for key in ("CURSOR_EXTENSION_HOST_ROLE", "CURSOR_TRACE_ID", "CURSOR_SESSION_ID")
    ):
        return "AskQuestion"

    if any(
        os.environ.get(key)
        for key in ("CLAUDE_PROJECT_DIR", "CLAUDECODE", "CLAUDE_SESSION_ID")
    ):
        return "AskUserQuestion"

    return None


def _normalize_string_list(values: Sequence[str] | None) -> list[str]:
    """Return a de-duplicated list of non-empty strings preserving order."""

    if not values:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = " ".join(value.split()).strip()
        if not cleaned:
            continue
        lowered = cleaned.casefold()
        if lowered in seen:
            continue
        normalized.append(cleaned)
        seen.add(lowered)
    return normalized


def _normalize_answer_mode(answer_mode: str | None, option_labels: Sequence[str]) -> str:
    """Resolve the effective question answer mode."""

    cleaned = (answer_mode or "").strip().lower()
    if cleaned == "multi_select" and option_labels:
        return "multi_select"
    if cleaned in _VALID_ANSWER_MODES:
        return "single_select"
    if option_labels:
        return "single_select"
    return "free_text"


def _build_option_id(label: str, *, index: int, seen: set[str]) -> str:
    """Create a stable ASCII-ish option identifier from a label."""

    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    if not slug:
        slug = f"opt_{index}"
    candidate = slug
    suffix = 2
    while candidate in seen:
        candidate = f"{slug}_{suffix}"
        suffix += 1
    seen.add(candidate)
    return candidate


def _build_cursor_options(option_labels: Sequence[str], *, include_custom_input: bool) -> list[dict[str, str]]:
    """Build Cursor `AskQuestion` options payload."""

    options: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, label in enumerate(option_labels, start=1):
        options.append({"id": _build_option_id(label, index=index, seen=seen_ids), "label": label})
    if include_custom_input:
        options.append(dict(_CURSOR_CUSTOM_OPTION))
    return options


def _build_claude_options(option_labels: Sequence[str], *, include_custom_input: bool) -> list[dict[str, str]]:
    """Build Claude Code `AskUserQuestion` options payload."""

    options = [{"label": label, "description": ""} for label in option_labels]
    if include_custom_input:
        options.append(dict(_CLAUDE_CUSTOM_OPTION))
    return options


def build_question_ui_meta(
    question: str,
    *,
    title: str,
    answer_mode: str | None = None,
    options: Sequence[str] | None = None,
    has_custom_input: bool | None = None,
    response_param: str = "answer",
) -> dict[str, Any]:
    """Return normalized structured metadata for a user-facing question."""

    prompt = " ".join(question.split()).strip()
    raw_option_labels = _normalize_string_list(options)
    include_custom_input = True if has_custom_input is None else bool(has_custom_input)

    option_labels = list(raw_option_labels)
    if not option_labels:
        # Keep interview turns inside structured UI even when the caller did not
        # provide meaningful canned options. "Other" remains the escape hatch.
        option_labels = [_FALLBACK_OPEN_ENDED_OPTION]
        include_custom_input = True

    normalized_mode = (
        "single_select"
        if not raw_option_labels
        else _normalize_answer_mode(answer_mode, option_labels)
    )
    allow_multiple = normalized_mode == "multi_select"

    question_spec: dict[str, Any] = {
        "answer_mode": normalized_mode,
        "allow_multiple": allow_multiple,
        "options": list(option_labels),
        "has_custom_input": include_custom_input,
    }

    cursor_options = _build_cursor_options(
        option_labels,
        include_custom_input=include_custom_input,
    )
    claude_options = _build_claude_options(
        option_labels,
        include_custom_input=include_custom_input,
    )

    meta: dict[str, Any] = {
        "input_type": "choice" if cursor_options else "freeText",
        "response_param": response_param,
        "question": prompt,
        "question_spec": question_spec,
    }

    preferred_ui_tool = detect_preferred_question_ui_tool()
    if preferred_ui_tool is not None:
        meta["preferred_ui_tool"] = preferred_ui_tool

    if cursor_options:
        meta["cursor_question_payload"] = {
            "title": title,
            "questions": [
                {
                    "id": "interview_turn",
                    "prompt": prompt,
                    "options": cursor_options,
                    "allow_multiple": allow_multiple,
                }
            ],
        }
        meta["claude_question_payload"] = {
            "questions": [
                {
                    "header": title,
                    "question": prompt,
                    "options": claude_options,
                    "multiSelect": allow_multiple,
                }
            ],
        }

    return meta
