"""Codex MCP setup helpers and Windows scheduled-task provisioning."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib import error as urlerror
from urllib import request as urlrequest
import uuid
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

_TASK_NAME = "Ouroboros MCP HTTP"
_RUNNER_NAME = "ouroboros-mcp-http.ps1"
_READINESS_TIMEOUT_SECONDS = 10.0
_READINESS_POLL_INTERVAL_SECONDS = 0.1
_RUNNER_MARKER = "# Ouroboros MCP HTTP generated runner"
_RUNNER_GENERATION_PREFIX = "# Ouroboros MCP HTTP generation: "
_TASK_GENERATION_PREFIX = "Ouroboros MCP HTTP generation: "
_CODEX_MCP_HTTP_SECTION_TEMPLATE = """# Ouroboros MCP hookup for Codex CLI.
# Keep Ouroboros runtime settings and per-role model overrides in
# ~/.ouroboros/config.yaml (for example: clarification.default_model,
# llm.qa_model, evaluation.semantic_model, consensus.*).
# This file is only for the Codex MCP/env registration block.

[mcp_servers.ouroboros]
url = "http://127.0.0.1:8765/mcp"
enabled = true
"""


def render_codex_mcp_http_section() -> str:
    """Render the managed local streamable-HTTP Codex MCP block."""
    return _CODEX_MCP_HTTP_SECTION_TEMPLATE


def has_active_plugin_scoped_codex_mcp(data: dict[str, object]) -> bool:
    """Return whether a plugin-scoped Ouroboros MCP could conflict with a global one."""
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return False
    plugin = plugins.get("ouroboros@ouroboros")
    if plugin is None:
        return False
    if not isinstance(plugin, dict):
        return True
    if plugin.get("enabled") is False:
        return False
    mcp_servers = plugin.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        return True
    mcp_entry = mcp_servers.get("ouroboros")
    if not isinstance(mcp_entry, dict):
        return True
    return mcp_entry.get("enabled") is not False


def plugin_scoped_codex_mcp_error() -> str:
    """Explain how to avoid duplicate Ouroboros MCP registrations."""
    return (
        "Active plugin-scoped Ouroboros MCP configuration prevents adding a global "
        'Ouroboros MCP server. Disable plugins."ouroboros@ouroboros" or its '
        "mcp_servers.ouroboros entry before rerunning setup."
    )


def _powershell_literal(value: str) -> str:
    """Quote one value for a PowerShell single-quoted string literal."""
    return "'" + value.replace("'", "''") + "'"


def _windows_command_line_argument(value: str) -> str:
    """Quote one argument according to the Windows command-line parsing rules."""
    escaped: list[str] = ['"']
    backslashes = 0
    for character in value:
        if character == "\\":
            backslashes += 1
        elif character == '"':
            escaped.append("\\" * (backslashes * 2 + 1))
            escaped.append(character)
            backslashes = 0
        else:
            escaped.append("\\" * backslashes)
            escaped.append(character)
            backslashes = 0
    escaped.append("\\" * (backslashes * 2))
    escaped.append('"')
    return "".join(escaped)


def _current_windows_identity() -> str | None:
    """Return the current interactive Windows account for a task XML UserId."""
    try:
        result = subprocess.run(["whoami"], capture_output=True, text=True, check=False)
    except OSError:
        return None
    identity = result.stdout.strip() if result.returncode == 0 else ""
    return identity or None


def _append_missing_mcp_option(arguments: list[str], option: str, value: str) -> list[str]:
    """Append an MCP option only when the resolved launcher did not provide it."""
    if option in arguments:
        return arguments
    return [*arguments, option, value]


def _task_xml(schtasks: str) -> str | None:
    """Return the current task XML, if the reserved task exists."""
    result = subprocess.run(
        [schtasks, "/Query", "/TN", _TASK_NAME, "/XML"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def _is_generated_runner(contents: bytes) -> bool:
    """Return whether contents carry setup's exact runner ownership marker."""
    try:
        lines = contents.decode("utf-8").splitlines()
        return bool(lines) and lines[0] == _RUNNER_MARKER
    except UnicodeDecodeError:
        return False


