"""Tests for stdlib .env loading helpers."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from ouroboros.config import loader
from ouroboros.config.loader import (
    _is_assignable_env_key,
    _load_env_file,
)
from ouroboros.config.untrusted_env import UNTRUSTED_ENV_DENYLIST


@pytest.fixture(autouse=True)
def _restore_environ():
    """`_load_env_file` writes os.environ directly, bypassing monkeypatch.

    Without an explicit restore these tests leak keys (notably the
    runtime/backend selectors) into the session and break every later
    test that resolves a backend.
    """
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_load_env_file_sets_missing_values(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("export FIRST=value\nSECOND='two words'\nTHIRD=three # trailing comment\n")

    monkeypatch.delenv("FIRST", raising=False)
    monkeypatch.delenv("SECOND", raising=False)
    monkeypatch.delenv("THIRD", raising=False)

    _load_env_file(env_file)

    assert os.environ["FIRST"] == "value"
    assert os.environ["SECOND"] == "two words"
    assert os.environ["THIRD"] == "three"


def test_load_env_file_does_not_override_existing_values(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("FIRST=from-file\n")
    monkeypatch.setenv("FIRST", "existing")

    _load_env_file(env_file)

    assert os.environ["FIRST"] == "existing"


def test_load_env_file_ignores_directory_path(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.mkdir()

    _load_env_file(env_path)


def test_load_env_file_skips_template_placeholders(tmp_path: Path, monkeypatch) -> None:
    """Template placeholders should not block later env values from loading."""
    repo_env = tmp_path / "repo.env"
    home_env = tmp_path / "home.env"

    repo_env.write_text("OPENROUTER_API_KEY=YOUR_OPENROUTER_API_KEY")
    home_env.write_text("OPENROUTER_API_KEY=real-key")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    _load_env_file(repo_env)
    _load_env_file(home_env)

    assert os.environ["OPENROUTER_API_KEY"] == "real-key"


# Derive directly from the source of truth so this regression suite can never
# drift out of sync with the denylist again — a previous drift (missing the
# gjc/PI/config-home roots) is exactly how an incomplete fix slips past CI.
_DENYLISTED_KEYS = tuple(sorted(UNTRUSTED_ENV_DENYLIST))


def test_denylist_covers_known_execution_routing_keys() -> None:
    """Pin the membership of every execution-routing class explicitly.

    Importing the source set guards against drift, but an explicit floor
    ensures a future edit cannot silently *shrink* the denylist (e.g. drop a
    config-home root) without a failing test.
    """
    required = {
        # Process lookup/preload hooks + explicit path overrides/bare alias.
        "PATH",
        "NODE_OPTIONS",
        "LDR_PRELOAD",
        "LIBPATH",
        "SHLIB_PATH",
        "OUROBOROS_CLI_PATH",
        "OPENCODE_CLI_PATH",
        # Suffix-less alias read by the `ooo` frontdoor bridges (gjc, Pi).
        "OUROBOROS_CLI",
        # Spawned-CLI / agent instruction + extension roots.
        "GJC_CODING_AGENT_DIR",
        "GJC_CONFIG_DIR",
        "PI_CONFIG_DIR",
        "COPILOT_CUSTOM_INSTRUCTIONS_DIRS",
        "OUROBOROS_AGENTS_DIR",
        # Backend config-home roots (Codex/OpenCode config files -> RCE +
        # approval-gate removal). Completes CVE-2026-47211.
        "CODEX_HOME",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "USERPROFILE",
        "XDG_CONFIG_HOME",
        # Ouroboros MCP-bridge / plugin execution roster + SSRF toggle.
        "OUROBOROS_MCP_CONFIG",
        "OUROBOROS_PLUGIN_LOCKFILE",
        "OUROBOROS_PLUGIN_TRUST_ROOT",
        "OUROBOROS_ALLOW_LOCAL_TRANSPORT",
        # Destructive-recovery authority: the fs_ownership transaction ledger.
        "OUROBOROS_FS_TRANSACTION_DIR",
        # Operator-owned telemetry preference and destination must never be
        # supplied by a cloned repository. DO_NOT_TRACK belongs here too: it
        # is the cross-tool opt-out signal get_telemetry_enabled() checks
        # first, and an untrusted project .env loads before the trusted
        # ~/.ouroboros/.env and never overrides an already-set value.
        # CI/GITHUB_ACTIONS are the same boundary: telemetry.py stamps
        # ci=true from these, and the published counting rule excludes
        # ci=true from the weekly-active metric, so a cloned repo shipping
        # CI=1 could deregister genuine local users from that metric.
        "OUROBOROS_TELEMETRY",
        "OUROBOROS_POSTHOG_API_KEY",
        "OUROBOROS_POSTHOG_HOST",
        "OUROBOROS_FIRST_COMMAND_SURFACE",
        "DO_NOT_TRACK",
        "CI",
        "GITHUB_ACTIONS",
        # Runtime/backend selectors + permission/capability overrides.
        "OUROBOROS_AGENT_RUNTIME",
        "OUROBOROS_LLM_BACKEND",
        "OUROBOROS_RUNTIME_PROFILE",
        "OUROBOROS_CROSS_HARNESS_REDISPATCH",
        "OUROBOROS_AGENT_PERMISSION_MODE",
        "OUROBOROS_TOOL_CAPABILITIES",
        # Execution-cost/behavior dial — must not be forced from an untrusted repo.
        "OUROBOROS_AGENT_REASONING_EFFORT",
        "OUROBOROS_EXECUTION_MODEL",
        "OUROBOROS_MODEL_TIER_ROUTING",
        "OUROBOROS_SHADOW_REPLAY",
        # Shell startup files and option state. Bash sources BASH_ENV before
        # the first command of `bash -c`, which is how the verify gate runs an
        # AC's contract: a repo-supplied file holding `exit 0` would make every
        # failing contract pass.
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "BASH_XTRACEFD",
    }
    missing = required - UNTRUSTED_ENV_DENYLIST
    assert not missing, f"denylist regressed, missing: {sorted(missing)}"


# The `ooo` frontdoor bridges pick the executable they dispatch to from an
# environment variable. Those sources live outside Python's import graph, so
# nothing else ties them to the denylist: the alias they read (OUROBOROS_CLI)
# was missed precisely because it lacks the _CLI_PATH suffix every sibling has.
# Discover dispatch sources from their executable sink instead of maintaining a
# second bridge roster here. Every discovered source must independently yield
# at least one inspected key, so one healthy bridge cannot hide parser drift in
# another bridge.
_PACKAGE_ROOT = Path(loader.__file__).resolve().parent.parent
_BRIDGE_SOURCE_SUFFIXES = frozenset({".cjs", ".js", ".mjs", ".py", ".ts"})
_JS_IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
_BRIDGE_DISPATCH_SINK_RE = re.compile(rf"\b{_JS_IDENTIFIER}\.command\b")
_BRIDGE_ENTRY_ENV_RE = re.compile(
    r"process\.env(?:\.([A-Z0-9_]+)|\[\s*['\"]([A-Z0-9_]+)['\"]\s*\])"
    r"\s*\)\s*return\s*\{\{?\s*command",
)


def _discover_bridge_dispatch_sources(package_root: Path) -> tuple[Path, ...]:
    """Find packaged sources that dispatch a resolved bridge entry command."""
    sources: list[Path] = []
    for source in package_root.rglob("*"):
        if not source.is_file() or source.suffix not in _BRIDGE_SOURCE_SUFFIXES:
            continue
        text = source.read_text(encoding="utf-8")
        if "process.env" in text and _BRIDGE_DISPATCH_SINK_RE.search(text):
            sources.append(source)
    return tuple(sorted(sources))


def _bridge_dispatch_entry_env_keys(source_text: str) -> frozenset[str]:
    """Extract dot- or bracket-style environment keys used as commands."""
    return frozenset(
        key
        for match in _BRIDGE_ENTRY_ENV_RE.finditer(source_text)
        for key in match.groups()
        if key is not None
    )


def _validated_bridge_dispatch_env_keys(package_root: Path) -> dict[Path, frozenset[str]]:
    """Discover every bridge and fail if any source cannot be inspected."""
    sources = _discover_bridge_dispatch_sources(package_root)
    assert sources, "no bridge dispatch sources found — discovery contract drifted"

    found_by_source: dict[Path, frozenset[str]] = {}
    for source in sources:
        keys = _bridge_dispatch_entry_env_keys(source.read_text(encoding="utf-8"))
        assert keys, f"bridge dispatch-entry extraction drifted for {source}"
        found_by_source[source] = keys
    return found_by_source


def test_bridge_dispatch_entry_env_keys_are_denylisted() -> None:
    """Every env var a bridge turns into an exec command must be denylisted."""
    found_by_source = _validated_bridge_dispatch_env_keys(_PACKAGE_ROOT)

    missing = {
        key: str(source)
        for source, keys in found_by_source.items()
        for key in keys
        if key not in UNTRUSTED_ENV_DENYLIST
    }
    assert not missing, f"bridge exec-command env keys missing from denylist: {missing}"


@pytest.mark.parametrize(
    "access",
    ("process.env.OUROBOROS_CLI", 'process.env["OUROBOROS_CLI"]'),
)
def test_bridge_dispatch_entry_env_key_extraction_covers_supported_access_styles(
    access: str,
) -> None:
    """A bridge changing to bracket-style access remains inspected."""
    source = f"if ({access}) return {{ command: {access}, args: [] }};"

    assert _bridge_dispatch_entry_env_keys(source) == frozenset({"OUROBOROS_CLI"})


def test_bridge_dispatch_source_discovery_covers_new_sources(tmp_path: Path) -> None:
    """Adding a packaged bridge does not require updating a test-side roster."""
    source = tmp_path / "future_bridge" / "index.ts"
    source.parent.mkdir()
    source.write_text(
        "if (process.env.NEW_BRIDGE_CLI) "
        "return { command: process.env.NEW_BRIDGE_CLI, args: [] };\n"
        "await vendor.exec(entry.command, []);\n",
        encoding="utf-8",
    )

    assert _validated_bridge_dispatch_env_keys(tmp_path) == {source: frozenset({"NEW_BRIDGE_CLI"})}


def test_bridge_dispatch_source_discovery_is_identifier_independent(tmp_path: Path) -> None:
    """Renaming one bridge's resolved entry cannot remove it from inspection."""
    entry_source = tmp_path / "entry.ts"
    entry_source.write_text(
        "if (process.env.ENTRY_CLI) "
        "return { command: process.env.ENTRY_CLI, args: [] };\n"
        "await vendor.exec(entry.command, []);\n",
        encoding="utf-8",
    )
    resolved_source = tmp_path / "resolved.ts"
    resolved_source.write_text(
        "if (process.env.RESOLVED_CLI) "
        "return { command: process.env.RESOLVED_CLI, args: [] };\n"
        "await vendor.exec(resolved.command, []);\n",
        encoding="utf-8",
    )

    assert _validated_bridge_dispatch_env_keys(tmp_path) == {
        entry_source: frozenset({"ENTRY_CLI"}),
        resolved_source: frozenset({"RESOLVED_CLI"}),
    }


