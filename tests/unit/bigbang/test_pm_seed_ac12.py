"""Tests for PMSeed field alignment with prd.md sections.

Verifies that PMSeed contains only the fields that map to prd.md sections:
Goal, User Stories, Constraints, Success Criteria, Assumptions, Decide Later,
Existing Codebase Context. Removed fields (seed, deferred_decisions,
referenced_repos, deferred_items) must not be present.
"""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from ouroboros.bigbang.pm_seed import PMSeed, UserStory


class TestPMSeedDeprecatedFields:
    """Tests that deprecated fields are accepted but merged into canonical fields."""

    def test_deferred_items_merged_into_decide_later(self):
        """Passing deferred_items merges them into decide_later_items."""
        pm = PMSeed(deferred_items=("DB selection",), decide_later_items=("Hosting?",))
        assert pm.decide_later_items == ("Hosting?", "DB selection")
        assert pm.deferred_items == ()  # cleared after merge

    def test_deferred_decisions_merged_into_decide_later(self):
        """Passing deferred_decisions merges them into decide_later_items."""
        pm = PMSeed(deferred_decisions=("Auth strategy?",))
        assert "Auth strategy?" in pm.decide_later_items
        assert pm.deferred_decisions == ()

    def test_referenced_repos_merged_into_brownfield(self):
        """Passing referenced_repos populates brownfield_repos."""
        repos = ({"path": "/x", "name": "x"},)
        pm = PMSeed(referenced_repos=repos)
        assert pm.brownfield_repos == repos
        assert pm.referenced_repos == ()

    def test_seed_string_preserved(self):
        """Passing seed as string preserves it for round-trip compatibility."""
        pm = PMSeed(seed="dev_seed_abc")
        assert pm.seed == "dev_seed_abc"
        d = pm.to_dict()
        assert d["seed"] == "dev_seed_abc"

    def test_seed_dict_preserved(self):
        """Passing seed as dict (legacy Seed.to_dict() output) is preserved."""
        seed_data = {"goal": "dev goal", "constraints": ["budget"]}
        pm = PMSeed(seed=seed_data)
        d = pm.to_dict()
        assert d["seed"] == seed_data

    def test_seed_object_with_to_dict(self):
        """Passing seed as object with to_dict() serializes correctly."""

        class MockSeed:
            def to_dict(self):
                return {"goal": "dev goal"}

        pm = PMSeed(seed=MockSeed())
        d = pm.to_dict()
        assert d["seed"] == {"goal": "dev goal"}


class TestPMSeedRetainedFields:
    """Tests that retained fields still work correctly."""

    def test_has_decide_later_items(self):
        """PMSeed has decide_later_items field."""
        pm = PMSeed(decide_later_items=("Q1?", "Q2?"))
        assert pm.decide_later_items == ("Q1?", "Q2?")

    def test_decide_later_items_default_empty(self):
        """decide_later_items defaults to empty tuple."""
        pm = PMSeed()
        assert pm.decide_later_items == ()

    def test_decide_later_items_frozen(self):
        """Cannot reassign decide_later_items on a frozen PMSeed."""
        pm = PMSeed(decide_later_items=("Q1?",))
        with pytest.raises(dataclasses.FrozenInstanceError):
            pm.decide_later_items = ("Q2?",)  # type: ignore[misc]


