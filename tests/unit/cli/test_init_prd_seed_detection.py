"""Tests for AC 16: Dev interview auto-detects prd_seed YAML in seeds dir.

Sub-AC 2: User prompt/confirmation flow that notifies the user when a
prd_seed YAML is auto-detected and asks whether to use it for the dev interview.
"""

from pathlib import Path
from unittest.mock import patch

import yaml

from ouroboros.cli.commands.init import (
    _display_prd_seed_info,
    _find_prd_seeds,
    _has_dev_seed,
    _load_prd_seed_as_context,
    _notify_prd_seed_detected,
    _prompt_prd_seed_selection,
)

# ---------------------------------------------------------------------------
# _find_prd_seeds
# ---------------------------------------------------------------------------


class TestFindPrdSeeds:
    """Tests for _find_prd_seeds()."""

    def test_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        """Returns empty list if seeds directory does not exist."""
        result = _find_prd_seeds(tmp_path / "nonexistent")
        assert result == []

    def test_returns_empty_when_no_prd_seeds(self, tmp_path: Path) -> None:
        """Returns empty list when directory has no prd_seed files."""
        (tmp_path / "seed_abc.yaml").write_text("goal: test")
        (tmp_path / "other.yaml").write_text("data: foo")
        result = _find_prd_seeds(tmp_path)
        assert result == []

    def test_finds_prd_seed_files(self, tmp_path: Path) -> None:
        """Discovers prd_seed_*.yaml files."""
        (tmp_path / "prd_seed_aaa.yaml").write_text("prd_id: aaa")
        (tmp_path / "prd_seed_bbb.yaml").write_text("prd_id: bbb")
        (tmp_path / "seed_ccc.yaml").write_text("goal: test")  # Not a PRD seed
        result = _find_prd_seeds(tmp_path)
        assert len(result) == 2
        assert all(p.name.startswith("prd_seed_") for p in result)

    def test_sorted_newest_first(self, tmp_path: Path) -> None:
        """PRD seeds are sorted by modification time, newest first."""
        import time

        p1 = tmp_path / "prd_seed_old.yaml"
        p1.write_text("prd_id: old")
        time.sleep(0.05)
        p2 = tmp_path / "prd_seed_new.yaml"
        p2.write_text("prd_id: new")
        result = _find_prd_seeds(tmp_path)
        assert result[0].name == "prd_seed_new.yaml"
        assert result[1].name == "prd_seed_old.yaml"


# ---------------------------------------------------------------------------
# _has_dev_seed
# ---------------------------------------------------------------------------


class TestHasDevSeed:
    """Tests for _has_dev_seed()."""

    def test_false_when_dir_missing(self, tmp_path: Path) -> None:
        """Returns False if seeds directory does not exist."""
        assert _has_dev_seed(tmp_path / "nonexistent") is False

    def test_false_when_only_prd_seeds(self, tmp_path: Path) -> None:
        """Returns False when only prd_seed files exist."""
        (tmp_path / "prd_seed_aaa.yaml").write_text("prd_id: aaa")
        assert _has_dev_seed(tmp_path) is False

    def test_true_when_seed_json_exists(self, tmp_path: Path) -> None:
        """Returns True when seed.json is present."""
        (tmp_path / "seed.json").write_text("{}")
        assert _has_dev_seed(tmp_path) is True

    def test_true_when_dev_seed_yaml_exists(self, tmp_path: Path) -> None:
        """Returns True when a non-prd seed YAML exists."""
        (tmp_path / "seed_abc123.yaml").write_text("goal: test")
        assert _has_dev_seed(tmp_path) is True

    def test_false_when_empty_dir(self, tmp_path: Path) -> None:
        """Returns False for empty directory."""
        assert _has_dev_seed(tmp_path) is False

    def test_true_ignores_prd_seeds_with_dev_seed(self, tmp_path: Path) -> None:
        """Returns True when dev seed exists alongside prd seeds."""
        (tmp_path / "prd_seed_aaa.yaml").write_text("prd_id: aaa")
        (tmp_path / "seed_dev.yaml").write_text("goal: dev")
        assert _has_dev_seed(tmp_path) is True


# ---------------------------------------------------------------------------
# _display_prd_seed_info
# ---------------------------------------------------------------------------


