"""Run an inner check and write its status as a bounded wrapper receipt."""

from __future__ import annotations

import argparse
import configparser
from contextlib import ExitStack
import json
from pathlib import Path
import re
import shlex
import subprocess
import tomllib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.core.filesystem_capability import NoFollowDirectoryChain

if "_WRAPPER_DIRECTORY_OPENER" not in globals():
    from ouroboros.core.filesystem_capability import (
        open_nofollow_directory_chain as _WRAPPER_DIRECTORY_OPENER,
    )

if "_WRAPPER_VERIFY_SHELL" not in globals():
    from ouroboros.orchestrator import verify_shell as _WRAPPER_VERIFY_SHELL


def _output_parent(path: str) -> NoFollowDirectoryChain:
    target = Path(path)
    if (
        target.is_absolute()
        or not target.parts
        or any(part in {"", ".", ".."} for part in target.parts)
    ):
        raise ValueError("wrapper output must be a contained relative path")
    return _WRAPPER_DIRECTORY_OPENER(Path.cwd(), relative_components=target.parts[:-1])


def _new_output(path: str, data: bytes, parent: NoFollowDirectoryChain) -> None:
    if not parent.create_exclusive(Path(path).name, data):
        raise FileExistsError(path)


def _normalize_pytest_options(options: list[str]) -> list[str]:
    """Expand built-in short flags without reinterpreting option values."""
    normalized = []
    arguments = iter(options)
    for option in arguments:
        if option == "--":
            normalized.extend([option, *arguments])
            break
        if option != "-" and option.startswith("-") and not option.startswith("--"):
            remaining = option[1:]
            while remaining[:1] in {"x", "V", "h", "v", "q", "l", "s"}:
                normalized.append("-" + remaining[0])
                remaining = remaining[1:]
            if not remaining:
                continue
            option = "-" + remaining
            if remaining[0] in {"o", "c"} and len(remaining) > 1:
                value = remaining[1:]
                normalized.extend(["-" + remaining[0], value.removeprefix("=")])
                continue
        normalized.append(option)
        if option in {"-k", "-m", "-W", "-c", "-p", "-o", "-r", "--override-ini", "--config-file"}:
            value = next(arguments, None)
            if value is not None:
                normalized.append(value)
    return normalized


def _nonexecuting_pytest_mode(command: list[str], *, check_config: bool = True) -> bool:
    """Reject pytest's reporting/collection modes, including addopts overrides."""

    def executable_name(value: str) -> str:
        return Path(value).name.lower().removesuffix(".exe")

    def coverage_pytest_options(args: list[str]) -> list[str] | None:
        # Coverage options end at the target; never interpret target arguments
        # as launcher options. Unknown options are not guessed.
        flags = {"-a", "--append", "--branch", "-L", "--pylib", "-p", "--parallel-mode", "--timid"}
        valued = {
            "--concurrency",
            "--context",
            "--data-file",
            "--include",
            "--omit",
            "--save-signal",
            "--source",
            "--debug",
            "--rcfile",
        }
        is_module = False
        index = 0
        while index < len(args):
            value = args[index]
            if value in {"--h", "--he", "--hel", "--help"} or (
                re.fullmatch(r"-[apLmh]+", value) and "h" in value
            ):
                return ["--help"]
            if value == "--":
                index += 1
                break
            if value in {"-m", "--module"}:
                is_module = True
            elif value in flags:
                pass
            elif re.fullmatch(r"-[apLm]+", value):
                is_module = is_module or "m" in value
            elif value in valued:
                index += 1
                if index >= len(args):
                    return None
            elif "=" in value and value.split("=", 1)[0] in valued:
                pass
            elif value.startswith("-"):
                return None
            else:
                break
            index += 1
        if is_module and args[index : index + 1] == ["pytest"]:
            return args[index + 1 :]
        if not is_module and args[index:] and executable_name(args[index]) in {"pytest", "py.test"}:
            return args[index + 1 :]
        return None

    def python_module(args: list[str]) -> tuple[str, list[str]] | None:
        index = 0
        while index < len(args):
            value = args[index]
            if value == "-m" and index + 1 < len(args):
                return args[index + 1], args[index + 2 :]
            if value in {"-W", "-X", "--check-hash-based-pycs"}:
                index += 1
                if index >= len(args):
                    return None
            elif (
                value in {"-", "--"}
                or re.match(r"-[bBdEhiIOPqRsSuvVx]*c", value)
                or not value.startswith("-")
            ):
                return None
            # Other interpreter/launcher flags must not hide a later -m pytest.
            # Invalid flags still fail in the inner process; script, stdin and
            # -c arguments remain outside the module command.
            index += 1
        return None

    executable = executable_name(command[0])
    if executable in {"pytest", "py.test"}:
        options = command[1:]
    elif executable in {"uv", "uvx"} and any(
        executable_name(part) in {"pytest", "py.test"} for part in command[1:]
    ):
        index = next(
            index
            for index, part in enumerate(command)
            if executable_name(part) in {"pytest", "py.test"}
        )
        options = command[index + 1 :]
    elif re.fullmatch(r"coverage(?:-?\d+(?:\.\d+)*)?", executable) and command[1:2] == ["run"]:
        options = coverage_pytest_options(command[2:])
        if options is None:
            return False
    elif (
        re.fullmatch(r"(?:(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?|py)", executable)
        and (module_call := python_module(command[1:])) is not None
    ):
        module, module_args = module_call
        if module == "pytest":
            options = module_args
        elif module == "coverage" and module_args[:1] == ["run"]:
            options = coverage_pytest_options(module_args[1:])
            if options is None:
                return False
        else:
            return False
    elif re.fullmatch(r"(?:(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?|py)", executable):
        return False
    elif "-m" in command and command[command.index("-m") + 1 :][:1] == ["pytest"]:
        # Preserve existing handling for host launchers such as env and custom
        # Python aliases; their command contracts are not parsed here.
        options = command[command.index("-m") + 2 :]
    else:
        return False
    options = _normalize_pytest_options(options)
    nonexecuting = {
        "--collect-only",
        "--collectonly",
        "--co",
        "--help",
        "-h",
        "--version",
        "-V",
        "--fixtures",
        "--funcargs",
        "--fixtures-per-test",
        "--markers",
        "--setup-plan",
        "--setupplan",
        "--setup-only",
        "--setuponly",
        "--cache-show",
    }
    arguments = iter(options)
    for option in arguments:
        if option == "--":
            break
        # Argument files can hide options and config paths; do not infer their
        # execution mode or grant test authority from an opaque option source.
        if option.startswith("@"):
            return True
        if option.split("=", 1)[0] in nonexecuting:
            return True
        override = None
        if option in {"-k", "-m", "-W", "-c", "-p", "-r", "--config-file"}:
            next(arguments, None)
            continue
        if option in {"-o", "--override-ini"}:
            override = next(arguments, None)
        elif option.startswith("--override-ini="):
            override = option.split("=", 1)[1]
        elif option.startswith("-o"):
            override = option[2:]
        if override and override.partition("=")[0].strip() == "addopts":
            try:
                nested = shlex.split(override.partition("=")[2])
            except ValueError:
                return True
            if _nonexecuting_pytest_mode(["pytest", *nested], check_config=False):
                return True
    return check_config and _nonexecuting_pytest_config(options)


