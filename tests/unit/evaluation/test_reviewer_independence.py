"""Tests for executor != reviewer independence binding (PR-X X2)."""

from __future__ import annotations

from ouroboros.evaluation import reviewer_independence as ri


class TestVendorMapping:
    def test_backend_vendor_families(self) -> None:
        assert ri.backend_vendor("claude") == "anthropic"
        assert ri.backend_vendor("claude_mcp") == "anthropic"
        assert ri.backend_vendor("codex") == "openai"
        assert ri.backend_vendor("gemini") == "google"
        assert ri.backend_vendor("grok") == "xai"

    def test_backend_vendor_alias(self) -> None:
        # "claude_code" alias resolves to the claude vendor family.
        assert ri.backend_vendor("claude_code") == "anthropic"

    def test_unknown_backend(self) -> None:
        assert ri.backend_vendor("nonesuch") is None
        assert ri.backend_vendor(None) is None

    def test_model_vendor_markers(self) -> None:
        assert ri.model_vendor("openrouter/anthropic/claude-3.5") == "anthropic"
        assert ri.model_vendor("gpt-4o") == "openai"
        assert ri.model_vendor("google/gemini-2.0") == "google"
        assert ri.model_vendor("") == "unknown"


class TestFilterVoterModels:
    def test_drops_same_vendor_when_jury_stays_viable(self) -> None:
        voters = ["anthropic/claude", "openai/gpt-4o", "google/gemini"]
        filtered = ri.filter_voter_models(voters, "claude")
        assert "anthropic/claude" not in filtered
        assert set(filtered) == {"openai/gpt-4o", "google/gemini"}

    def test_keeps_roster_when_filtering_would_break_quorum(self) -> None:
        # Only one non-anthropic voter -> filtering would drop below 2, keep all.
        voters = ["anthropic/claude", "anthropic/claude-haiku", "openai/gpt-4o"]
        filtered = ri.filter_voter_models(voters, "claude")
        assert filtered == tuple(voters)

    def test_unknown_executor_is_noop(self) -> None:
        voters = ["anthropic/claude", "openai/gpt-4o"]
        assert ri.filter_voter_models(voters, "nonesuch") == tuple(voters)


class TestResolveIndependence:
    def test_single_backend_is_unavailable(self) -> None:
        # Only anthropic configured -> no independent reviewer possible.
        result = ri.resolve_reviewer_independence(
            "claude",
            ["anthropic/claude", "anthropic/claude-haiku"],
            configured_backends=["claude", "claude_mcp"],
        )
        assert result.status == ri.UNAVAILABLE
        # No behavior change: voters returned untouched.
        assert result.filtered_voters == ("anthropic/claude", "anthropic/claude-haiku")

    def test_independent_when_cross_vendor_available(self) -> None:
        result = ri.resolve_reviewer_independence(
            "claude",
            ["anthropic/claude", "openai/gpt-4o", "google/gemini"],
            configured_backends=["claude", "codex", "gemini"],
        )
        assert result.status == ri.INDEPENDENT
        assert result.is_independent is True
        assert "anthropic/claude" not in result.filtered_voters

    def test_same_vendor_when_quorum_forces_it(self) -> None:
        # Alternatives configured, but the roster is all-anthropic and filtering
        # would break quorum -> honest "same_vendor" rather than a false claim.
        result = ri.resolve_reviewer_independence(
            "claude",
            ["anthropic/claude", "anthropic/claude-haiku"],
            configured_backends=["claude", "codex"],
        )
        assert result.status == ri.SAME_VENDOR
        assert result.is_independent is False
