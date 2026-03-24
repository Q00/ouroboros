"""Tests for AC 12: PMSeed frozen dataclass with seed, deferred_decisions, referenced_repos."""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from ouroboros.bigbang.pm_seed import PMSeed, UserStory
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)


def _make_seed(**overrides) -> Seed:
    """Create a minimal valid Seed for testing."""
    defaults = {
        "goal": "Build a test widget",
        "ontology_schema": OntologySchema(
            name="TestSchema",
            description="Test schema",
            fields=(
                OntologyField(
                    name="output",
                    field_type="string",
                    description="Test output",
                ),
            ),
        ),
        "metadata": SeedMetadata(ambiguity_score=0.1),
        "constraints": ("Python 3.12+",),
        "acceptance_criteria": ("Widget works",),
        "evaluation_principles": (
            EvaluationPrinciple(
                name="correctness",
                description="Output is correct",
            ),
        ),
        "exit_conditions": (
            ExitCondition(
                name="done",
                description="All done",
                evaluation_criteria="100%",
            ),
        ),
    }
    defaults.update(overrides)
    return Seed(**defaults)


class TestPMSeedNewFields:
    """Tests that PMSeed has the three AC 12 fields."""

    def test_has_seed_field(self):
        """PMSeed has a 'seed' field typed Seed | None."""
        pm = PMSeed()
        assert pm.seed is None

    def test_seed_field_accepts_seed(self):
        """PMSeed.seed accepts a Seed instance."""
        seed = _make_seed()
        pm = PMSeed(seed=seed)
        assert pm.seed is seed
        assert pm.seed.goal == "Build a test widget"

    def test_has_deferred_decisions_field(self):
        """PMSeed has a 'deferred_decisions' field defaulting to empty tuple."""
        pm = PMSeed()
        assert pm.deferred_decisions == ()

    def test_deferred_decisions_accepts_tuple(self):
        """PMSeed.deferred_decisions stores tuple of strings."""
        decisions = ("Use SQL vs NoSQL", "Cloud provider choice")
        pm = PMSeed(deferred_decisions=decisions)
        assert pm.deferred_decisions == decisions
        assert len(pm.deferred_decisions) == 2

    def test_has_referenced_repos_field(self):
        """PMSeed has a 'referenced_repos' field defaulting to empty tuple."""
        pm = PMSeed()
        assert pm.referenced_repos == ()

    def test_referenced_repos_accepts_tuple_of_dicts(self):
        """PMSeed.referenced_repos stores tuple of dicts."""
        repos = (
            {"path": "/code/api", "name": "api", "desc": "API service"},
            {"path": "/code/web", "name": "web", "desc": "Web frontend"},
        )
        pm = PMSeed(referenced_repos=repos)
        assert pm.referenced_repos == repos
        assert pm.referenced_repos[0]["name"] == "api"


class TestPMSeedFrozen:
    """Tests that new fields are frozen (immutable)."""

    def test_seed_is_frozen(self):
        """Cannot reassign seed on a frozen PMSeed."""
        pm = PMSeed()
        with pytest.raises(dataclasses.FrozenInstanceError):
            pm.seed = _make_seed()  # type: ignore[misc]

    def test_deferred_decisions_is_frozen(self):
        """Cannot reassign deferred_decisions on a frozen PMSeed."""
        pm = PMSeed(deferred_decisions=("Choice A",))
        with pytest.raises(dataclasses.FrozenInstanceError):
            pm.deferred_decisions = ("Choice B",)  # type: ignore[misc]

    def test_referenced_repos_is_frozen(self):
        """Cannot reassign referenced_repos on a frozen PMSeed."""
        pm = PMSeed(referenced_repos=({"path": "/x", "name": "x", "desc": "x"},))
        with pytest.raises(dataclasses.FrozenInstanceError):
            pm.referenced_repos = ()  # type: ignore[misc]


