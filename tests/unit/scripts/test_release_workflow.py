"""Tests for the release workflow's trigger paths, job conditions and steps.

GitHub evaluates `.github/workflows/release.yml`, not these tests, so the job
`if:` expressions are evaluated here by a deliberately small translator that
accepts only the syntax the workflow uses. Anything else raises, so a condition
the translator cannot represent fails the test instead of passing silently.

The dispatch preflight script is extracted from the YAML and run against a stub
`gh`, so the tests exercise the workflow's own copy of it.
"""

from __future__ import annotations

from collections.abc import Mapping
import itertools
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release.yml"

DISPATCH_JOBS = {"dispatch-preflight", "build-tui", "attach-tui-binaries", "homebrew"}
TAG_SHA = "54dad8e1dd93aee31858bde72493cd7a0f938739"
RETIRED_RUNNERS = {"macos-13", "macos-14"}


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _jobs() -> dict[str, Any]:
    return _workflow()["jobs"]


def _triggers() -> dict[str, Any]:
    workflow = _workflow()
    # YAML 1.1 (PyYAML) reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def _needs(job: Mapping[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _step(job_name: str, step_name: str) -> dict[str, Any]:
    steps = _jobs()[job_name]["steps"]
    return next(step for step in steps if step.get("name") == step_name)


# --- expression translator -------------------------------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')"
    r"|(?P<status>(?:success|cancelled)\(\))"
    r"|(?P<op>&&|\|\||==|!=|!|\(|\))"
    r"|(?P<path>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+))"
)
_STATUS_FUNCTION = re.compile(r"\b(?:success|failure|cancelled|always)\(\)")


def _unwrap(expression: str) -> str:
    text = expression.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return text.strip()


def _evaluate(
    expression: str,
    context: Mapping[str, object],
    status: Mapping[str, bool] | None = None,
) -> object:
    """Evaluate a GitHub expression limited to the operators release.yml uses.

    `&&`/`||` return an operand, as in GitHub, which Python's `and`/`or` match.
    `!` binds tighter than `==` in GitHub but not in Python, so it is accepted
    only directly before a status function.
    """
    text = _unwrap(expression)
    python: list[str] = []
    position = 0
    negate = False
    while position < len(text):
        match = _TOKEN.match(text, position)
        if match is None or match.end() == position:
            raise ValueError(f"unsupported expression syntax at {text[position:]!r}")
        position = match.end()
        if negate and match.lastgroup != "status":
            raise ValueError("`!` is supported only before a status function")
        if match.lastgroup == "string":
            python.append(repr(match.group("string")[1:-1].replace("''", "'")))
        elif match.lastgroup == "status":
            name = match.group("status")[:-2]
            value = f"_status[{name!r}]"
            python.append(f"(not {value})" if negate else value)
            negate = False
        elif match.lastgroup == "path":
            python.append(f"_context[{match.group('path')!r}]")
        else:
            op = match.group("op")
            if op == "!":
                negate = True
                continue
            python.append({"&&": "and", "||": "or"}.get(op, op))
    if negate:
        raise ValueError("dangling `!`")
    namespace = {"_context": context, "_status": status or {}}
    # Every token was produced above from the whitelist; nothing else reaches eval.
    return eval(" ".join(python), {"__builtins__": {}}, namespace)


def _simulate(
    event: str,
    outcomes: Mapping[str, str] | None = None,
    *,
    cancelled: bool = False,
) -> dict[str, str]:
    """Return each job's result for one run, given the result of jobs that run.

    A job that runs ends with `outcomes[job]` (default "success"); a job whose
    condition is false is "skipped". A job-level `if` without a status function
    gets GitHub's implicit `success() &&`.
    """
    outcomes = outcomes or {}
    jobs = _jobs()
    results: dict[str, str] = {}
    pending = list(jobs)
    while pending:
        name = next(
            (job for job in pending if all(need in results for need in _needs(jobs[job]))),
            None,
        )
        assert name is not None, f"dependency cycle among {pending}"
        pending.remove(name)
        needs = _needs(jobs[name])
        condition = _unwrap(str(jobs[name].get("if", "success()")))
        if not _STATUS_FUNCTION.search(condition):
            condition = f"success() && ({condition})"
        context = {"github.event_name": event}
        context.update({f"needs.{need}.result": results[need] for need in needs})
        status = {
            "success": not cancelled and all(results[need] == "success" for need in needs),
            "cancelled": cancelled,
        }
        runs = bool(_evaluate(condition, context, status))
        results[name] = outcomes.get(name, "success") if runs else "skipped"
    return results


def _ran(results: Mapping[str, str]) -> set[str]:
    return {job for job, result in results.items() if result != "skipped"}


def test_translator_rejects_syntax_it_does_not_model() -> None:
    with pytest.raises(ValueError):
        _evaluate("contains(github.ref, 'v')", {"github.ref": "v1"})
    with pytest.raises(ValueError):
        _evaluate("!github.event_name == 'push'", {"github.event_name": "push"})


# --- triggers --------------------------------------------------------------


