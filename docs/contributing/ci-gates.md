# CI Gates and Branch Protection

Single source of truth for what CI enforces on a pull request, how to reproduce
each gate locally, and how to satisfy one legitimately when it does not apply.

Written for contributors *and* for AI agents working in this repository: a gate
that only exists in a workflow file is invisible until it fails, and an agent
that cannot see it will guess. Everything an agent needs to get a PR green
should be discoverable from this page.

> **This page can go stale. The API is authoritative.** Branch protection is
> configured on GitHub, not in this repo. Before relying on the required-check
> list below, verify it:
>
> ```bash
> gh api repos/Q00/ouroboros/branches/main/protection \
>   --jq '{checks: .required_status_checks.contexts,
>          strict: .required_status_checks.strict,
>          enforce_admins: .enforce_admins.enabled,
>          reviews: .required_pull_request_reviews.required_approving_review_count}'
> ```

## `main` is protected — you cannot push to it

Verified 2026-08-24:

| Setting | Value | What it means for you |
|---|---|---|
| Required status checks | `Bridge TypeScript`, `MyPy Type Check`, `Ruff Lint`, `Test Python 3.12` | These four must be green to merge. |
| `strict` | `true` | Your branch must be up to date with `main` before merging. Rebase if `main` moved. |
| `enforce_admins` | `true` | **No admin bypass.** Even the repository owner gets `GH006: Protected branch update failed` on a direct push. |
| Required approving reviews | `1` | A review from `ouroboros-agent` (ourobot) satisfies this. |
| Force pushes | disabled | Never try to rewrite `main`. |

Every change to `main` — including a release commit — goes through a pull
request and is **squash-merged**, which mints a new commit SHA. Anything that
must point at the landed commit (most importantly a release tag) has to be
created *after* the merge, on the updated `main`.

```bash
git checkout -b chore/my-change
# ... commit ...
git push -u origin chore/my-change
gh pr create --base main --head chore/my-change --title "..." --body "..."
```

## The gates

`Required` marks a check listed in branch protection. The others still report
on the PR and are still expected to pass — treat a red non-required check as a
defect, not as noise.

| Check | Required | Workflow | Applies to |
|---|---|---|---|
| Ruff Lint | ✅ | `lint.yml` | every PR |
| MyPy Type Check | ✅ | `lint.yml` | every PR |
| Test Python 3.12 | ✅ | `test.yml` | every PR |
| Bridge TypeScript | ✅ | `lint.yml` | every PR (runs `tsc --noEmit` + `bun test` in `src/ouroboros/opencode/plugin`) |
| Native TUI (Rust) | — | `lint.yml` | every PR (`cargo test` in `crates/ouroboros-tui`) |
| Test Claude SDK (MCP 1) | — | `test.yml` | every PR |
| Issue link present | — | `pr-hygiene.yml` | every PR, unless exempt |
| enforce-boundary | — | `auto-boundary.yml` | `src/ouroboros/auto/**`, `cli/commands/auto.py` |
| enforce-module-size | — | `module-size.yml` | every `src/ouroboros/**/*.py` |
| enforce-envelope | — | `max-turns-envelope.yml` | any call passing `max_turns=1` |
| enforce-perf-budget | — | `auto-perf-budget.yml` | PRs touching `src/ouroboros/auto/` |

### Reproduce the required checks locally

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/ --fix
uv run mypy src/ouroboros
uv run pytest
```

Run `ruff format` **before** pushing, not after CI complains: the format check
is a required gate and a formatting-only failure costs a full CI round trip.

> **Local test caveat (shared worktrees).** A full `tests/unit/mcp` run has
> leaked to the real server and the real `~/.ouroboros` state in the past. When
> iterating locally inside a shared worktree, scope the run:
>
> ```bash
> uv run pytest tests/ --ignore=tests/unit/mcp --ignore=tests/integration/mcp \
>   --ignore=tests/e2e -n auto --dist worksteal
> ```
>
> CI runs the full suite in a clean container — that is where `tests/unit/mcp`
> belongs. See [Testing Guide](./testing-guide.md).

### Issue link present (`pr-hygiene.yml`)

Every PR must reference an issue — a plain `Refs #123` counts, a closing
keyword is not required. The gate reads GitHub's *rendered* PR body, so a `#N`
inside a code block or an HTML comment does not count.

It is exempt when **any** of these hold:

- the PR carries the **`no-issue`** label;
- the author is `dependabot[bot]`;
- the title starts with `chore(deps)`, `chore(release)`, or `release:`.

Note the exemption is on the *title prefix*. The historical release-commit
convention `chore: release vX.Y.Z` does **not** match — add the `no-issue`
label to a release PR, or change the title.

### enforce-boundary (`auto-boundary.yml`)

`ooo auto` core sources must stay domain-agnostic: no `github`, `pull_request`,
`jira`, `slack`, … as case-insensitive substrings (so `GitHubClient` and
`github_client` both trip it). Domain workflows belong in UserLevel plugins.

```bash
python3 scripts/check-auto-boundary.py
```

