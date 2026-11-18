"""The model-rejection classifier: explicit provider signals only, never a guess."""

from __future__ import annotations

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.providers.model_rejection import ModelRejection, classify_model_rejection

# The exact body Codex CLI 0.155.1 returned for a ChatGPT account (live, 2026-11-19).
_CODEX_BODY = (
    '{"error":{"type":"invalid_request_error","message":"The \'{model}\' model is not '
    'supported when using Codex with a ChatGPT account.","code":"model_not_found"}}'
)


def _codex_error(model: str) -> ProviderError:
    """The ProviderError the Codex adapter builds from that failure (codex_cli_adapter.py)."""
    body = _CODEX_BODY.replace("{model}", model)
    return ProviderError(
        body,
        provider="codex_cli",
        details={
            "returncode": 1,
            "session_id": "019a8c2e-7f1d-7b40-9d2a-3c5e8f0a1b2c",
            "stderr": (
                "Reading prompt from stdin...\n2026-11-19T00:12:03.512Z ERROR "
                "codex_api::endpoint::responses_websocket: websocket closed: " + body
            ),
            "stdout_errors": [body, body],
        },
    )


@pytest.mark.parametrize("model", ["gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-terra", "gpt-6.1-sol"])
def test_the_live_codex_chatgpt_account_refusal(model: str) -> None:
    found = classify_model_rejection(_codex_error(model), requested_model=model)

    assert found == ModelRejection(
        model=model,
        reason=(f"The '{model}' model is not supported when using Codex with a ChatGPT account."),
        rule="model_not_found_code",
        reason_code="chatgpt_account",
    )


def test_the_codex_refusal_reads_the_same_through_str_and_wrapping_exceptions() -> None:
    error = _codex_error("gpt-6.2-astra")
    expected = classify_model_rejection(error)

    assert expected is not None and expected.model == "gpt-6.2-astra"
    assert classify_model_rejection(str(error)) == expected
    assert classify_model_rejection(RuntimeError(str(error))) == expected
    # The reason is the provider's sentence, never the stderr or details blob.
    assert "stderr" not in expected.reason and "returncode" not in expected.reason


def test_a_codex_refusal_cut_short_still_names_its_model() -> None:
    # The interview handler keeps only the first 200 characters of an error.
    cut = _CODEX_BODY.replace("{model}", "gpt-6.2-astra")[:120]

    found = classify_model_rejection(cut)

    assert found is not None
    assert (found.model, found.rule) == ("gpt-6.2-astra", "model_not_supported")
    assert found.reason.startswith("The 'gpt-6.2-astra' model is not supported")


@pytest.mark.parametrize(
    ("text", "rule", "model"),
    [
        # Codex text without its JSON envelope.
        (
            "The 'gpt-6.2-sol' model is not supported when using Codex with a ChatGPT account.",
            "model_not_supported",
            "gpt-6.2-sol",
        ),
        ("Error: model 'gpt-q-1' is not supported", "model_not_supported", "gpt-q-1"),
        ("Error: model is not supported", "model_not_supported", "requested-1"),
        # Claude Code (tests/unit/providers/test_claude_code_adapter.py fixture).
        (
            "There's an issue with the selected model (missing-model). "
            "It may not exist or you may not have access to it.",
            "selected_model",
            "missing-model",
        ),
        # Claude CLI result envelope text, and Copilot.
        ("invalid model name", "invalid_model", "requested-1"),
        ("Invalid model name", "invalid_model", "requested-1"),
        ("Invalid model: claude-q-9", "invalid_model", "claude-q-9"),
        (
            "Your organization does not have access to model 'claude-opus-9'.",
            "no_model_access",
            "claude-opus-9",
        ),
        # OpenAI through LiteLLM, as text and as the OpenAI SDK's repr.
        (
            "litellm.NotFoundError: OpenAIException - The model `gpt-9` does not exist "
            "or you do not have access to it.",
            "model_does_not_exist",
            "gpt-9",
        ),
        (
            "litellm.NotFoundError: NotFoundError: OpenAIException - Error code: 404 - "
            "{'error': {'message': 'The model `gpt-9` does not exist or you do not have "
            "access to it.', 'type': 'invalid_request_error', 'param': None, "
            "'code': 'model_not_found'}}",
            "model_does_not_exist",
            "gpt-9",
        ),
        # Gemini API text without its JSON, and Gemini CLI / Pi.
        (
            "[API Error: models/gemini-9-pro is not found for API version v1beta]",
            "models_path_not_found",
            "gemini-9-pro",
        ),
        ("Model not found: gemini-9-pro", "model_not_found", "gemini-9-pro"),
        ("Model not found", "model_not_found", "requested-1"),
        # The phrases the Codex adapter already recognises.
        ("Codex: unsupported model", "unsupported_model", "requested-1"),
        ("Unknown model: x-1", "unknown_model", "x-1"),
        ("request failed (model_not_found)", "model_not_found_code", "requested-1"),
    ],
)
def test_each_phrase_rule(text: str, rule: str, model: str) -> None:
    found = classify_model_rejection(text, requested_model="requested-1")

    assert found is not None
    assert (found.rule, found.model) == (rule, model)
    assert found.reason and "\n" not in found.reason


