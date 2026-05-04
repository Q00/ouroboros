"""Tests for scripts/ralph.sh project directory handling."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

ROOT = Path(__file__).resolve().parents[3]
RALPH_SH = ROOT / "scripts" / "ralph.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def test_ralph_sh_help_documents_project_dir() -> None:
    result = subprocess.run(
        ["bash", str(RALPH_SH), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "--project-dir PATH" in result.stdout


def test_ralph_sh_rejects_project_dir_outside_git_worktree(tmp_path: Path) -> None:
    project_dir = tmp_path / "plain-dir"
    project_dir.mkdir()

    result = subprocess.run(
        [
            "bash",
            str(RALPH_SH),
            "--lineage-id",
            "lin_scope",
            "--project-dir",
            str(project_dir),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "must be inside a git worktree" in result.stderr


def test_ralph_sh_scopes_git_snapshot_to_project_dir(tmp_path: Path) -> None:
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    shutil.copy2(RALPH_SH, script_dir / "ralph.sh")
    (script_dir / "ralph.py").write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import argparse, json
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument('--lineage-id', required=True)
            parser.add_argument('--max-retries')
            parser.add_argument('--project-dir', required=True)
            args = parser.parse_args()

            Path(args.project_dir, 'ralph-output.txt').write_text('from fake ralph\\n')
            print(json.dumps({
                'action': 'converged',
                'generation': 1,
                'similarity': 1.0,
                'lineage_id': args.lineage_id,
            }))
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    cwd_repo = tmp_path / "cwd-repo"
    target_repo = tmp_path / "target repo"
    _init_repo(cwd_repo)
    _init_repo(target_repo)

    result = subprocess.run(
        [
            "bash",
            str(script_dir / "ralph.sh"),
            "--lineage-id",
            "lin_scope",
            "--max-cycles",
            "1",
            "--project-dir",
            str(target_repo),
        ],
        cwd=cwd_repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    assert '"action": "converged"' in result.stdout
    assert "ooo/lin_scope/gen_1" in _git(target_repo, "tag", "--list")
    assert "ooo/lin_scope/gen_1" not in _git(cwd_repo, "tag", "--list")
    assert (target_repo / "ralph-output.txt").exists()
    assert not (cwd_repo / "ralph-output.txt").exists()


def test_ralph_sh_scopes_rollback_to_project_dir(tmp_path: Path) -> None:
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    shutil.copy2(RALPH_SH, script_dir / "ralph.sh")
    (script_dir / "ralph.py").write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env python3
            import argparse, json
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument('--lineage-id', required=True)
            parser.add_argument('--max-retries')
            parser.add_argument('--project-dir', required=True)
            args = parser.parse_args()

            Path(args.project_dir, 'README.md').write_text('bad generation\\n')
            Path(args.project_dir, 'untracked.txt').write_text('remove me\\n')
            print(json.dumps({
                'action': 'failed',
                'generation': 2,
                'lineage_id': args.lineage_id,
                'error': 'fake failure',
            }))
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    cwd_repo = tmp_path / "cwd-repo"
    target_repo = tmp_path / "rollback target"
    nested_project_dir = target_repo / "packages" / "app"
    _init_repo(cwd_repo)
    _init_repo(target_repo)
    nested_project_dir.mkdir(parents=True)
    (target_repo / "outside-nested.txt").write_text("safe baseline\n", encoding="utf-8")
    _git(target_repo, "add", "outside-nested.txt")
    _git(target_repo, "commit", "-m", "add outside nested baseline")
    _git(target_repo, "tag", "ooo/lin_scope/gen_1")

    result = subprocess.run(
        [
            "bash",
            str(script_dir / "ralph.sh"),
            "--lineage-id",
            "lin_scope",
            "--max-cycles",
            "1",
            "--project-dir",
            str(nested_project_dir),
        ],
        cwd=cwd_repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    assert result.returncode == 12
    assert (target_repo / "README.md").read_text(encoding="utf-8") == "initial\n"
    assert (target_repo / "outside-nested.txt").read_text(encoding="utf-8") == "safe baseline\n"
    assert not (target_repo / "untracked.txt").exists()
    assert not (cwd_repo / "untracked.txt").exists()
