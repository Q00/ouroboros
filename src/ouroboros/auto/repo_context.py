"""Bounded repository context for deterministic auto interview answers."""

from __future__ import annotations

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


def repo_auto_answer_context(cwd: str | Path) -> AutoAnswerContext:
    """Derive minimal local repo facts from fixed, bounded paths under ``cwd``."""
    root = Path(cwd)
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return AutoAnswerContext()

    pyproject_data = _read_pyproject(pyproject)
    if not pyproject_data:
        return AutoAnswerContext()

    project = _table(pyproject_data.get("project"))
    facts: dict[str, str] = {}
    evidence: dict[str, tuple[str, ...]] = {}
    runtime_parts: list[str] = []
    runtime_evidence = ["pyproject.toml"]
    strong_runtime_fact = False

    requires_python = _clean_str(project.get("requires-python"))
    if requires_python:
        runtime_parts.append(f"Python project requiring {requires_python}")
        strong_runtime_fact = True
    elif project:
        facts["project_kind"] = "Python project declared in pyproject.toml"
        evidence["project_kind"] = ("pyproject.toml",)

    package_manager = _package_manager(root, pyproject_data)
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


def _read_pyproject(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _table(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _package_manager(root: Path, pyproject_data: dict[str, Any]) -> str:
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