def _nonexecuting_pytest_config(options: list[str]) -> bool:
    # Inspect a conservative superset of pytest's configuration search. A config
    # in an ancestor of cwd or a supplied test path must not hide collection mode.
    names = (
        "pytest.toml",
        ".pytest.toml",
        "pytest.ini",
        ".pytest.ini",
        "pyproject.toml",
        "tox.ini",
        "setup.cfg",
    )
    configs: set[Path] = set()
    explicit_configs = []
    for index, option in enumerate(options):
        if option in {"-c", "--config-file"} and index + 1 < len(options):
            explicit_configs.append(options[index + 1])
        elif option.startswith("--config-file="):
            explicit_configs.append(option.split("=", 1)[1])
        elif option.startswith("-c"):
            explicit_configs.append(option[2:])
    configs.update(Path(value).resolve() for value in explicit_configs)
    for value in [str(Path.cwd()), *options]:
        path = Path(value.split("::", 1)[0])
        if not path.exists():
            continue
        path = path.resolve()
        if path.is_file():
            if path.name in names or path.suffix in {".ini", ".cfg", ".toml"}:
                configs.add(path)
            path = path.parent
        for directory in (path, *path.parents):
            configs.update(directory / name for name in names if (directory / name).is_file())
    for path in configs:
        try:
            if path.suffix == ".toml":
                data = tomllib.loads(path.read_text(encoding="utf-8"))
                settings = (
                    data.get("tool", {}).get("pytest", {})
                    if path.name == "pyproject.toml"
                    else data.get("pytest", {})
                )
                settings = settings.get("ini_options", settings)
                addopts = settings.get("addopts", "")
            else:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(path.read_text(encoding="utf-8"))
                section = "tool:pytest" if path.name == "setup.cfg" else "pytest"
                addopts = parser.get(section, "addopts", fallback="")
            nested = shlex.split(addopts) if isinstance(addopts, str) else addopts
            if not isinstance(nested, list) or not all(isinstance(item, str) for item in nested):
                return True
            if _nonexecuting_pytest_mode(["pytest", *nested], check_config=False):
                return True
        except (OSError, ValueError, configparser.Error, TypeError, AttributeError):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--stdout", required=True)
    parser.add_argument("--stderr", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or len({args.receipt, args.stdout, args.stderr}) != 3:
        parser.error("provide a command and distinct output paths")
    with ExitStack() as stack:
        parents = {}
        for path in (args.receipt, args.stdout, args.stderr):
            parent = _output_parent(path)
            stack.callback(parent.close)
            parents[path] = parent
        nonexecuting = _nonexecuting_pytest_mode(command)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                env=_WRAPPER_VERIFY_SHELL.sanitized_verify_environment(),
            )
            inner_status = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except OSError as exc:
            inner_status = 127
            stdout = b""
            stderr = str(exc).encode("utf-8", errors="replace")
        _new_output(args.stdout, stdout, parents[args.stdout])
        _new_output(args.stderr, stderr, parents[args.stderr])
        outer_status = inner_status
        if inner_status == 0 and nonexecuting:
            outer_status = 1
        receipt = {
            "schema_version": 1,
            "kind": "ouroboros.test-wrapper-receipt",
            "inner_command": shlex.join(command),
            "inner_exit_status": inner_status,
            "outer_exit_status": outer_status,
            "stdout_ref": args.stdout,
            "stderr_ref": args.stderr,
            "test_result": "passed" if outer_status == 0 else "failed",
        }
        _new_output(
            args.receipt, json.dumps(receipt, sort_keys=True).encode("utf-8"), parents[args.receipt]
        )
        return outer_status


if __name__ == "__main__":
    raise SystemExit(main())
