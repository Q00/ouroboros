"""Bounded repository context for deterministic auto interview answers."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
from typing import Any

from ouroboros.auto.answerer import AutoAnswerContext

_FRAMEWORK_DEPENDENCIES = {
    "click": "Click CLI",
    "django": "Django",
    "fastapi": "FastAPI",
    "flask": "Flask",
    "streamlit": "Streamlit",
    "textual": "Textual TUI",
    "typer": "Typer CLI",
}

_JS_LOCKFILES = {
    "bun.lockb": "Bun",
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "Yarn",
}

_SCRIPT_FACTS = {
    "build": "build_command",
    "dev": "dev_command",
    "lint": "lint_command",
    "test": "test_command",
}

_DOCKER_TASK_FILES = {
    "Dockerfile": "Dockerfile present",
    "compose.yaml": "Docker Compose file present",
    "compose.yml": "Docker Compose file present",
    "docker-compose.yml": "Docker Compose file present",
    "Makefile": "Makefile task runner present",
    "justfile": "just task runner present",
    "Taskfile.yml": "Taskfile task runner present",
}


def repo_auto_answer_context(cwd: str | Path) -> AutoAnswerContext:
    """Derive minimal local repo facts from fixed, bounded paths under ``cwd``."""
    root = Path(cwd)
    facts: dict[str, str] = {}
    evidence: dict[str, tuple[str, ...]] = {}
    runtime_parts: list[str] = []
    runtime_evidence = ["pyproject.toml"]
    strong_runtime_fact = False

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        pyproject_data = _read_pyproject(pyproject)
        if pyproject_data is not None:
            project = _table(pyproject_data.get("project"))

            requires_python = _clean_str(project.get("requires-python"))
            if requires_python:
                runtime_parts.append(f"Python project requiring {requires_python}")
                strong_runtime_fact = True
            elif project:
                facts["project_kind"] = "Python project declared in pyproject.toml"
                evidence["project_kind"] = ("pyproject.toml",)

            package_manager = _python_package_manager(root, pyproject_data)
            if package_manager:
                facts["package_manager"] = package_manager
                evidence["package_manager"] = ("pyproject.toml",)
                runtime_parts.append(f"managed with {package_manager}")

            framework = _framework(project)
            if framework:
                facts["framework"] = framework
                evidence["framework"] = ("pyproject.toml",)
                runtime_parts.append(f"using {framework}")
                strong_runtime_fact = True

    _add_javascript_facts(root, facts, evidence)
    _add_rust_facts(root, facts, evidence)
    _add_go_facts(root, facts, evidence)
    _add_jvm_facts(root, facts, evidence)
    _add_docker_task_facts(root, facts, evidence)

    structure = _project_structure(root)
    if structure:
        facts["project_structure"] = structure
        structure_evidence = tuple(_structure_evidence(root))
        evidence["project_structure"] = structure_evidence
        runtime_parts.append(structure)
        runtime_evidence.extend(structure_evidence)

    if strong_runtime_fact and runtime_parts:
        facts["runtime_context"] = "; ".join(runtime_parts) + "."
        evidence["runtime_context"] = tuple(dict.fromkeys(runtime_evidence))

    return AutoAnswerContext(repo_facts=facts, evidence=evidence)


def _add_javascript_facts(
    root: Path, facts: dict[str, str], evidence: dict[str, tuple[str, ...]]
) -> None:
    package_json = root / "package.json"
    if not package_json.is_file():
        return
    data = _read_json_object(package_json)
    if data is None:
        return
    package_name = _clean_str(data.get("name"))
    project_kind = "JavaScript/TypeScript project"
    if package_name:
        project_kind = f"JavaScript/TypeScript project {package_name!r}"
    _set_fact(facts, evidence, "project_kind", project_kind, ("package.json",))

    manager, manager_evidence = _javascript_package_manager(root)
    if manager:
        _set_fact(facts, evidence, "package_manager", manager, manager_evidence)

    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return
    if manager and not manager.startswith("ambiguous "):
        script_runner = manager
    else:
        script_runner = "package"
    for script_name, fact_key in _SCRIPT_FACTS.items():
        value = _clean_str(scripts.get(script_name))
        if value:
            _set_fact(
                facts,
                evidence,
                fact_key,
                f"{script_runner} script `{script_name}`: {value}",
                ("package.json",),
            )


def _add_rust_facts(
    root: Path, facts: dict[str, str], evidence: dict[str, tuple[str, ...]]
) -> None:
    cargo_toml = root / "Cargo.toml"
    if not cargo_toml.is_file():
        return
    cargo_data = _read_toml(cargo_toml)
    if cargo_data is None:
        return
    package = _table(cargo_data.get("package"))
    package_name = _clean_str(package.get("name"))
    project_kind = "Rust project declared in Cargo.toml"
    if package_name:
        project_kind = f"Rust project {package_name!r} declared in Cargo.toml"
    _set_fact(facts, evidence, "project_kind", project_kind, ("Cargo.toml",))
    manager_evidence = ["Cargo.toml"]
    if (root / "Cargo.lock").is_file():
        manager_evidence.append("Cargo.lock")
    _set_fact(facts, evidence, "package_manager", "Cargo", tuple(manager_evidence))


def _add_go_facts(root: Path, facts: dict[str, str], evidence: dict[str, tuple[str, ...]]) -> None:
    go_mod = root / "go.mod"
    if not go_mod.is_file():
        return
    module_name = _read_go_module(go_mod)
    if module_name is None:
        return
    project_kind = "Go module declared in go.mod"
    if module_name:
        project_kind = f"Go module {module_name!r} declared in go.mod"
    _set_fact(facts, evidence, "project_kind", project_kind, ("go.mod",))
    manager_evidence = ["go.mod"]
    if (root / "go.sum").is_file():
        manager_evidence.append("go.sum")
    _set_fact(facts, evidence, "package_manager", "Go modules", tuple(manager_evidence))


def _add_jvm_facts(root: Path, facts: dict[str, str], evidence: dict[str, tuple[str, ...]]) -> None:
    for filename, manager in (
        ("pom.xml", "Maven"),
        ("build.gradle", "Gradle"),
        ("build.gradle.kts", "Gradle"),
        ("settings.gradle", "Gradle"),
    ):
        if (root / filename).is_file():
            _set_fact(facts, evidence, "project_kind", "JVM project", (filename,))
            _set_fact(facts, evidence, "package_manager", manager, (filename,))
            return


def _add_docker_task_facts(
    root: Path, facts: dict[str, str], evidence: dict[str, tuple[str, ...]]
) -> None:
    hints: list[str] = []
    hint_evidence: list[str] = []
    for filename, description in _DOCKER_TASK_FILES.items():
        if (root / filename).is_file():
            hints.append(description)
            hint_evidence.append(filename)
    if hints:
        _set_fact(
            facts,
            evidence,
            "execution_hints",
            "; ".join(hints),
            tuple(hint_evidence),
        )


def _set_fact(
    facts: dict[str, str],
    evidence: dict[str, tuple[str, ...]],
    key: str,
    value: str,
    paths: tuple[str, ...],
) -> None:
    if key in facts:
        facts[key] = f"{facts[key]}; {value}"
        evidence[key] = tuple(dict.fromkeys((*evidence.get(key, ()), *paths)))
        return
    facts[key] = value
    evidence[key] = paths


def _read_pyproject(path: Path) -> dict[str, Any] | None:
    return _read_toml(path)


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_go_module(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "module":
            return ""
        if stripped.startswith("module "):
            parts = stripped.split(None, 1)
            return parts[1].strip() if len(parts) > 1 else ""
    return ""


def _table(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _python_package_manager(root: Path, pyproject_data: dict[str, Any]) -> str:
    if (root / "uv.lock").is_file():
        return "uv"
    if (root / "poetry.lock").is_file():
        return "Poetry"
    if (root / "pdm.lock").is_file():
        return "PDM"
    build_system = _table(pyproject_data.get("build-system"))
    backend = _clean_str(build_system.get("build-backend"))
    if "hatchling" in backend:
        return "hatchling/pyproject"
    if backend:
        return f"{backend}/pyproject"
    return "pyproject.toml"


def _javascript_package_manager(root: Path) -> tuple[str, tuple[str, ...]]:
    matched = [
        (filename, manager)
        for filename, manager in _JS_LOCKFILES.items()
        if (root / filename).is_file()
    ]
    if len(matched) > 1:
        filenames = tuple(filename for filename, _manager in matched)
        managers = ", ".join(manager for _filename, manager in matched)
        return (
            f"ambiguous JavaScript package manager ({managers})",
            ("package.json", *filenames),
        )
    if matched:
        filename, manager = matched[0]
        return manager, ("package.json", filename)
    return "", ()


def _framework(project: dict[str, Any]) -> str:
    dependencies = project.get("dependencies")
    if not isinstance(dependencies, list):
        return ""
    normalized = {_dependency_name(item) for item in dependencies if isinstance(item, str)}
    frameworks = [
        framework
        for dependency, framework in _FRAMEWORK_DEPENDENCIES.items()
        if dependency in normalized
    ]
    return ", ".join(frameworks)


def _dependency_name(requirement: str) -> str:
    name = requirement.strip().split("[", 1)[0]
    for separator in ("<", ">", "=", "!", "~", ";", " "):
        name = name.split(separator, 1)[0]
    return name.lower().replace("_", "-")


def _project_structure(root: Path) -> str:
    has_src = (root / "src").is_dir()
    has_tests = (root / "tests").is_dir()
    if has_src and has_tests:
        return "src layout with tests directory"
    if has_src:
        return "src layout"
    if has_tests:
        return "tests directory present"
    return ""


def _structure_evidence(root: Path) -> list[str]:
    evidence: list[str] = []
    if (root / "src").is_dir():
        evidence.append("src/")
    if (root / "tests").is_dir():
        evidence.append("tests/")
    return evidence