def test_triggers_keep_the_tag_push_and_require_a_dispatch_tag() -> None:
    triggers = _triggers()

    assert triggers["push"] == {"tags": ["v*.*.*"]}
    tag_input = triggers["workflow_dispatch"]["inputs"]["tag"]
    assert tag_input["required"] is True
    assert tag_input["type"] == "string"


# --- job graph per event ---------------------------------------------------


def test_dispatch_runs_only_the_finishing_jobs() -> None:
    assert _ran(_simulate("workflow_dispatch")) == DISPATCH_JOBS


@pytest.mark.parametrize(
    "outcomes",
    [
        dict(zip(sorted(DISPATCH_JOBS), combo, strict=True))
        for combo in itertools.product(("success", "failure"), repeat=len(DISPATCH_JOBS))
    ],
)
def test_dispatch_never_creates_a_release_or_publishes(outcomes: dict[str, str]) -> None:
    results = _simulate("workflow_dispatch", outcomes)

    assert results["release"] == "skipped"
    assert results["publish"] == "skipped"


def test_failed_dispatch_preflight_stops_every_other_job() -> None:
    results = _simulate("workflow_dispatch", {"dispatch-preflight": "failure"})

    assert _ran(results) == {"dispatch-preflight"}


@pytest.mark.parametrize(
    ("release", "build_tui", "publish"),
    list(itertools.product(("success", "failure"), repeat=3)),
)
def test_tag_push_runs_the_jobs_it_ran_before(release: str, build_tui: str, publish: str) -> None:
    outcomes = {"release": release, "build-tui": build_tui, "publish": publish}
    results = _simulate("push", outcomes)

    assert results["dispatch-preflight"] == "skipped"
    assert results["release"] == release
    # Unchanged: each ran iff its single upstream succeeded.
    assert (results["build-tui"] != "skipped") == (release == "success")
    assert (results["publish"] != "skipped") == (release == "success")
    assert (results["homebrew"] != "skipped") == (results["publish"] == "success")
    # Changed on purpose: attaching no longer waits for every build leg.
    assert (results["attach-tui-binaries"] != "skipped") == (release == "success")


def test_a_failed_build_leg_still_attaches_the_others() -> None:
    results = _simulate("push", {"build-tui": "failure"})

    assert results["attach-tui-binaries"] == "success"


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_cancelled_run_starts_nothing_downstream(event: str) -> None:
    results = _simulate(event, cancelled=True)

    assert _ran(results) <= {"release", "dispatch-preflight"}


# --- values that differ per event -------------------------------------------

_PUSH = {"github.event_name": "push", "github.ref_name": "v9.9.9"}
_DISPATCH = {
    "github.event_name": "workflow_dispatch",
    "github.ref_name": "main",
    "needs.dispatch-preflight.outputs.tag": "v0.60.0",
    "needs.dispatch-preflight.outputs.sha": TAG_SHA,
}


@pytest.mark.parametrize("job", ["build-tui", "homebrew"])
def test_checkout_uses_the_pushed_ref_or_the_resolved_commit(job: str) -> None:
    ref = _step(job, "Checkout code")["with"]["ref"]

    # Empty is what actions/checkout treats as unset: the pushed tag.
    assert _evaluate(ref, {**_PUSH, "needs.dispatch-preflight.outputs.sha": ""}) == ""
    assert _evaluate(ref, _DISPATCH) == TAG_SHA


def test_attach_and_homebrew_target_the_dispatched_tag_not_the_branch() -> None:
    tag_name = _step("attach-tui-binaries", "Attach binaries to release")["with"]["tag_name"]
    ref_name = _step("homebrew", "Resolve release channel")["env"]["REF_NAME"]

    for expression in (tag_name, ref_name):
        assert _evaluate(expression, {**_PUSH, "needs.dispatch-preflight.outputs.tag": ""}) == (
            "v9.9.9"
        )
        assert _evaluate(expression, _DISPATCH) == "v0.60.0"


def test_attach_fails_when_no_binary_was_built() -> None:
    attach = _step("attach-tui-binaries", "Attach binaries to release")["with"]

    assert attach["fail_on_unmatched_files"] is True


# --- TUI build matrix --------------------------------------------------------


def test_tui_matrix_cross_compiles_on_supported_runners() -> None:
    job = _jobs()["build-tui"]
    legs = job["strategy"]["matrix"]["include"]
    steps = {step["name"]: step for step in job["steps"]}

    assert job["strategy"]["fail-fast"] is False
    assert {leg["runner"] for leg in legs}.isdisjoint(RETIRED_RUNNERS)
    # The host never decides the output: the target is installed, passed to
    # cargo, and used for every path, so a leg may build on any host arch.
    assert steps["Install Rust toolchain"]["with"]["targets"] == "${{ matrix.target }}"
    assert "--target ${{ matrix.target }}" in steps["Build release binary"]["run"]
    for name in ("Rename binary (unix)", "Rename binary (windows)"):
        assert "target/${{ matrix.target }}/release/" in steps[name]["run"]


# --- Homebrew tap trust ------------------------------------------------------


