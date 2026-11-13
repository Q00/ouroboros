"""Report the welcome skill's completion marker and runtime readiness.

The welcome skill feeds this file to ``ouroboros_python`` on stdin, the same
``python -`` invocation its inline heredocs used::

    ouroboros_python - precheck "$HOME" "${CODEX_HOME:-$HOME/.codex}" \
        "${SETUP_READY:-}" "${CODEX_READY:-}" "${LEGACY_CODEX_MODEL_MIGRATION_REQUIRED:-}" \
        < "$WELCOME_DIR/scripts/welcome_state.py"
    ouroboros_python - setup-gate "$HOME" < "$WELCOME_DIR/scripts/welcome_state.py"
    ouroboros_python - codex-gate "$HOME" "${CODEX_HOME:-$HOME/.codex}" < "$WELCOME_DIR/scripts/welcome_state.py"

``precheck`` prints ``completed=<marker> setup=<true|> codex=<true|> migration=<true|>``,
``setup-gate`` prints ``SETUP_READY`` or ``SETUP_REQUIRED``, and ``codex-gate``
prints ``CODEX_READY`` or ``CODEX_SETUP_REQUIRED``.

Inputs keep the source they had in the skill's inline programs. What the shell
expanded (``$HOME``, ``${CODEX_HOME:-$HOME/.codex}``, the ``${SETUP_READY:-}``
fields) arrives as arguments, so a shell variable that is set but not exported
still counts. What those programs read from their own environment (``~`` in
``expanduser``, ``OUROBOROS_CODEX_CLI_PATH``, ``PATH`` for bare command names)
is still read from the environment.

Each check used to be its own ``python -`` process. An exception that escaped
one printed its traceback and failed that check without stopping the others,
so every check runs isolated here and imports what it used to import. The
checks only read files, and every mode except a usage error exits 0.
"""

from __future__ import annotations

import sys

_USAGE = (
    "usage: welcome_state.py precheck HOME CODEX_HOME_DIR SETUP_READY CODEX_READY "
    "LEGACY_CODEX_MODEL_MIGRATION_REQUIRED\n"
    "       welcome_state.py setup-gate HOME\n"
    "       welcome_state.py codex-gate HOME CODEX_HOME_DIR"
)


def _import_yaml():
    try:
        import yaml
    except ModuleNotFoundError:
        return None
    return yaml


def _scalar(raw: str) -> str:
    return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")


def _load_yaml(path: str, yaml) -> dict:
    from pathlib import Path

    source = Path(path).read_text(encoding="utf-8")
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}
    parsed: dict[str, dict[str, str]] = {}
    section = None
    for line in source.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        key = key.strip("'\"")
        if not line[0].isspace():
            section = key
            parsed[section] = {}
            flow = value.strip().split(" #", 1)[0].strip()
            if flow.startswith("{") and flow.endswith("}"):
                for part in flow[1:-1].split(","):
                    field, separator, field_value = part.partition(":")
                    if separator:
                        parsed[section][field.strip().strip("'\"")] = _scalar(field_value)
        elif section is not None:
            parsed[section][key] = _scalar(value)
    return parsed


def welcome_completed() -> str:
    """Return the completion marker; legacy ``welcomeShown: true`` also counts."""
    import codecs
    import json
    import os

    try:
        prefs = json.load(open(os.path.expanduser("~/.ouroboros/prefs.json"), encoding="utf-8"))  # noqa: SIM115
    except Exception:
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    marker = str(
        prefs.get("welcomeCompleted")
        or ("legacy-welcomeShown" if prefs.get("welcomeShown") else "")
    )
    # The marker used to be printed by its own process and captured with
    # $(...): a value stdout's encoder rejects failed that process, and
    # command substitution dropped trailing newlines.
    codecs.getincrementalencoder(sys.stdout.encoding)(sys.stdout.errors).encode(marker + "\n")
    return marker.rstrip("\n")


def setup_ready(home: str) -> bool:
    """Claude Code: both backends name Claude in ``$HOME/.ouroboros/config.yaml``."""
    yaml = _import_yaml()
    try:
        config = _load_yaml(f"{home}/.ouroboros/config.yaml", yaml)
    except (OSError, ValueError):
        return False
    orchestrator, llm = config.get("orchestrator"), config.get("llm")
    return (
        isinstance(orchestrator, dict)
        and orchestrator.get("runtime_backend") in {"claude", "claude_mcp"}
        and isinstance(llm, dict)
        and llm.get("backend") == "claude"
    )


def _codex_home_dir(home: str, codex_home_dir: str) -> str:
    # The shell expands CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"; these are
    # the "~" and "~/"* arms of the case statement that followed it.
    if codex_home_dir == "~":
        return home
    if codex_home_dir.startswith("~/"):
        return f"{home}/{codex_home_dir[2:]}"
    return codex_home_dir