def _task_arguments(runner_path: Path) -> str:
    """Return the exact PowerShell action arguments for the generated runner."""
    return (
        "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "
        + _windows_command_line_argument(str(runner_path))
    )


def _is_setup_managed_task(task_xml: str, runner_path: Path) -> bool:
    """Return whether a task XML has exactly setup's generated PowerShell action."""
    try:
        root = ElementTree.fromstring(task_xml)
    except ElementTree.ParseError:
        return False

    actions = [action for action in root.iter() if action.tag.rsplit("}", 1)[-1] == "Exec"]
    if len(actions) != 1:
        return False
    action = actions[0]
    children = {child.tag.rsplit("}", 1)[-1]: child.text for child in action}
    return children.get("Command") == "powershell.exe" and children.get(
        "Arguments"
    ) == _task_arguments(runner_path)


def _runner_generation(contents: bytes) -> tuple[bool, str | None]:
    """Return whether a runner claim exists and its valid non-empty value."""
    if not _is_generated_runner(contents):
        return False, None
    try:
        lines = contents.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return True, None
    if len(lines) < 2:
        return False, None
    if not lines[1].startswith(_RUNNER_GENERATION_PREFIX):
        return True, None
    return True, lines[1][len(_RUNNER_GENERATION_PREFIX) :] or None


def _task_generation(task_xml: str) -> tuple[bool, str | None]:
    """Return whether a task claim exists and its valid non-empty value."""
    try:
        root = ElementTree.fromstring(task_xml)
    except ElementTree.ParseError:
        return True, None
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Description" and element.text:
            if element.text.startswith(_TASK_GENERATION_PREFIX):
                return True, element.text[len(_TASK_GENERATION_PREFIX) :] or None
    return False, None


def _read_runner(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _unchanged(
    schtasks: str, runner_path: Path, task_xml: str | None, runner: bytes | None
) -> bool:
    """Check that neither observed artifact changed before a destructive operation."""
    return _task_xml(schtasks) == task_xml and _read_runner(runner_path) == runner


def _owns_generation(schtasks: str, runner_path: Path, generation: str, runner: bytes) -> bool:
    """Check the exact task and runner created by this invocation still exist."""
    task_xml = _task_xml(schtasks)
    return (
        task_xml is not None
        and _is_setup_managed_task(task_xml, runner_path)
        and _task_generation(task_xml) == (True, generation)
        and _runner_generation(_read_runner(runner_path) or b"") == (True, generation)
        and _read_runner(runner_path) == runner
    )


def _is_coherent_managed_pair(
    task_xml: str | None, runner: bytes | None, runner_path: Path
) -> bool:
    """Return whether the reserved artifacts have one unambiguous ownership claim."""
    if task_xml is None and runner is None:
        return True
    if task_xml is None or runner is None:
        return False
    if not _is_setup_managed_task(task_xml, runner_path) or not _is_generated_runner(runner):
        return False
    task_claimed, task_generation = _task_generation(task_xml)
    runner_claimed, runner_generation = _runner_generation(runner)
    if not task_claimed and not runner_claimed:
        return True
    return (
        task_claimed
        and runner_generation is not None
        and task_generation is not None
        and task_generation == runner_generation
    )


def _restore_prior_task(
    schtasks: str,
    prior_task_xml: str | None,
    prior_running: bool,
    runner_path: Path,
    prior_runner: bytes | None,
    generated_runner: bytes,
) -> str | None:
    """Restore a prior generation after a failure before replacement registration."""
    try:
        current_runner = _read_runner(runner_path)
        if _task_xml(schtasks) != prior_task_xml or current_runner not in {
            prior_runner,
            generated_runner,
        }:
            return "Task or runner changed ownership before rollback."
        if current_runner == generated_runner:
            _restore_file(runner_path, prior_runner)
        if prior_running:
            if (
                subprocess.run(
                    [schtasks, "/Run", "/TN", _TASK_NAME],
                    capture_output=True,
                    text=True,
                    check=False,
                ).returncode
                != 0
            ):
                return "Could not restart the previous scheduled task."
            if (
                not _scheduled_task_is_running()
                or not _wait_for_http_readiness()
                or not _unchanged(schtasks, runner_path, prior_task_xml, prior_runner)
            ):
                return "The restored scheduled task did not become ready."
    except OSError as exc:
        return f"Could not restore the previous scheduled task: {_bounded_error(str(exc))}"
    return None


def _restore_file(path: Path, contents: bytes | None) -> None:
    if contents is None:
        path.unlink(missing_ok=True)
    else:
        path.write_bytes(contents)


def _restore_absent_file(path: Path, contents: bytes | None) -> bool:
    """Restore a missing file without overwriting a concurrent owner."""
    if contents is None:
        return not path.exists()
    try:
        with path.open("xb") as restored:
            restored.write(contents)
    except FileExistsError:
        return False
    return True


def _create_private_task_xml(config_dir: Path, contents: str) -> Path:
    """Create a uniquely named task XML that belongs only to this invocation."""
    task_xml_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-16",
            dir=config_dir,
            prefix="ouroboros-mcp-http-task-",
            suffix=".xml",
            delete=False,
        ) as task_xml:
            task_xml_path = Path(task_xml.name)
            task_xml.write(contents)
        assert task_xml_path is not None
        return task_xml_path
    except OSError:
        if task_xml_path is not None:
            task_xml_path.unlink(missing_ok=True)
        raise


