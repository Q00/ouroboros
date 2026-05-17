"""Tests for ouroboros.orchestrator.profile_strategy (RFC v2 #830, PR 9)."""

from __future__ import annotations

import pytest

from ouroboros.core.seed import OntologyField, OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.execution_strategy import ExecutionStrategy
from ouroboros.orchestrator.profile_loader import ExecutionProfile, load_profile
from ouroboros.orchestrator.profile_strategy import ProfileBackedStrategy
from ouroboros.orchestrator.runner import build_system_prompt, build_task_prompt
from ouroboros.orchestrator.workflow_state import ActivityType


@pytest.fixture(params=["code", "research", "analysis"])
def profile(request: pytest.FixtureRequest) -> ExecutionProfile:
    return load_profile(request.param)


class TestProtocolConformance:
    def test_satisfies_execution_strategy(self, profile: ExecutionProfile) -> None:
        strategy = ProfileBackedStrategy(profile)
        # Runtime-checkable Protocol from execution_strategy.py.
        assert isinstance(strategy, ExecutionStrategy)

    def test_tools_come_from_profile(self, profile: ExecutionProfile) -> None:
        strategy = ProfileBackedStrategy(profile)
        assert strategy.get_tools() == list(profile.suggested_tools)

    def test_get_tools_returns_fresh_list(self, profile: ExecutionProfile) -> None:
        strategy = ProfileBackedStrategy(profile)
        first = strategy.get_tools()
        first.append("Mutated")
        # Mutating the returned list must not bleed into the strategy's
        # view — frozen profile data should stay frozen for callers.
        assert strategy.get_tools() != first


class TestSystemPromptFragment:
    def test_mentions_profile_axis_and_min_unit(self, profile: ExecutionProfile) -> None:
        fragment = ProfileBackedStrategy(profile).get_system_prompt_fragment()
        assert profile.profile in fragment
        assert profile.axis in fragment
        assert profile.min_unit in fragment

    def test_surfaces_verifier_focus(self, profile: ExecutionProfile) -> None:
        fragment = ProfileBackedStrategy(profile).get_system_prompt_fragment()
        # First word of verifier focus must be present so the leaf
        # sees the verifier's expectation before acting.
        first_token = profile.verifier_focus.strip().split()[0]
        assert first_token in fragment

    def test_profiles_produce_distinct_fragments(self) -> None:
        c = ProfileBackedStrategy(load_profile("code")).get_system_prompt_fragment()
        r = ProfileBackedStrategy(load_profile("research")).get_system_prompt_fragment()
        a = ProfileBackedStrategy(load_profile("analysis")).get_system_prompt_fragment()
        # Pairwise distinct — `a != b != c` only proves the adjacent pairs.
        assert c != r
        assert r != a
        assert c != a

    def test_fragment_carries_post_block_with_evidence_fields(
        self, profile: ExecutionProfile
    ) -> None:
        # The fragment must surface every required evidence field name
        # verbatim so the leaf can emit a record the H2 validator
        # accepts. Without this, opting into ProfileBackedStrategy
        # would bypass H2/H1.
        fragment = ProfileBackedStrategy(profile).get_system_prompt_fragment()
        assert "[POST" in fragment
        for required in profile.evidence_schema.required:
            assert required in fragment, f"{required!r} missing from system prompt"

    def test_fragment_preserves_legacy_domain_guidance(self) -> None:
        # Bot finding on #891 r4: dropping the legacy markdown agent
        # guidance regressed behavior callers relied on. ProfileBacked-
        # Strategy must preserve the domain-specific instructions for
        # each built-in profile.
        c = ProfileBackedStrategy(load_profile("code")).get_system_prompt_fragment()
        assert "clean, well-tested code" in c

        r = ProfileBackedStrategy(load_profile("research")).get_system_prompt_fragment()
        assert "Cite sources" in r
        assert "markdown" in r.lower()

        a = ProfileBackedStrategy(load_profile("analysis")).get_system_prompt_fragment()
        assert "tradeoffs" in a.lower() or "trade-offs" in a.lower()
        assert "structured analytical" in a.lower()

    def test_fragment_directs_consolidated_record(self, profile: ExecutionProfile) -> None:
        # Bot finding on #891 r6 round 2: H2's extract_evidence parses
        # ONE JSON object per dispatch, so the multi-AC contract must
        # ask for a single consolidated record (not one per AC). The
        # fragment must reflect that: "every AC", "single ... record".
        fragment = ProfileBackedStrategy(profile).get_system_prompt_fragment()
        assert "every acceptance criterion" in fragment.lower() or "every AC" in fragment
        # "exactly ONE" or "single ... record" — accept either wording.
        assert "exactly one" in fragment.lower() or "single" in fragment.lower()

    def test_fragment_omits_blocker_marker_until_schema_extension(
        self, profile: ExecutionProfile
    ) -> None:
        # Bot finding on #891 r7: encoding a blocker marker (empty list
        # / BLOCKED string) in the evidence record would trigger
        # rejected_if and surface as EVIDENCE_MISSING anyway. Until
        # H2/H7 gain a structured BLOCKED channel, the prompt must NOT
        # instruct the executor to fabricate blocker markers — that
        # would actively misroute. The contract is documented as a
        # follow-up dependency instead.
        fragment = ProfileBackedStrategy(profile).get_system_prompt_fragment()
        # The system prompt should not instruct the executor to encode
        # blockers using markers that conflict with rejected_if.
        assert "empty list" not in fragment.lower()
        assert "BLOCKED:" not in fragment

    def test_suffix_demands_restatement_and_preconditions(self, profile: ExecutionProfile) -> None:
        suffix = ProfileBackedStrategy(profile).get_task_prompt_suffix()
        assert "[PRE" in suffix
        assert "restate" in suffix.lower()
        assert "precondition" in suffix.lower()


