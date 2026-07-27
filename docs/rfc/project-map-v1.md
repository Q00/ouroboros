# Project Map V1 — cross-run read projection

> Issue: [#1389](https://github.com/Q00/ouroboros/issues/1389)

## Status and delivery slices

Project Map is a read-only, replayable view over EventStore history. It does
not introduce a project state machine and cannot steer execution, dispatch a
provider, alter evidence, or declare acceptance.

The V1 delivery is intentionally split:

1. **Identity anchor (implemented)** — one canonical resolver and additive
   `orchestrator.session.started` fields.
2. **Projection (planned)** — bounded EventStore queries plus frozen
   `ProjectRunSummary` and `ProjectRecord` values.
3. **Query surfaces (planned)** — read-only MCP/CLI output with JSON parity.

This document fixes the identity decision that previously blocked #1389. A
project is a deterministic label over run events, not a writable first-class
aggregate.

## Project identity

One V1 identity contains:

| Field | Contract |
| --- | --- |
| `project_id` | `project_` plus the full 32-character UUIDv5 hex digest |
| `project_root` | canonical, absolute, symlink-resolved source repository root |
| `workspace_path` | canonical POSIX path relative to the active checkout root; `.` at root |

The exact ID algorithm is:

```text
project_id = "project_" + uuid5(NAMESPACE_URL, project_root).hex
```

Repository moves and clones therefore receive a new V1 identity. Portable
remote-based identity is explicitly deferred.

### Direct checkouts

The resolver walks from the effective cwd to the nearest `.git` marker. A
normal checkout uses that root. A linked worktree is joined to its primary
source checkout only when its bounded `.git` pointer and `commondir` prove the
relationship. A submodule-style `.git` file without `commondir` remains a
separate project. Non-Git directories remain valid local-first projects and
use their canonical cwd with `workspace_path="."`.

### Managed task worktrees

Ouroboros `TaskWorkspace` already persists both generated checkout paths and
the durable source paths. Project identity uses `repo_root` plus the
source-relative `original_cwd`, never the generated `worktree_path`. Two task
worktrees for the same source/workspace therefore join one project. A source
workspace outside its declared root fails before session publication.

## Durable session anchor

Runner-owned new sessions resolve identity once before building the execution
contract. That same immutable value supplies:

- top-level `project_id`, `project_root`, and `workspace_path` on
  `orchestrator.session.started`; and
- the existing nested `execution_contract.frugality_proof.project_root` and
  `.workspace_path` fields.

The start event is already the immutable run-ownership record. Adding the
project fields there avoids a second write and makes a crash immediately after
session publication attributable. Reconstruction copies these fields into the
immutable `_session_start_identity` snapshot; later progress cannot replace
them.

Historical session starts without top-level identity remain readable. The
projection slice may identify an older row from its nested execution contract
and must label that source explicitly. If top-level and nested identities
conflict, it must fail the complete project query rather than return a partial
map.

## Authority boundary

Project identity is an indexing and attribution contract only:

- EventStore remains the source of truth.
- A `ProjectRecord` will be rebuilt from events and never written back as
  execution state.
- `project_id` does not authenticate a caller or authorize a provider effect.
- Project Map cannot turn provisional route success into acceptance; the Final
  Gate remains the sole acceptance authority.
- Workspace filtering cannot hide conflicts and then claim a complete project
  map. Truncation and identity conflicts must be explicit.

## V1 projection requirements

The next slice must:

1. replay every attributable session needed for the requested project;
2. reuse `SessionRepository` lifecycle semantics rather than inventing status;
3. return frozen, deterministic `ProjectRunSummary` and `ProjectRecord` values;
4. distinguish top-level anchors from compatible nested-only legacy identity;
5. reject conflicting identity and unmarked truncation; and
6. remain read-only at the storage boundary.

## V1 non-goals

- Seed shard or child-Seed graphs;
- writable milestones or a project decision ledger;
- Auto/Ralph lineage integration before they emit the common anchor;
- materialized project tables or expression indexes;
- dashboard grouping;
- repository move/clone portability;
- cross-run route learning, trust reuse, or automatic guardrail enforcement.