def _bounded_error(message: str, limit: int = 240) -> str:
    """Keep user-facing transaction errors actionable without unbounded command output."""
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _scheduled_task_is_running() -> bool:
    """Return task state through PowerShell's numeric ScheduledTasks enum."""
    state_query = (
        f"$task = Get-ScheduledTask -TaskName {_powershell_literal(_TASK_NAME)} "
        "-ErrorAction Stop; if ([int]$task.State -eq 4) { 'running' } else { 'stopped' }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", state_query],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise OSError(f"Could not query the previous scheduled task state: {exc}") from exc
    state = (result.stdout or "").strip().lower()
    if result.returncode != 0 or state not in {"running", "stopped"}:
        raise OSError("Could not query the previous scheduled task state.")
    return state == "running"


def _mcp_initialize_response(response: object, request_id: str) -> bool:
    """Validate an initialize response received as JSON or streamable-HTTP SSE."""
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    headers = getattr(response, "headers", None)
    content_type = headers.get("Content-Type", "") if headers is not None else ""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if status != 200 or media_type not in {"application/json", "text/event-stream"}:
        return False
    body = response.read().decode("utf-8")
    if media_type == "text/event-stream":
        payloads = [
            "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            for event in body.replace("\r\n", "\n").split("\n\n")
        ]
    else:
        payloads = [body]
    for payload in payloads:
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        result = message.get("result")
        server_info = result.get("serverInfo") if isinstance(result, dict) else None
        if (
            message.get("id") == request_id
            and isinstance(server_info, dict)
            and server_info.get("name") == "ouroboros-mcp"
            and result.get("protocolVersion") == "2025-03-26"
        ):
            return True
    return False


def _wait_for_http_readiness() -> bool:
    """Wait for the expected Ouroboros MCP initialize response."""
    request_id = "ouroboros-readiness"
    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ouroboros-setup", "version": "1"},
            },
        }
    ).encode()
    deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = None
        try:
            response = urlrequest.urlopen(
                urlrequest.Request(
                    "http://127.0.0.1:8765/mcp",
                    data=initialize,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                    method="POST",
                ),
                timeout=1,
            )
            if _mcp_initialize_response(response, request_id):
                return True
        except (OSError, urlerror.URLError, urlerror.HTTPError, UnicodeDecodeError):
            pass
        finally:
            if response is not None:
                response.close()
        time.sleep(_READINESS_POLL_INTERVAL_SECONDS)
    return False


