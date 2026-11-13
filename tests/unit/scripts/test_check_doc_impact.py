"""Self-tests for ``scripts/check-doc-impact.py`` and ``docs/doc-map.yml``.

The script is advisory: a mapped doc left untouched is reported and the exit
status stays 0. What must hold:

* globs mean what the map's header says (``*`` stays in one segment, ``**``
  spans segments), so an entry never silently covers more or less than written;
* a malformed map fails loud with every problem listed, never half-parsed;
* ``--validate`` catches a mapped doc that is gone and a glob that matches
  nothing, which is how the map rots;
* the report lists an untouched mapped doc, never a touched one, and reaches
  stdout, the job summary and the annotations;
* git failures and an unwritable summary are distinct non-zero exits.

The real map is validated against the real checkout, so a rename that strands
an entry fails here, in the required test job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-doc-impact.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "doc-impact.yml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

MAPPING_HEADINGS = (
    "### CLI Commands → Doc Mapping",
    "### Orchestrator → Doc Mapping",
    "### Capability Graph → Doc Mapping",
    "### Configuration → Doc Mapping",
    "### Evaluation Pipeline → Doc Mapping",
    "### TUI Source → Doc Mapping",
    "### Skills / Plugin → Doc Mapping",
)


def _load_module() -> ModuleType:
    """Load the hyphenated script as a module so ``main()`` can be called."""
    spec = importlib.util.spec_from_file_location("check_doc_impact", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


doc_impact = _load_module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


_MAP = """\
- sources: [src/pkg/*.py]
  docs: [docs/guide.md]
  note: Check the guide.
- sources: [src/pkg/**/deep.py, tools/run.sh]
  docs: [docs/guide.md, docs/deep.md]
