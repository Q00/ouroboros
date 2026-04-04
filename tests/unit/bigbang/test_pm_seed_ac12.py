"""Tests for PMSeed field alignment with pm.md sections."""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from ouroboros.bigbang.pm_seed import PMSeed, UserStory


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
    """Tests for to_dict / from_dict with canonical fields only."""

    def test_to_dict_excludes_noncanonical_fields(self):
        """to_dict only includes fields shared with pm.md."""
        pm = PMSeed()
        d = pm.to_dict()
        assert "codebase_context" not in d
        assert "seed" not in d
        assert "deferred_decisions" not in d
        assert "referenced_repos" not in d
        assert "deferred_items" not in d

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
            "deferred_decisions": ["Choice X"],
            "referenced_repos": [{"path": "/x", "name": "x", "desc": "x"}],
            "codebase_context": "legacy brownfield summary",
        }
        pm = PMSeed.from_dict(data)
        assert pm.product_name == "Widget"
        assert "Choice X" in pm.decide_later_items
        assert len(pm.brownfield_repos) == 1
        assert "codebase_context" not in pm.to_dict()

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

    def test_from_dict_merges_referenced_repos_into_brownfield_repos(self):
        """from_dict merges legacy referenced_repos additively."""
        data = {
            "brownfield_repos": [{"path": "/a", "name": "a"}],
            "referenced_repos": [{"path": "/b", "name": "b"}],
        }
        pm = PMSeed.from_dict(data)
        paths = [r["path"] for r in pm.brownfield_repos]
        assert "/a" in paths
        assert "/b" in paths


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
            brownfield_repos=({"path": "/mono", "name": "mono", "desc": "monolith"},),
        )
        assert pm.pm_id == "pm_seed_test123"
        assert pm.decide_later_items == ("What DB?", "Phase 2 feature")
        assert len(pm.user_stories) == 1