class TestPMSeedSerialization:
    """Tests for to_dict / from_dict without removed fields."""

    def test_to_dict_excludes_deprecated_fields(self):
        """to_dict does not include deprecated fields (except seed when non-empty)."""
        pm = PMSeed()
        d = pm.to_dict()
        assert "seed" not in d  # empty seed is omitted
        assert "deferred_decisions" not in d
        assert "referenced_repos" not in d
        assert "deferred_items" not in d

    def test_to_dict_preserves_seed_when_nonempty(self):
        """to_dict includes seed when non-empty for round-trip safety."""
        pm = PMSeed(seed="dev_seed_abc")
        d = pm.to_dict()
        assert d["seed"] == "dev_seed_abc"

    def test_to_dict_includes_decide_later_items(self):
        """to_dict includes decide_later_items."""
        pm = PMSeed(decide_later_items=("DB choice?", "Auth strategy?"))
        d = pm.to_dict()
        assert d["decide_later_items"] == ["DB choice?", "Auth strategy?"]

    def test_from_dict_migrates_legacy_fields(self):
        """from_dict migrates legacy fields into canonical counterparts."""
        data = {
            "product_name": "Widget",
            "goal": "Build widget",
            "seed": "dev_seed_123",
            "deferred_decisions": ["Choice X"],
            "referenced_repos": [{"path": "/x", "name": "x", "desc": "x"}],
        }
        pm = PMSeed.from_dict(data)
        assert pm.product_name == "Widget"
        assert pm.seed == "dev_seed_123"
        assert "Choice X" in pm.decide_later_items
        assert len(pm.brownfield_repos) == 1

    def test_from_dict_merges_both_brownfield_and_referenced_repos(self):
        """from_dict merges referenced_repos into brownfield_repos additively."""
        data = {
            "brownfield_repos": [{"path": "/a", "name": "a"}],
            "referenced_repos": [{"path": "/b", "name": "b"}],
        }
        pm = PMSeed.from_dict(data)
        paths = [r["path"] for r in pm.brownfield_repos]
        assert "/a" in paths
        assert "/b" in paths

    def test_post_init_merges_both_brownfield_and_referenced_repos(self):
        """Constructor merges referenced_repos into brownfield_repos additively."""
        pm = PMSeed(
            brownfield_repos=({"path": "/a", "name": "a"},),
            referenced_repos=({"path": "/b", "name": "b"},),
        )
        paths = [r["path"] for r in pm.brownfield_repos]
        assert "/a" in paths
        assert "/b" in paths
        assert pm.referenced_repos == ()

    def test_to_dict_preserves_falsey_seed(self):
        """to_dict preserves falsey-but-present seed values like {}."""
        pm = PMSeed(seed={})
        d = pm.to_dict()
        assert "seed" in d
        assert d["seed"] == {}

    def test_from_dict_rehydrates_seed_dict(self):
        """from_dict attempts to rehydrate dict seed into Seed object."""
        # Even if Seed.from_dict fails, the dict should be preserved
        data = {"seed": {"goal": "dev goal", "unknown_field": True}}
        pm = PMSeed.from_dict(data)
        # Should be either a Seed object or the raw dict (no data loss)
        assert pm.seed is not None
        if isinstance(pm.seed, dict):
            assert pm.seed["goal"] == "dev goal"
        else:
            # Successfully rehydrated as Seed
            assert hasattr(pm.seed, "to_dict")

    def test_from_dict_merges_legacy_deferred_items(self):
        """from_dict merges legacy deferred_items into decide_later_items."""
        data = {
            "deferred_items": ["DB selection", "CI/CD pipeline"],
            "decide_later_items": ["What caching?"],
        }
        pm = PMSeed.from_dict(data)
        assert "What caching?" in pm.decide_later_items
        assert "DB selection" in pm.decide_later_items
        assert "CI/CD pipeline" in pm.decide_later_items

    def test_from_dict_deduplicates_merged_items(self):
        """from_dict deduplicates when same item in both legacy and current."""
        data = {
            "deferred_items": ["DB selection"],
            "decide_later_items": ["DB selection", "What caching?"],
        }
        pm = PMSeed.from_dict(data)
        assert pm.decide_later_items.count("DB selection") == 1


class TestPMSeedYAMLRoundtrip:
    """Tests that fields survive YAML serialization roundtrip."""

    def test_roundtrip_preserves_decide_later_items(self):
        """YAML roundtrip preserves decide_later_items."""
        pm = PMSeed(
            product_name="Test",
            decide_later_items=("Choice A?", "Choice B?"),
        )
        yaml_str = pm.to_initial_context()
        loaded = yaml.safe_load(yaml_str)
        restored = PMSeed.from_dict(loaded)
        assert restored.decide_later_items == ("Choice A?", "Choice B?")

    def test_roundtrip_all_retained_fields(self):
        """YAML roundtrip preserves all retained fields."""
        pm = PMSeed(
            product_name="Full Test",
            goal="Test everything",
            decide_later_items=("DB choice?", "Hosting?"),
            brownfield_repos=(
                {"path": "/api", "name": "api", "desc": "API"},
                {"path": "/web", "name": "web", "desc": "Web"},
            ),
        )
        yaml_str = pm.to_initial_context()
        loaded = yaml.safe_load(yaml_str)
        restored = PMSeed.from_dict(loaded)
        assert restored.decide_later_items == ("DB choice?", "Hosting?")
        assert len(restored.brownfield_repos) == 2
        assert restored.brownfield_repos[1]["name"] == "web"


class TestPMSeedWithAllFields:
    """Tests that PMSeed can be constructed with all retained fields."""

    def test_full_pm_seed_construction(self):
        """PMSeed can be constructed with all retained fields."""
        pm = PMSeed(
            pm_id="pm_seed_test123",
            product_name="My Product",
            goal="Deliver value",
            user_stories=(UserStory(persona="PM", action="create PMs", benefit="ship faster"),),
            constraints=("Budget < $10k",),
            success_criteria=("Users adopt",),
            decide_later_items=("What DB?", "Phase 2 feature"),
            assumptions=("Users have internet",),
            interview_id="int_abc",
            codebase_context="existing monolith",
            brownfield_repos=({"path": "/mono", "name": "mono", "desc": "monolith"},),
        )
        assert pm.pm_id == "pm_seed_test123"
        assert pm.decide_later_items == ("What DB?", "Phase 2 feature")
        assert len(pm.user_stories) == 1