class TestTaskPromptSuffix:
    def test_directs_evidence_record_on_completion(self, profile: ExecutionProfile) -> None:
        # The suffix must direct the executor to emit the consolidated
        # evidence record described in the system prompt, not declare
        # DONE in prose.
        suffix = ProfileBackedStrategy(profile).get_task_prompt_suffix()
        assert "evidence" in suffix.lower()

    def test_suffix_is_profile_independent(self) -> None:
        # The suffix is structural (H1/H2 hooks), so it should be the
        # same string across profiles.
        c = ProfileBackedStrategy(load_profile("code")).get_task_prompt_suffix()
        r = ProfileBackedStrategy(load_profile("research")).get_task_prompt_suffix()
        assert c == r

    def test_suffix_mentions_blocker_capture_in_prose_fields(self) -> None:
        # Bot finding on #891 r7: the harness has no structured BLOCKED
        # channel today, so we cannot route blockers via markers in the
        # record. The PRE gate instead asks the executor to capture the
        # blocker in prose fields of the evidence record so the
        # operator can act on it; routing the failure class to BLOCKED
        # is a documented follow-up.
        suffix = ProfileBackedStrategy(load_profile("code")).get_task_prompt_suffix()
        assert "blocker" in suffix.lower()
        assert "evidence record" in suffix.lower()


class TestActivityMap:
    def test_known_tools_get_canonical_activity(self) -> None:
        strategy = ProfileBackedStrategy(load_profile("code"))
        activity_map = strategy.get_activity_map()
        assert activity_map["Read"] == ActivityType.EXPLORING
        assert activity_map["Edit"] == ActivityType.BUILDING
        assert activity_map["Bash"] == ActivityType.TESTING

    def test_only_profile_tools_appear(self, profile: ExecutionProfile) -> None:
        strategy = ProfileBackedStrategy(profile)
        activity_map = strategy.get_activity_map()
        assert set(activity_map.keys()) == set(profile.suggested_tools)

    def test_unknown_tool_defaults_to_exploring(self) -> None:
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        custom = load_profile("code").model_copy(
            update={
                "suggested_tools": ("Read", "MysteryTool"),
                "evidence_schema": EvidenceSchema(),
            }
        )
        activity_map = ProfileBackedStrategy(custom).get_activity_map()
        assert activity_map["MysteryTool"] == ActivityType.EXPLORING

    def test_bash_semantics_match_legacy_per_profile(self) -> None:
        # Bot finding on #891 r2: hardcoding Bash → TESTING for every
        # profile regressed research dashboard semantics (legacy
        # ResearchStrategy mapped Bash → EXPLORING because Bash there
        # backs grep/curl, not test runs).
        from ouroboros.orchestrator.profile_loader import EvidenceSchema

        # Force Bash into each profile so the activity_map can be asserted
        # uniformly (research.yaml does not declare Bash by default).
        for name, expected in (
            ("code", ActivityType.TESTING),
            ("analysis", ActivityType.TESTING),
            ("research", ActivityType.EXPLORING),
        ):
            base = load_profile(name)
            forced = base.model_copy(
                update={
                    "suggested_tools": tuple({*base.suggested_tools, "Bash"}),
                    "evidence_schema": EvidenceSchema(),
                }
            )
            activity_map = ProfileBackedStrategy(forced).get_activity_map()
            assert activity_map["Bash"] == expected, (
                f"{name} profile mapped Bash → {activity_map['Bash']}, "
                f"expected {expected} (matches legacy execution_strategy)"
            )


class TestRunnerPromptIntegration:
    """Strategy must wire H3 wrappers through runner.build_*_prompt.

    Without this, opting into ProfileBackedStrategy would compose a
    prompt that bypasses H2/H1 — the verifier loop would FAIL on every
    attempt because the leaf never sees the evidence_schema field
    names.
    """

    def _seed(self) -> Seed:
        return Seed(
            goal="Ship feature",
            constraints=("Python 3.14+",),
            acceptance_criteria=("Add caching layer", "Cover with tests"),
            ontology_schema=OntologySchema(
                name="Cache",
                description="Caching layer ontology",
                fields=(
                    OntologyField(name="entries", field_type="array", description="cached items"),
                ),
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )

    def test_system_prompt_includes_post_block_required_fields(self) -> None:
        profile = load_profile("code")
        strategy = ProfileBackedStrategy(profile)
        prompt = build_system_prompt(self._seed(), strategy=strategy)
        # H3 POST block markers must reach the actual system prompt.
        assert "[POST" in prompt
        for required in profile.evidence_schema.required:
            assert required in prompt, f"{required!r} missing from build_system_prompt output"
        assert "tests_passed == []" in prompt
        assert "docs verification commands" in prompt
        assert "Do not include exploratory discovery commands" in prompt
        assert "rg, grep, sed, cat, ls, find, or pwd" in prompt

    def test_task_prompt_includes_pre_gate(self) -> None:
        prompt = build_task_prompt(
            self._seed(), strategy=ProfileBackedStrategy(load_profile("code"))
        )
        assert "[PRE" in prompt
        assert "restate" in prompt.lower()
        assert "precondition" in prompt.lower()
        assert "Add caching layer" in prompt
        assert "Cover with tests" in prompt