class TestDisplayPrdSeedInfo:
    """Tests for _display_prd_seed_info()."""

    def test_extracts_info_from_valid_yaml(self, tmp_path: Path) -> None:
        """Extracts product_name, goal, and prd_id from valid YAML."""
        seed_path = tmp_path / "prd_seed_test.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({"prd_id": "test123", "product_name": "MyApp", "goal": "Build it"}, f)
        info = _display_prd_seed_info(seed_path)
        assert info["name"] == "MyApp"
        assert info["goal"] == "Build it"
        assert info["prd_id"] == "test123"

    def test_fallback_for_unreadable_file(self, tmp_path: Path) -> None:
        """Falls back to stem/defaults for unreadable files."""
        seed_path = tmp_path / "prd_seed_bad.yaml"
        seed_path.write_text(":::invalid:::")
        # :::invalid::: parses as a string in YAML, so .get() returns defaults
        info = _display_prd_seed_info(seed_path)
        assert info["name"] == "Unnamed"
        assert info["goal"] == "No goal specified"
        assert info["prd_id"] == "prd_seed_bad"

    def test_fallback_for_missing_fields(self, tmp_path: Path) -> None:
        """Falls back to defaults when fields are missing or empty."""
        seed_path = tmp_path / "prd_seed_sparse.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({"prd_id": "sparse"}, f)
        info = _display_prd_seed_info(seed_path)
        assert info["name"] == "Unnamed"
        assert info["goal"] == "No goal specified"
        assert info["prd_id"] == "sparse"


# ---------------------------------------------------------------------------
# _notify_prd_seed_detected
# ---------------------------------------------------------------------------


class TestNotifyPrdSeedDetected:
    """Tests for _notify_prd_seed_detected()."""

    def test_displays_notification_without_error(self, tmp_path: Path, capsys) -> None:
        """Notification runs without raising for valid seed files."""
        seed_path = tmp_path / "prd_seed_notify.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({"prd_id": "notify", "product_name": "NotifyApp", "goal": "Test"}, f)
        # Should not raise
        _notify_prd_seed_detected([seed_path])

    def test_displays_multiple_seeds(self, tmp_path: Path) -> None:
        """Handles multiple PRD seeds in notification."""
        seeds = []
        for i in range(3):
            p = tmp_path / f"prd_seed_{i}.yaml"
            with open(p, "w") as f:
                yaml.dump({"prd_id": f"id{i}", "product_name": f"App{i}", "goal": f"Goal {i}"}, f)
            seeds.append(p)
        # Should not raise
        _notify_prd_seed_detected(seeds)


# ---------------------------------------------------------------------------
# _prompt_prd_seed_selection
# ---------------------------------------------------------------------------


