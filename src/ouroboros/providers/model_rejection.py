"""Recognise a runtime's "this model cannot be used" failure.

A surface that lets a person pick a model (the console's chats) has to tell a
rejected model apart from every other failure, so it can say so plainly and
offer the agent's default instead of a raw error. This module is that test and
nothing more. It reads the provider's own error text, never retries, and never
changes a model.

It matches only the signals below. Anything else is not a model rejection. A
model name merely appearing in an error is not enough, and neither is an HTTP
404 by itself or transient wording such as "overloaded" or "temporarily
unavailable".

Structured signals, read from a JSON error object embedded in the message:

* ``error.code == "model_not_found"``. Codex with a ChatGPT account says
  ``{"error": {"type": "invalid_request_error", "message": "The '<model>'
  model is not supported when using Codex with a ChatGPT account.", "code":
  "model_not_found"}}``. The OpenAI API (and LiteLLM, which wraps it) sends the
  same code for a 404 model.
* ``error.type == "not_found_error"`` with a ``"model: <id>"`` message. This is
  the Anthropic API's 404 for a model.
* ``error.status == "NOT_FOUND"`` with a ``"models/<id> is not found"``
  message. This is the Gemini API's 404 for a model.

Phrases, matched case-insensitively against the message (``_PHRASES``):

* ``The '<model>' model is not supported``, ``model '<model>' is not
  supported``, and a bare ``model is not supported`` that opens a sentence
  (Codex). "Streaming for this model is not supported" is a capability error
  and does not match.
* ``There's an issue with the selected model (<model>)`` (Claude Code).
* ``The model `<model>` does not exist`` (OpenAI, LiteLLM).
* ``models/<model> is not found`` (Gemini API text without its JSON).
* ``model_not_found`` as a bare token.
* ``model not found`` (Gemini CLI, Pi, Codex), ``does not have access to
  model`` (Claude, Codex).
* ``invalid model`` (Claude CLI, Copilot), ``unsupported model`` and
  ``unknown model`` (Codex). These three must end the phrase, optionally after
  ``name``, ``id`` or ``identifier``. A colon, a quote, punctuation or the end
  of the text must follow, so "invalid model output" or "unknown model role"
  never match.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Final, Literal

# Only the head of a message is read: provider errors put the reason first, and
# a bounded read keeps a huge stderr echo from costing anything.
_MAX_TEXT_CHARS: Final = 8000
_MAX_JSON_OBJECTS: Final = 16
_MAX_REASON_CHARS: Final = 300
_MAX_MODEL_CHARS: Final = 200

_MODEL_ID = r"[A-Za-z0-9][\w./:@+-]*"
_SAFE_MODEL = re.compile(rf"^{_MODEL_ID}$")
_CHATGPT_ACCOUNT = re.compile(
    r"\bnot supported when using Codex with a ChatGPT account\b", re.IGNORECASE
)
# A leading exception chain such as "litellm.NotFoundError: OpenAIException - ".
_EXCEPTION_PREFIX = re.compile(r"^(?:[\w.]*(?:Error|Exception)\s*(?::|-)\s*)+")
# A "message" field on the matched line: JSON (double quotes) or a Python repr
# of the same object (single quotes, as the OpenAI SDK prints it).
_QUOTED_MESSAGE = re.compile(r"""(["'])message\1\s*:\s*(["'])((?:(?!\2)[^\\]|\\.)*)""")

ReasonCode = Literal["chatgpt_account"]


@dataclass(frozen=True, slots=True)
class ModelRejection:
    """A failure the provider attributes to the model it was asked to use.

    Attributes:
        model: The model the provider named, else the model the caller asked
            for, else ``None``. The ``"default"`` sentinel is never reported.
        reason: The provider's own one-line reason.
        rule: Name of the signal that matched (see the module docstring).
        reason_code: ``"chatgpt_account"`` when the provider says the model is
            not available to Codex signed in with a ChatGPT account, so a page
            can show that reason in the reader's language; otherwise ``None``.
    """

    model: str | None
    reason: str
    rule: str
    reason_code: ReasonCode | None = None


def _bounded(name: str) -> str:
    """A phrase that must end where it ends: "invalid model" but not "invalid model output"."""
    return rf"\b{name}(?:\s+(?:name|id|identifier))?(?=\s*(?:$|[:.,;!)\]}}'\"`]))"


_Q = "['\"`]"  # a quote around a model id
_AFTER_COLON = rf"(?::\s*{_Q}?(?P<model>{_MODEL_ID}))?"

_PHRASES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (rule, re.compile(pattern, re.IGNORECASE | re.MULTILINE))
    for rule, pattern in (
        (
            "model_not_supported",
            rf"\bThe\s+{_Q}(?P<model>{_MODEL_ID}){_Q}\s+model is not supported\b",
        ),
        ("model_not_supported", rf"\bmodel\s+{_Q}(?P<model>{_MODEL_ID}){_Q}\s+is not supported\b"),
        # Bare, only where it opens a sentence: "Streaming for this model is
        # not supported" is a capability error, not a rejected model.
        ("model_not_supported", r"(?:^|(?<=[.!?:]\s))(?:The\s+)?model is not supported\b"),
        ("selected_model", rf"\bissue with the selected model \((?P<model>{_MODEL_ID})\)"),
        ("model_does_not_exist", rf"\bThe model {_Q}(?P<model>{_MODEL_ID}){_Q} does not exist\b"),
        ("models_path_not_found", rf"\bmodels/(?P<model>{_MODEL_ID}) is not found\b"),
        ("model_not_found_code", r"\bmodel_not_found\b"),
        ("model_not_found", rf"\bmodel not found\b{_AFTER_COLON}"),
        (
            "no_model_access",
            rf"\b(?:does|do) not have access to (?:the )?model\b(?!['’]s)"
            rf"(?:\s+{_Q}(?P<model>{_MODEL_ID}){_Q})?",
        ),
        ("invalid_model", _bounded("invalid model") + _AFTER_COLON),
        ("unsupported_model", _bounded("unsupported model") + _AFTER_COLON),
        ("unknown_model", _bounded("unknown model") + _AFTER_COLON),
    )
)

# Where a model id sits in a reason that a structured rule matched.
_MODEL_IN_REASON: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(rf"['\"`](?P<model>{_MODEL_ID})['\"`] model\b", re.IGNORECASE),
    re.compile(rf"\bmodel ['\"`](?P<model>{_MODEL_ID})['\"`]", re.IGNORECASE),
    re.compile(rf"^model:\s*(?P<model>{_MODEL_ID})", re.IGNORECASE),
    re.compile(rf"\bmodels/(?P<model>{_MODEL_ID})\b"),
)


