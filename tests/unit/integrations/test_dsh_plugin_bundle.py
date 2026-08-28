"""Contract tests for the shipped `dsh-ouroboros` bundle.

The bundle is config only — no Python runs from it — so nothing in the normal
suite would catch a drift between what it declares and what DeepSeek Harness
actually does with that declaration. These tests pin the three edges that were
wrong before and would fail silently in CI otherwise:

1. `dsh plugin` requires `--profile`, so every published install command must
   carry it (a command without it exits with a CLI error, not an install).
2. dsh's subprocess seam scrubs credential-shaped names (`/KEY|PASSWORD|SECRET|
   TOKEN/i`) out of the child environment; the plugin's explicit `env` layer is
   the only way a credential reaches `ouroboros mcp serve`.
3. `OUROBOROS_LLM_BACKEND=dsh` fails closed unless the composition path reaches
   the child too, so that selector and its prerequisites travel together.
"""

import json
from pathlib import Path
from typing import Any

import yaml

BUNDLE_DIR = Path("integrations") / "dsh-plugin"

# Credential-shaped names dsh removes from the ambient child environment. The
# explicit env layer merges after the scrub, so these have to be named.
FORWARDED_CREDENTIALS = ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY")

# Selectors and the `dsh` LLM backend's fail-closed prerequisites.
FORWARDED_SELECTORS = (
    "OUROBOROS_LLM_BACKEND",
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_DSH_CONFIG_PATH",
    "OUROBOROS_DSH_CLI_PATH",
)


class _JsTolerantLoader(yaml.SafeLoader):
    """SafeLoader that keeps `!!js <expr>` scalars as their source text.

    Cordis parses `!!js` into an expression node evaluated at plugin load. For
    these tests the source text is exactly what needs asserting.
    """


def _construct_js(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    return str(loader.construct_scalar(node))  # type: ignore[arg-type]


_JsTolerantLoader.add_constructor("tag:yaml.org,2002:js", _construct_js)


def _load_patch() -> list[dict[str, Any]]:
    text = (BUNDLE_DIR / "cordis.patch.yml").read_text(encoding="utf-8")
    return list(yaml.load(text, Loader=_JsTolerantLoader))


def _mcp_row() -> dict[str, Any]:
    patch = _load_patch()
    assert len(patch) == 1, "the bundle contributes exactly one patch operation"
    inserts = patch[0]["insert"]
    assert len(inserts) == 1, "the bundle inserts exactly one row"
    return dict(inserts[0])


def test_package_manifest_points_at_shipped_files() -> None:
    manifest = json.loads((BUNDLE_DIR / "package.json").read_text(encoding="utf-8"))

    patch_ref = manifest["dsh"]["bundle"]["patch"]
    assert (BUNDLE_DIR / patch_ref).is_file()

    for shipped in manifest["files"]:
        assert (BUNDLE_DIR / shipped).is_file(), f"declared file missing: {shipped}"


def test_row_spawns_ouroboros_mcp_server_over_stdio() -> None:
    row = _mcp_row()

    assert row["id"] == "mcp-ouroboros"
    assert row["name"] == "@deepseek-ai/dsh-mcp-client"

    config = row["config"]
    assert config["serverName"] == "ouroboros"
    assert config["transport"] == "stdio"
    assert config["command"] == "uvx"
    assert config["args"] == [
        "--from",
        "ouroboros-ai[mcp]",
        "--with",
        "mcp==2.0.0",
        "ouroboros",
        "mcp",
        "serve",
    ]


def test_credential_shaped_names_are_forwarded_explicitly() -> None:
    # Ambient forwarding does not exist for these: dsh drops every name matching
    # /KEY|PASSWORD|SECRET|TOKEN/i before spawning. Dropping a row here makes the
    # tools list fine and the first call fail — exactly the failure this pins.
    env = _mcp_row()["config"]["env"]

    for name in FORWARDED_CREDENTIALS:
        assert name in env, f"{name} would be scrubbed out of the child environment"
        assert env[name] == f"process.env.{name} ?? ''"


def test_dsh_backend_prerequisites_travel_with_the_selector() -> None:
    env = _mcp_row()["config"]["env"]

    for name in FORWARDED_SELECTORS:
        assert name in env
        if name == "OUROBOROS_AGENT_RUNTIME":
            continue
        assert env[name] == f"process.env.{name} ?? ''"


def test_agent_runtime_selector_defaults_to_host() -> None:
    # dsh has no installable execution CLI of its own, so an unset selector
    # must fall back to the one runtime that works out of the box rather than
    # `''` (which would leave `mcp serve` to inherit whatever the operator's
    # own `ouroboros setup` default happens to be, or nothing at all).
    env = _mcp_row()["config"]["env"]

    assert env["OUROBOROS_AGENT_RUNTIME"] == "process.env.OUROBOROS_AGENT_RUNTIME || 'host'"


def test_startup_is_soft_and_calls_get_real_headroom() -> None:
    config = _mcp_row()["config"]

    assert config["failOnStartupError"] is False
    assert config["toolCallTimeoutMs"] == 1800000


def test_every_published_install_command_passes_a_profile() -> None:
    # `dsh plugin` declares --profile as a required option; a command without it
    # errors out before installing anything.
    readmes = (BUNDLE_DIR / "README.md", Path("README.md"))

    for readme in readmes:
        for line in readme.read_text(encoding="utf-8").splitlines():
            if "dsh plugin" not in line:
                continue
            assert "--profile" in line, f"{readme}: install command without --profile: {line}"


def test_bundle_readme_does_not_promise_automatic_recovery() -> None:
    # The published mcp-client has no reconnect loop, and the one on main gives
    # up after a bounded number of attempts.
    readme = (BUNDLE_DIR / "README.md").read_text(encoding="utf-8")

    assert "Recovery is not automatic in general." in readme
    assert "reload the plugin or restart dsh" in readme