def test_bridge_dispatch_validation_is_non_vacuous_per_source(tmp_path: Path) -> None:
    """One parseable bridge cannot hide extraction drift in another bridge."""
    good = tmp_path / "good.ts"
    good.write_text(
        "if (process.env.GOOD_CLI) return { command: process.env.GOOD_CLI, args: [] };\n"
        "await vendor.exec(entry.command, []);\n",
        encoding="utf-8",
    )
    drifted = tmp_path / "drifted.ts"
    drifted.write_text(
        "const command = process.env.DRIFTED_CLI;\n"
        "if (command) return { command, args: [] };\n"
        "await vendor.exec(entry.command, []);\n",
        encoding="utf-8",
    )

    with pytest.raises(AssertionError, match=r"extraction drifted for .*drifted\.ts"):
        _validated_bridge_dispatch_env_keys(tmp_path)


def test_untrusted_env_cannot_set_bridge_cli_alias(tmp_path: Path, monkeypatch) -> None:
    """A cloned repo's .env must not choose the binary the `ooo` bridge runs."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_CLI=./.tools/helper\n", encoding="utf-8")
    monkeypatch.delenv("OUROBOROS_CLI", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "OUROBOROS_CLI" not in os.environ


@pytest.mark.parametrize(
    "key",
    ["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD", "BASH_FUNC_pytest%%"],
)
def test_untrusted_env_cannot_set_shell_startup_controls(
    key: str, tmp_path: Path, monkeypatch
) -> None:
    """Bash sources `BASH_ENV` before the first command of `bash -c`, which is
    how the verify gate runs an AC's contract: a repo-supplied file holding
    `exit 0` would make every failing contract pass."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=./.tools/always-pass.sh\n", encoding="utf-8")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


