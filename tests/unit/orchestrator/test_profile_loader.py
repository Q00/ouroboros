"""Tests for ouroboros.orchestrator.profile_loader (RFC v2 #830, PR 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.orchestrator.profile_loader import (
    ExecutionProfile,
    ProfileError,
    VerifierCapability,
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
        assert isinstance(profile.verifier_capability, VerifierCapability)

    def test_available_lists_all_builtins(self) -> None:
        discovered = available_profiles()
        for name in BUILTIN_PROFILES:
            assert name in discovered

    def test_code_profile_has_test_evidence(self) -> None:
        profile = load_profile("code")
        assert "tests_passed" in profile.evidence_schema.required
        assert "Read" in profile.suggested_tools
        assert profile.verifier_capability is VerifierCapability.SUBPROCESS_TEST_RUNNER

    @pytest.mark.parametrize("name", BUILTIN_PROFILES)
    def test_builtin_profiles_declare_rfc_v2_structured_knobs(self, name: str) -> None:
        """The thin YAML card must carry every knob the harness consumes.

        Issue #830's accepted v2 contract names these as structured profile
        fields, not prose. Keeping them on the schema makes the RFC example
        representable even while downstream PRs wire individual consumers.
        """
        profile = load_profile(name)

        assert profile.schema_version == 1
        assert profile.max_branching >= 2
        assert profile.must_produce
        assert set(profile.must_produce).issubset(profile.evidence_schema.required)
        assert profile.suggested_model_tier == "medium"

    def test_research_profile_requires_triangulation(self) -> None:
        profile = load_profile("research")
        assert "triangulated_sources" in profile.evidence_schema.required
        assert profile.verifier_capability is VerifierCapability.READ_ONLY_DISCOVERY

    def test_analysis_profile_requires_perspectives(self) -> None:
        profile = load_profile("analysis")
        assert "perspectives_compared" in profile.evidence_schema.required
        assert profile.verifier_capability is VerifierCapability.READ_ONLY_DISCOVERY


class TestSchemaValidation:
    """Loader rejects ill-formed profile files."""

    def _write(self, dir_: Path, name: str, body: str) -> Path:
        path = dir_ / f"{name}.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def _valid_body(self, name: str, *, extra: str = "") -> str:
        return f"""
profile: {name}
schema_version: 1
axis: source
min_unit: claim
max_branching: 3
must_produce: [claims]
evidence_schema:
  required: [claims]
verifier_capability: read_only_discovery
verifier_focus: "Check claims."
suggested_model_tier: medium
{extra}
"""

    def test_missing_required_field(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "broken",
            self._valid_body("broken").replace('verifier_focus: "Check claims."\n', ""),
        )
        with pytest.raises(ProfileError, match="schema validation"):
            load_profile("broken", profiles_dir=tmp_path)

    @pytest.mark.parametrize(
        "knob",
        ("schema_version", "max_branching", "must_produce", "suggested_model_tier"),
    )
    def test_structured_profile_knobs_are_required_for_yaml_loading(
        self, tmp_path: Path, knob: str
    ) -> None:
        lines = [
            line
            for line in self._valid_body("missing_knob").splitlines()
            if not line.startswith(f"{knob}:")
        ]
        self._write(tmp_path, "missing_knob", "\n".join(lines))

        with pytest.raises(ProfileError, match=rf"required structured profile knob.*{knob}"):
            load_profile("missing_knob", profiles_dir=tmp_path)

    def test_verifier_capability_is_required(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "missing_capability",
            self._valid_body("missing_capability").replace(
                "verifier_capability: read_only_discovery\n", ""
            ),
        )
        with pytest.raises(ProfileError, match="verifier_capability"):
            load_profile("missing_capability", profiles_dir=tmp_path)

    def test_extra_field_rejected(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "extra",
            self._valid_body("extra", extra="unknown_field: oops\n"),
        )
        with pytest.raises(ProfileError, match="schema validation"):
            load_profile("extra", profiles_dir=tmp_path)

    def test_rfc_v2_example_profile_shape_loads(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "code",
            """
profile: code
schema_version: 1
axis: testable_unit
min_unit: "single function with at least one test"
cut_signal: "sub-AC produces an independently runnable test"
max_branching: 4
must_produce:
  - tests_passed
  - files_touched
evidence_schema:
  required: [files_touched, commands_run, tests_passed]
  rejected_if:
    - "tests_passed == []"
verifier_capability: subprocess_test_runner
verifier_focus: "Run the project's test command."
suggested_tools: [Read, Edit, Write, Bash, Glob, Grep]
suggested_model_tier: medium
""",
        )

        profile = load_profile("code", profiles_dir=tmp_path)

        assert profile.schema_version == 1
        assert profile.max_branching == 4
        assert profile.must_produce == ("tests_passed", "files_touched")
        assert profile.suggested_model_tier == "medium"

    def test_unsupported_schema_version_rejected(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "future",
            """
