"""Run an inner check and write its status as a bounded wrapper receipt."""

from __future__ import annotations

import argparse
import configparser
from contextlib import ExitStack
import json
from pathlib import Path
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


def _nonexecuting_pytest_mode(command: list[str], *, check_config: bool = True) -> bool:
    """Reject pytest's reporting/collection modes, including addopts overrides."""
    executable = Path(command[0]).name
    if executable in {"pytest", "py.test"}:
        options = command[1:]
    elif "-m" in command and command[command.index("-m") + 1 :][:1] == ["pytest"]:
        options = command[command.index("-m") + 2 :]
    else:
        return False
    nonexecuting = {
        "--collect-only",
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
        "--setup-only",
        "--cache-show",
    }
    for index, option in enumerate(options):
        if option == "--":
            break
        if option.split("=", 1)[0] in nonexecuting:
            return True
        override = None
        if option in {"-o", "--override-ini"} and index + 1 < len(options):
            override = options[index + 1]
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
        path = Path(value)
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