def test_trusted_env_may_still_set_bridge_cli_alias(tmp_path: Path, monkeypatch) -> None:
    """~/.ouroboros/.env keeps the operator's escape hatch for a non-PATH install."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_CLI=/opt/ouroboros/bin/ouroboros\n", encoding="utf-8")
    monkeypatch.delenv("OUROBOROS_CLI", raising=False)

    _load_env_file(env_file, trusted=True)

    assert os.environ["OUROBOROS_CLI"] == "/opt/ouroboros/bin/ouroboros"


def test_project_env_cannot_redirect_trusted_home_during_import(tmp_path: Path) -> None:
    """The project layer cannot choose the later trusted config directory."""
    project = tmp_path / "project"
    safe_home = tmp_path / "safe-home"
    attacker_home = project / "attacker-home"
    project.mkdir()
    safe_home.mkdir()
    (attacker_home / ".ouroboros").mkdir(parents=True)
    (project / ".env").write_text(f"HOME={attacker_home}\n", encoding="utf-8")
    (attacker_home / ".ouroboros" / ".env").write_text(
        "OUROBOROS_CODEX_CLI_PATH=/tmp/attacker-codex\n",
        encoding="utf-8",
    )
    script = f"""
import os
from pathlib import Path
from unittest.mock import patch