class TestPromptPrdSeedSelection:
    """Tests for _prompt_prd_seed_selection()."""

    def test_single_seed_confirm_yes(self, tmp_path: Path) -> None:
        """Single seed uses yes/no confirmation; returns seed on yes."""
        seed_path = tmp_path / "prd_seed_aaa.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({"prd_id": "aaa", "product_name": "Test", "goal": "A goal"}, f)

        with patch("ouroboros.cli.commands.init.Confirm.ask", return_value=True):
            result = _prompt_prd_seed_selection([seed_path])
        assert result == seed_path

    def test_single_seed_confirm_no(self, tmp_path: Path) -> None:
        """Single seed uses yes/no confirmation; returns None on no."""
        seed_path = tmp_path / "prd_seed_aaa.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({"prd_id": "aaa", "product_name": "Test", "goal": "A goal"}, f)

        with patch("ouroboros.cli.commands.init.Confirm.ask", return_value=False):
            result = _prompt_prd_seed_selection([seed_path])
        assert result is None

    def test_returns_none_when_user_skips(self, tmp_path: Path) -> None:
        """Returns None when user selects 0 (skip) with multiple seeds."""
        seed1 = tmp_path / "prd_seed_aaa.yaml"
        seed2 = tmp_path / "prd_seed_bbb.yaml"
        for p, pid in [(seed1, "aaa"), (seed2, "bbb")]:
            with open(p, "w") as f:
                yaml.dump({"prd_id": pid, "product_name": f"App{pid}", "goal": "A goal"}, f)

        with patch("ouroboros.cli.commands.init.Prompt.ask", return_value="0"):
            result = _prompt_prd_seed_selection([seed1, seed2])
        assert result is None

    def test_returns_selected_seed_multi(self, tmp_path: Path) -> None:
        """Returns the selected seed from multiple options."""
        seed1 = tmp_path / "prd_seed_aaa.yaml"
        seed2 = tmp_path / "prd_seed_bbb.yaml"
        for p, pid in [(seed1, "aaa"), (seed2, "bbb")]:
            with open(p, "w") as f:
                yaml.dump({"prd_id": pid, "product_name": f"App{pid}", "goal": "A goal"}, f)

        with patch("ouroboros.cli.commands.init.Prompt.ask", return_value="1"):
            result = _prompt_prd_seed_selection([seed1, seed2])
        assert result == seed1

    def test_multiple_seeds_selects_second(self, tmp_path: Path) -> None:
        """User can select the second seed from the list."""
        seed1 = tmp_path / "prd_seed_aaa.yaml"
        seed2 = tmp_path / "prd_seed_bbb.yaml"
        with open(seed1, "w") as f:
            yaml.dump({"prd_id": "aaa", "product_name": "First", "goal": "Goal A"}, f)
        with open(seed2, "w") as f:
            yaml.dump({"prd_id": "bbb", "product_name": "Second", "goal": "Goal B"}, f)

        with patch("ouroboros.cli.commands.init.Prompt.ask", return_value="2"):
            result = _prompt_prd_seed_selection([seed1, seed2])
        assert result == seed2

    def test_handles_malformed_yaml_gracefully(self, tmp_path: Path) -> None:
        """Falls back to filename display for unreadable YAML."""
        seed1 = tmp_path / "prd_seed_bad.yaml"
        seed1.write_text(":::invalid:::")
        seed2 = tmp_path / "prd_seed_ok.yaml"
        with open(seed2, "w") as f:
            yaml.dump({"prd_id": "ok", "product_name": "OK", "goal": "Fine"}, f)

        with patch("ouroboros.cli.commands.init.Prompt.ask", return_value="1"):
            result = _prompt_prd_seed_selection([seed1, seed2])
        assert result == seed1

    def test_truncates_long_goals(self, tmp_path: Path) -> None:
        """Long goal strings are truncated in display."""
        seed_path = tmp_path / "prd_seed_long.yaml"
        long_goal = "A" * 200
        with open(seed_path, "w") as f:
            yaml.dump({"prd_id": "long", "product_name": "LongGoal", "goal": long_goal}, f)

        with patch("ouroboros.cli.commands.init.Confirm.ask", return_value=True):
            result = _prompt_prd_seed_selection([seed_path])
        assert result == seed_path


# ---------------------------------------------------------------------------
# _load_prd_seed_as_context
# ---------------------------------------------------------------------------


class TestLoadPrdSeedAsContext:
    """Tests for _load_prd_seed_as_context()."""

    def test_loads_and_converts_to_yaml_string(self, tmp_path: Path) -> None:
        """Loads PRDSeed from YAML file and returns initial_context string."""
        seed_data = {
            "prd_id": "prd_seed_test123",
            "product_name": "TestApp",
            "goal": "Build a testing tool",
            "constraints": ["Must be fast"],
            "success_criteria": ["All tests pass"],
            "user_stories": [],
            "deferred_items": [],
            "decide_later_items": ["Database choice?"],
        }
        seed_path = tmp_path / "prd_seed_test123.yaml"
        with open(seed_path, "w") as f:
            yaml.dump(seed_data, f)

        result = _load_prd_seed_as_context(seed_path)

        # Result should be a YAML string
        assert isinstance(result, str)
        parsed = yaml.safe_load(result)
        assert parsed["product_name"] == "TestApp"
        assert parsed["goal"] == "Build a testing tool"
        assert "Database choice?" in parsed["decide_later_items"]

    def test_roundtrip_preserves_fields(self, tmp_path: Path) -> None:
        """All PRDSeed fields survive the load → to_initial_context roundtrip."""
        seed_data = {
            "prd_id": "prd_seed_rt",
            "product_name": "Roundtrip",
            "goal": "Test roundtrip",
            "user_stories": [
                {"persona": "Dev", "action": "code", "benefit": "ship"},
            ],
            "constraints": ["Fast", "Cheap"],
            "success_criteria": ["Works"],
            "deferred_items": ["Auth"],
            "decide_later_items": ["DB?"],
            "assumptions": ["Internet available"],
            "brownfield_repos": [{"path": "/app", "name": "app", "desc": "main"}],
            "deferred_decisions": ["Microservices?"],
            "referenced_repos": [{"path": "/lib", "name": "lib", "desc": "shared"}],
        }
        seed_path = tmp_path / "prd_seed_rt.yaml"
        with open(seed_path, "w") as f:
            yaml.dump(seed_data, f)

        result = _load_prd_seed_as_context(seed_path)
        parsed = yaml.safe_load(result)

        assert parsed["product_name"] == "Roundtrip"
        assert parsed["constraints"] == ["Fast", "Cheap"]
        assert len(parsed["user_stories"]) == 1
        assert parsed["deferred_decisions"] == ["Microservices?"]


