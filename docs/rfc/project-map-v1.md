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
| `project_root` | canonical, absolute, symlink-resolved identity root; normally the source checkout, or a validated common Git directory for positively proven linked peers with no primary owner |
| `workspace_path` | canonical POSIX path relative to the active checkout root; `.` at root |

The exact ID algorithm is:

```text
project_id = "project_" + uuid5(NAMESPACE_URL, project_root).hex
```

Repository moves and clones therefore receive a new V1 identity. Portable
remote-based identity is explicitly deferred.

### Direct checkouts

The resolver walks from the effective cwd to the nearest `.git` directory
entry. Every entry shape is a discovery boundary: a broken symlink or other
malformed child marker stays scoped to that child instead of inheriting a
parent repository. A positively proven bare repository is detected before a
directory literally named `.git` can be interpreted as its parent's marker. A
normal checkout uses its resolved owner. A linked worktree is joined to its
primary source checkout only when its bounded `.git` pointer, `commondir`,
worktree record, and backlink prove the relationship. Submodules use their
configured worktree and therefore remain separate projects.

Each Git pointer is parsed as one complete UTF-8 record bounded to 4,096 bytes,
with only one optional final line ending. Extra records, oversized content,
NULs, invalid UTF-8, target-leading or surrounding whitespace, and symlinked
records invalidate the topology proof; a valid first line cannot hide malformed
trailing data, and pointer paths never receive shell-style `~` expansion.

A direct gitfile without `commondir` proves ownership only when its bounded Git
core config names the active checkout as `core.worktree` and that checkout
points back to the same Git directory. An alias to another checkout `.git` or
configured submodule Git directory therefore stays separate. Core section names
are interpreted case-insensitively with later values winning, matching Git.
The bounded core parser accepts Git section comments, valueless boolean
shorthand, empty boolean values, quoted values, documented escapes, inline
comments, variable-value backslash continuations, and Git's signed 32-bit
numeric boolean forms, including octal, hexadecimal, and `k`/`m`/`g` scaling.
Boolean tokens are ASCII-bounded before case normalization. Section headers
cannot span physical lines and fail closed before continuation folding, while a
same-line section assignment may continue its value after the closing bracket.
Modern subsection suffixes must be consumed completely as one quoted value;
embedded quotes and backslashes use Git's `\"` and `\\` escapes, while a
backslash before another character is discarded as Git specifies. Raw interior
quotes or trailing subsection junk cannot authorize later identity data.
Includes fail
closed because they escape the bounded config file used for identity proof.

When the common config enables `extensions.worktreeConfig`, the resolver reads
the bounded, regular, non-symlink main-worktree `config.worktree` after the
common `config`, matching Git's later-value override order for `core.bare` and
`core.worktree`. A missing worktree config is an empty overlay; malformed,
oversized, or including worktree config cannot prove an identity owner. This
keeps direct, positively proven linked, and managed paths on the same explicit
main-worktree owner when Git stores that owner outside the common config.
`core.worktree` is interpreted verbatim: absolute values stay absolute and all
relative values, including a leading `~` component, resolve from the Git
directory without consulting process `HOME`. A positively proven explicit
owner is evaluated before the common directory's basename; an external common
directory named `.git` therefore cannot be mistaken for its parent checkout.
The explicit owner must point back to the common directory through either a
bounded regular gitfile or its exact regular, non-symlink `.git` directory;
both standard and external Git-directory representations therefore converge.
A directory-backed checkout passes through this same owner resolver rather than
receiving an early parent-directory identity.

Git does not persist the primary working-tree path for a non-bare repository
created with `--separate-git-dir` unless `core.worktree` is configured. Without
that owner, the direct checkout stays scoped to itself; copying its gitfile
cannot claim a durable identity. Linked peers that separately prove membership
through `commondir`, one worktree record, and a backlink may share the validated
external common directory. When `core.worktree` is configured, the direct and
linked paths share that explicit owner. A bare common repository that positively
owns linked worktrees likewise uses its common directory for those peers.
Malformed or unproven metadata stays scoped to the active checkout. Non-Git
directories remain valid local-first projects and use their canonical cwd with
`workspace_path="."`.

### Managed task worktrees

Ouroboros `TaskWorkspace` already persists both generated checkout paths and
the durable source paths. Project identity uses `repo_root` plus the
source-relative `original_cwd`, never the generated `worktree_path`. Two task
worktrees for the same source/workspace therefore join one project. A source
workspace outside its declared root fails before session publication.
Omission of `source_workspace` intentionally selects the source root, while an
explicit empty or otherwise malformed workspace value fails validation instead
of silently widening scope to `workspace_path="."`.

## Durable session anchor

Runner-owned new sessions resolve identity once before building the execution
contract. That same immutable value supplies:

- top-level `project_id`, `project_root`, and `workspace_path` on
  `orchestrator.session.started`; and
- the existing nested `execution_contract.frugality_proof.project_root` and
  `.workspace_path` fields.

The unresolved input state and the resolved-absence result are distinct. If a
runner has no identity, that single result is preserved on both publication
surfaces and is never interpreted as permission to invoke the resolver again.

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
