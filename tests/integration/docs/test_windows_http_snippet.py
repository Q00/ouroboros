"""Run the documented listener inspection against native Windows TCP/CIM APIs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
import pytest

_DOC: Final = Path(__file__).resolve().parents[3] / "docs/runtime-guides/codex.md"
_PORT_ASSIGNMENT: Final = re.compile(r"^\$mcpPort = [0-9]+$", re.MULTILINE)
_WINDOWS_ONLY: Final = pytest.mark.skipif(os.name != "nt", reason="requires native Windows")


class ListenerRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    address: str = Field(alias="LocalAddress")
    port: int = Field(alias="LocalPort")
    pid: int = Field(alias="OwningProcess")


class ProcessRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pid: int = Field(alias="ProcessId")
    parent_pid: int = Field(alias="ParentProcessId")
    executable: str = Field(alias="ExecutablePath")
    command_line: str = Field(alias="CommandLine")


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str
    rows: tuple[str | ListenerRow | ProcessRow, ...]


def _documented_snippet() -> str:
    blocks = re.findall(r"```powershell\n(.*?)\n```", _DOC.read_text(encoding="utf-8"), re.S)
    matches = [block for block in blocks if _PORT_ASSIGNMENT.search(block)]
    assert len(matches) == 1, "Expected one documented $mcpPort inspection block"
    return matches[0]


def _with_port(snippet: str, port: int) -> str:
    replaced, count = _PORT_ASSIGNMENT.subn(f"$mcpPort = {port}", snippet)
    assert count == 1, "Only the documented port knob may be substituted"
    return replaced


@contextmanager
def _listeners(count: int) -> Iterator[tuple[int, tuple[str, ...]]]:
    with ExitStack() as stack:
        port = 0
        addresses = tuple(f"127.0.0.{index + 1}" for index in range(count))
        # A bound, non-listening wildcard socket reserves the no-listener case.
        for address in addresses or ("0.0.0.0",):
            listener = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((address, port))
            port = listener.getsockname()[1]
            if count:
                listener.listen()
        yield port, addresses


@pytest.fixture(params=["powershell", "pwsh"], ids=["powershell51", "pwsh7"])
def windows_shell(request: pytest.FixtureRequest) -> Path:
    executable = shutil.which(request.param)
    assert executable is not None, f"Required native shell is missing: {request.param}"
    return Path(executable)


def _run_snippet(
    shell: Path, snippet: str, port: int, addresses: tuple[str, ...], tmp_path: Path, case: str
) -> tuple[Observation, Path]:
    script = tmp_path / "inspect-listener.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
        "$observed = @(& {\n" + snippet + "\n})\n"
        "[pscustomobject]@{ version = $PSVersionTable.PSVersion.ToString(); "
        "rows = @($observed) } | ConvertTo-Json -Depth 8 -Compress\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [
            str(shell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    evidence = Path(os.environ.get("OOO_WINDOWS_HTTP_EVIDENCE", str(tmp_path / "evidence")))
    evidence.mkdir(parents=True, exist_ok=True)
    artifact = evidence / f"{shell.stem}-{case}.json"
    artifact.write_text(
        json.dumps(
            {
                "source": str(_DOC.relative_to(_DOC.parents[2])),
                "source_sha256": hashlib.sha256(_DOC.read_bytes()).hexdigest(),
                "snippet_sha256": hashlib.sha256(_documented_snippet().encode()).hexdigest(),
                "executed_sha256": hashlib.sha256(snippet.encode()).hexdigest(),
                "shell": str(shell),
                "case": case,
                "port": port,
                "expected_pid": os.getpid(),
                "expected_parent_pid": os.getppid(),
                "expected_addresses": addresses,
                "returncode": result.returncode,
                "stdout": result.stdout[:65536],
                "stderr": result.stderr[:65536],
                "oracle": "not_checked",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    assert result.returncode == 0, f"Native shell failed; see {artifact}: {result.stderr}"
    assert not result.stderr, f"Native shell reported errors; see {artifact}"
    assert len(result.stdout) <= 65536, "Unexpectedly large snippet output"
    observation = Observation.model_validate_json(result.stdout)
    expected_version = "5.1." if shell.stem.lower() == "powershell" else "7."
    assert observation.version.startswith(expected_version), observation.version
    return observation, artifact


def _assert_observation(observation: Observation, port: int, addresses: tuple[str, ...]) -> None:
    listeners = [row for row in observation.rows if isinstance(row, ListenerRow)]
    processes = [row for row in observation.rows if isinstance(row, ProcessRow)]
    assert len(listeners) == len(addresses), "Expected each real listener exactly once"
    assert {row.address for row in listeners} == set(addresses)
    assert all(row.port == port and row.pid == os.getpid() for row in listeners)
    if not addresses:
        assert not processes, "No listener must produce no process rows"
        assert len(observation.rows) == 1
        notice = observation.rows[0]
        assert isinstance(notice, str) and str(port) in notice
        return
    assert len(processes) == 1, "Expected one deduplicated process row for the owning PID"
    process = processes[0]
    assert process.pid == os.getpid() and process.parent_pid == os.getppid()
    assert Path(process.executable).is_file()
    assert Path(process.executable).name.lower() == Path(sys.executable).name.lower()
    assert process.command_line.strip(), "Process command line must be visible"
    assert len(observation.rows) == len(addresses) + 1


def _record_oracle(artifact: Path, result: str) -> None:
    record = json.loads(artifact.read_text(encoding="utf-8"))
    record["oracle"] = result
    record["observed"] = json.loads(record["stdout"])
    record["shell_version"] = record["observed"]["version"]
    artifact.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def test_extracted_snippet_changes_only_the_documented_port_knob() -> None:
    # Given the actual fenced block, When choosing an ephemeral test port,
    # Then every byte outside the single port assignment stays unchanged.
    original = _documented_snippet()
    assignment = _PORT_ASSIGNMENT.search(original)
    assert assignment is not None
    changed = _with_port(original, 49152)
    assert (
        changed
        == original[: assignment.start()] + "$mcpPort = 49152" + original[assignment.end() :]
    )


@_WINDOWS_ONLY
@pytest.mark.parametrize("count", [0, 1, 2], ids=["none", "single", "multiple"])
def test_native_windows_http_snippet(count: int, windows_shell: Path, tmp_path: Path) -> None:
    # Given real sockets owned by this process, When running the docs verbatim
    # except for the port knob, Then TCP and CIM results identify this owner.
    with _listeners(count) as (port, addresses):
        observation, artifact = _run_snippet(
            windows_shell,
            _with_port(_documented_snippet(), port),
            port,
            addresses,
            tmp_path,
            str(count),
        )
        _assert_observation(observation, port, addresses)
        _record_oracle(artifact, "passed")


@_WINDOWS_ONLY
def test_native_windows_http_snippet_rejects_missing_process_query(
    windows_shell: Path, tmp_path: Path
) -> None:
    # Given the same real listener, When the process-query pipeline is removed,
    # Then the behavioral oracle rejects the otherwise successful shell run.
    snippet, marker, _tail = _documented_snippet().partition(
        "$listeners | Select-Object -ExpandProperty OwningProcess"
    )
    assert marker, "Mutation must remove the documented process-query pipeline"
    with _listeners(1) as (port, addresses):
        observation, artifact = _run_snippet(
            windows_shell,
            _with_port(snippet, port),
            port,
            addresses,
            tmp_path,
            "missing-process-query",
        )
        with pytest.raises(AssertionError, match="process row"):
            _assert_observation(observation, port, addresses)
        _record_oracle(artifact, "mutant_rejected")