def codex_ready(home: str, codex_home_dir: str) -> bool:
    """Codex: both backends, a runnable CLI, and a usable ``mcp_servers.ouroboros``."""
    import os
    from pathlib import Path
    import shutil
    import tomllib

    yaml = _import_yaml()

    def runnable(candidate: str) -> bool:
        value = candidate.strip()
        if not value:
            return False
        if "/" not in value:
            return shutil.which(value) is not None
        path = Path(value).expanduser()
        return path.is_file() and os.access(path, os.X_OK)

    def mcp_ready(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        command, url = entry.get("command"), entry.get("url")
        if isinstance(command, str) and command.strip():
            return runnable(command)
        return isinstance(url, str) and bool(url.strip())

    try:
        config = _load_yaml(f"{home}/.ouroboros/config.yaml", yaml)
        codex_config = tomllib.loads(
            Path(f"{_codex_home_dir(home, codex_home_dir)}/config.toml").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    orchestrator, llm = config.get("orchestrator"), config.get("llm")
    servers = codex_config.get("mcp_servers")
    cli_path = os.environ.get("OUROBOROS_CODEX_CLI_PATH")
    if not cli_path and isinstance(orchestrator, dict):
        cli_path = orchestrator.get("codex_cli_path")
    if not isinstance(cli_path, str) or not cli_path.strip():
        cli_path = "codex"
    return (
        isinstance(orchestrator, dict)
        and orchestrator.get("runtime_backend") == "codex"
        and isinstance(llm, dict)
        and llm.get("backend") == "codex"
        and runnable(cli_path)
        and mcp_ready(servers.get("ouroboros") if isinstance(servers, dict) else None)
    )


def legacy_codex_model_migration_required(home: str) -> bool:
    """All four stage models still pinned to ``gpt-5``, or an interrupted migration."""
    import json
    from pathlib import Path

    config_path = Path(f"{home}/.ouroboros/config.yaml")
    prefs_path = Path(f"{home}/.ouroboros/prefs.json")

    def yaml_mapping(source: str) -> dict[str, dict[str, str]]:
        parsed: dict[str, dict[str, str]] = {}
        section: str | None = None
        for raw_line in source.splitlines():
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip())
            key, separator, raw_value = raw_line.strip().partition(":")
            if not separator:
                continue
            value = raw_value.strip().split(" #", 1)[0].strip().strip("'\"")
            if indent == 0:
                section = key.strip("'\"")
                parsed.setdefault(section, {})
            elif section is not None:
                parsed[section][key.strip("'\"")] = value
        return parsed

    try:
        config = yaml_mapping(config_path.read_text(encoding="utf-8"))
    except OSError:
        return False
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}

    stage_values = (
        config.get("clarification", {}).get("default_model"),
        config.get("execution", {}).get("default_model"),
        config.get("evaluation", {}).get("semantic_model"),
        config.get("resilience", {}).get("reflect_model"),
    )
    legacy_gpt5 = all(value == "gpt-5" for value in stage_values)
    partial_automatic_migration = (
        any(value == "gpt-5" for value in stage_values)
        and any(value == "default" for value in stage_values)
        and all(value in {"gpt-5", "default"} for value in stage_values)
    )
    handled = prefs.get("codexModelMigration") in {"automatic-v1", "kept-gpt-5-v1"}
    return (legacy_gpt5 or partial_automatic_migration) and not handled


def _isolated(check, failed):
    """Run one check; an escaping exception prints its traceback and yields ``failed``."""
    try:
        return check()
    except Exception:
        import traceback

        traceback.print_exc()
        return failed


def _flag(shell_value: str, ready: bool) -> str:
    # The line used to print "${SETUP_READY:-}" and friends, which were set
    # only on success, so a value the shell already held showed through.
    return "true" if ready else shell_value


def main(argv: list[str]) -> int:
    mode, values = (argv[0], argv[1:]) if argv else ("", [])
    if mode == "precheck" and len(values) == 5:
        home, codex_home_dir, setup_flag, codex_flag, migration_flag = values
        completed = _isolated(welcome_completed, "")
        setup = _flag(setup_flag, _isolated(lambda: setup_ready(home), False))
        codex = _flag(codex_flag, _isolated(lambda: codex_ready(home, codex_home_dir), False))
        migration = _flag(
            migration_flag,
            _isolated(lambda: legacy_codex_model_migration_required(home), False),
        )
        print(f"completed={completed} setup={setup} codex={codex} migration={migration}")
        return 0
    if mode == "setup-gate" and len(values) == 1:
        (home,) = values
        print("SETUP_READY" if _isolated(lambda: setup_ready(home), False) else "SETUP_REQUIRED")
        return 0
    if mode == "codex-gate" and len(values) == 2:
        home, codex_home_dir = values
        ready = _isolated(lambda: codex_ready(home, codex_home_dir), False)
        print("CODEX_READY" if ready else "CODEX_SETUP_REQUIRED")
        return 0
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
