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
parent repository.

Git owns every Git-format decision. The resolver invokes the installed `git`
binary with argv rather than a shell and asks it for:

- the absolute common directory via `rev-parse --path-format=absolute
  --git-common-dir`;
- bare status via `rev-parse --is-bare-repository`;
- the primary checkout via `worktree list --porcelain -z`; and
- the configured top level via `rev-parse --path-format=absolute
  --show-toplevel`.

The process environment removes caller-supplied `GIT_*` overrides and gives Git
a fixed neutral `HOME`, so home-relative local includes cannot change identity
between start and resume. The central untrusted-project `.env` boundary rejects
platform home selectors and dynamic-loader controls (`LD_*`, `DYLD_*`, and
platform equivalents). Git queries disable global/system config and prompting
and set a five-second timeout. Only complete UTF-8 paths from successful
commands with at most 1 MiB of stdout are accepted. When a checkout marker is
present, it is passed back to Git explicitly as `--git-dir`; a malformed nested
marker therefore cannot be skipped in favor of a parent repository.
Primary-top-level discovery is likewise bound to the already validated common
directory, so a markerless reported path cannot fall through to an unrelated
ancestor checkout. Acceptance of that argument is not ownership proof: the
active checkout must also appear in Git's returned worktree population or equal
Git's configured top level for an explicit `core.worktree` owner.

Git availability is a separate outcome from topology rejection. The resolver
first proves that the installed Git command can run without depending on the
candidate path. Spawn errors, timeouts, and oversized output raise a transient
identity-unavailable error; they never publish the active checkout as a local
fallback. A paused public resume preserves its durable state and live authority
generation while releasing the exclusive claim for retry. Git successfully
answering but rejecting a repository/config/topology remains the only path to
the conservative local boundary.

This deliberately does not reimplement `config.c`. BOM handling, whitespace,
comments, quoting, continuations, numeric booleans, later-value precedence,
`extensions.worktreeConfig`, includes, `core.worktree`, gitfiles, `commondir`,
`HEAD`, and submodule/worktree metadata all have exactly the semantics of the
installed Git version. A rejected config or topology cannot contribute a
cross-checkout identity and falls back to the nearest active checkout boundary.

A standard checkout and its linked worktrees use Git's primary configured top
level. An explicit `core.worktree` owner therefore wins for direct, linked, and
managed callers. A bare repository and its linked worktrees use the absolute
common directory, including when that directory is literally named `.git`.
Git is asked whether the active directory is bare before an enclosing checkout
marker is adopted, so a markerless bare repository nested inside another
checkout remains a separate project and still joins its registered worktrees.
Bare attribution additionally requires Git to validate `HEAD` as either a
symbolic/unborn ref (`symbolic-ref HEAD`) or a detached object
(`rev-parse --verify HEAD^{object}`); an arbitrary nonempty record cannot join
linked worktrees.
An initial `--separate-git-dir` checkout without an explicit `core.worktree`
owner is intentionally not joined: Git records no worktree membership or
backlink that distinguishes it from an arbitrary redirected gitfile. Registered
linked worktrees may still join the common bare owner. Submodules use their own
Git-reported top level and remain separate projects. Non-Git directories remain
valid local-first projects and use their canonical cwd with
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

Runner-owned new sessions must resolve an identity before publication. Resolved
absence fails session creation, so it cannot be confused with a historical
start event that predates these fields. `SessionRepository.create_session`
also rejects identity-free execution contracts, top-level-only, nested-only,
partial, or conflicting identity payloads before appending the immutable start
event. Contract-free utility sessions remain valid; historical events are read
without being recreated through this API.

The shared provider-neutral worker cwd boundary normalizes direct runtimes,
leader-driven runtimes, and runner/executor `task_cwd` overrides to one absolute
path, resolving relative inputs against construction-time process cwd. If an
omitted cwd is unavailable, the boundary preserves `None`; the runner may still
select an explicit task/runtime path and raises its domain error only when no
usable workspace exists. Persistent Claude transport and runtime objects share
the same normalized value for both spawn and resume. The resulting concrete
workspace is therefore available before a runner-owned session publishes its
mandatory identity and remains the path passed to provider subprocesses.

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
