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
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

from ouroboros.package_profiles import SDK_RUNTIME_IN_MCP_SERVER_MESSAGE
from ouroboros.plugin.manifest import SUPPORTED_SCHEMA_VERSIONS

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUILTIN_GLOSSARY_PACKS = ("ui_ux_basics.yaml",)
PYTHON_RESOLVER_START = "<!-- ouroboros-python-resolver:start -->"
PYTHON_RESOLVER_END = "<!-- ouroboros-python-resolver:end -->"


def _extract_python_resolver(contents: str) -> str:
    """Extract the executable resolver contract from one skill artifact."""
    start = contents.index(PYTHON_RESOLVER_START) + len(PYTHON_RESOLVER_START)
    end = contents.index(PYTHON_RESOLVER_END, start)
    fenced = contents[start:end].strip()
    assert fenced.startswith("```bash\n") and fenced.endswith("\n```")
    return fenced.removeprefix("```bash\n").removesuffix("\n```")


@pytest.mark.slow
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
        provided_extras = set(metadata.get_all("Provides-Extra") or [])

        wheel_skill_paths = {
            skill: f"ouroboros/skills/{skill}/SKILL.md" for skill in ("welcome", "setup", "seed")
        }
        wheel_resolvers: dict[str, str] = {}
        for skill, path in wheel_skill_paths.items():
            assert names.count(path) == 1, (
                f"built wheel must ship {path} exactly once; found {names.count(path)} entries"
            )
            wheel_resolvers[skill] = _extract_python_resolver(archive.read(path).decode("utf-8"))

        source_resolver = _extract_python_resolver(
            (PROJECT_ROOT / "skills" / "welcome" / "SKILL.md").read_text(encoding="utf-8")
        )
        assert set(wheel_resolvers.values()) == {source_resolver}, wheel_resolvers

    assert {"claude", "claude-cli", "claude-sdk", "mcp", "all"} <= provided_extras
    sdk_requirements = [
        requirement
        for requirement in requires_dist
        if requirement.startswith(("claude-agent-sdk", "anthropic"))
    ]
    assert len(sdk_requirements) == 6
    assert all(
        "extra == 'all'" in requirement
        or "extra == 'claude'" in requirement
        or "extra == 'claude-sdk'" in requirement
        for requirement in sdk_requirements
    )
    assert any("extra == 'all'" in requirement for requirement in sdk_requirements)
    assert any("extra == 'claude'" in requirement for requirement in sdk_requirements)
    assert any("extra == 'claude-sdk'" in requirement for requirement in sdk_requirements)

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

    supported_profiles = (
        "claude",
        "claude-cli",
        "mcp",
        "mcp,claude-cli",
        "claude-sdk",
        "all",
    )
    for profiles in supported_profiles:
        result = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--dry-run",
                "--python",
                sys.executable,
                "--target",
                str(tmp_path / f"target-{profiles.replace(',', '-')}"),
                f"{wheels[0]}[{profiles}]",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, (
            f"wheel profile [{profiles}] must resolve: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )

    for profiles in ("mcp,claude", "mcp,claude-sdk", "all,mcp"):
        unsupported = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--dry-run",
                "--python",
                sys.executable,
                "--target",
                str(tmp_path / f"target-{profiles.replace(',', '-')}"),
                f"{wheels[0]}[{profiles}]",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert unsupported.returncode != 0
        resolver_error = unsupported.stdout + unsupported.stderr
        assert "claude-agent-sdk" in resolver_error
        assert "mcp" in resolver_error

    # A clean MCP 2 + CLI-worker install with no persisted runtime would
    # otherwise inherit the SDK default. The built console script must reject
    # that bare invocation before creating any durable state.
    probe_venv = tmp_path / "bare-mcp-cli-probe"
    create_venv = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(probe_venv)],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert create_venv.returncode == 0, create_venv.stderr
    probe_python = probe_venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    install_probe = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(probe_python),
            f"{wheels[0]}[mcp,claude-cli]",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert install_probe.returncode == 0, install_probe.stderr
    probe_cli = probe_venv / (
        "Scripts/ouroboros.exe" if sys.platform == "win32" else "bin/ouroboros"
    )
    probe_home = tmp_path / "bare-probe-home"
    probe_home.mkdir()
    probe_env = os.environ.copy()
    probe_env["HOME"] = str(probe_home)
    probe_env["USERPROFILE"] = str(probe_home)
    # The inherited SDK runtime may now fall back to an installed CLI. Keep this
    # rejection probe independent of the developer machine and login shell.
    probe_env["PATH"] = os.pathsep.join((str(probe_python.parent), os.defpath))
    probe_env["SHELL"] = str(probe_home / "missing-shell")
    for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_RUNTIME", "_OUROBOROS_NESTED"):
        probe_env.pop(key, None)
    bare_probe = subprocess.run(
        [str(probe_cli), "mcp", "serve"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=probe_env,
    )
    assert bare_probe.returncode == 1
    rendered_probe = " ".join((bare_probe.stdout + bare_probe.stderr).split())
    assert " ".join(SDK_RUNTIME_IN_MCP_SERVER_MESSAGE.split()) in rendered_probe
    assert "--runtime claude-cli" in rendered_probe
    assert "ouroboros-ai[mcp]" not in rendered_probe
    state_dir = probe_home / ".ouroboros"
    assert not (state_dir / "ouroboros.db").exists()
    assert not (state_dir / "config.yaml").exists()
    assert not (state_dir / "sessions").exists()

    # The actionable replacements named by the diagnostic must both cross the
    # real wheel/console-script/MCP-v2 boundary, not only a mocked unit dispatch.
    initialize_request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "wheel-probe", "version": "1.0"},
            },
        }
    )
    for executable_runtime in ("claude-cli", "codex"):
        runtime_home = tmp_path / f"{executable_runtime}-probe-home"
        runtime_home.mkdir()
        runtime_env = probe_env.copy()
        runtime_env["HOME"] = str(runtime_home)
        runtime_env["USERPROFILE"] = str(runtime_home)
        runtime_probe = subprocess.run(
            [str(probe_cli), "mcp", "serve", "--runtime", executable_runtime],
            input=f"{initialize_request}\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=runtime_env,
        )
        assert runtime_probe.returncode == 0, (
            f"wheel runtime {executable_runtime!r} failed its MCP initialize handshake: "
            f"stdout={runtime_probe.stdout!r} stderr={runtime_probe.stderr!r}"
        )
        responses = [
            json.loads(line)
            for line in runtime_probe.stdout.splitlines()
            if line.lstrip().startswith("{")
        ]
        assert any(response.get("id") == 1 and "result" in response for response in responses), (
            responses
        )


@pytest.mark.slow
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