"""


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repository with a map whose every doc and glob resolves."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for rel in ("src/pkg/a.py", "src/pkg/sub/deep.py", "tools/run.sh", "docs/guide.md"):
        _write(root, rel)
    _write(root, "docs/deep.md")
    _write(root, "docs/doc-map.yml", _MAP)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _commit(repo: Path, *rels: str) -> None:
    for rel in rels:
        _write(repo, rel, f"changed {rel}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")


@pytest.fixture(autouse=True)
def _no_ci_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


# --------------------------------------------------------------------------
# Globs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/pkg/*.py", "src/pkg/a.py", True),
        ("src/pkg/*.py", "src/pkg/sub/a.py", False),  # * stays in one segment
        ("src/**/deep.py", "src/deep.py", True),  # ** may match zero segments
        ("src/**/deep.py", "src/a/b/c/deep.py", True),
        ("src/**", "src/a/b.py", True),
        ("src/pkg/?.py", "src/pkg/a.py", True),
        ("src/pkg/?.py", "src/pkg/ab.py", False),
        ("skills/*/SKILL.md", "skills/run/SKILL.md", True),
        ("skills/*/SKILL.md", "skills/SKILL.md", False),
        ("README.md", "README.md", True),
        ("README.md", "docs/README.md", False),
        ("src/pkg/A.py", "src/pkg/a.py", False),  # case-sensitive everywhere
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert doc_impact.glob_matches(pattern, path) is expected


# --------------------------------------------------------------------------
# Map parsing
# --------------------------------------------------------------------------


def test_valid_map_parses() -> None:
    entries = doc_impact.parse_map(_MAP)
    assert [[s.value for s in e.sources] for e in entries] == [
        ["src/pkg/*.py"],
        ["src/pkg/**/deep.py", "tools/run.sh"],
    ]
    assert entries[0].note == "Check the guide." and entries[1].note is None
    assert entries[1].docs[1].line == 5


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "map — must be a non-empty list"),
        ("[]\n", "map — must be a non-empty list"),
        ("sources: [a]\n", "map — must be a non-empty list"),
        ("- [a]\n", "entry — must be a mapping"),
        ("- {sources: [a], docs: [b], owner: me}\n", "'owner' — unknown key"),
        ("- {sources: [a]}\n", "docs — missing from the entry"),
        ("- {sources: [], docs: [b]}\n", "sources — must be a non-empty list"),
        ("- {sources: a, docs: [b]}\n", "sources — must be a non-empty list"),
        ("- {sources: [1], docs: [b]}\n", "sources — every item must be a string"),
        ("- {sources: [a, a], docs: [b]}\n", "a — listed twice"),
        ("- sources: [a]\n  sources: [c]\n  docs: [b]\n", "sources — given twice"),
        ("- {sources: [a], docs: ['docs/*.md']}\n", "must be a file path, not a glob"),
        ("- {sources: ['src/[ab].py'], docs: [b]}\n", "glob syntax this map does not support"),
        ("- {sources: ['src/a**.py'], docs: [b]}\n", "** only as a whole path segment"),
        ("- {sources: [/abs/a.py], docs: [b]}\n", "relative to the repository root"),
        ("- {sources: [src/../a.py], docs: [b]}\n", "'.' or '..' segments"),
        ("- {sources: ['src\\\\a.py'], docs: [b]}\n", "forward slashes"),
        ("- {sources: [a], docs: [b], note: ''}\n", "note — must be a non-empty string"),
        ("- {sources: [a], docs: [b], note: [x]}\n", "note — must be a non-empty string"),
        ("- {sources: [a, docs: [b]\n", "YAML — does not parse"),
    ],
)
def test_malformed_map_is_rejected(text: str, expected: str) -> None:
    with pytest.raises(doc_impact.MapError) as caught:
        doc_impact.parse_map(text)
    assert any(expected in problem for problem in caught.value.problems), caught.value.problems
    assert all(
        re.match(r"^docs/doc-map\.yml:\d+: .+ — ", problem) for problem in caught.value.problems
    )


def test_every_map_problem_is_reported_at_once() -> None:
    text = "- {sources: [], docs: [b]}\n- {sources: [a], docs: ['x/*']}\n- [c]\n"
    with pytest.raises(doc_impact.MapError) as caught:
        doc_impact.parse_map(text)
    assert [problem.split(":")[1] for problem in caught.value.problems] == ["1", "2", "3"]


def test_malformed_map_exits_1(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, "docs/doc-map.yml", "- {sources: [src/pkg/*.py]}\n")
    assert doc_impact.main(["--repo", str(repo), "--base", "HEAD"]) == doc_impact.EXIT_MAP
    assert "docs/doc-map.yml:1: docs — missing from the entry" in capsys.readouterr().err


def test_unreadable_map_exits_1(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert doc_impact.main(["--repo", str(repo), "--validate", "--map", "nope.yml"]) == 1
    assert "nope.yml:1: map — cannot be read" in capsys.readouterr().err


# --------------------------------------------------------------------------
# --validate
# --------------------------------------------------------------------------


def test_validate_passes_on_a_resolving_map(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert doc_impact.main(["--repo", str(repo), "--validate"]) == doc_impact.EXIT_OK
    assert "has 2 entries; every doc and glob resolves" in capsys.readouterr().out


def test_validate_catches_a_missing_doc_and_a_dead_glob(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(
        repo,
        "docs/doc-map.yml",
        _MAP + "- sources: [src/gone/*.py]\n  docs: [docs/gone.md]\n",
    )
    _git(repo, "add", "-A")
    assert doc_impact.main(["--repo", str(repo), "--validate"]) == doc_impact.EXIT_MAP
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "docs/doc-map.yml:6: src/gone/*.py — source glob matches no tracked file; "
        "fix the glob or remove it",
        "docs/doc-map.yml:7: docs/gone.md — mapped doc is not a tracked file; "
        "fix the path or remove it",
    ]


def test_validate_counts_only_tracked_files(repo: Path) -> None:
    _write(repo, "docs/untracked.md")
    _write(
        repo, "docs/doc-map.yml", _MAP + "- sources: [tools/run.sh]\n  docs: [docs/untracked.md]\n"
    )
    _git(repo, "add", "docs/doc-map.yml")
    assert doc_impact.main(["--repo", str(repo), "--validate"]) == doc_impact.EXIT_MAP


# --------------------------------------------------------------------------
# --base report
# --------------------------------------------------------------------------


def test_report_lists_an_untouched_mapped_doc(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/pkg/a.py")
    assert doc_impact.main(["--repo", str(repo), "--base", base]) == doc_impact.EXIT_OK
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "doc-impact: 1 changed file(s); 1 mapped doc(s) not changed in this diff (advisory).",
        "docs/guide.md — not changed; mapped from src/pkg/a.py",
        "    check: Check the guide.",
    ]


def test_report_does_not_list_a_touched_doc(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/pkg/sub/deep.py", "docs/guide.md")
    assert doc_impact.main(["--repo", str(repo), "--base", base]) == doc_impact.EXIT_OK
    out = capsys.readouterr().out
    assert "docs/deep.md — not changed; mapped from src/pkg/sub/deep.py" in out
    assert "docs/guide.md — not changed" not in out


def test_report_merges_entries_per_doc(repo: Path) -> None:
    entries = doc_impact.parse_map(_MAP)
    findings = doc_impact.impact(entries, ["tools/run.sh", "src/pkg/a.py", "README.md"])
    assert [(f.doc, f.sources, f.notes) for f in findings] == [
        ("docs/deep.md", ["tools/run.sh"], []),
        ("docs/guide.md", ["src/pkg/a.py", "tools/run.sh"], ["Check the guide."]),
    ]


def test_report_ignores_unmapped_changes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "README.md")
    assert doc_impact.main(["--repo", str(repo), "--base", base]) == doc_impact.EXIT_OK
    assert capsys.readouterr().out.splitlines() == [
        "doc-impact: 1 changed file(s); 0 mapped doc(s) not changed in this diff (advisory)."
    ]


def test_report_diffs_from_the_merge_base(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A doc changed only on the base branch after the fork is not this PR's change.
    fork = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "src/pkg/a.py")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "docs/guide.md")
    main_tip = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "feature")
    assert fork != main_tip
    assert doc_impact.main(["--repo", str(repo), "--base", main_tip]) == doc_impact.EXIT_OK
    assert "docs/guide.md — not changed; mapped from src/pkg/a.py" in capsys.readouterr().out


def test_renamed_source_counts_under_its_old_path(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "mv", "tools/run.sh", "tools/start.sh")
    _git(repo, "commit", "-q", "-m", "rename")
    assert doc_impact.main(["--repo", str(repo), "--base", base]) == doc_impact.EXIT_OK
    assert "docs/deep.md — not changed; mapped from tools/run.sh" in capsys.readouterr().out


def test_step_summary_gets_a_table_and_keeps_existing_content(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/pkg/a.py")
    assert doc_impact.main(["--repo", str(repo), "--base", base]) == doc_impact.EXIT_OK
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("earlier step\n### Doc impact (advisory)\n")
    assert "| Doc | Changed sources | What to check |" in text
    assert "| `docs/guide.md` | `src/pkg/a.py` | Check the guide. |" in text


def test_step_summary_says_so_when_nothing_is_left(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/pkg/a.py", "docs/guide.md")
    assert doc_impact.main(["--repo", str(repo), "--base", base]) == doc_impact.EXIT_OK
    text = summary.read_text(encoding="utf-8")
    assert "Every doc that `docs/doc-map.yml` maps to them changed too" in text
    assert "| Doc |" not in text


def test_summary_cells_escape_pipes() -> None:
    finding = doc_impact.Finding("docs/a.md", ["src/a|b.py"], ["x | y\nz"])
    text = doc_impact.summary_markdown([finding], 1, "docs/doc-map.yml")
    assert "| `docs/a.md` | `src/a\\|b.py` | x \\| y z |" in text


def test_unwritable_step_summary_exits_4(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))  # a directory
    assert doc_impact.main(["--repo", str(repo), "--base", "HEAD"]) == doc_impact.EXIT_SUMMARY


def test_annotations_only_on_github_actions(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/pkg/a.py")
    doc_impact.main(["--repo", str(repo), "--base", base])
    assert "::notice" not in capsys.readouterr().out
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert doc_impact.main(["--repo", str(repo), "--base", base]) == doc_impact.EXIT_OK
    notices = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::")]
    assert notices == [
        "::notice file=docs/guide.md,title=Doc impact (advisory)::"
        "Not changed in this PR, but its mapped sources are: src/pkg/a.py"
    ]


def test_annotation_values_are_escaped() -> None:
    finding = doc_impact.Finding("docs/a,b:c.md", ["src/100%\nx.py"])
    (line,) = doc_impact.annotation_lines([finding])
    assert line.startswith("::notice file=docs/a%2Cb%3Ac.md,title=Doc impact (advisory)::")
    assert line.endswith("src/100%25%0Ax.py")


# --------------------------------------------------------------------------
# Failures that are not findings
# --------------------------------------------------------------------------


def test_unknown_base_ref_exits_3(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert doc_impact.main(["--repo", str(repo), "--base", "no-such-ref"]) == doc_impact.EXIT_GIT
    assert "base ref 'no-such-ref' is not a commit" in capsys.readouterr().err


def test_outside_a_repository_exits_3(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    assert doc_impact.main(["--repo", str(outside), "--validate"]) == doc_impact.EXIT_GIT


def test_git_timeout_exits_3(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(doc_impact.subprocess, "run", hang)
    assert doc_impact.main(["--repo", str(repo), "--validate"]) == doc_impact.EXIT_GIT


@pytest.mark.parametrize("base", ["--output=/tmp/x", "-p", " "])
def test_base_that_is_not_a_ref_is_a_usage_error(repo: Path, base: str) -> None:
    with pytest.raises(SystemExit) as caught:
        doc_impact.main(["--repo", str(repo), f"--base={base}"])
    assert caught.value.code == doc_impact.EXIT_USAGE


def test_a_mode_is_required() -> None:
    with pytest.raises(SystemExit) as caught:
        doc_impact.main([])
    assert caught.value.code == doc_impact.EXIT_USAGE


# --------------------------------------------------------------------------
# The real repository
# --------------------------------------------------------------------------


def test_real_map_validates_against_the_checkout(capsys: pytest.CaptureFixture[str]) -> None:
    code = doc_impact.main(["--repo", str(REPO_ROOT), "--validate"])
    assert code == doc_impact.EXIT_OK, capsys.readouterr().err


def test_script_runs_as_a_program() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--validate"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_contributing_points_at_the_map_instead_of_restating_it() -> None:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    for heading in MAPPING_HEADINGS:
        assert text.count(f"\n{heading}\n") == 1, heading
        body = text.split(f"\n{heading}\n", 1)[1].split("\n### ", 1)[0]
        assert "docs/doc-map.yml" in body, heading
        assert not re.search(r"(?m)^\|", body), f"{heading} still holds a mapping table"


def test_contributing_sends_nobody_to_a_removed_table() -> None:
    # The decay checklist once said to add new orchestrator files "to the
    # Orchestrator → Doc Mapping table above"; that table is now the map.
    lines = CONTRIBUTING.read_text(encoding="utf-8").splitlines()
    stale = [
        f"CONTRIBUTING.md:{number}: {line.strip()}"
        for number, line in enumerate(lines, start=1)
        if re.search(r"Doc Mapping\b.*\btables?\b", line) or "skills/*.yaml" in line
    ]
    assert not stale, "\n".join(stale)
    new_file = next(line for line in lines if "**Orchestrator new file**" in line)
    assert "docs/doc-map.yml" in new_file


def test_workflow_is_advisory_and_diffs_from_the_pr_base() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    # PyYAML reads the bare `on:` key as True.
    assert workflow[True] == {"pull_request": {"branches": ["main"]}}
    (job,) = workflow["jobs"].values()
    assert job["name"] == "doc-impact (advisory)"
    checkout = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    runs = [step["run"] for step in job["steps"] if "run" in step]
    validate = next(i for i, run in enumerate(runs) if "check-doc-impact.py --validate" in run)
    report = next(
        i for i, run in enumerate(runs) if 'check-doc-impact.py --base "${BASE_SHA}"' in run
    )
    assert validate < report
    report_step = next(step for step in job["steps"] if step.get("run") == runs[report])
    assert report_step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