def classify_model_rejection(
    error: object, *, requested_model: str | None = None
) -> ModelRejection | None:
    """Return the model rejection ``error`` reports, or ``None`` if it reports none.

    Args:
        error: A ``ProviderError`` (its ``message`` is read, not its details),
            another exception, or the error text itself.
        requested_model: The model the caller asked for. It is reported when
            the provider's text does not name one.
    """
    text = _error_text(error)
    if not text:
        return None
    for payload in _json_errors(text):
        found = _structured(payload)
        if found is not None:
            rule, reason, model = found
            return _rejection(rule, reason, model, requested_model)
    for rule, pattern in _PHRASES:
        match = pattern.search(text)
        if match is None:
            continue
        model = match.groupdict().get("model")
        return _rejection(rule, _reason_at(text, match.start()), model, requested_model)
    return None


def _error_text(error: object) -> str:
    if isinstance(error, str):
        text = error
    elif isinstance(error, BaseException):
        message = getattr(error, "message", None)
        text = message if isinstance(message, str) else str(error)
    else:
        return ""
    return text[:_MAX_TEXT_CHARS].strip()


def _json_errors(text: str) -> list[dict[str, object]]:
    """Error objects in ``text``: each ``{"error": {...}}`` found, outermost first."""
    decoder = json.JSONDecoder()
    found: list[dict[str, object]] = []
    start = text.find("{")
    attempts = 0
    while start != -1 and attempts < _MAX_JSON_OBJECTS:
        attempts += 1
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            inner = value.get("error")
            if isinstance(inner, dict):
                found.append(inner)
        start = text.find("{", end)
    return found


def _structured(payload: dict[str, object]) -> tuple[str, str, str | None] | None:
    """``(rule, reason, model)`` when an error object is a structured model rejection."""
    message = payload.get("message")
    reason = _one_line(message) if isinstance(message, str) else ""
    if payload.get("code") == "model_not_found":
        return "model_not_found_code", reason or "model_not_found", _model_in(reason)
    if payload.get("type") == "not_found_error" and reason.lower().startswith("model:"):
        return "anthropic_not_found", reason, _model_in(reason)
    if payload.get("status") == "NOT_FOUND" and re.search(r"\bmodels/\S+ is not found\b", reason):
        return "gemini_not_found", reason, _model_in(reason)
    return None


def _model_in(reason: str) -> str | None:
    for pattern in _MODEL_IN_REASON:
        match = pattern.search(reason)
        if match:
            return match.group("model")
    return None


def _reason_at(text: str, index: int) -> str:
    """The provider's sentence around ``index``: its line, or the JSON message on it."""
    line_start = text.rfind("\n", 0, index) + 1
    line_end = text.find("\n", index)
    line = text[line_start : line_end if line_end != -1 else len(text)]
    quoted = _QUOTED_MESSAGE.search(line)
    if quoted:
        body = quoted.group(3)
        try:
            line = json.loads(f'"{body}"') if quoted.group(2) == '"' else body.replace("\\'", "'")
        except ValueError:
            # A message cut mid-escape: show it as it reads.
            line = body
    return _one_line(line)


def _one_line(value: str) -> str:
    collapsed = " ".join(value.split())
    collapsed = _EXCEPTION_PREFIX.sub("", collapsed).strip()
    if len(collapsed) > _MAX_REASON_CHARS:
        collapsed = collapsed[: _MAX_REASON_CHARS - 1].rstrip() + "…"
    return collapsed


def _clean_model(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().rstrip(".,;:")
    if (
        not candidate
        or candidate.lower() == "default"
        or len(candidate) > _MAX_MODEL_CHARS
        or not _SAFE_MODEL.match(candidate)
    ):
        return None
    return candidate


def _rejection(rule: str, reason: str, named: str | None, requested: str | None) -> ModelRejection:
    model = _clean_model(named) or _clean_model(requested)
    return ModelRejection(
        model=model,
        reason=reason or rule,
        rule=rule,
        reason_code="chatgpt_account" if _CHATGPT_ACCOUNT.search(reason) else None,
    )


__all__ = ["ModelRejection", "ReasonCode", "classify_model_rejection"]