def test_homebrew_trusts_only_the_formula_before_anything_loads_it() -> None:
    steps = [step.get("name") for step in _jobs()["homebrew"]["steps"]]
    bump = _step("homebrew", "Bump formula")["run"]

    assert bump.index("brew tap Q00/tap") < bump.index("brew trust --formula Q00/tap/ouroboros-ai")
    assert bump.index("brew trust --formula Q00/tap/ouroboros-ai") < bump.index(
        "bump-homebrew-formula.sh"
    )
    assert steps.index("Bump formula") < steps.index("Verify formula")
    # Never the whole tap: that would also trust its future formulae and commands.
    assert re.findall(r"brew trust\b[^\n]*", WORKFLOW_PATH.read_text(encoding="utf-8")) == [
        "brew trust --formula Q00/tap/ouroboros-ai"
    ]


# --- dispatch preflight script ----------------------------------------------

_STUB_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GH_LOG"
case "$2" in
  repos/Q00/ouroboros/releases/tags/*)
    if [ "$STUB_RELEASE" != present ]; then echo "gh: Not Found (HTTP 404)" >&2; exit 1; fi
    echo 123 ;;
  repos/Q00/ouroboros/releases/latest) echo "$STUB_LATEST" ;;
  repos/Q00/ouroboros/commits/refs/tags/*) echo "$STUB_SHA" ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
"""


def _run_preflight(
    tmp_path: Path,
    tag: str,
    *,
    release: str = "present",
    latest: str = "v0.60.0",
    sha: str = TAG_SHA,
) -> tuple[subprocess.CompletedProcess[str], str, list[str]]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "gh"
    stub.write_text(_STUB_GH, encoding="utf-8")
    stub.chmod(0o755)
    script = tmp_path / "preflight.sh"
    script.write_text(_step("dispatch-preflight", "Resolve tag, release and commit")["run"])
    output = tmp_path / "github_output"
    output.touch()
    log = tmp_path / "gh.log"
    log.touch()
    env = {
        "PATH": f"{stub_dir}:/usr/bin:/bin",
        "TAG": tag,
        "REPO": "Q00/ouroboros",
        "GH_TOKEN": "unused",
        "GITHUB_OUTPUT": str(output),
        "GH_LOG": str(log),
        "STUB_RELEASE": release,
        "STUB_LATEST": latest,
        "STUB_SHA": sha,
    }
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=30,
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    return result, output.read_text(encoding="utf-8"), calls


preflight = pytest.mark.skipif(shutil.which("bash") is None, reason="the step runs under bash")


@preflight
def test_preflight_resolves_the_latest_stable_release(tmp_path: Path) -> None:
    result, output, calls = _run_preflight(tmp_path, "v0.60.0")

    assert result.returncode == 0, result.stderr
    assert output.splitlines() == ["tag=v0.60.0", f"sha={TAG_SHA}"]
    assert [call.split()[1] for call in calls] == [
        "repos/Q00/ouroboros/releases/tags/v0.60.0",
        "repos/Q00/ouroboros/releases/latest",
        "repos/Q00/ouroboros/commits/refs/tags/v0.60.0",
    ]


@preflight
@pytest.mark.parametrize(
    "tag",
    ["", "0.60.0", "v0.60", "main", "v0.60.0 ", "v0.60.0/../x", "v0.60.0;id", "v0.60.0\nx"],
)
def test_preflight_rejects_a_malformed_tag_before_any_api_call(tmp_path: Path, tag: str) -> None:
    result, output, calls = _run_preflight(tmp_path, tag)

    assert result.returncode == 1
    assert output == ""
    assert calls == []
    assert "tag must look like" in result.stderr
    if tag:
        assert tag not in result.stderr


@preflight
def test_preflight_refuses_a_tag_without_a_published_release(tmp_path: Path) -> None:
    result, output, calls = _run_preflight(tmp_path, "v0.60.0", release="missing")

    assert result.returncode == 1
    assert output == ""
    assert "no published GitHub Release" in result.stderr
    assert len(calls) == 1


@preflight
def test_preflight_refuses_an_older_stable_release(tmp_path: Path) -> None:
    result, output, _ = _run_preflight(tmp_path, "v0.59.9", latest="v0.60.0")

    assert result.returncode == 1
    assert output == ""
    assert "not the latest release (v0.60.0)" in result.stderr


@preflight
def test_preflight_allows_a_prerelease_without_consulting_latest(tmp_path: Path) -> None:
    result, output, calls = _run_preflight(tmp_path, "v0.61.0b1", latest="v0.60.0")

    assert result.returncode == 0, result.stderr
    assert output.splitlines() == ["tag=v0.61.0b1", f"sha={TAG_SHA}"]
    assert not any("releases/latest" in call for call in calls)


@preflight
@pytest.mark.parametrize("sha", ["", "not-a-sha", TAG_SHA[:12]])
def test_preflight_refuses_an_unresolved_commit(tmp_path: Path, sha: str) -> None:
    result, output, _ = _run_preflight(tmp_path, "v0.60.0", sha=sha)

    assert result.returncode == 1
    assert output == ""
    assert "could not resolve v0.60.0 to a commit" in result.stderr
