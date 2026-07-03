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

    def test_unknown_vendor_voters_are_never_dropped(self) -> None:
        # "default" (Codex sentinel) is unmappable: it cannot be proven
        # same-vendor, so it must survive filtering unchanged.
        voters = ["default", "default", "openai/gpt-4o"]
        filtered = ri.filter_voter_models(voters, "codex")
        assert "default" in filtered
        assert filtered.count("default") == 2
        # The known same-vendor voter is the only one dropped... unless quorum
        # forbids it; here 2 unknowns remain, so gpt-4o (openai == codex) goes.
        assert "openai/gpt-4o" not in filtered


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

    def test_unknown_vendors_are_not_independence_evidence(self) -> None:
        # Bot repro: Codex consensus rosters normalize to ("default",)*3, and
        # "default" means "the Codex CLI's own default model" — very possibly
        # the executor's own vendor. Must be "unverified", NEVER "independent".
        result = ri.resolve_reviewer_independence(
            "codex",
            ["default", "default", "default"],
            configured_backends=["codex", "claude"],
        )
        assert result.status == ri.UNVERIFIED
        assert result.status != ri.INDEPENDENT
        assert result.is_independent is False
        # Unknown voters were not dropped either.
        assert result.filtered_voters == ("default", "default", "default")

    def test_mixed_unknown_and_known_different_is_independent(self) -> None:
        # One voter is provably a different vendor (google vs openai executor),
        # so independence IS positively proven despite the unknown sentinel.
        result = ri.resolve_reviewer_independence(
            "codex",
            ["default", "gemini-2.5-pro"],
            configured_backends=["codex", "gemini"],
        )
        assert result.status == ri.INDEPENDENT
        assert result.is_independent is True

    def test_unmappable_executor_vendor_is_unverified(self) -> None:
        # Executor backend not in the vendor map: independence is unprovable in
        # the other direction too — honest "unverified", not "independent".
        result = ri.resolve_reviewer_independence(
            "nonesuch",
            ["openai/gpt-4o", "google/gemini"],
            configured_backends=["codex", "gemini"],
        )
        assert result.status == ri.UNVERIFIED
        assert result.is_independent is False