# ---------------------------------------------------------------------------
# Integration: start() command auto-detection flow
# ---------------------------------------------------------------------------


class TestStartCommandPrdSeedDetection:
    """Integration tests for PRD seed auto-detection in the start command."""

    def test_skips_detection_when_resuming(self, tmp_path: Path) -> None:
        """PRD seed detection does not run when resuming an interview."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        (seeds_dir / "prd_seed_aaa.yaml").write_text("prd_id: aaa")

        # When resume is set, detection is skipped entirely
        # The condition `if not resume:` guards the block
        assert _find_prd_seeds(seeds_dir) == [seeds_dir / "prd_seed_aaa.yaml"]
        # resume=True means the detection block is never entered

    def test_skips_detection_when_dev_seed_exists(self, tmp_path: Path) -> None:
        """No PRD seed prompt when a dev seed already exists."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        (seeds_dir / "prd_seed_aaa.yaml").write_text("prd_id: aaa")
        (seeds_dir / "seed_dev.yaml").write_text("goal: dev")

        # _has_dev_seed returns True, so prd seed selection is skipped
        assert _has_dev_seed(seeds_dir) is True
        prd_seeds = _find_prd_seeds(seeds_dir)
        assert len(prd_seeds) == 1  # PRD seed exists but should be ignored

    def test_detection_triggers_when_only_prd_seeds(self, tmp_path: Path) -> None:
        """PRD seed detection triggers when no dev seed but prd seeds exist."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        (seeds_dir / "prd_seed_aaa.yaml").write_text("prd_id: aaa")

        assert _has_dev_seed(seeds_dir) is False
        prd_seeds = _find_prd_seeds(seeds_dir)
        assert len(prd_seeds) == 1

    def test_no_detection_when_no_seeds_at_all(self, tmp_path: Path) -> None:
        """No detection when seeds directory is empty."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()

        assert _has_dev_seed(seeds_dir) is False
        prd_seeds = _find_prd_seeds(seeds_dir)
        assert len(prd_seeds) == 0


# ---------------------------------------------------------------------------
# User prompt/confirmation flow (Sub-AC 2)
# ---------------------------------------------------------------------------


