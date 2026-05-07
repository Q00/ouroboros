from __future__ import annotations

import json
from pathlib import Path

from ouroboros.auto.repo_context import repo_auto_answer_context


def test_repo_context_extracts_javascript_scripts_without_runtime_context(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-web",
                "scripts": {
                    "test": "vitest run",
                    "build": "vite build",
                    "lint": "eslint .",
                    "dev": "vite --host 0.0.0.0",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" not in context.repo_facts
    assert "JavaScript/TypeScript project 'demo-web'" in context.repo_facts["project_kind"]
    assert context.repo_facts["package_manager"] == "pnpm"
    assert context.repo_facts["test_command"] == "pnpm script `test`: vitest run"
    assert context.repo_facts["build_command"] == "pnpm script `build`: vite build"
    assert context.repo_facts["lint_command"] == "pnpm script `lint`: eslint ."
    assert context.repo_facts["dev_command"] == "pnpm script `dev`: vite --host 0.0.0.0"
    assert context.evidence["package_manager"] == ("package.json", "pnpm-lock.yaml")
    assert context.evidence["test_command"] == ("package.json",)


def test_repo_context_extracts_rust_facts_as_partial_hints(tmp_path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo-cli"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "Cargo.lock").write_text("# lock\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" not in context.repo_facts
    assert context.repo_facts["project_kind"] == "Rust project 'demo-cli' declared in Cargo.toml"
    assert context.repo_facts["package_manager"] == "Cargo"
    assert context.evidence["project_kind"] == ("Cargo.toml",)
    assert context.evidence["package_manager"] == ("Cargo.toml", "Cargo.lock")


def test_repo_context_extracts_go_module_facts_as_partial_hints(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
    (tmp_path / "go.sum").write_text("", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" not in context.repo_facts
    assert context.repo_facts["project_kind"] == "Go module 'example.com/demo' declared in go.mod"
    assert context.repo_facts["package_manager"] == "Go modules"
    assert context.evidence["project_kind"] == ("go.mod",)
    assert context.evidence["package_manager"] == ("go.mod", "go.sum")


def test_repo_context_handles_malformed_go_module_without_crashing(tmp_path) -> None:
    (tmp_path / "go.mod").write_text("module\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" not in context.repo_facts
    assert context.repo_facts["project_kind"] == "Go module declared in go.mod"
    assert context.repo_facts["package_manager"] == "Go modules"
    assert context.evidence["project_kind"] == ("go.mod",)


def test_repo_context_keeps_docker_and_task_runner_hints_out_of_runtime(tmp_path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (tmp_path / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
    (tmp_path / "justfile").write_text("test:\n    pytest\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" not in context.repo_facts
    assert context.repo_facts["execution_hints"] == (
        "Dockerfile present; Docker Compose file present; Makefile task runner present; "
        "just task runner present"
    )
    assert context.evidence["execution_hints"] == (
        "Dockerfile",
        "compose.yaml",
        "Makefile",
        "justfile",
    )


def test_repo_context_preserves_python_strong_runtime_behavior(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "\n".join(
            [
                "[project]",
                'name = "demo-cli"',
                'requires-python = ">=3.12"',
                'dependencies = ["typer>=0.12"]',
                "",
                "[build-system]",
                'build-backend = "hatchling.build"',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" in context.repo_facts
    assert "Python project requiring >=3.12" in context.repo_facts["runtime_context"]
    assert "Typer CLI" in context.repo_facts["runtime_context"]
    assert context.evidence["runtime_context"] == ("pyproject.toml", "src/", "tests/")


def test_repo_context_surfaces_ambiguous_javascript_lockfiles(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert (
        context.repo_facts["package_manager"] == "ambiguous JavaScript package manager (npm, pnpm)"
    )
    assert context.repo_facts["test_command"] == "package script `test`: vitest run"
    assert context.evidence["package_manager"] == (
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
    )


def test_repo_context_recognizes_compose_yml_hint(tmp_path) -> None:
    (tmp_path / "compose.yml").write_text("services: {}\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert context.repo_facts["execution_hints"] == "Docker Compose file present"
    assert context.evidence["execution_hints"] == ("compose.yml",)


def test_repo_context_ignores_malformed_pyproject_conservatively(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert context.repo_facts == {}
    assert context.evidence == {}


def test_repo_context_keeps_non_python_facts_when_pyproject_malformed(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo-web", "scripts": {"test": "vitest run"}}),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo-cli"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" not in context.repo_facts
    assert "JavaScript/TypeScript project 'demo-web'" in context.repo_facts["project_kind"]
    assert "Rust project 'demo-cli'" in context.repo_facts["project_kind"]
    assert "pnpm" in context.repo_facts["package_manager"]
    assert "Cargo" in context.repo_facts["package_manager"]
    assert context.repo_facts["test_command"] == "pnpm script `test`: vitest run"
    assert context.repo_facts["execution_hints"] == "Dockerfile present"
    assert "pyproject.toml" not in context.evidence["project_kind"]


def test_repo_context_ignores_malformed_package_json_conservatively(tmp_path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": ', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")

    context = repo_auto_answer_context(tmp_path)

    assert "project_kind" not in context.repo_facts
    assert "package_manager" not in context.repo_facts
    assert "test_command" not in context.repo_facts


def test_repo_context_handles_javascript_without_lockfile(tmp_path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo-web", "scripts": {"test": "vitest run"}}),
        encoding="utf-8",
    )

    context = repo_auto_answer_context(tmp_path)

    assert "runtime_context" not in context.repo_facts
    assert context.repo_facts["project_kind"] == "JavaScript/TypeScript project 'demo-web'"
    assert "package_manager" not in context.repo_facts
    assert context.repo_facts["test_command"] == "package script `test`: vitest run"
    assert context.evidence["test_command"] == ("package.json",)


def test_repo_context_skips_go_facts_when_go_mod_unreadable(tmp_path, monkeypatch) -> None:
    go_mod = tmp_path / "go.mod"
    go_mod.write_text("module example.com/demo\n", encoding="utf-8")
    (tmp_path / "go.sum").write_text("", encoding="utf-8")

    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if self == go_mod:
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    context = repo_auto_answer_context(tmp_path)

    assert "project_kind" not in context.repo_facts
    assert "package_manager" not in context.repo_facts
    assert context.evidence == {}
