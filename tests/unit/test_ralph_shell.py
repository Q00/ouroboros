"""Unit tests for scripts/ralph.sh wrapper behavior."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RALPH_SH = ROOT / "scripts" / "ralph.sh"


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _run(["git", "init"], cwd=path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=path)
    _run(["git", "config", "user.name", "Test User"], cwd=path)
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=path)
    _run(["git", "commit", "-m", "initial"], cwd=path)


def _copy_wrapper(script_dir: Path) -> None:
    script_dir.mkdir(parents=True)
    target = script_dir / "ralph.sh"
    shutil.copy2(RALPH_SH, target)
    target.chmod(0o755)


def _write_fake_ralph_py(script_dir: Path, argv_file: Path, *, mutate: bool = False) -> None:
    lines = [
        "import json",
        "import sys",
        "from pathlib import Path",
        "",
        "args = sys.argv[1:]",
        f"Path({str(argv_file)!r}).write_text(json.dumps(args), encoding='utf-8')",
    ]
    if mutate:
        lines.extend(
            [
                "project_dir = args[args.index('--project-dir') + 1]",
                "Path(project_dir, 'generated.txt').write_text('changed\\n', encoding='utf-8')",
            ]
        )
    lines.append("print(json.dumps({'action': 'converged', 'generation': 1, 'similarity': 1.0}))")
    (script_dir / "ralph.py").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_project_dir_with_spaces_is_preserved_as_single_python_arg(tmp_path: Path) -> None:
    script_dir = tmp_path / "scripts"
    project_dir = tmp_path / "target project"
    other_cwd = tmp_path / "launcher cwd"
    argv_file = tmp_path / "argv.json"
    _init_repo(project_dir)
    other_cwd.mkdir()
    _copy_wrapper(script_dir)
    _write_fake_ralph_py(script_dir, argv_file)

    result = subprocess.run(
        [
            "bash",
            str(script_dir / "ralph.sh"),
            "--lineage-id",
            "lin_space",
            "--project-dir",
            str(project_dir),
            "--max-cycles",
            "1",
        ],
        cwd=other_cwd,
        text=True,
        capture_output=True,
        check=True,
    )

    argv = json.loads(argv_file.read_text(encoding="utf-8"))
    assert argv[argv.index("--project-dir") + 1] == str(project_dir.resolve())
    assert "Committed changes" not in result.stderr
    assert _run(["git", "rev-list", "--count", "HEAD"], cwd=project_dir).stdout.strip() == "1"


def test_project_dir_git_snapshot_commits_target_repo_changes(tmp_path: Path) -> None:
    script_dir = tmp_path / "scripts"
    project_dir = tmp_path / "target project"
    other_cwd = tmp_path / "launcher cwd"
    argv_file = tmp_path / "argv.json"
    _init_repo(project_dir)
    other_cwd.mkdir()
    _copy_wrapper(script_dir)
    _write_fake_ralph_py(script_dir, argv_file, mutate=True)

    result = subprocess.run(
        [
            "bash",
            str(script_dir / "ralph.sh"),
            "--lineage-id",
            "lin_commit",
            "--project-dir",
            str(project_dir),
            "--max-cycles",
            "1",
        ],
        cwd=other_cwd,
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Committed changes for gen 1" in result.stderr
    assert _run(["git", "rev-list", "--count", "HEAD"], cwd=project_dir).stdout.strip() == "2"
    assert (
        _run(["git", "tag", "--list", "ooo/lin_commit/gen_1"], cwd=project_dir).stdout.strip()
        == "ooo/lin_commit/gen_1"
    )
    assert (project_dir / "generated.txt").read_text(encoding="utf-8") == "changed\n"


def test_no_execute_ontology_stable_promotes_next_cycle_to_execution(tmp_path: Path) -> None:
    script_dir = tmp_path / "scripts"
    calls_file = tmp_path / "calls.json"
    _copy_wrapper(script_dir)
    (script_dir / "ralph.py").write_text(
        "\n".join(
            (
                "import json, sys",
                "from pathlib import Path",
                f"calls_path = Path({str(calls_file)!r})",
                "calls = json.loads(calls_path.read_text()) if calls_path.exists() else []",
                "calls.append(sys.argv[1:])",
                "calls_path.write_text(json.dumps(calls))",
                "ontology_only = '--no-execute' in sys.argv[1:]",
                "action = 'ontology_stable' if ontology_only else 'converged'",
                "generation = 1 if ontology_only else 2",
                "print(json.dumps({'action': action, 'generation': generation, 'similarity': 1.0}))",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            str(script_dir / "ralph.sh"),
            "--lineage-id",
            "lin_ontology_handoff",
            "--no-execute",
            "--max-cycles",
            "2",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )

    calls = json.loads(calls_file.read_text(encoding="utf-8"))
    assert "--no-execute" in calls[0]
    assert "--no-execute" not in calls[1]
    assert calls[0][calls[0].index("--lineage-id") + 1] == "lin_ontology_handoff"
    assert calls[1][calls[1].index("--lineage-id") + 1] == "lin_ontology_handoff"
    assert "ONTOLOGY STABLE" in result.stderr
    assert '"action": "converged"' in result.stdout