@pytest.mark.parametrize(
    ("text", "rule", "model", "reason"),
    [
        (
            '{"error": {"message": "The model `gpt-9` does not exist or you do not have '
            'access to it.", "type": "invalid_request_error", "param": null, '
            '"code": "model_not_found"}}',
            "model_not_found_code",
            "gpt-9",
            "The model `gpt-9` does not exist or you do not have access to it.",
        ),
        (
            "litellm.NotFoundError: AnthropicException - "
            '{"type":"error","error":{"type":"not_found_error","message":"model: claude-x-9"}}',
            "anthropic_not_found",
            "claude-x-9",
            "model: claude-x-9",
        ),
        (
            '[API Error: {"error":{"code":404,"message":"models/gemini-9-pro is not found '
            'for API version v1beta, or is not supported for generateContent.",'
            '"status":"NOT_FOUND"}}]',
            "gemini_not_found",
            "gemini-9-pro",
            "models/gemini-9-pro is not found for API version v1beta, or is not supported "
            "for generateContent.",
        ),
    ],
)
def test_each_structured_rule(text: str, rule: str, model: str, reason: str) -> None:
    found = classify_model_rejection(text, requested_model="requested-1")

    assert found == ModelRejection(model=model, reason=reason, rule=rule)


def test_only_the_chatgpt_account_sentence_gets_a_reason_code() -> None:
    claude = classify_model_rejection("There's an issue with the selected model (m-1).")

    assert claude is not None and claude.reason_code is None


@pytest.mark.parametrize(
    "text",
    [
        "The model is overloaded. Please try again later.",
        "Rate limit reached for gpt-6-sol in organization org-x on tokens per min.",
        "503 Service Unavailable: model gpt-6-sol is temporarily unavailable",
        "Connection timed out while checking the selected model",
        "Streaming for this model is not supported",
        "invalid model output: expected JSON",
        "Unknown model role: 'x'. Valid roles are: clarification, interview.",
        "502 unknown provider for model gpt-x",
        "Authentication failed - check API key",
        "Context length exceeded for model gpt-6-sol (max 400000 tokens)",
        "you do not have access to the model's tools",
        "codex CLI exited with code 1",
        "Empty response from Codex CLI",
        '{"error":{"type":"invalid_request_error","message":"Unsupported parameter: '
        'temperature","code":"unsupported_parameter"}}',
        '{"type":"error","error":{"type":"not_found_error","message":"Not found: /v1/x"}}',
        '{"error":{"code":404,"message":"Requested entity was not found.","status":"NOT_FOUND"}}',
        "",
    ],
)
def test_other_failures_are_not_model_rejections(text: str) -> None:
    assert classify_model_rejection(text, requested_model="gpt-6-sol") is None


def test_an_http_404_alone_is_not_a_model_rejection() -> None:
    error = ProviderError("Not Found", provider="litellm", status_code=404)

    assert classify_model_rejection(error, requested_model="gpt-6-sol") is None


@pytest.mark.parametrize("value", [None, 42, object(), ["model_not_found"]])
def test_values_that_are_not_errors_are_not_classified(value: object) -> None:
    assert classify_model_rejection(value) is None


def test_details_are_not_read_only_the_message() -> None:
    """A refusal echoed in stderr does not turn an unrelated message into a rejection."""
    error = ProviderError(
        "Request timed out after 30s",
        details={"stderr": "The model `gpt-9` does not exist"},
    )

    assert classify_model_rejection(error) is None


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("gpt-6-sol", "gpt-6-sol"), ("default", None), ("", None), ("bad model", None), (None, None)],
)
def test_the_requested_model_fills_in_only_a_usable_id(
    requested: str | None, expected: str | None
) -> None:
    found = classify_model_rejection("Model not found", requested_model=requested)

    assert found is not None and found.model == expected


def test_only_the_head_of_a_huge_message_is_read() -> None:
    tail = "x" * 10_000 + " Model not found"

    assert classify_model_rejection(tail) is None


def test_a_long_reason_is_cut_to_one_line() -> None:
    found = classify_model_rejection("Model not found: m-1 " + "because " * 100)

    assert found is not None
    assert len(found.reason) <= 300 and found.reason.endswith("…")


def test_the_reason_inside_an_sdk_repr_is_the_provider_sentence() -> None:
    found = classify_model_rejection(
        "litellm.NotFoundError: NotFoundError: OpenAIException - Error code: 404 - "
        "{'error': {'message': 'The model `gpt-9` does not exist or you do not have access "
        "to it.', 'type': 'invalid_request_error', 'param': None, 'code': 'model_not_found'}}"
    )

    assert found is not None
    assert found.reason == "The model `gpt-9` does not exist or you do not have access to it."