profile: future
schema_version: 2
axis: source
min_unit: claim
max_branching: 3
must_produce: [claims]
evidence_schema:
  required: [claims]
verifier_capability: read_only_discovery
verifier_focus: "Check claims."
suggested_model_tier: medium
""",
        )
        with pytest.raises(ProfileError, match="schema_version"):
            load_profile("future", profiles_dir=tmp_path)

    def test_max_branching_must_leave_room_to_split(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "too_narrow",
            """
profile: too_narrow
schema_version: 1
axis: source
min_unit: claim
max_branching: 1
must_produce: [claims]
evidence_schema:
  required: [claims]
verifier_capability: read_only_discovery
verifier_focus: "Check claims."
suggested_model_tier: medium
""",
        )
        with pytest.raises(ProfileError, match="max_branching"):
            load_profile("too_narrow", profiles_dir=tmp_path)

    def test_must_produce_must_be_required_evidence(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "mismatch",
            """
profile: mismatch
schema_version: 1
axis: source
min_unit: claim
max_branching: 3
must_produce: [citations]
evidence_schema:
  required: [claims]
verifier_capability: read_only_discovery
verifier_focus: "Check claims."
suggested_model_tier: medium
""",
        )
        with pytest.raises(ProfileError, match="must_produce"):
            load_profile("mismatch", profiles_dir=tmp_path)

    def test_filename_must_match_profile_field(self, tmp_path: Path) -> None:
        self._write(
            tmp_path,
            "alpha",
            self._valid_body("beta"),
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


class TestIoErrorNormalization:
    """Filesystem + decoding errors must surface as ProfileError.

    The loader documents that callers see ProfileError on every failure
    mode, but the read-text call could leak OSError / UnicodeDecodeError
    (bot finding on PR #881). Normalize both to ProfileError so the
    contract holds in production.
    """

    def test_invalid_utf8_raises_profile_error(self, tmp_path: Path) -> None:
        path = tmp_path / "garbled.yaml"
        # 0xff is not a valid first byte in any UTF-8 sequence.
        path.write_bytes(b"\xff\xfe garbage\n")
        with pytest.raises(ProfileError, match="not valid UTF-8"):
            load_profile("garbled", profiles_dir=tmp_path)

    def test_unreadable_file_raises_profile_error(self, tmp_path: Path) -> None:
        import os
        import stat

        path = tmp_path / "locked.yaml"
        path.write_text(
            "profile: locked\naxis: x\nmin_unit: y\nverifier_capability: read_only_discovery\nverifier_focus: z\n",
            encoding="utf-8",
        )
        path.chmod(0)
        try:
            # Root can bypass the permission denial — skip if so.
            if os.geteuid() == 0:
                pytest.skip("root bypasses permission denial")
            with pytest.raises(ProfileError, match="could not be read"):
                load_profile("locked", profiles_dir=tmp_path)
        finally:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)


class TestWheelPackaging:
    """The bundled profile YAMLs must reach the installed wheel.

    The loader resolves YAMLs via ``Path(__file__).parent.parent /
    "profiles"``. If the ``pyproject.toml`` ``force-include`` entry for
    ``src/ouroboros/profiles`` is dropped, the source tree still works
    but ``pip install`` of the wheel ships a loader that cannot find
    any profile.

    This regression test builds the wheel in a tmpdir and asserts every
    bundled profile is present at the expected path inside the .whl.
    Skipped if ``uv`` is not available on PATH (CI sandboxes that
    cannot spawn external builds).
    """

    @pytest.mark.slow
    def test_wheel_contains_profile_yamls(self, tmp_path: Path) -> None:
        import shutil
        import subprocess
        import zipfile

        if shutil.which("uv") is None:
            pytest.skip("uv is not on PATH; cannot build the wheel here")

        repo_root = Path(__file__).resolve().parents[3]
        out = tmp_path / "dist"
        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        # The whole point of this test is to catch packaging regressions —
        # a non-zero build status is the regression signal, not a skip.
        assert result.returncode == 0, (
            f"uv build failed (rc={result.returncode}):\n"
            f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}"
        )

        wheels = list(out.glob("*.whl"))
        assert wheels, f"no wheel produced in {out}"
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as zf:
            # Keep the list — not a set — so duplicate ZIP entries (which
            # PyPI rejects) are caught here. The exclude/force-include
            # pairing in pyproject.toml is fragile; this is the guard.
            names = zf.namelist()

        for stem in ("code", "research", "analysis"):
            expected = f"ouroboros/profiles/{stem}.yaml"
            occurrences = sum(1 for n in names if n == expected)
            assert occurrences == 1, (
                f"{expected!r} appears {occurrences} times in wheel "
                f"(should be exactly 1). Duplicate entries usually mean "
                f"the wheel `exclude` for src/ouroboros/profiles is "
                f"missing alongside the `force-include`. Wheel contents "
                f"(profiles sample): "
                f"{sorted(n for n in names if 'profile' in n)}"
            )
