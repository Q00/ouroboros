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
        # dsh's effective provider is composition-owned; DeepSeek, OpenAI,
        # and any other composition must all remain unknown until the provider
        # is transported as verified runtime metadata.
        assert ri.backend_vendor("dsh") is None
        assert ri.backend_vendor(None) is None

    def test_model_vendor_markers(self) -> None:
        assert ri.model_vendor("openrouter/anthropic/claude-3.5") == "anthropic"
        assert ri.model_vendor("gpt-4o") == "openai"
        assert ri.model_vendor("google/gemini-2.0") == "google"
        assert ri.model_vendor("") == "unknown"


class TestVoterVendor:
    def test_alias_models_take_the_backend_vendor(self) -> None:
        for alias in ("opus", "sonnet", "haiku"):
            assert ri.voter_vendor(alias, "claude") == "anthropic"
            assert ri.voter_vendor(alias, "claude_code") == "anthropic"
        # Without a backend the bare alias carries no vendor marker.
        assert ri.voter_vendor("opus", None) == "unknown"

    def test_sentinel_proves_no_vendor(self) -> None:
        # "default" runs whatever the backend is configured to run, which may be
        # another provider (OpenCode, Codex with a custom provider), so it stays
        # unknown on every sentinel backend.
        for backend in ("codex", "gemini", "opencode", "goose", "dsh"):
            assert ri.voter_vendor("default", backend) == "unknown"
        assert ri.voter_vendor("gpt-4o", "dsh") == "openai"

    def test_explicit_backends_infer_from_the_model_id(self) -> None:
        assert ri.voter_vendor("openrouter/google/gemini-2.5-pro", "litellm") == "google"
        assert ri.voter_vendor("openrouter/openai/gpt-4o", "litellm") == "openai"
        assert ri.voter_vendor("default", "litellm") == "unknown"
        assert ri.voter_vendor("default", "copilot") == "unknown"

    def test_unknown_backend_infers_from_the_model_id(self) -> None:
        assert ri.voter_vendor("gpt-4o", "nonesuch") == "openai"
        assert ri.voter_vendor("gpt-4o", None) == ri.model_vendor("gpt-4o")


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

    def test_claude_alias_roster_is_independent_of_a_codex_executor(self) -> None:
        result = ri.resolve_reviewer_independence(
            "codex",
            ["opus", "sonnet"],
            configured_backends=["codex", "claude"],
            voter_backend="claude",
        )
        assert result.status == ri.INDEPENDENT
        assert result.voter_vendors == ("anthropic",)

    def test_claude_alias_roster_is_same_vendor_for_a_claude_executor(self) -> None:
        result = ri.resolve_reviewer_independence(
            "claude",
            ["opus", "sonnet"],
            configured_backends=["codex", "claude"],
            voter_backend="claude",
        )
        assert result.status == ri.SAME_VENDOR
        assert result.filtered_voters == ("opus", "sonnet")

    def test_sentinel_roster_on_a_multi_provider_backend_stays_unverified(self) -> None:
        result = ri.resolve_reviewer_independence(
            "codex",
            ["default", "default", "default"],
            configured_backends=["codex", "opencode"],
            voter_backend="opencode",
        )
        assert result.status == ri.UNVERIFIED
        assert result.voter_vendors == ("unknown",)

    def test_litellm_roster_is_classified_by_model_id(self) -> None:
        voters = [
            "openrouter/openai/gpt-4o",
            "openrouter/anthropic/claude-opus-4.8",
            "openrouter/google/gemini-2.5-pro",
        ]
        with_backend = ri.resolve_reviewer_independence(
            "claude", voters, configured_backends=["claude", "codex"], voter_backend="litellm"
        )
        without_backend = ri.resolve_reviewer_independence(
            "claude", voters, configured_backends=["claude", "codex"]
        )
        assert with_backend == without_backend
        assert with_backend.status == ri.INDEPENDENT
        assert "openrouter/anthropic/claude-opus-4.8" not in with_backend.filtered_voters

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
