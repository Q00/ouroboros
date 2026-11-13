# Docs Freshness

How this repository keeps its documentation true as the code moves, and what
to do when a docs check fails on your PR.

Tracking issue: [#2921](https://github.com/Q00/ouroboros/issues/2921).

## The problem, measured

A scan of `main` at `592c667f` (2026-11-06):

| Symptom | Count |
|---|---|
| Markdown files under `docs/` | 157 (241 repo-wide) |
| `src/ouroboros/...` paths named in docs that no longer exist | 8 (1 in maintained docs, the rest in RFCs and dated plans) |
| `OUROBOROS_*` names in maintained docs that no code, script, or workflow reads | 7 (`OUROBOROS_<BACKEND>_RPM` names such as `OUROBOROS_CODEX_RPM` look stale to a grep but are built at runtime by `orchestrator/backend_limits.py`, which is why checks derive names from the code) |
| Top-level CLI commands with no section in `docs/cli-reference.md` (`pm`, `plugin`, `workflow-ir`) | 3 of 26 |
| Source commits since `llms.txt` / `llms-full.txt` were last edited (2026-08-12) | 311 |
| Docs under `docs/` not reachable from `docs/README.md` | 95 of 157 |
| Dated plans, handoffs, and analyses (2026-05, 2026-07) at the top level of `docs/`, next to the reference pages | 4 |

None of these is a single big error. They add up: each one sends a user, or an
AI agent reading `llms.txt`, to a path, variable, or command that does not
exist, and nothing reports it until someone trips over it.

## Root cause

Every fact in a doc is **restated by hand** from the code, and nothing ties the
restatement back to its source. Drift is found only when someone hits it.

The repository already has the right instinct, applied one incident at a time.
`tests/unit/test_cli_surface_docs_contract.py`,
`tests/unit/test_cli_reference_backend_parity.py`,
`tests/unit/config/test_model_document_contract.py`, and
`tests/unit/test_tui_usage_docs_contract.py` each guard one claim that broke
once. What is missing is the general version. The *Documentation Decay
Detection* checklist in `CONTRIBUTING.md` is manual, so it runs only when a
reviewer remembers it, and it has gone stale itself: it still tells reviewers
to grep for `opencode` lacking a `[Not yet available]` marker, directly below
a line saying `opencode` is fully implemented.

Asking people to try harder does not scale to 157 files. The fix is to make
each kind of drift **fail in the PR that causes it**. That PR's author renamed
the flag and still has the context to update the doc.

## The rule: classify the fact, then pick the cheapest mechanism that keeps it true

| Kind of fact | Examples | Mechanism |
|---|---|---|
| Enumerable from a registry in code | CLI commands and flags, config keys, MCP tool names, bundled agent names, `OUROBOROS_*` variables | **Verify** against the registry in CI; **generate** where the doc is just a list |
| Reference to a repository artifact | `src/...` paths, relative links | **Verify** existence in CI |
| Behavioral claim in prose | "auto stops after N repair rounds" | **Trigger**: changing the mapped source flags the doc for review |
| Point-in-time record | plans, handoffs, audits, marketing drafts | **Quarantine** in `docs/history/` behind the snapshot banner; not maintained |

### 1. Verify: pytest checks in the required test job

Checks live in `tests/unit/docs/`, one module per kind of fact. They share
`tests/unit/docs/_corpus.py`, which decides once which files count as
maintained docs.

| Check | Fails when a maintained doc… |
|---|---|
| `test_repo_paths.py` | names a repository path or relative link target that does not exist |
| `test_env_vars.py` | names an `OUROBOROS_*` variable that no code, script, or workflow reads |
| `test_tool_and_agent_names.py` | names an MCP tool that is not registered, or a bundled agent with no definition file |
| `test_cli_reference.py` | omits a registered top-level command, or shows a flag the command does not accept |
| `test_config_reference.py` | omits a config key from `config/models.py`, or documents a key the models do not have |

They run as ordinary pytest, so they are part of the required
`Test Python 3.12` check. There is no new status check and no change to branch
protection. A failure prints `path:line`, the stale token, and what to do.

**The corpus.** It covers `README*.md`, `CONTRIBUTING.md`, `llms.txt`,
`llms-full.txt`, `docs/**/*.md`, and `skills/**/*.md`, including translations,
since paths and names do not change with language. Excluded on purpose:
`docs/history/`, `docs/rfc/`, and `docs/research/`. Those are point-in-time
records, and an RFC may name a path that does not exist *yet*.

**When a name must not exist.** Some docs name a nonexistent thing on purpose,
such as the severity rubric's example of a variable that does nothing. Put
`<!-- docs-drift: ignore -->` on that line, or bracket a region with
`<!-- docs-drift: ignore-start -->` and `<!-- docs-drift: ignore-end -->`. The
marker does not render. Each use should be explainable in one sentence of
review.

### 2. Generate: lists that mirror a registry

Where a doc section is only a copy of a registry (the command and tool
catalogs in `llms.txt` and `llms-full.txt`), the section is rendered from the
code between markers:

```markdown
<!-- generated:cli-commands begin -->
…rendered from cli/main.py…
<!-- generated:cli-commands end -->
```

A test asserts that each block matches what the generator would write now. Its
failure message gives the one command that regenerates it. Hand-written text
outside the markers is untouched.

### 3. Trigger: behavioral prose

Prose about behavior cannot be checked mechanically, so the goal is to route a
reviewer's attention to it, not to judge it. `docs/doc-map.yml` maps source <!-- docs-drift: ignore -->
globs to the docs that describe them. It is the machine-readable form of the
*Doc Mapping* tables in `CONTRIBUTING.md`. A non-required workflow lists, in
the PR's job summary, the mapped docs that a PR did not touch. It is advisory
by design. Whether it should ever block is a maintainer decision, best made
after it has produced some data.

### 4. Quarantine: point-in-time records

Plans, handoffs, analyses, and drafts move to `docs/history/` behind the
existing snapshot banner (see
[Master Roadmap 2026-07](../history/master-roadmap-2026-07.md)). They stay
available as provenance, stop competing with the reference pages, and leave
the verify corpus. A new dated plan belongs in `docs/history/` or in an issue
from the day it is written.

## When a docs check fails on your PR

1. Read the failure. It names the file, the line, and the stale token.
2. If your PR renamed or removed the thing, update the doc in the same PR.
   That is the point of the check.
3. If the doc is a point-in-time record, move it under `docs/history/` with
   the banner instead of editing history.
4. If the line names a nonexistent thing on purpose, add the ignore marker and
   say why in the PR description.
5. Do not widen `_corpus.py` exclusions to make a failure go away. Corpus
   rules change only in a dedicated PR.

## What this does not do

- It does not verify behavioral prose. The trigger layer routes it to a
  person.
- It does not check translation parity. Translated docs are in the corpus for
  names and paths only.
- It does not add a required status check, and it does not touch branch
  protection.
- It does not rewrite existing docs wholesale. Each check lands together with
  the fixes for the drift it finds, so `main` stays green.

## Rollout

Each item is an independent PR against `main`. Status is tracked in
[#2921](https://github.com/Q00/ouroboros/issues/2921).

| Item | Layer | Deliverable |
|---|---|---|
| D1 | — | This policy |
| D2 | Verify | Repository paths and relative links |
| D3 | Verify | `OUROBOROS_*` environment variables |
| D4 | Verify | MCP tool and bundled agent names |
| D5 | Verify | CLI commands and flags in `cli-reference.md` |
| D6 | Verify | Config keys in `config-reference.md` |
| D7 | Quarantine | Move dated plans and handoffs into `docs/history/` |
| D8 | Generate | Command and tool catalogs in `llms.txt` / `llms-full.txt` |
| D9 | Trigger | `docs/doc-map.yml` and the advisory doc-impact summary | <!-- docs-drift: ignore -->