class TestPMSeedSerialization:
    """Tests for to_dict / from_dict with new fields."""

    def test_to_dict_includes_seed_none(self):
        """to_dict includes seed as None when not set."""
        pm = PMSeed()
        d = pm.to_dict()
        assert "seed" in d
        assert d["seed"] is None

    def test_to_dict_includes_seed_data(self):
        """to_dict serializes Seed via to_dict()."""
        seed = _make_seed()
        pm = PMSeed(seed=seed)
        d = pm.to_dict()
        assert d["seed"] is not None
        assert d["seed"]["goal"] == "Build a test widget"
        assert isinstance(d["seed"], dict)

    def test_to_dict_includes_deferred_decisions(self):
        """to_dict includes deferred_decisions as a list."""
        pm = PMSeed(deferred_decisions=("DB choice", "Auth strategy"))
        d = pm.to_dict()
        assert d["deferred_decisions"] == ["DB choice", "Auth strategy"]

    def test_to_dict_includes_referenced_repos(self):
        """to_dict includes referenced_repos as a list of dicts."""
        repos = ({"path": "/a", "name": "a", "desc": "repo a"},)
        pm = PMSeed(referenced_repos=repos)
        d = pm.to_dict()
        assert d["referenced_repos"] == [{"path": "/a", "name": "a", "desc": "repo a"}]

    def test_from_dict_without_seed(self):
        """from_dict handles missing seed gracefully."""
        data = {"product_name": "Widget", "goal": "Build widget"}
        pm = PMSeed.from_dict(data)
        assert pm.seed is None
        assert pm.product_name == "Widget"

    def test_from_dict_with_seed(self):
        """from_dict deserializes seed via Seed.from_dict."""
        seed = _make_seed()
        data = PMSeed(seed=seed, product_name="Widget").to_dict()
        restored = PMSeed.from_dict(data)
        assert restored.seed is not None
        assert restored.seed.goal == "Build a test widget"
        assert restored.seed.constraints == ("Python 3.12+",)

    def test_from_dict_with_deferred_decisions(self):
        """from_dict restores deferred_decisions."""
        data = {"deferred_decisions": ["Choice X", "Choice Y"]}
        pm = PMSeed.from_dict(data)
        assert pm.deferred_decisions == ("Choice X", "Choice Y")

    def test_from_dict_without_deferred_decisions(self):
        """from_dict defaults deferred_decisions to empty tuple."""
        pm = PMSeed.from_dict({})
        assert pm.deferred_decisions == ()

    def test_from_dict_with_referenced_repos(self):
        """from_dict restores referenced_repos."""
        data = {
            "referenced_repos": [
                {"path": "/r", "name": "r", "desc": "repo r"},
            ],
        }
        pm = PMSeed.from_dict(data)
        assert len(pm.referenced_repos) == 1
        assert pm.referenced_repos[0]["name"] == "r"

    def test_from_dict_without_referenced_repos(self):
        """from_dict defaults referenced_repos to empty tuple."""
        pm = PMSeed.from_dict({})
        assert pm.referenced_repos == ()


class TestPMSeedYAMLRoundtrip:
    """Tests that new fields survive YAML serialization roundtrip."""

    def test_roundtrip_without_seed(self):
        """YAML roundtrip preserves PMSeed when seed is None."""
        pm = PMSeed(
            product_name="Test",
            deferred_decisions=("Choice A",),
            referenced_repos=({"path": "/x", "name": "x", "desc": "x"},),
        )
        yaml_str = pm.to_initial_context()
        loaded = yaml.safe_load(yaml_str)
        restored = PMSeed.from_dict(loaded)
        assert restored.seed is None
        assert restored.deferred_decisions == ("Choice A",)
        assert restored.referenced_repos == ({"path": "/x", "name": "x", "desc": "x"},)

    def test_roundtrip_all_fields(self):
        """YAML roundtrip preserves all three new fields."""
        seed = _make_seed()
        pm = PMSeed(
            product_name="Full Test",
            goal="Test everything",
            seed=seed,
            deferred_decisions=("DB choice", "Hosting provider"),
            referenced_repos=(
                {"path": "/api", "name": "api", "desc": "API"},
                {"path": "/web", "name": "web", "desc": "Web"},
            ),
        )
        yaml_str = pm.to_initial_context()
        loaded = yaml.safe_load(yaml_str)
        restored = PMSeed.from_dict(loaded)
        assert restored.seed is not None
        assert restored.seed.goal == "Build a test widget"
        assert restored.deferred_decisions == ("DB choice", "Hosting provider")
        assert len(restored.referenced_repos) == 2
        assert restored.referenced_repos[1]["name"] == "web"


class TestPMSeedWithAllFields:
    """Tests that new fields coexist with existing fields."""

    def test_full_pm_seed_construction(self):
        """PMSeed can be constructed with all fields including new ones."""
        seed = _make_seed()
        pm = PMSeed(
            pm_id="pm_seed_test123",
            product_name="My Product",
            goal="Deliver value",
            user_stories=(UserStory(persona="PM", action="create PMs", benefit="ship faster"),),
            constraints=("Budget < $10k",),
            success_criteria=("Users adopt",),
            deferred_items=("Phase 2 feature",),
            decide_later_items=("What DB?",),
            assumptions=("Users have internet",),
            interview_id="int_abc",
            codebase_context="existing monolith",
            brownfield_repos=({"path": "/mono", "name": "mono", "desc": "monolith"},),
            seed=seed,
            deferred_decisions=("Cloud provider",),
            referenced_repos=({"path": "/mono", "name": "mono", "desc": "monolith"},),
        )
        assert pm.pm_id == "pm_seed_test123"
        assert pm.seed is seed
        assert pm.deferred_decisions == ("Cloud provider",)
        assert pm.referenced_repos[0]["name"] == "mono"
        assert len(pm.user_stories) == 1