Escape hatch: append `# domain-keyword-allowed: <reason>` on the line and
justify it in the PR description. If you rename or delete a file listed in
`ANCHOR_FILES` inside the script, update that list in the same PR — the gate
fails loud rather than silently losing coverage.

### enforce-module-size (`module-size.yml`)

Any module under `src/ouroboros/` is capped at **2000 lines** (`SOFT_CAP`).
Modules that already exceeded it when the gate landed are listed in
`GRANDFATHERED` in `scripts/check-module-size.py`, each with its own budget.

```bash
python3 scripts/check-module-size.py --baseline-ref origin/main
```

The rules that trip people up:

- A grandfathered module **may shrink, never grow**. Adding lines to
  `parallel_executor.py` fails even though it is on the list.
- **Never add a new entry.** A new module over 2000 lines must be split.
- Shrink a module by more than `RESEED_SLACK` (200) and you must lower its
  budget in the same PR; drop it to `SOFT_CAP` or below and delete the entry
  entirely — it can never be re-added.
- The policy constants are read from the **baseline copy on `origin/main`**
  first, so raising `SOFT_CAP` in the same PR that grows a module does not
  work.

### enforce-envelope (`max-turns-envelope.yml`)

A call passing `max_turns=1` must pair it with `allowed_tools=[]` on the same
call (#781) — otherwise a single tool-use block burns the only turn and the
model never emits text.

```bash
python3 scripts/check-max-turns-envelope.py
```

Only literal forms count: `allowed_tools=[]`, or `[] if cond else None`. A
function call, a name reference, or a non-empty list fails.

### enforce-perf-budget (`auto-perf-budget.yml`)

Only applies when the PR touches `src/ouroboros/auto/`. It requires an R-run
comparison section in the **PR body** with a table whose Baseline / This PR /
Ratio cells are all filled — see `.github/PULL_REQUEST_TEMPLATE.md`. It does
not run benchmarks; it only proves the numbers are visible to reviewers.

```bash
python3 scripts/check-auto-perf-budget.py --body /path/to/body.md
```

Two behaviors worth knowing:

- It **reruns when the PR body is edited**, so deleting the section after a
  green run does not stick.
- It **fails closed**: if changed-path discovery fails, it errors rather than
  passing. A red gate here is not always about your table.
- Keep metric-shaped prose out of the surrounding text — the parser locks onto
  the first matching line.

## Releases

`release.yml` fires on a `v*` tag push and does the build, the GitHub Release,
and the PyPI publish. Never run `uv publish` locally — the local build produces
different hashes and PyPI rejects the duplicate.

Because `main` is protected, the order is:

1. Sync version-bearing metadata and commit it on a branch:
   ```bash
   python3 scripts/sync-plugin-version.py --write --version X.Y.Z
   uv lock
   ```
2. Open the release PR (`no-issue` label if the title is `chore: release vX.Y.Z`),
   get the four required checks green, squash-merge.
3. Pull the merged `main`, then tag **that** commit and push the tag:
   ```bash
   git checkout main && git pull origin main --ff-only
   git tag -a vX.Y.Z -m "Release vX.Y.Z" && git push origin vX.Y.Z
   ```
4. `release.yml` re-runs `sync-plugin-version.py --require-canonical` and
   refuses to build if the tag version and the tracked metadata disagree.

Version numbers come from git tags via `hatch-vcs`; `__init__.py` is never
hand-edited. Pushes to `main` between tags publish dev builds through
`dev-publish.yml`.

### Homebrew formula

After the PyPI publish, the `Bump Homebrew Formula` job updates
`Q00/homebrew-tap` so `brew install q00/tap/ouroboros-ai` tracks the release.
The formula vendors its whole dependency graph, so the job regenerates every
`resource` block via `scripts/bump-homebrew-formula.sh` rather than swapping the
url and sha256 alone.

The regenerated graph tracks `ouroboros-ai[mcp,tui]` — the same profile
`install.sh` selects by default — so `brew` and `install.sh` do not diverge in
which features are present. Change the extras in one place only: `FORMULA_EXTRAS`
in that script.

Two things this job needs, and how it fails without them:

- `HOMEBREW_TAP_TOKEN`, a PAT with `contents: write` on `Q00/homebrew-tap`.
  Missing, the job **fails the release run** on purpose. Skipping quietly is how
  the formula drifted from 0.51.1 to 17 releases behind before the job existed.
- A green `brew audit`, `brew install --build-from-source` and `brew test`,
  which run **before** the push. A formula that cannot build is worse than one
  that is a release behind, and the tap has no CI of its own.

Pre-release tags are skipped: the tap tracks stable only, and `v*.*.*` also
matches `v0.52.0b1`.

The GitHub Release is created by CI with auto-generated notes. To add a
curated narrative, **prepend** it and keep the generated list:

```bash
gh release view vX.Y.Z --json body -q .body > auto.md
cat curated.md auto.md > final.md
gh release edit vX.Y.Z --notes-file final.md
```

## When a gate blocks something it should not

Do not disable the gate, and do not widen an allowlist to make red go green.
Every gate on this page exists because something already broke. Use the
documented escape hatch, or open an issue proposing the policy change and say
in the PR why the current rule is wrong.
