"""Unit tests for the Zcode CLI model-config readiness probe."""

from __future__ import annotations

import json

from ouroboros.config.zcode_model_config import inspect_zcode_model_config


def _write_config(tmp_path, payload) -> None:
    (tmp_path / "config.json").write_text(
        json.dumps(payload) if not isinstance(payload, str) else payload,
        encoding="utf-8",
    )


_PROVIDER_ENTRY = {
    "name": "Z.ai - Coding Plan",
    "kind": "anthropic",
    "options": {
        "baseURL": "https://api.z.ai/api/anthropic",
        "apiKey": "key",
        "apiKeyRequired": True,
    },
    "enabled": True,
}


def test_missing_file_reports_missing_config(tmp_path):
    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert not status.ok
    assert "not found" in status.detail
    assert "Model config is missing" in status.detail


def test_invalid_json_reports_parse_error(tmp_path):
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8")

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert not status.ok
    assert "not readable JSON" in status.detail


def test_top_level_non_object_is_rejected(tmp_path):
    _write_config(tmp_path, ["model"])

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert not status.ok
    assert "not an object" in status.detail


def test_no_model_reference_is_rejected(tmp_path):
    _write_config(tmp_path, {"provider": {"builtin:zai-coding-plan": _PROVIDER_ENTRY}})

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert not status.ok
    assert "no model or model.main" in status.detail


def test_object_form_model_main_is_rejected(tmp_path):
    # The Zcode CLI silently drops object-form model.main entries and then
    # fails with the same "Model config is missing" error; call that out
    # explicitly instead of reporting a generic gap.
    _write_config(
        tmp_path,
        {
            "model": {
                "main": {
                    "provider": "builtin:zai-coding-plan",
                    "model": "GLM-5.3",
                    "kind": "anthropic",
                }
            },
            "provider": {"builtin:zai-coding-plan": _PROVIDER_ENTRY},
        },
    )

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert not status.ok
    assert "object" in status.detail
    assert "string form" in status.detail


def test_model_ref_without_slash_is_rejected(tmp_path):
    _write_config(tmp_path, {"model": "builtin:zai-coding-plan"})

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert not status.ok
    assert "'provider-id/model-id' form" in status.detail


def test_provider_missing_from_registry_is_rejected(tmp_path):
    _write_config(
        tmp_path,
        {"model": {"main": "builtin:zai-coding-plan/GLM-5.3"}, "provider": {}},
    )

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert not status.ok
    assert "no entry" in status.detail


def test_provider_entry_without_kind_or_base_url_is_rejected(tmp_path):
    _write_config(
        tmp_path,
        {
            "model": {"main": "builtin:zai-coding-plan/GLM-5.3"},
            "provider": {"builtin:zai-coding-plan": {"name": "Z.ai"}},
        },
    )

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert not status.ok
    assert "no non-empty kind" in status.detail


def test_provider_entry_requires_kind_base_url_and_api_key(tmp_path):
    for entry in (
        {"options": {"baseURL": "https://example.test", "apiKey": "key"}},
        {"kind": "anthropic", "options": {"apiKey": "key"}},
        {"kind": "anthropic", "options": {"baseURL": "https://example.test"}},
        {
            "kind": "anthropic",
            "options": {"baseURL": "https://example.test", "apiKey": ""},
        },
    ):
        _write_config(
            tmp_path,
            {
                "model": {"main": "p/m"},
                "provider": {"p": entry},
            },
        )
        status = inspect_zcode_model_config(tmp_path / "config.json")
        assert not status.ok


def test_string_model_with_provider_entry_passes(tmp_path):
    _write_config(
        tmp_path,
        {
            "model": "builtin:zai-coding-plan/GLM-5.3",
            "provider": {"builtin:zai-coding-plan": _PROVIDER_ENTRY},
        },
    )

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert status.ok
    assert "builtin:zai-coding-plan/GLM-5.3" in status.detail


def test_model_main_string_with_provider_entry_passes(tmp_path):
    _write_config(
        tmp_path,
        {
            "model": {"main": "builtin:zai-coding-plan/GLM-5.3"},
            "provider": {"builtin:zai-coding-plan": _PROVIDER_ENTRY},
        },
    )

    status = inspect_zcode_model_config(tmp_path / "config.json")

    assert status.ok
    assert "provider entry present" in status.detail
