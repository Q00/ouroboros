"""Shell-level tests for the PR hygiene workflow's API handling.

The helpers under test are defined inside the workflow's `run:` block, so they
are extracted from the YAML and executed against a stub `gh`. That keeps the
tests honest: if the workflow's copy changes, these run against the change
rather than against a drifted duplicate.

Two behaviours are load-bearing and were both regressions found in review:

* `api_retry` must write diagnostics to **stderr**. `closing=$(api_retry ...)`
  captures stdout, so a warning printed there is concatenated with the count
  and turns a successful retry into `integer expression expected`, silently
  discarding a valid closing link.
* `resolve_issue` must separate "no such issue" from "could not tell". Treating
  a 5xx or a rate limit as a 404 turns a GitHub outage into a red check on a
  correctly linked PR.
"""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-hygiene.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq are required; the workflow uses both",
)


def _step_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["require-issue-link"]["steps"]
    return next(step["run"] for step in steps if "run" in step)


def _extract_function(name: str) -> str:
    """Return the named shell function as written in the workflow."""
    script = _step_script()
    match = re.search(rf"^\s*{name}\(\) \{{.*?^\s*\}}$", script, re.DOTALL | re.MULTILINE)
    assert match, f"{name}() not found in the workflow step"
    # The block is indented inside the YAML literal; dedent for `bash`.
    return "\n".join(
        line[10:] if line.startswith(" " * 10) else line for line in match.group(0).splitlines()
    )


