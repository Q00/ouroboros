"""Tests for ouroboros.orchestrator.profile_loader (RFC v2 #830, PR 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.orchestrator.profile_loader import (
    ExecutionProfile,
    ProfileError,
    available_profiles,
    load_profile,
)

BUILTIN_PROFILES = ("analysis", "code", "research")


class TestBuiltinProfiles:
    """Bundled profiles must load and expose the H4 surface."""

    @pytest.mark.parametrize("name", BUILTIN_PROFILES)
    def test_loads(self, name: str) -> None:
        profile = load_profile(name)
        assert isinstance(profile, ExecutionProfile)
        assert profile.profile == name
        assert profile.axis
        assert profile.min_unit
        assert profile.verifier_focus

    def test_available_lists_all_builtins(self) -> None:
        discovered = available_profiles()
        for name in BUILTIN_PROFILES:
            assert name in discovered

    def test_code_profile_has_test_evidence(self) -> None:
        profile = load_profile("code")
        assert "tests_passed" in profile.evidence_schema.required
        assert "Read" in profile.suggested_tools

    def test_research_profile_requires_triangulation(self) -> None:
        profile = load_profile("research")
        assert "triangulated_sources" in profile.evidence_schema.required

    def test_analysis_profile_requires_perspectives(self) -> None:
        profile = load_profile("analysis")
        assert "perspectives_compared" in profile.evidence_schema.required


class TestSchemaValidation:
    """Loader rejects ill-formed profile files."""

    def _write(self, dir_: Path, name: str, body: str) -> Path:
        path = dir_ / f"{name}.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_missing_required_field(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "broken",
            "profile: broken\naxis: x\nmin_unit: y\n",  # no verifier_focus
        )
        with pytest.raises(ProfileError, match="schema validation"):
            load_profile("broken", profiles_dir=tmp_path)

    def test_extra_field_rejected(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "extra",
            ("profile: extra\naxis: x\nmin_unit: y\nverifier_focus: z\nunknown_field: oops\n"),
        )
        with pytest.raises(ProfileError, match="schema validation"):
            load_profile("extra", profiles_dir=tmp_path)

    def test_filename_must_match_profile_field(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "alpha",
            "profile: beta\naxis: x\nmin_unit: y\nverifier_focus: z\n",
        )
        with pytest.raises(ProfileError, match="name mismatch"):
            load_profile("alpha", profiles_dir=tmp_path)

    def test_non_mapping_top_level(self, tmp_path: Path) -> None:
        self._write(tmp_path, "list", "- a\n- b\n")
        with pytest.raises(ProfileError, match="mapping"):
            load_profile("list", profiles_dir=tmp_path)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        self._write(tmp_path, "bad", "profile: [unterminated\n")
        with pytest.raises(ProfileError, match="not valid YAML"):
            load_profile("bad", profiles_dir=tmp_path)

    def test_unknown_profile(self, tmp_path: Path) -> None:
        with pytest.raises(ProfileError, match="not found"):
            load_profile("ghost", profiles_dir=tmp_path)

    def test_invalid_name_rejected(self, tmp_path: Path) -> None:
        for bad in ("../etc/passwd", "a/b", ".hidden", ""):
            with pytest.raises(ProfileError, match="Invalid profile name"):
                load_profile(bad, profiles_dir=tmp_path)

    def test_profile_is_frozen(self) -> None:
        profile = load_profile("code")
        with pytest.raises(ValueError, match="frozen"):
            profile.axis = "mutated"  # type: ignore[misc]
