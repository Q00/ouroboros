"""Tests for Codex's installed-plugin inventory boundary."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from ouroboros.cli.codex_plugin import CodexPluginVersion, inspect_codex_plugin

_COMMAND = ["/selected/codex", "plugin", "list", "--marketplace", "ouroboros", "--json"]
_ENTRY = {"pluginId": "ouroboros@ouroboros", "installed": True, "version": "0.55.6"}


def _inspect_inventory(inventory: object) -> CodexPluginVersion:
    completed = subprocess.CompletedProcess(_COMMAND, 0, json.dumps(inventory).encode(), b"")
    with patch("ouroboros.cli.codex_plugin.subprocess.run", return_value=completed):
        return inspect_codex_plugin(_COMMAND[0])


@pytest.mark.parametrize(
    "version",
    ["0.55.6", "0.55.7a1", "0.55.7b2", "0.55.7rc1", "0.55.7.dev3", "0.55.7+local.1"],
)
def test_reads_installed_release_versions(version: str) -> None:
    result = _inspect_inventory({"installed": [{**_ENTRY, "version": version}]})
    assert result == CodexPluginVersion("installed", version=version)


def test_disabled_plugin_still_counts_as_installed() -> None:
    result = _inspect_inventory({"installed": [{**_ENTRY, "enabled": False}]})
    assert result == CodexPluginVersion("installed", version="0.55.6")


def test_available_versions_and_other_plugin_identities_are_not_installed_evidence() -> None:
    result = _inspect_inventory(
        {
            "installed": [
                {**_ENTRY, "pluginId": "other@ouroboros", "version": "99.0.0"},
                {**_ENTRY, "pluginId": "ouroboros@other", "version": "99.0.0"},
            ],
            "available": [_ENTRY],
        }
    )
    assert result == CodexPluginVersion("not_installed")


def test_available_version_cannot_override_the_installed_version() -> None:
    result = _inspect_inventory(
        {
            "installed": [{**_ENTRY, "version": "0.52.0"}],
            "available": [{**_ENTRY, "version": "0.55.6"}],
        }
    )
    assert result == CodexPluginVersion("installed", version="0.52.0")


@pytest.mark.parametrize("installed", [[], [{**_ENTRY, "installed": False}]])
def test_valid_uninstalled_inventory(installed: list[dict[str, object]]) -> None:
    assert _inspect_inventory({"installed": installed}) == CodexPluginVersion("not_installed")


@pytest.mark.parametrize(
    "inventory",
    [
        None,
        [],
        {},
        {"installed": None},
        {"installed": {}},
        {"installed": [None]},
        {"installed": [{}]},
        {"installed": [{**_ENTRY, "pluginId": True}]},
        {"installed": [_ENTRY, _ENTRY]},
        {"installed": [_ENTRY, {**_ENTRY, "installed": False}]},
        {"installed": [{"pluginId": "ouroboros@ouroboros", "version": "0.55.6"}]},
        {"installed": [{**_ENTRY, "installed": "true"}]},
        {"installed": [{**_ENTRY, "installed": 1}]},
    ],
)
def test_incomplete_or_ambiguous_inventory_is_unknown(inventory: object) -> None:
    result = _inspect_inventory(inventory)
    assert result.status == "unknown"
    assert result.version is None
    assert result.reason


@pytest.mark.parametrize(
    "version",
    [
        None,
        True,
        55,
        "",
        "local",
        "garbage",
        "0.55.6garbage",
        "0.55.6\n",
        "0.55.6\x1b[31m",
        "1" * 129,
    ],
)
def test_invalid_versions_are_unknown(version: object) -> None:
    result = _inspect_inventory({"installed": [{**_ENTRY, "version": version}]})
    assert result.status == "unknown"
    assert result.version is None
    assert result.reason == "Codex did not report a release plugin version"


@pytest.mark.parametrize(
    "output",
    [
        "",
        "private/path: failed",
        '{"installed": []} trailing',
        pytest.param(
            '{"installed": [], "private": ' + "[" * 10_000 + "0" + "]" * 10_000 + "}",
            id="deep-json",
        ),
    ],
)
def test_invalid_json_is_unknown_without_echoing_output(output: str) -> None:
    completed = subprocess.CompletedProcess(_COMMAND, 0, output.encode(), b"private stderr")
    with patch("ouroboros.cli.codex_plugin.subprocess.run", return_value=completed):
        result = inspect_codex_plugin(_COMMAND[0])
    assert result.status == "unknown"
    assert result.reason == "Codex returned invalid plugin JSON"


@pytest.mark.parametrize("depth", [63, 64, 65])
@pytest.mark.parametrize("containers", ["arrays", "objects", "mixed"])
@pytest.mark.parametrize("installed", [[], [_ENTRY], [{**_ENTRY, "installed": False}]])
def test_inventory_depth_is_bounded_before_classifying_installation(
    depth: int, containers: str, installed: list[dict[str, object]]
) -> None:
    """All records and private metadata share the fixed 64-container limit."""
    metadata: object = 0
    for index in range(depth - 1):  # The inventory object is the outer container.
        metadata = (
            {"private": metadata}
            if containers == "objects" or (containers == "mixed" and index % 2)
            else [metadata]
        )
    result = _inspect_inventory({"installed": installed, "private": metadata})
    if depth > 64:
        assert result == CodexPluginVersion("unknown", reason="Codex returned invalid plugin JSON")
    elif installed and installed[0]["installed"]:
        assert result == CodexPluginVersion("installed", version="0.55.6")
    else:
        assert result == CodexPluginVersion("not_installed")


@pytest.mark.parametrize("depth", [64, 65])
def test_empty_containers_also_count_toward_inventory_depth(depth: int) -> None:
    metadata: object = []
    for _ in range(depth - 2):
        metadata = {"private": metadata}
    result = _inspect_inventory({"installed": [], "private": metadata})
    assert result.status == ("unknown" if depth > 64 else "not_installed")


@pytest.mark.parametrize("installed", [[], [_ENTRY]])
def test_duplicate_keys_cannot_hide_deep_inventory(installed: list[dict[str, object]]) -> None:
    output = (
        '{"installed": '
        + json.dumps(installed)
        + ', "private": '
        + "[" * 10_000
        + "0"
        + "]" * 10_000
        + ', "private": 0}'
    )
    completed = subprocess.CompletedProcess(_COMMAND, 0, output.encode(), b"")
    with patch("ouroboros.cli.codex_plugin.subprocess.run", return_value=completed):
        result = inspect_codex_plugin(_COMMAND[0])
    assert result == CodexPluginVersion("unknown", reason="Codex returned invalid plugin JSON")


@pytest.mark.parametrize("text", ["[{" * 10_000, 'brackets \\"[{]}', "\\\\", '"quoted"'])
def test_quoted_keys_and_values_do_not_count_as_inventory_depth(text: str) -> None:
    assert _inspect_inventory({"installed": [_ENTRY], text: text}) == CodexPluginVersion(
        "installed", version="0.55.6"
    )


@pytest.mark.parametrize("backslashes", [0, 1, 2, 3])
def test_escaped_quotes_and_trailing_backslashes_do_not_hide_depth(backslashes: int) -> None:
    text = "\\" * backslashes + '"' + "[{" * 65 + "\\" * backslashes
    assert _inspect_inventory({"installed": [_ENTRY], "text": text}).status == "installed"
    metadata: object = 0
    for _ in range(64):
        metadata = [metadata]
    assert _inspect_inventory(
        {"installed": [_ENTRY], "text": text, "private": metadata}
    ).status == ("unknown")


@pytest.mark.parametrize("executable", [None, ""])
def test_missing_executable_is_unknown_without_launching(executable: str | None) -> None:
    with patch("ouroboros.cli.codex_plugin.subprocess.run") as run:
        result = inspect_codex_plugin(executable)
    run.assert_not_called()
    assert result.status == "unknown"


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("private/path"),
        PermissionError("private/path"),
        subprocess.TimeoutExpired(_COMMAND, 5, output="private output"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte"),
        ValueError("private error"),
    ],
)
def test_query_failures_are_unknown_with_bounded_reasons(error: Exception) -> None:
    with patch("ouroboros.cli.codex_plugin.subprocess.run", side_effect=error):
        result = inspect_codex_plugin(_COMMAND[0])
    assert result.status == "unknown"
    assert result.version is None
    assert result.reason is not None and len(result.reason) < 100
    assert "private" not in result.reason


def test_nonzero_exit_cannot_be_mistaken_for_valid_inventory() -> None:
    completed = subprocess.CompletedProcess(
        _COMMAND, 1, json.dumps({"installed": [_ENTRY]}).encode(), b"private error"
    )
    with patch("ouroboros.cli.codex_plugin.subprocess.run", return_value=completed):
        result = inspect_codex_plugin(_COMMAND[0])
    assert result == CodexPluginVersion(
        "unknown", reason="Codex plugin query exited unsuccessfully"
    )


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_query_is_bounded_noninteractive_and_creates_no_windows(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr("ouroboros.cli.codex_plugin.sys.platform", platform)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    completed = subprocess.CompletedProcess(_COMMAND, 0, b'{"installed": []}', b"")
    with patch("ouroboros.cli.codex_plugin.subprocess.run", return_value=completed) as run:
        inspect_codex_plugin(_COMMAND[0])
    run.assert_called_once_with(
        _COMMAND,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
        check=False,
        creationflags=0x08000000 if platform == "win32" else 0,
    )


def test_utf8_inventory_inherits_codex_home_and_preserves_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use a real subprocess boundary with a fixture CLI, including UTF-8 bytes."""
    codex_home = tmp_path / "selected-codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    original = b'[marketplaces.ouroboros]\r\nref = "v0.52.0"\r\n'
    config.write_bytes(original)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.chdir(tmp_path)
    # Python treats the first command argument ("plugin") as a script path.
    # The fixture validates the exact remaining arguments and inherited home.
    (tmp_path / "plugin").write_text(
        "import json, os, pathlib, sys\n"
        "assert sys.argv[1:] == ['list', '--marketplace', 'ouroboros', '--json']\n"
        "assert b'v0.52.0' in (pathlib.Path(os.environ['CODEX_HOME']) / 'config.toml').read_bytes()\n"
        "inventory = {'installed': [{'pluginId': 'ouroboros@ouroboros', "
        "'installed': True, 'version': '0.52.0', 'description': '한국어 플러그인'}]}\n"
        "sys.stdout.buffer.write(json.dumps(inventory, ensure_ascii=False).encode('utf-8'))\n",
        encoding="utf-8",
    )

    assert inspect_codex_plugin(sys.executable) == CodexPluginVersion("installed", version="0.52.0")
    assert config.read_bytes() == original


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "status"),
    [
        (b"private-output-\xff", b"", 0, "unknown"),
        (json.dumps({"installed": [_ENTRY]}).encode(), b"private-stderr-\xff", 0, "installed"),
        (json.dumps({"installed": [_ENTRY]}).encode(), b"private-stderr-\xff", 1, "unknown"),
        pytest.param(
            b'{"installed": [], "private": ' + b"[" * 10_000 + b"0" + b"]" * 10_000 + b"}",
            b"private-stderr",
            0,
            "unknown",
            id="deep-json",
        ),
    ],
)
def test_real_pipe_failures_never_escape_as_tracebacks(
    tmp_path: Path,
    stdout: bytes,
    stderr: bytes,
    returncode: int,
    status: str,
) -> None:
    """Inspect the actual child pipes outside pytest's thread-exception hooks."""
    (tmp_path / "plugin").write_text(
        "import sys\n"
        f"sys.stdout.buffer.write({stdout!r})\n"
        f"sys.stderr.buffer.write({stderr!r})\n"
        f"sys.exit({returncode})\n",
        encoding="utf-8",
    )
    spec = importlib.util.find_spec("ouroboros.cli.codex_plugin")
    assert spec is not None and spec.origin is not None
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('inventory_probe', {spec.origin!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = module\n"
        "spec.loader.exec_module(module)\n"
        "result = module.inspect_codex_plugin(sys.executable)\n"
        "print(json.dumps({'status': result.status, 'reason': result.reason}))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-I", str(probe)],
        cwd=tmp_path,
        capture_output=True,
        timeout=15,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert json.loads(result.stdout)["status"] == status
    assert b"private" not in result.stdout
