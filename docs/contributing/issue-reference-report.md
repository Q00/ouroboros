# Read-only issue reference report

The manually dispatched **Open issues with merged PR references** workflow gives
maintainers a list of open issues whose visible, directly cross-referencing
pull requests in this repository are all merged. Open, draft, and closed
unmerged PRs exclude an issue. An issue without such a PR is also excluded.
Each candidate lists the PR state, merge time, and GitHub's closing-reference
indicator. An indicator that is false means GitHub did not mark that timeline
event as closing; it is not proof that the PR never contained a closing keyword.

From the Actions tab, choose the workflow and use **Run workflow**. The report
appears in the run's step summary. Alternatively, authenticate `gh` for
read-only access to issues and pull requests and run from this checkout:

```sh
python3 scripts/report-merged-issue-references.py
```

The report only reads GitHub. It does not edit issues, labels, PRs, comments, or
projects. It follows the cross-reference relationships GitHub exposes, not a
search of arbitrary text. References that GitHub does not expose cannot be
reported. A missing reference source or failed/incomplete API response makes the
run **indeterminate**, with a nonzero exit, rather than an empty clean report.
The pages are not an atomic snapshot: concurrent activity may change results.
Before any disposition, inspect the current issue, acceptance criteria and PRs;
a merged PR is not proof that the whole issue is resolved.