def _rollback_task(
    schtasks: str,
    task_xml_path: Path,
    prior_task_xml: str | None,
    prior_task_was_running: bool,
    generation: str,
    runner_path: Path,
    generated_runner: bytes,
    prior_runner: bytes | None,
) -> str | None:
    """Restore only the generation created by this invocation."""
    try:
        if not _owns_generation(schtasks, runner_path, generation, generated_runner):
            return "Replacement task or runner changed ownership before rollback."
        if _scheduled_task_is_running():
            ended = subprocess.run(
                [schtasks, "/End", "/TN", _TASK_NAME], capture_output=True, text=True, check=False
            )
            if ended.returncode != 0:
                return "Could not end the replacement scheduled task during rollback."
        if not _owns_generation(schtasks, runner_path, generation, generated_runner):
            return "Replacement task or runner changed ownership during rollback."
        if prior_task_xml is None:
            deleted = subprocess.run(
                [schtasks, "/Delete", "/TN", _TASK_NAME, "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            if deleted.returncode != 0:
                return "Could not remove the replacement scheduled task during rollback."
            if _task_xml(schtasks) is not None or _read_runner(runner_path) != generated_runner:
                return "Replacement ownership changed before runner rollback."
            _restore_file(runner_path, prior_runner)
            return None
        task_xml_path.write_text(prior_task_xml, encoding="utf-16")
        if not _owns_generation(schtasks, runner_path, generation, generated_runner):
            return "Replacement task or runner changed ownership before task restoration."
        created = subprocess.run(
            [schtasks, "/Create", "/TN", _TASK_NAME, "/XML", str(task_xml_path), "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            return "Could not restore the previous scheduled task."
        if _task_xml(schtasks) != prior_task_xml or _read_runner(runner_path) != generated_runner:
            return "Replacement ownership changed before runner rollback restoration."
        _restore_file(runner_path, prior_runner)
        if prior_task_was_running:
            started = subprocess.run(
                [schtasks, "/Run", "/TN", _TASK_NAME], capture_output=True, text=True, check=False
            )
            if started.returncode != 0:
                return "Could not restart the previous scheduled task."
            if (
                not _scheduled_task_is_running()
                or not _wait_for_http_readiness()
                or not _unchanged(schtasks, runner_path, prior_task_xml, prior_runner)
            ):
                return "The restored scheduled task did not become ready."
    except OSError as exc:
        return f"Could not restore the previous scheduled task: {_bounded_error(str(exc))}"
    return None


def provision_windows_codex_mcp_http(
    config_dir: Path, launcher: tuple[str, list[str]]
) -> str | None:
    """Install, start, and verify a claimed per-user HTTP MCP task."""
    schtasks = shutil.which("schtasks")
    identity = _current_windows_identity()
    if schtasks is None or identity is None:
        return "Could not find the Ouroboros MCP launcher, Windows Task Scheduler, or current user."
    command, server_args = launcher[0], list(launcher[1])
    for option, value in (
        ("--runtime", "codex"),
        ("--llm-backend", "codex"),
        ("--transport", "streamable-http"),
        ("--host", "127.0.0.1"),
        ("--port", "8765"),
    ):
        server_args = _append_missing_mcp_option(server_args, option, value)
    runner_path = config_dir / _RUNNER_NAME
    generation = uuid.uuid4().hex
    runner_contents = "\n".join(
        [
            _RUNNER_MARKER,
            _RUNNER_GENERATION_PREFIX + generation,
            "$ErrorActionPreference = 'Stop'",
            "$arguments = @(",
            *[
                f"    {_powershell_literal(arg)}{',' if i < len(server_args) - 1 else ''}"
                for i, arg in enumerate(server_args)
            ],
            ")",
            f"& {_powershell_literal(command)} @arguments",
            "exit $LASTEXITCODE",
            "",
        ]
    )
    generated_runner = runner_contents.encode()
    task_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{_TASK_GENERATION_PREFIX}{generation}</Description></RegistrationInfo>
  <Triggers><LogonTrigger><UserId>{xml_escape(identity)}</UserId><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{xml_escape(identity)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure><AllowStartOnDemand>true</AllowStartOnDemand><Hidden>true</Hidden><Enabled>true</Enabled></Settings>
  <Actions Context="Author"><Exec><Command>powershell.exe</Command><Arguments>{xml_escape(_task_arguments(runner_path))}</Arguments></Exec></Actions>
</Task>
"""
    task_xml_path: Path | None = None
    prior_runner: bytes | None = None
    prior_task_xml: str | None = None
    prior_running = False
    replacement_created = False
    prior_ended = False
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        prior_runner, prior_task_xml = _read_runner(runner_path), _task_xml(schtasks)
        if prior_task_xml is not None and not _is_setup_managed_task(prior_task_xml, runner_path):
            return "Refusing to overwrite a reserved task not managed by Ouroboros MCP HTTP setup."
        if prior_runner is not None and not _is_generated_runner(prior_runner):
            return "Refusing to overwrite a runner not managed by Ouroboros MCP HTTP setup."
        if not _is_coherent_managed_pair(prior_task_xml, prior_runner, runner_path):
            return (
                "Refusing to modify task and runner with mismatched Ouroboros MCP ownership claims."
            )
        if prior_task_xml is None and _wait_for_http_readiness():
            return (
                "Refusing to provision while an existing Ouroboros MCP HTTP endpoint is listening."
            )
        task_xml_path = _create_private_task_xml(config_dir, task_xml)
        if prior_task_xml is not None:
            prior_running = _scheduled_task_is_running()
            if prior_running:
                if not _unchanged(schtasks, runner_path, prior_task_xml, prior_runner):
                    raise OSError("Scheduled task or runner changed ownership before replacement.")
                if (
                    subprocess.run(
                        [schtasks, "/End", "/TN", _TASK_NAME],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).returncode
                    != 0
                ):
                    raise OSError("Could not end the previous scheduled task.")
                prior_ended = True
                if _wait_for_http_readiness():
                    raise OSError(
                        "Refusing to provision while an existing Ouroboros MCP HTTP endpoint is listening."
                    )
            elif _wait_for_http_readiness():
                return "Refusing to provision while an existing Ouroboros MCP HTTP endpoint is listening."
        if not _unchanged(schtasks, runner_path, prior_task_xml, prior_runner):
            raise OSError("Scheduled task or runner changed ownership before replacement.")
        runner_path.write_bytes(generated_runner)
        if _task_xml(schtasks) != prior_task_xml or _read_runner(runner_path) != generated_runner:
            raise OSError("Scheduled task or runner changed ownership before task replacement.")
        if (
            subprocess.run(
                [schtasks, "/Create", "/TN", _TASK_NAME, "/XML", str(task_xml_path), "/F"],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            != 0
        ):
            raise OSError("Could not provision the Ouroboros MCP HTTP scheduled task.")
        replacement_created = True
        if not _owns_generation(schtasks, runner_path, generation, generated_runner):
            raise OSError("Could not establish ownership of the new scheduled task generation.")
        if (
            subprocess.run(
                [schtasks, "/Run", "/TN", _TASK_NAME], capture_output=True, text=True, check=False
            ).returncode
            != 0
        ):
            raise OSError("Could not start the Ouroboros MCP HTTP scheduled task.")
        if (
            not _wait_for_http_readiness()
            or not _owns_generation(schtasks, runner_path, generation, generated_runner)
            or not _scheduled_task_is_running()
        ):
            raise OSError("The Ouroboros MCP HTTP scheduled task did not become ready.")
    except OSError as exc:
        if replacement_created:
            assert task_xml_path is not None
            rollback_error = _rollback_task(
                schtasks,
                task_xml_path,
                prior_task_xml,
                prior_running,
                generation,
                runner_path,
                generated_runner,
                prior_runner,
            )
            if rollback_error:
                return (
                    f"{_bounded_error(str(exc))} Rollback failed: {_bounded_error(rollback_error)}"
                )
        elif prior_ended:
            rollback_error = _restore_prior_task(
                schtasks, prior_task_xml, prior_running, runner_path, prior_runner, generated_runner
            )
            if rollback_error:
                return (
                    f"{_bounded_error(str(exc))} Rollback failed: {_bounded_error(rollback_error)}"
                )
        elif (
            _task_xml(schtasks) == prior_task_xml and _read_runner(runner_path) == generated_runner
        ):
            _restore_file(runner_path, prior_runner)
        return _bounded_error(str(exc))
    finally:
        if task_xml_path is not None:
            task_xml_path.unlink(missing_ok=True)
    return None


def finalize_windows_codex_mcp_service(
    config_dir: Path,
    codex_config: Path,
    *,
    is_setup_managed_entry: Callable[[dict[str, object], str], bool],
    resolve_launcher: Callable[[], tuple[str, list[str]] | None],
) -> str | None:
    """Reconcile the Windows HTTP task with the final managed Codex MCP entry."""
    if sys.platform != "win32":
        return None
    try:
        raw = codex_config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"Could not inspect final Codex MCP config: {exc}"
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        return f"Could not inspect final Codex MCP config: {exc}"
    mcp_servers = parsed.get("mcp_servers")
    entry = mcp_servers.get("ouroboros") if isinstance(mcp_servers, dict) else None
    if not isinstance(entry, dict) or not is_setup_managed_entry(entry, raw):
        return None
    if "url" in entry:
        launcher = resolve_launcher()
        if launcher is None:
            return (
                "Could not find the Ouroboros MCP launcher, Windows Task Scheduler, "
                "or current user."
            )
        return provision_windows_codex_mcp_http(config_dir, launcher)
    return remove_windows_codex_mcp_http(config_dir)


def remove_windows_codex_mcp_http(config_dir: Path) -> str | None:
    """Transactionally remove only unchanged setup-owned task artifacts."""
    schtasks = shutil.which("schtasks")
    if schtasks is None:
        return "Could not find Windows Task Scheduler."
    runner_path = config_dir / _RUNNER_NAME
    task_xml_path: Path | None = None
    prior_task_xml: str | None = None
    runner: bytes | None = None
    prior_running = False
    task_deleted = False
    try:
        prior_task_xml, runner = _task_xml(schtasks), _read_runner(runner_path)
        managed_task = prior_task_xml is not None and _is_setup_managed_task(
            prior_task_xml, runner_path
        )
        managed_runner = runner is not None and _is_generated_runner(runner)
        if not managed_task and not managed_runner:
            return None
        if managed_task != managed_runner:
            return (
                "Refusing to remove task and runner with mismatched Ouroboros MCP ownership claims."
            )
        if not _is_coherent_managed_pair(prior_task_xml, runner, runner_path):
            return (
                "Refusing to remove task and runner with mismatched Ouroboros MCP ownership claims."
            )
        if managed_task:
            prior_running = _scheduled_task_is_running()
            if not _unchanged(schtasks, runner_path, prior_task_xml, runner):
                return "Refusing to remove task or runner whose ownership changed."
            if prior_running:
                ended = subprocess.run(
                    [schtasks, "/End", "/TN", _TASK_NAME],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if ended.returncode != 0:
                    return (
                        "Could not end the Ouroboros MCP HTTP scheduled task; nothing was removed."
                    )
            if not _unchanged(schtasks, runner_path, prior_task_xml, runner):
                return "Refusing to remove task or runner whose ownership changed during removal."
            deleted = subprocess.run(
                [schtasks, "/Delete", "/TN", _TASK_NAME, "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            if deleted.returncode != 0:
                current_task_xml = _task_xml(schtasks)
                current_runner = _read_runner(runner_path)
                if current_task_xml is None and current_runner == runner:
                    assert prior_task_xml is not None
                    task_xml_path = _create_private_task_xml(config_dir, prior_task_xml)
                    restored = subprocess.run(
                        [schtasks, "/Create", "/TN", _TASK_NAME, "/XML", str(task_xml_path)],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if restored.returncode != 0 or _task_xml(schtasks) != prior_task_xml:
                        return "Could not remove the Ouroboros MCP HTTP scheduled task. Rollback failed to restore task."
                    current_task_xml = prior_task_xml
                if current_task_xml != prior_task_xml or current_runner != runner:
                    return "Could not remove the Ouroboros MCP HTTP scheduled task. Rollback failed: task or runner ownership changed."
                if prior_running:
                    if not _unchanged(schtasks, runner_path, prior_task_xml, runner):
                        return "Could not remove the Ouroboros MCP HTTP scheduled task. Rollback failed: task or runner ownership changed."
                    restarted = subprocess.run(
                        [schtasks, "/Run", "/TN", _TASK_NAME],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if (
                        restarted.returncode != 0
                        or not _scheduled_task_is_running()
                        or not _wait_for_http_readiness()
                        or not _unchanged(schtasks, runner_path, prior_task_xml, runner)
                    ):
                        return "Could not remove the Ouroboros MCP HTTP scheduled task. Rollback failed to restore the running task."
                return "Could not remove the Ouroboros MCP HTTP scheduled task; previous state was restored."
            task_deleted = True
        if managed_runner:
            expected_task = None if managed_task else prior_task_xml
            if _task_xml(schtasks) != expected_task or _read_runner(runner_path) != runner:
                raise OSError("Task or runner changed ownership before runner removal.")
            runner_path.unlink()
    except OSError as exc:
        if task_deleted:
            try:
                if _task_xml(schtasks) is not None or _read_runner(runner_path) not in {
                    runner,
                    None,
                }:
                    return f"Could not remove the Ouroboros MCP HTTP scheduled task: {exc}. Rollback failed: task or runner ownership changed."
                if _read_runner(runner_path) is None and not _restore_absent_file(
                    runner_path, runner
                ):
                    return f"Could not remove the Ouroboros MCP HTTP scheduled task: {exc}. Rollback failed: runner ownership changed."
                if _task_xml(schtasks) is not None or _read_runner(runner_path) != runner:
                    return f"Could not remove the Ouroboros MCP HTTP scheduled task: {exc}. Rollback failed: task or runner ownership changed."
                assert prior_task_xml is not None
                task_xml_path = _create_private_task_xml(config_dir, prior_task_xml)
                restored = subprocess.run(
                    [schtasks, "/Create", "/TN", _TASK_NAME, "/XML", str(task_xml_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if restored.returncode != 0 or not _unchanged(
                    schtasks, runner_path, prior_task_xml, runner
                ):
                    return f"Could not remove the Ouroboros MCP HTTP scheduled task: {exc}. Rollback failed to restore task."
                if prior_running:
                    restarted = subprocess.run(
                        [schtasks, "/Run", "/TN", _TASK_NAME],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if (
                        restarted.returncode != 0
                        or not _scheduled_task_is_running()
                        or not _wait_for_http_readiness()
                        or not _unchanged(schtasks, runner_path, prior_task_xml, runner)
                    ):
                        return f"Could not remove the Ouroboros MCP HTTP scheduled task: {exc}. Rollback failed to restore running task."
            except OSError as rollback_exc:
                return f"Could not remove the Ouroboros MCP HTTP scheduled task: {exc}. Rollback failed: {rollback_exc}"
        return f"Could not remove the Ouroboros MCP HTTP scheduled task: {exc}"
    finally:
        if task_xml_path is not None:
            task_xml_path.unlink(missing_ok=True)
    return None