safe_home = Path({str(safe_home)!r})
with patch.object(Path, "home", side_effect=lambda: Path(os.environ.get("HOME", safe_home))):
    import ouroboros.config.loader
    from ouroboros.config.models import get_config_dir
    print(os.environ.get("OUROBOROS_CODEX_CLI_PATH", ""))
    print(get_config_dir())
"""
    environment = os.environ.copy()
    for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        environment.pop(key, None)
    environment.pop("OUROBOROS_CODEX_CLI_PATH", None)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == ["", str(safe_home / ".ouroboros")]


def test_untrusted_env_cannot_set_bare_opencode_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Regression: opencode_config reads bare OPENCODE_CLI_PATH and runs it."""
    env_file = tmp_path / ".env"
    env_file.write_text("OPENCODE_CLI_PATH=./evil\n")
    monkeypatch.delenv("OPENCODE_CLI_PATH", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "OPENCODE_CLI_PATH" not in os.environ


def test_untrusted_env_cannot_set_do_not_track(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Simple case: a project .env's DO_NOT_TRACK is simply never loaded --
    no value from an untrusted source can reach os.environ at all, so it
    can neither force-disable telemetry nor block a later trusted value."""
    env_file = tmp_path / ".env"
    env_file.write_text("DO_NOT_TRACK=0\n")
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "DO_NOT_TRACK" not in os.environ


def test_project_do_not_track_cannot_override_persisted_user_opt_out(
    tmp_path: Path,
) -> None:
    """The reviewer's exact probe: project DO_NOT_TRACK=0 must not prevent a
    user's persisted DO_NOT_TRACK=1 (in the trusted ~/.ouroboros/.env) from
    ever being applied. Before the fix, the untrusted project .env loaded
    first (module import order), set DO_NOT_TRACK=0 unconditionally, and the
    trusted home .env's "do not override an already-set value" precedence
    then silently discarded the user's opt-out -- get_telemetry_enabled()
    returned True. Spawned as a real subprocess so both module-import-time
    _load_env_file() calls run in their real order, exactly like production."""
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    (home / ".ouroboros").mkdir(parents=True)
    (project / ".env").write_text("DO_NOT_TRACK=0\n", encoding="utf-8")
    (home / ".ouroboros" / ".env").write_text("DO_NOT_TRACK=1\n", encoding="utf-8")

    script = f"""
import os
from pathlib import Path
from unittest.mock import patch

home = Path({str(home)!r})
with patch.object(Path, "home", side_effect=lambda: home):
    import ouroboros.config.loader
    from ouroboros.config.loader import get_telemetry_enabled
    print(os.environ.get("DO_NOT_TRACK", "<unset>"))
    print(get_telemetry_enabled())
"""
    environment = os.environ.copy()
    for key in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "DO_NOT_TRACK"):
        environment.pop(key, None)

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == ["1", "False"]


@pytest.mark.parametrize("key", ["CI", "GITHUB_ACTIONS"])
def test_untrusted_env_cannot_set_ci_classification(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """Simple case: a project .env's CI/GITHUB_ACTIONS is simply never
    loaded -- CI classification feeds the published counting rule's
    ci!=true exclusion, so an untrusted value here could silently
    deregister genuine local users from that metric."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=1\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


def test_project_ci_cannot_deregister_local_users_from_the_counting_rule(
    tmp_path: Path,
) -> None:
    """The reviewer's exact probe: a project .env shipping CI=1 must not
    stamp ci=true on a genuine local command_run event -- that would
    silently exclude the event from the published active-user rule
    (ci!=true). Spawned as a real subprocess so module-import-time
    _load_env_file() runs in its real order before telemetry ever
    captures anything, exactly like production. Real CI runners set CI in
    the actual process environment (stripped from this subprocess's env
    below), which the loader never overrides regardless of the denylist,
    so this only proves a cloned repo's own .env lost the ability to
    forge the classification -- genuine CI detection is untouched (see
    test_genuine_process_ci_still_stamps_ci_true in test_telemetry.py)."""
    project = tmp_path / "project"
    home = tmp_path / "home"
    project.mkdir()
    (home / ".ouroboros").mkdir(parents=True)
    (project / ".env").write_text("CI=1\n", encoding="utf-8")

    script = f"""
import os
from pathlib import Path
from unittest.mock import patch

home = Path({str(home)!r})
with patch.object(Path, "home", side_effect=lambda: home):
    import ouroboros.config.loader
    from ouroboros import telemetry

    print(os.environ.get("CI", "<unset>"))

    captured = []
    telemetry._post = lambda batch: captured.extend(batch)
    telemetry.capture_cli_command("run")
    telemetry.flush(timeout=2.0)
    props = captured[0]["properties"] if captured else {{}}
    print("ci" in props)
"""
    environment = os.environ.copy()
    for key in (
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "CI",
        "GITHUB_ACTIONS",
        "DO_NOT_TRACK",
        "OUROBOROS_TELEMETRY",
    ):
        environment.pop(key, None)
    environment["OUROBOROS_POSTHOG_API_KEY"] = "phc_test"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.splitlines() == ["<unset>", "False"]


def test_untrusted_env_cannot_set_node_options(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A cloned repo cannot preload JavaScript into a spawned Node CLI."""
    env_file = tmp_path / ".env"
    env_file.write_text("NODE_OPTIONS=--require ./evil.js\n")
    monkeypatch.delenv("NODE_OPTIONS", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "NODE_OPTIONS" not in os.environ


@pytest.mark.parametrize(
    "key",
    [
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FRAMEWORK_PATH",
        "LDR_PRELOAD",
        "LIBPATH",
        "SHLIB_PATH",
    ],
)
def test_untrusted_env_cannot_set_dynamic_loader_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=./attacker-library\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


def test_untrusted_env_cannot_disable_approval_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Regression: a cloned repo must not force bypassPermissions."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_AGENT_PERMISSION_MODE=bypassPermissions\n")
    monkeypatch.delenv("OUROBOROS_AGENT_PERMISSION_MODE", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "OUROBOROS_AGENT_PERMISSION_MODE" not in os.environ


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OUROBOROS_EXECUTION_MODEL", "attacker/expensive-model"),
        ("OUROBOROS_MODEL_TIER_ROUTING", "off"),
        ("OUROBOROS_SHADOW_REPLAY", "on"),
    ],
)
def test_untrusted_env_cannot_change_model_tier_experiment_controls(
    tmp_path: Path,
    monkeypatch,
    key: str,
    value: str,
) -> None:
    """A cloned repo cannot change routing cost or arm double-spend replay."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}={value}\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


@pytest.mark.parametrize(
    "key",
    [
        "OUROBOROS_MCP_CONFIG",
        "OUROBOROS_PLUGIN_LOCKFILE",
        "OUROBOROS_PLUGIN_TRUST_ROOT",
        "OUROBOROS_ALLOW_LOCAL_TRANSPORT",
    ],
)
def test_untrusted_env_cannot_set_mcp_or_plugin_roster(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """A cloned repo must not redirect the MCP bridge / plugin dispatcher at
    an attacker-controlled command roster, nor disable the SSRF transport
    guard, via the auto-loaded project .env."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=./.evil\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


@pytest.mark.parametrize(
    "key",
    ["CODEX_HOME", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"],
)
def test_untrusted_env_cannot_redirect_backend_config_home(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """CVE-2026-47211 completion: a cloned repo must not redirect a spawned
    backend (Codex/OpenCode) at attacker-controlled config that can launch
    MCP servers or disable the approval gate."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=./.evil\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


@pytest.mark.parametrize(
    "key",
    [
        "UV_INDEX",
        "UV_DEFAULT_INDEX",
        "UV_CONFIG_FILE",
        "UV_INDEX_CORP_USERNAME",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_CONFIG_FILE",
        "PIPX_DEFAULT_PYTHON",
    ],
)
def test_untrusted_env_cannot_configure_package_manager(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """A cloned repo cannot redirect the persistent self-update package source."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=https://attacker.invalid/simple\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


@pytest.mark.parametrize("key", ["UV_INDEX", "PIP_INDEX_URL", "PIPX_DEFAULT_PYTHON"])
def test_trusted_env_may_configure_package_manager(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """Real process/home configuration stays available for private installations."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=https://packages.example/simple\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=True)

    assert os.environ[key] == "https://packages.example/simple"


def test_untrusted_env_cannot_set_mixed_case_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Mixed-case PATH variants must not bypass the denylist on Windows."""
    env_file = tmp_path / ".env"
    env_file.write_text("Path=./malicious-bin\n")
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.delenv("Path", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "PATH" not in os.environ
    assert "Path" not in os.environ


@pytest.mark.parametrize("key", _DENYLISTED_KEYS)
def test_untrusted_env_cannot_redirect_executable(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """A cloned-repo .env must not set executable-path vars (RCE guard)."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=./malicious_script.sh\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=False)

    assert key not in os.environ


@pytest.mark.parametrize("key", _DENYLISTED_KEYS)
def test_trusted_env_may_set_executable_path(
    tmp_path: Path,
    monkeypatch,
    key: str,
) -> None:
    """The home .env stays trusted and may set a custom CLI path."""
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=/usr/local/bin/claude\n")
    monkeypatch.delenv(key, raising=False)

    _load_env_file(env_file, trusted=True)

    assert os.environ[key] == "/usr/local/bin/claude"


def test_untrusted_env_does_not_override_process_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Project .env must not replace an existing process PATH."""
    process_path = "/usr/bin:/bin"
    env_file = tmp_path / ".env"
    env_file.write_text(f"PATH=./malicious-bin:{process_path}\n")
    monkeypatch.setenv("PATH", process_path)

    _load_env_file(env_file, trusted=False)

    assert os.environ["PATH"] == process_path


def test_trusted_env_does_not_override_process_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Trusted .env keeps normal process-environment precedence for PATH."""
    process_path = "/usr/bin:/bin"
    env_file = tmp_path / ".env"
    env_file.write_text(f"PATH=/trusted/bin:{process_path}\n")
    monkeypatch.setenv("PATH", process_path)

    _load_env_file(env_file, trusted=True)

    assert os.environ["PATH"] == process_path


def test_untrusted_env_still_loads_non_sensitive_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Denylisting must be surgical: ordinary keys still load untrusted."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_CLI_PATH=./evil.sh\nOPENROUTER_API_KEY=key-123\n")
    monkeypatch.delenv("OUROBOROS_CLI_PATH", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    _load_env_file(env_file, trusted=False)

    assert "OUROBOROS_CLI_PATH" not in os.environ
    assert os.environ["OPENROUTER_API_KEY"] == "key-123"


def test_load_env_file_defaults_to_untrusted_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Default trusted=False: callers are safe-by-default (fail-closed)."""
    env_file = tmp_path / ".env"
    env_file.write_text("OUROBOROS_CLI_PATH=./evil.sh\n")
    monkeypatch.delenv("OUROBOROS_CLI_PATH", raising=False)

    _load_env_file(env_file)  # no trusted kwarg → must be treated as untrusted

    assert "OUROBOROS_CLI_PATH" not in os.environ


def test_native_session_index_default_off(monkeypatch) -> None:
    from ouroboros.config import get_native_session_index_enabled

    monkeypatch.delenv("OUROBOROS_NATIVE_SESSION_INDEX", raising=False)
    assert get_native_session_index_enabled() is False


def test_native_session_index_opt_in_values(monkeypatch) -> None:
    from ouroboros.config import get_native_session_index_enabled

    for truthy in ("1", "true", "on", "yes", "YES"):
        monkeypatch.setenv("OUROBOROS_NATIVE_SESSION_INDEX", truthy)
        assert get_native_session_index_enabled() is True
    for falsy in ("0", "off", "false", "no", ""):
        monkeypatch.setenv("OUROBOROS_NATIVE_SESSION_INDEX", falsy)
        assert get_native_session_index_enabled() is False


class TestEnvValueGrammar:
    """Value parsing follows `.env` grammar, not Python string grammar.

    The previous implementation ran quoted values through `ast.literal_eval`,
    so any single-quoted value containing a backslash escape was silently
    rewritten. `X='C:\new\table'` arrived as ten characters holding a real
    newline and a real tab.
    """

    @staticmethod
    def _load(tmp_path: Path, body: str, key: str, monkeypatch) -> str | None:
        env_file = tmp_path / ".env"
        env_file.write_text(body, encoding="utf-8")
        monkeypatch.delenv(key, raising=False)
        _load_env_file(env_file, trusted=True)
        return os.environ.get(key)

    def test_single_quoted_windows_path_survives_intact(self, tmp_path: Path, monkeypatch) -> None:
        """The regression this class exists for."""
        value = self._load(tmp_path, r"WINPATH='C:\new\table'" + "\n", "WINPATH", monkeypatch)

        assert value == r"C:\new\table"
        assert len(value or "") == 12
        assert not any(ord(ch) < 0x20 for ch in value or "")

    def test_single_quotes_do_not_process_escapes(self, tmp_path: Path, monkeypatch) -> None:
        """Single quotes are literal in POSIX shells and in every .env dialect."""
        assert self._load(tmp_path, r"LITERAL='a\nb'" + "\n", "LITERAL", monkeypatch) == r"a\nb"

    def test_double_quoted_escapes_are_still_processed(self, tmp_path: Path, monkeypatch) -> None:
        """Unchanged behaviour: double quotes keep interpreting escapes."""
        assert self._load(tmp_path, r'ESCAPED="a\nb"' + "\n", "ESCAPED", monkeypatch) == "a\nb"

    @pytest.mark.parametrize(
        ("body", "key"),
        [
            pytest.param('CONCAT="abc" "def"\n', "CONCAT", id="python-implicit-concatenation"),
            pytest.param('TAIL="value" junk\n', "TAIL", id="trailing-junk"),
        ],
    )
    def test_malformed_lines_are_rejected_not_invented(
        self, tmp_path: Path, monkeypatch, body: str, key: str
    ) -> None:
        """`"abc" "def"` used to yield `abcdef`, a value present nowhere in the file."""
        assert self._load(tmp_path, body, key, monkeypatch) is None

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param("DOLLAR=$HOME\n", "$HOME", id="bare"),
            pytest.param("DOLLAR=${HOME}\n", "${HOME}", id="braced"),
        ],
    )
    def test_variable_references_are_not_interpolated(
        self, tmp_path: Path, monkeypatch, body: str, expected: str
    ) -> None:
        """`interpolate=False` keeps today's behaviour and denies an untrusted
        `.env` a way to read the real process environment into a value."""
        assert self._load(tmp_path, body, "DOLLAR", monkeypatch) == expected

    def test_untrusted_env_cannot_interpolate_a_denied_key(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Interpolation would otherwise let a cloned repo's .env read PATH."""
        monkeypatch.setenv("PATH", "/sentinel/bin")
        leaked = self._load(tmp_path, "LEAK=${PATH}\n", "LEAK", monkeypatch)

        assert leaked == "${PATH}"
        assert "/sentinel/bin" not in (leaked or "")

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param("PLAIN=value\n", "value", id="unquoted"),
            pytest.param("export EXPORTED=value\n", "value", id="export-prefix"),
            pytest.param('QUOTED="two words"\n', "two words", id="double-quoted-space"),
            pytest.param("INLINE=value # comment\n", "value", id="inline-comment"),
            pytest.param('HASH="a # b"\n', "a # b", id="hash-inside-quotes"),
            pytest.param("PAD=  padded  \n", "padded", id="surrounding-whitespace"),
            pytest.param("UNICODE=한글값\n", "한글값", id="non-ascii"),
        ],
    )
    def test_established_forms_are_unchanged(
        self, tmp_path: Path, monkeypatch, body: str, expected: str
    ) -> None:
        key = body.replace("export ", "").split("=", 1)[0]
        assert self._load(tmp_path, body, key, monkeypatch) == expected

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("# COMMENTED=value\n", id="comment-line"),
            pytest.param("BARE\n", id="bare-key-no-equals"),
            pytest.param("EMPTY=\n", id="empty-value"),
        ],
    )
    def test_non_assignments_set_nothing(self, tmp_path: Path, monkeypatch, body: str) -> None:
        for key in ("COMMENTED", "BARE", "EMPTY"):
            monkeypatch.delenv(key, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(body, encoding="utf-8")

        _load_env_file(env_file, trusted=True)

        assert not {"COMMENTED", "BARE", "EMPTY"} & set(os.environ)

    def test_denylist_still_applies_to_the_delegated_parser(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Delegating the grammar must not delegate the trust policy."""
        denied = sorted(UNTRUSTED_ENV_DENYLIST)[0]
        monkeypatch.setenv(denied, "original")
        env_file = tmp_path / ".env"
        env_file.write_text(f"{denied}=hijacked\n", encoding="utf-8")

        _load_env_file(env_file, trusted=False)

        assert os.environ[denied] == "original"


class TestHostileEnvCannotBlockStartup:
    """`_load_env_file` runs at module import, so it must never raise.

    python-dotenv's grammar is wider than the environment's: a quoted
    left-hand side like `'BROKEN=KEY'=value` parses to the key `BROKEN=KEY`,
    which CPython refuses with `ValueError: illegal environment variable
    name`. Unhandled, that turns a cloned repository's `.env` into a denial of
    service against every Ouroboros command.
    """

    HOSTILE = "'BROKEN=KEY'=value\n"

    def test_key_containing_equals_is_skipped(self, tmp_path: Path, monkeypatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(self.HOSTILE, encoding="utf-8")

        _load_env_file(env_file, trusted=True)

        assert "BROKEN=KEY" not in os.environ

    def test_hostile_key_does_not_stop_later_entries(self, tmp_path: Path, monkeypatch) -> None:
        """One bad line must cost one entry, not the rest of the file."""
        monkeypatch.delenv("SURVIVOR", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(self.HOSTILE + "SURVIVOR=ok\n", encoding="utf-8")

        _load_env_file(env_file, trusted=True)

        assert os.environ.get("SURVIVOR") == "ok"

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("BROKEN=KEY", id="equals"),
            pytest.param("HAS SPACE", id="space"),
            pytest.param("HAS\tTAB", id="tab"),
            pytest.param("", id="empty"),
            pytest.param("NUL\0KEY", id="nul"),
        ],
    )
    def test_unassignable_keys_are_rejected(self, key: str) -> None:
        assert _is_assignable_env_key(key) is False

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("PLAIN", id="plain"),
            pytest.param("WITH_UNDERSCORE", id="underscore"),
            pytest.param("MiXeD123", id="mixed-case-digits"),
            pytest.param("OUROBOROS_RUNTIME_BACKEND", id="real-key"),
        ],
    )
    def test_ordinary_keys_are_accepted(self, key: str) -> None:
        assert _is_assignable_env_key(key) is True

    @pytest.mark.parametrize(
        ("name", "content"),
        [
            pytest.param("illegal-key", b"'BROKEN=KEY'=value\n", id="illegal-key"),
            pytest.param("invalid-utf8", b"BROKEN=\xff\n", id="invalid-utf8"),
            pytest.param("binary-garbage", bytes(range(0, 32)), id="binary-garbage"),
        ],
    )
    def test_malformed_env_files_do_not_abort_import(
        self, tmp_path: Path, name: str, content: bytes
    ) -> None:
        """Every parser failure must degrade to "no variables loaded"."""
        (tmp_path / ".env").write_bytes(content)

        result = subprocess.run(
            [sys.executable, "-c", "import ouroboros.config.loader; print('started')"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "started" in result.stdout

    def test_non_utf8_env_leaves_the_environment_untouched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv("AFTER_BAD_BYTE", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_bytes(b"BROKEN=\xff\nAFTER_BAD_BYTE=value\n")

        _load_env_file(env_file, trusted=True)

        assert "AFTER_BAD_BYTE" not in os.environ

    def test_import_survives_a_hostile_project_env(self, tmp_path: Path) -> None:
        """The regression the blocker describes: startup, not just this function.

        Imports the loader in a fresh interpreter whose cwd holds the hostile
        `.env`, because the module-level `_load_env_file(Path(".env"))` call is
        what a crash would take down.
        """
        (tmp_path / ".env").write_text(self.HOSTILE, encoding="utf-8")

        result = subprocess.run(
            [sys.executable, "-c", "import ouroboros.config.loader; print('started')"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert "started" in result.stdout
        assert "illegal environment variable name" not in result.stderr


def test_untrusted_env_cannot_select_fs_transaction_dir_for_recover_owned_claims(
    tmp_path: Path, monkeypatch
) -> None:
    """Loader-to-recovery path for the destructive-replay authority.

    ``core/fs_ownership.py`` deletes a stale shared-directory container only
    when the transaction ledger vouches for it, so whoever selects the ledger
    holds destructive-recovery authority. A cloned repository's ``.env``
    must not be able to select it: with the key denied, an
    attacker-controlled ledger record plus a matching forged ``.importing``
    container cannot make ``recover_owned_claims`` delete the operator
    payload. The trusted process environment may still select an isolated
    ledger.
    """
    import json

    from ouroboros.core import fs_ownership

    shared = tmp_path / "shared"
    shared.mkdir()
    target = shared / "artifact-dir"
    nonce = "0123456789abcdef"
    forged = shared / f".{target.name}.{nonce}.importing"
    forged.mkdir(mode=0o700)
    (forged / ".ouroboros-intent").write_text(target.name, encoding="utf-8")
    (forged / "entry").mkdir()
    (forged / "entry" / "operator.txt").write_text("operator data\n", encoding="utf-8")

    attacker_ledger = tmp_path / "attacker-ledger"
    attacker_ledger.mkdir(mode=0o700)
    (attacker_ledger / f"{nonce}.json").write_text(
        json.dumps(
            {
                "canonical": os.path.abspath(os.fspath(target)),
                "container": forged.name,
                "operation": "importing",
            }
        ),
        encoding="utf-8",
    )

    env_file = tmp_path / ".env"
    env_file.write_text(f"OUROBOROS_FS_TRANSACTION_DIR={attacker_ledger}\n", encoding="utf-8")
    monkeypatch.delenv("OUROBOROS_FS_TRANSACTION_DIR", raising=False)
    # Keep the trusted-home fallback out of the real home directory.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))

    _load_env_file(env_file, trusted=False)

    assert "OUROBOROS_FS_TRANSACTION_DIR" not in os.environ
    assert fs_ownership._transaction_ledger_root() != attacker_ledger

    assert not fs_ownership.recover_owned_claims(target, is_owned=lambda _p: False)
    assert (forged / "entry" / "operator.txt").read_text(encoding="utf-8") == "operator data\n"
    assert (attacker_ledger / f"{nonce}.json").exists()

    # The real process environment (and the trusted home .env, which follows
    # the same precedence) remains free to select an isolated ledger.
    trusted_ledger = tmp_path / "trusted-ledger"
    monkeypatch.setenv("OUROBOROS_FS_TRANSACTION_DIR", str(trusted_ledger))
    assert fs_ownership._transaction_ledger_root() == trusted_ledger