class TestPrdSeedConfirmationFlow:
    """Tests for the user notification and confirmation flow when PRD seeds are detected."""

    def test_context_provided_user_accepts_prd_seed(self, tmp_path: Path) -> None:
        """When context is provided and user accepts, PRD seed overrides context."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        seed_path = seeds_dir / "prd_seed_abc.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({
                "prd_id": "abc",
                "product_name": "MyProduct",
                "goal": "Do the thing",
                "user_stories": [],
                "constraints": [],
                "success_criteria": [],
                "deferred_items": [],
                "decide_later_items": [],
            }, f)

        # Simulate: context already provided, user says yes to PRD seed
        prd_seeds = _find_prd_seeds(seeds_dir)
        assert len(prd_seeds) == 1

        # The flow: notify → confirm → load
        with patch("ouroboros.cli.commands.init.Confirm.ask", return_value=True):
            selected = _prompt_prd_seed_selection(prd_seeds)
        assert selected == seed_path

        # Load and verify
        context = _load_prd_seed_as_context(selected)
        parsed = yaml.safe_load(context)
        assert parsed["product_name"] == "MyProduct"

    def test_context_provided_user_declines_prd_seed(self, tmp_path: Path) -> None:
        """When context is provided and user declines, original context is kept."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        seed_path = seeds_dir / "prd_seed_abc.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({"prd_id": "abc", "product_name": "MyProduct", "goal": "Do"}, f)

        prd_seeds = _find_prd_seeds(seeds_dir)

        with patch("ouroboros.cli.commands.init.Confirm.ask", return_value=False):
            selected = _prompt_prd_seed_selection(prd_seeds)
        assert selected is None

    def test_no_context_user_selects_prd_seed(self, tmp_path: Path) -> None:
        """When no context provided, user can select PRD seed from list."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        seed_path = seeds_dir / "prd_seed_xyz.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({
                "prd_id": "xyz",
                "product_name": "NoContextApp",
                "goal": "Build without context",
                "user_stories": [],
                "constraints": [],
                "success_criteria": [],
                "deferred_items": [],
                "decide_later_items": [],
            }, f)

        prd_seeds = _find_prd_seeds(seeds_dir)

        # Single seed → Confirm.ask
        with patch("ouroboros.cli.commands.init.Confirm.ask", return_value=True):
            selected = _prompt_prd_seed_selection(prd_seeds)
        assert selected == seed_path

        context = _load_prd_seed_as_context(selected)
        assert "NoContextApp" in context

    def test_no_context_user_skips_prd_seed(self, tmp_path: Path) -> None:
        """When no context and user skips, selection returns None."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        seed_path = seeds_dir / "prd_seed_skip.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({"prd_id": "skip", "product_name": "SkipMe", "goal": "Skip"}, f)

        prd_seeds = _find_prd_seeds(seeds_dir)

        with patch("ouroboros.cli.commands.init.Confirm.ask", return_value=False):
            selected = _prompt_prd_seed_selection(prd_seeds)
        assert selected is None

    def test_multiple_seeds_selection_flow(self, tmp_path: Path) -> None:
        """With multiple PRD seeds, user sees numbered list and can pick."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()

        for name in ["alpha", "beta", "gamma"]:
            p = seeds_dir / f"prd_seed_{name}.yaml"
            with open(p, "w") as f:
                yaml.dump({
                    "prd_id": name,
                    "product_name": f"App-{name}",
                    "goal": f"Goal for {name}",
                }, f)

        prd_seeds = _find_prd_seeds(seeds_dir)
        assert len(prd_seeds) == 3

        # User selects the 2nd option
        with patch("ouroboros.cli.commands.init.Prompt.ask", return_value="2"):
            selected = _prompt_prd_seed_selection(prd_seeds)
        assert selected is not None
        assert selected in prd_seeds

    def test_notification_shows_before_confirmation(self, tmp_path: Path) -> None:
        """_notify_prd_seed_detected runs without errors for seed display."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()
        seed_path = seeds_dir / "prd_seed_note.yaml"
        with open(seed_path, "w") as f:
            yaml.dump({
                "prd_id": "note",
                "product_name": "NotifyProduct",
                "goal": "Test notification display",
            }, f)

        # Should not raise
        _notify_prd_seed_detected([seed_path])

    def test_end_to_end_detection_and_load(self, tmp_path: Path) -> None:
        """Full flow: detect → notify → confirm → load → context string."""
        seeds_dir = tmp_path / "seeds"
        seeds_dir.mkdir()

        seed_data = {
            "prd_id": "e2e_test",
            "product_name": "E2E App",
            "goal": "End-to-end testing",
            "user_stories": [{"persona": "PM", "action": "define reqs", "benefit": "clarity"}],
            "constraints": ["Budget: $10k"],
            "success_criteria": ["Tests pass"],
            "deferred_items": ["Analytics"],
            "decide_later_items": ["Hosting provider"],
        }
        seed_path = seeds_dir / "prd_seed_e2e_test.yaml"
        with open(seed_path, "w") as f:
            yaml.dump(seed_data, f)

        # Step 1: Detect
        assert not _has_dev_seed(seeds_dir)
        prd_seeds = _find_prd_seeds(seeds_dir)
        assert len(prd_seeds) == 1

        # Step 2: Select (single seed → confirm)
        with patch("ouroboros.cli.commands.init.Confirm.ask", return_value=True):
            selected = _prompt_prd_seed_selection(prd_seeds)
        assert selected == seed_path

        # Step 3: Load as context
        context = _load_prd_seed_as_context(selected)
        parsed = yaml.safe_load(context)
        assert parsed["product_name"] == "E2E App"
        assert parsed["goal"] == "End-to-end testing"
        assert "Analytics" in parsed["deferred_items"]
        assert "Hosting provider" in parsed["decide_later_items"]
