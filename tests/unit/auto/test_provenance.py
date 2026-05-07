from __future__ import annotations

import json

import pytest

from ouroboros.auto.provenance import (
    ALLOWED_KEYS,
    KNOWN_INVOKED_BY,
    MAX_STRING_LEN,
    PROVENANCE_ENV_VAR,
    invoked_by_label,
    load_provenance_from_env,
    redact_provenance,
)
from ouroboros.auto.state import AutoPipelineState, AutoStore


def test_redact_drops_unknown_keys() -> None:
    raw = {
        "invoked_by": "gateway",
        "source_platform": "discord-hermes",
        # Unknown keys MUST be dropped — never leak channel/token-like data.
        "channel_id": "1234567890",
        "user_token": "ghp_secretsecretsecret",
        "raw_user_message": "long private chat content",
    }
    out = redact_provenance(raw)
    assert set(out) == {"invoked_by", "source_platform"}
    assert out["invoked_by"] == "gateway"
    assert out["source_platform"] == "discord-hermes"


def test_redact_normalizes_unknown_invoked_by() -> None:
    out = redact_provenance({"invoked_by": "definitely-not-real"})
    assert out["invoked_by"] == "unknown"


def test_redact_truncates_long_strings_and_drops_blanks() -> None:
    long_note = "x" * (MAX_STRING_LEN + 50)
    out = redact_provenance({"notes": long_note, "command_kind": "  "})
    assert "command_kind" not in out
    assert len(out["notes"]) == MAX_STRING_LEN


def test_redact_drops_non_string_values() -> None:
    out = redact_provenance({"invoked_by": 1, "notes": ["list", "not", "str"]})
    assert out == {}


def test_redact_handles_none_and_non_mapping() -> None:
    assert redact_provenance(None) == {}
    assert redact_provenance("not a mapping") == {}  # type: ignore[arg-type]


def test_load_from_env_returns_empty_when_unset() -> None:
    assert load_provenance_from_env({}) == {}


def test_load_from_env_parses_and_redacts() -> None:
    payload = {
        "invoked_by": "gateway",
        "source_platform": "discord-hermes",
        "command_kind": "rewrite",
        "channel_id": "leaked-id",  # must be dropped
    }
    env = {PROVENANCE_ENV_VAR: json.dumps(payload)}
    out = load_provenance_from_env(env)
    assert out == {
        "invoked_by": "gateway",
        "source_platform": "discord-hermes",
        "command_kind": "rewrite",
    }


def test_load_from_env_returns_empty_on_malformed_json() -> None:
    env = {PROVENANCE_ENV_VAR: "{not json"}
    assert load_provenance_from_env(env) == {}


def test_load_from_env_returns_empty_on_non_object_payload() -> None:
    env = {PROVENANCE_ENV_VAR: "[1, 2, 3]"}
    assert load_provenance_from_env(env) == {}


def test_invoked_by_label_defaults_to_direct() -> None:
    assert invoked_by_label({}) == "direct"
    assert invoked_by_label(None) == "direct"
    assert invoked_by_label({"invoked_by": "gateway"}) == "gateway"
    # Unknown values fall back to direct so blocker output never lies.
    assert invoked_by_label({"invoked_by": "totally-fake"}) == "direct"


def test_state_persists_redacted_provenance(tmp_path) -> None:
    store = AutoStore(tmp_path)
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    state.provenance = {
        "invoked_by": "gateway",
        "source_platform": "discord-hermes",
    }
    store.save(state)
    loaded = store.load(state.auto_session_id)
    assert loaded.provenance == {
        "invoked_by": "gateway",
        "source_platform": "discord-hermes",
    }


def test_state_rejects_disallowed_provenance_keys(tmp_path) -> None:
    """Persisting a payload that contains a non-allowlisted key (e.g. a token)
    must fail loudly so callers cannot route sensitive data through the
    provenance field."""
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    state.provenance = {
        "invoked_by": "gateway",
        "user_token": "ghp_secret",
    }
    with pytest.raises(ValueError, match="provenance contains disallowed keys"):
        AutoStore(tmp_path).save(state)


def test_state_treats_missing_provenance_as_empty(tmp_path) -> None:
    """Legacy state files without a `provenance` key must load successfully
    with an empty dict (back-compat)."""
    state = AutoPipelineState(goal="Build a CLI", cwd="/repo")
    raw = state.to_dict()
    del raw["provenance"]
    revived = AutoPipelineState.from_dict(raw)
    assert revived.provenance == {}


def test_allowed_keys_invariants() -> None:
    """Guard against accidental allowlist drift in review."""
    assert "invoked_by" in ALLOWED_KEYS
    # These are explicitly forbidden — if anyone adds them, this test fails so
    # the security review can intervene.
    for forbidden in {"channel_id", "user_token", "raw_user_message", "private_message"}:
        assert forbidden not in ALLOWED_KEYS
    assert "unknown" in KNOWN_INVOKED_BY
