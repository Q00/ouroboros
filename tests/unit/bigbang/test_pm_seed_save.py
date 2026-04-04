"""Tests for PM Seed JSON persistence.

Verifies that PMSeed is saved as JSON at ~/.ouroboros/seeds/pm_seed_{id}.json
with correct naming, content roundtrip, and directory creation.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.pm_seed import PMSeed, UserStory
from ouroboros.bigbang.question_classifier import QuestionClassifier


def _make_engine(tmp_path: Path) -> PMInterviewEngine:
    """Create a minimal PMInterviewEngine for testing."""
    mock_adapter = MagicMock()
    mock_adapter.complete = AsyncMock()

    inner = MagicMock()
    inner.llm_adapter = mock_adapter
    inner.state_dir = tmp_path / "data"

    classifier = QuestionClassifier(llm_adapter=mock_adapter)

    return PMInterviewEngine(
        inner=inner,
        classifier=classifier,
        llm_adapter=mock_adapter,
    )


def _make_seed(pm_id: str = "pm_seed_abc123def456") -> PMSeed:
    """Create a sample PMSeed for testing."""
    return PMSeed(
        pm_id=pm_id,
        product_name="TaskFlow",
        goal="Task management for distributed teams",
        user_stories=(
            UserStory(persona="PM", action="create tasks", benefit="track progress"),
            UserStory(persona="Developer", action="update status", benefit="visibility"),
        ),
        constraints=("Must work offline", "Under 100ms latency"),
        success_criteria=("Create task in 10s", "99.9% uptime"),
        decide_later_items=(
            "What caching strategy?",
            "Which cloud provider?",
            "Database selection",
            "CI/CD pipeline",
        ),
        assumptions=("Users have internet for initial sync",),
        interview_id="interview_xyz",
        codebase_context="existing Flask app",
        brownfield_repos=({"path": "/code/app", "name": "app", "desc": "main"},),
    )


class TestPMSeedSaveJSON:
    """PM Seed saved as JSON at ~/.ouroboros/seeds/pm_seed_{id}.json."""

    def test_filename_matches_pm_id(self, tmp_path: Path) -> None:
        """Saved file is named {pm_id}.json."""
        engine = _make_engine(tmp_path)
        seed = _make_seed(pm_id="pm_seed_abc123def456")

        filepath = engine.save_pm_seed(seed, output_dir=tmp_path / "seeds")

        assert filepath.name == "pm_seed_abc123def456.json"

    def test_file_saved_in_seeds_directory(self, tmp_path: Path) -> None:
        """File is saved inside the specified seeds directory."""
        engine = _make_engine(tmp_path)
        seed = _make_seed()
        seeds_dir = tmp_path / "seeds"

        filepath = engine.save_pm_seed(seed, output_dir=seeds_dir)

        assert filepath.parent == seeds_dir
        assert filepath.exists()

    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        """Seeds directory is created automatically if it doesn't exist."""
        engine = _make_engine(tmp_path)
        seed = _make_seed()
        seeds_dir = tmp_path / "nonexistent" / "seeds"

        assert not seeds_dir.exists()

        filepath = engine.save_pm_seed(seed, output_dir=seeds_dir)

        assert seeds_dir.exists()
        assert filepath.exists()

    def test_default_output_dir_is_ouroboros_seeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default output directory is ~/.ouroboros/seeds/."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        # Re-import to pick up patched home
        import ouroboros.bigbang.pm_interview as mod

        monkeypatch.setattr(mod, "_SEED_DIR", fake_home / ".ouroboros" / "seeds")

        engine = _make_engine(tmp_path)
        seed = _make_seed()

        filepath = engine.save_pm_seed(seed)

        expected_dir = fake_home / ".ouroboros" / "seeds"
        assert filepath.parent == expected_dir
        assert filepath.exists()

    def test_json_content_is_valid(self, tmp_path: Path) -> None:
        """Saved file contains valid JSON."""
        engine = _make_engine(tmp_path)
        seed = _make_seed()

        filepath = engine.save_pm_seed(seed, output_dir=tmp_path / "seeds")

        loaded = json.loads(filepath.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict)

    def test_json_contains_all_fields(self, tmp_path: Path) -> None:
        """Saved JSON contains all PMSeed fields."""
        engine = _make_engine(tmp_path)
        seed = _make_seed()

        filepath = engine.save_pm_seed(seed, output_dir=tmp_path / "seeds")
        loaded = json.loads(filepath.read_text(encoding="utf-8"))

        assert loaded["pm_id"] == "pm_seed_abc123def456"
        assert loaded["product_name"] == "TaskFlow"
        assert loaded["goal"] == "Task management for distributed teams"
        assert len(loaded["user_stories"]) == 2
        assert loaded["user_stories"][0]["persona"] == "PM"
        assert loaded["constraints"] == ["Must work offline", "Under 100ms latency"]
        assert loaded["success_criteria"] == ["Create task in 10s", "99.9% uptime"]
        assert loaded["decide_later_items"] == [
            "What caching strategy?",
            "Which cloud provider?",
            "Database selection",
            "CI/CD pipeline",
        ]
        assert loaded["assumptions"] == ["Users have internet for initial sync"]
        assert loaded["interview_id"] == "interview_xyz"
        assert loaded["codebase_context"] == "existing Flask app"
        assert loaded["brownfield_repos"] == [{"path": "/code/app", "name": "app", "desc": "main"}]
        assert "created_at" in loaded
        # Removed fields must not appear
        assert "deferred_items" not in loaded
        assert "deferred_decisions" not in loaded
        assert "referenced_repos" not in loaded
        assert "seed" not in loaded

    def test_json_roundtrip_produces_equal_seed(self, tmp_path: Path) -> None:
        """PMSeed survives JSON save -> load roundtrip."""
        engine = _make_engine(tmp_path)
        seed = _make_seed()

        filepath = engine.save_pm_seed(seed, output_dir=tmp_path / "seeds")
        loaded_data = json.loads(filepath.read_text(encoding="utf-8"))
        restored = PMSeed.from_dict(loaded_data)

        assert restored.pm_id == seed.pm_id
        assert restored.product_name == seed.product_name
        assert restored.goal == seed.goal
        assert len(restored.user_stories) == len(seed.user_stories)
        assert restored.constraints == seed.constraints
        assert restored.success_criteria == seed.success_criteria
        assert restored.decide_later_items == seed.decide_later_items
        assert restored.assumptions == seed.assumptions
        assert restored.interview_id == seed.interview_id

    def test_pm_id_default_format(self) -> None:
        """Default pm_id starts with 'pm_seed_'."""
        seed = PMSeed()
        assert seed.pm_id.startswith("pm_seed_")
        # 12 hex chars after prefix
        suffix = seed.pm_id[len("pm_seed_") :]
        assert len(suffix) == 12
        # Verify it's valid hex
        int(suffix, 16)

    def test_pm_id_unique_across_instances(self) -> None:
        """Each PMSeed gets a unique pm_id by default."""
        seeds = [PMSeed() for _ in range(10)]
        ids = {s.pm_id for s in seeds}
        assert len(ids) == 10

    def test_saved_filename_uses_pm_id_as_stem(self, tmp_path: Path) -> None:
        """The JSON filename stem matches pm_id exactly."""
        engine = _make_engine(tmp_path)
        custom_id = "pm_seed_custom12345"
        seed = _make_seed(pm_id=custom_id)

        filepath = engine.save_pm_seed(seed, output_dir=tmp_path / "seeds")

        assert filepath.stem == custom_id
        assert filepath.suffix == ".json"

    def test_overwrite_existing_file(self, tmp_path: Path) -> None:
        """Saving with same pm_id overwrites the existing file."""
        engine = _make_engine(tmp_path)
        seeds_dir = tmp_path / "seeds"

        seed_v1 = PMSeed(pm_id="pm_seed_overwrite", product_name="V1")
        seed_v2 = PMSeed(pm_id="pm_seed_overwrite", product_name="V2")

        engine.save_pm_seed(seed_v1, output_dir=seeds_dir)
        filepath = engine.save_pm_seed(seed_v2, output_dir=seeds_dir)

        loaded = json.loads(filepath.read_text(encoding="utf-8"))
        assert loaded["product_name"] == "V2"

    def test_multiple_seeds_coexist(self, tmp_path: Path) -> None:
        """Multiple PM seeds can be saved in the same directory."""
        engine = _make_engine(tmp_path)
        seeds_dir = tmp_path / "seeds"

        seed_a = _make_seed(pm_id="pm_seed_aaa111")
        seed_b = _make_seed(pm_id="pm_seed_bbb222")

        path_a = engine.save_pm_seed(seed_a, output_dir=seeds_dir)
        path_b = engine.save_pm_seed(seed_b, output_dir=seeds_dir)

        assert path_a.exists()
        assert path_b.exists()
        assert path_a != path_b

        # Both can be loaded independently
        data_a = json.loads(path_a.read_text())
        data_b = json.loads(path_b.read_text())
        assert data_a["pm_id"] == "pm_seed_aaa111"
        assert data_b["pm_id"] == "pm_seed_bbb222"

    def test_returns_path_object(self, tmp_path: Path) -> None:
        """save_pm_seed returns a Path object."""
        engine = _make_engine(tmp_path)
        seed = _make_seed()

        result = engine.save_pm_seed(seed, output_dir=tmp_path / "seeds")

        assert isinstance(result, Path)

    def test_utf8_encoding(self, tmp_path: Path) -> None:
        """JSON file is saved with UTF-8 encoding, supporting unicode."""
        engine = _make_engine(tmp_path)
        seed = PMSeed(
            pm_id="pm_seed_unicode",
            product_name="Unicod\u00e9 Pr\u00f6d\u00fcct",
            goal="Support f\u00fcr internationale M\u00e4rkte",
        )

        filepath = engine.save_pm_seed(seed, output_dir=tmp_path / "seeds")
        content = filepath.read_text(encoding="utf-8")
        loaded = json.loads(content)

        assert loaded["product_name"] == "Unicod\u00e9 Pr\u00f6d\u00fcct"
        assert loaded["goal"] == "Support f\u00fcr internationale M\u00e4rkte"