def run_bash(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "case.sh"
    path.write_text(script, encoding="utf-8")
    return subprocess.run(
        ["bash", str(path)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )


class TestApiRetry:
    # The retry backoff is the behaviour of the *runner*, not of these tests;
    # waiting it out would cost five real seconds per case.
    HEADER = "set -euo pipefail\nRUNNER_TEMP=.\nAPI_RETRY_DELAY_SECONDS=0\n"

    def test_success_on_first_try_prints_only_the_payload(self, tmp_path: Path) -> None:
        script = f"""{self.HEADER}
{_extract_function("api_retry")}
fake() {{ echo "7"; }}
out=$(api_retry fake)
echo "captured=[$out]"
"""
        result = run_bash(script, tmp_path)
        assert result.returncode == 0
        assert "captured=[7]" in result.stdout

    def test_first_failure_then_success_keeps_stdout_clean(self, tmp_path: Path) -> None:
        """The regression: a retry warning on stdout corrupts the captured count."""
        script = f"""{self.HEADER}
{_extract_function("api_retry")}
attempt=0
fake() {{
  attempt=$((attempt + 1))
  if [ "$attempt" -eq 1 ]; then return 1; fi
  echo "1"
}}
out=$(api_retry fake 2>/dev/null)
echo "captured=[$out]"
if [ "$out" -gt 0 ]; then echo "arithmetic-ok"; fi
"""
        result = run_bash(script, tmp_path)
        assert result.returncode == 0, result.stderr
        assert "captured=[1]" in result.stdout
        assert "arithmetic-ok" in result.stdout, (
            "the captured value was not a bare integer, so `-gt` would have failed"
        )

    def test_retry_warning_goes_to_stderr(self, tmp_path: Path) -> None:
        script = f"""{self.HEADER}
{_extract_function("api_retry")}
attempt=0
fake() {{
  attempt=$((attempt + 1))
  if [ "$attempt" -eq 1 ]; then return 1; fi
  echo "1"
}}
api_retry fake > out.txt 2> err.txt || true
echo "STDOUT:$(cat out.txt)"
echo "STDERR:$(cat err.txt)"
"""
        result = run_bash(script, tmp_path)
        assert "STDOUT:1" in result.stdout
        assert "::warning::" in result.stdout.split("STDERR:")[1]
        assert "::warning::" not in result.stdout.split("STDERR:")[0]

    def test_two_failures_return_nonzero(self, tmp_path: Path) -> None:
        script = f"""{self.HEADER}
{_extract_function("api_retry")}
fake() {{ return 1; }}
if api_retry fake 2>/dev/null; then echo "unexpected-success"; else echo "failed=$?"; fi
"""
        result = run_bash(script, tmp_path)
        assert "failed=" in result.stdout
        assert "unexpected-success" not in result.stdout


class TestResolveIssue:
    """`resolve_issue` returns 0 = issue, 1 = definitively not, 2 = undecidable."""

    # Matches the workflow's own `set -euo pipefail`. Omitting `-e` here is
    # what let the bare-call bug reach review: under `-e`, a non-zero
    # `resolve_issue` terminates the step before its status is inspected.
    HEADER = 'set -euo pipefail\nRUNNER_TEMP=.\nOWNER=Q00\nREPO=ouroboros\nPATH="$PWD/bin:$PATH"\n'

    @staticmethod
    def _stub_gh(tmp_path: Path, *, stdout: str = "", stderr: str = "", code: int = 0) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        gh = bin_dir / "gh"
        gh.write_text(
            f"#!/usr/bin/env bash\nprintf '%s' {stdout!r}\nprintf '%s' {stderr!r} >&2\nexit {code}\n",
            encoding="utf-8",
        )
        gh.chmod(0o755)

    def _run(self, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        script = f"""{self.HEADER}
{_extract_function("resolve_issue")}
status=0
resolve_issue 1777 || status=$?
echo "status=${{status}}"
"""
        return run_bash(script, tmp_path)

    def test_existing_issue_returns_zero(self, tmp_path: Path) -> None:
        self._stub_gh(tmp_path, stdout='{"number": 1777}')
        assert "status=0" in self._run(tmp_path).stdout

    def test_pull_request_returns_one(self, tmp_path: Path) -> None:
        self._stub_gh(tmp_path, stdout='{"number": 1735, "pull_request": {}}')
        result = self._run(tmp_path)
        assert "status=1" in result.stdout
        assert "not an issue" in result.stdout

    def test_404_returns_one(self, tmp_path: Path) -> None:
        self._stub_gh(tmp_path, stderr="gh: Not Found (HTTP 404)", code=1)
        result = self._run(tmp_path)
        assert "status=1" in result.stdout
        assert "no such issue" in result.stdout

    @pytest.mark.parametrize(
        "stderr",
        [
            pytest.param("gh: Internal Server Error (HTTP 500)", id="server-error"),
            pytest.param("gh: API rate limit exceeded (HTTP 403)", id="rate-limited"),
            pytest.param("dial tcp: lookup api.github.com: no such host", id="network"),
        ],
    )
    def test_infrastructure_failure_returns_two(self, tmp_path: Path, stderr: str) -> None:
        """A GitHub outage must not read as "this issue does not exist"."""
        self._stub_gh(tmp_path, stderr=stderr, code=1)
        result = self._run(tmp_path)
        assert "status=2" in result.stdout
        assert "no such issue" not in result.stdout


class TestResolutionLoopUnderSetE:
    """The loop must survive a non-zero `resolve_issue` under `set -e`.

    A bare call terminates the step, so one invalid candidate ahead of a valid
    one rejects the PR, and an outage exits red instead of failing open. The
    earlier helper tests ran without `-e` and missed exactly this.
    """

    @staticmethod
    def _loop(tmp_path: Path, statuses: dict[str, int]) -> subprocess.CompletedProcess[str]:
        """Run the workflow's resolution loop with `resolve_issue` stubbed."""
        script = _step_script()
        # Anchored on the fail-open branch's own text so the match cannot stop
        # at an inner `fi` and yield an unbalanced fragment.
        match = re.search(
            r"^\s*RESOLVE_CAP=.*?Could not determine.*?^\s*fi$",
            script,
            re.DOTALL | re.MULTILINE,
        )
        assert match, "resolution loop not found in the workflow step"
        loop = "\n".join(
            line[10:] if line.startswith(" " * 10) else line for line in match.group(0).splitlines()
        )
        cases = "\n".join(f"    {number}) return {code} ;;" for number, code in statuses.items())
        stub = f"""set -euo pipefail
candidates="{" ".join(statuses)}"
resolve_issue() {{
  case "$1" in
{cases}
    *) return 1 ;;
  esac
}}
{loop}
echo "fell-through"
"""
        return run_bash(stub, tmp_path)

    def test_invalid_candidate_before_a_valid_one_does_not_reject(self, tmp_path: Path) -> None:
        result = self._loop(tmp_path, {"1": 1, "1777": 0})
        assert result.returncode == 0, result.stderr
        assert "links to issue #1777" in result.stdout
        assert "fell-through" not in result.stdout

    def test_all_invalid_falls_through_to_the_failure_path(self, tmp_path: Path) -> None:
        result = self._loop(tmp_path, {"1": 1, "2": 1})
        assert result.returncode == 0, result.stderr
        assert "fell-through" in result.stdout

    def test_an_outage_fails_open_instead_of_exiting_red(self, tmp_path: Path) -> None:
        result = self._loop(tmp_path, {"1": 2})
        assert result.returncode == 0, result.stderr
        assert "Could not determine" in result.stdout
        assert "fell-through" not in result.stdout

    def test_a_valid_candidate_wins_over_an_earlier_outage(self, tmp_path: Path) -> None:
        result = self._loop(tmp_path, {"1": 2, "1777": 0})
        assert result.returncode == 0, result.stderr
        assert "links to issue #1777" in result.stdout
