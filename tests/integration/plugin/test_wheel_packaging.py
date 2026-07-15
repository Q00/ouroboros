"""Integration test: built wheel preserves packaged assets and metadata.

This guards the failure mode the round-2 bot review flagged and
round-4 sharpened: the unit-level `resources.files()` check passes
against the editable source tree even when `pyproject.toml` is
mis-configured, so it cannot catch a `force-include` regression on
its own. This test rebuilds the wheel for real and inspects the
shipped archive.
"""

from __future__ import annotations

from email.parser import Parser
from pathlib import Path
import shutil
import subprocess
import tomllib
import zipfile

import pytest

from ouroboros.plugin.manifest import SUPPORTED_SCHEMA_VERSIONS

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILTIN_GLOSSARY_PACKS = ("ui_ux_basics.yaml",)


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not on PATH — wheel build cannot run in this environment.",
)
def test_built_wheel_preserves_packaging_contracts(tmp_path: Path) -> None:
    """Build the wheel and verify schema assets plus dependency markers.

    A future change that drops the `force-include` for
    `src/ouroboros/plugin/schemas` from `pyproject.toml` will fail this
    test before it can ship a broken wheel that raises
    `vendored schema directory missing from installed package` for every
    `load_manifest()` call in production.
    """
    out_dir = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"`uv build --wheel` failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    expected = {
        f"ouroboros/plugin/schemas/{version}/{asset}"
        for version in SUPPORTED_SCHEMA_VERSIONS
        for asset in ("plugin.schema.json", "audit-event.schema.json")
    }
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        present = [n for n in names if n.startswith("ouroboros/plugin/schemas/")]
        # Each schema asset must appear exactly once. Hatchling's
        # `force-include` plus the matching `exclude` in the wheel target
        # is the existing pattern that prevents duplicate ZIP local
        # headers (which PyPI rejects); regressing into a duplicate would
        # also fail this assertion.
        for path in present:
            assert names.count(path) == 1, (
                f"{path} appears {names.count(path)} times — duplicate ZIP entries "
                "indicate the wheel `exclude`/`force-include` pair is misaligned."
            )

        present_set = set(present)
        missing = expected - present_set
        assert not missing, (
            "wheel is missing required schema assets — likely a "
            "`pyproject.toml` `force-include` regression. "
            f"Missing: {sorted(missing)}. Wheel ships: {sorted(present_set)}"
        )

        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        assert len(metadata_paths) == 1, f"expected one METADATA file, got {metadata_paths}"
        metadata = Parser().parsestr(archive.read(metadata_paths[0]).decode("utf-8"))
        requires_dist = metadata.get_all("Requires-Dist") or []

    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    litellm_requirement = pyproject["project"]["optional-dependencies"]["litellm"][0]
    litellm_pin = litellm_requirement.partition(";")[0].strip()
    wheel_litellm_requirements = [
        requirement for requirement in requires_dist if requirement.startswith(litellm_pin)
    ]

    assert len(wheel_litellm_requirements) == 2
    for extra in ("all", "litellm"):
        assert any(
            "python_version < '3.14'" in requirement and f"extra == '{extra}'" in requirement
            for requirement in wheel_litellm_requirements
        ), f"wheel metadata lost the LiteLLM Python marker for [{extra}]"


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv not on PATH — wheel build cannot run in this environment.",
)
def test_built_wheel_ships_builtin_interview_adapter_packs_once_and_loadable(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"`uv build --wheel` failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"

    expected = {
        f"ouroboros/interview_adapters/packs/{pack_name}" for pack_name in BUILTIN_GLOSSARY_PACKS
    }
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        present = [n for n in names if n.startswith("ouroboros/interview_adapters/packs/")]
        for path in present:
            assert names.count(path) == 1, (
                f"{path} appears {names.count(path)} times — duplicate ZIP entries "
                "indicate the wheel `exclude`/`force-include` pair is misaligned."
            )
        assert expected <= set(present)

        import yaml

        for path in expected:
            loaded = yaml.safe_load(archive.read(path).decode("utf-8"))
            assert loaded["name"] == Path(path).stem
            assert loaded["schema_version"] == 1
            assert loaded["glossary_terms"]
