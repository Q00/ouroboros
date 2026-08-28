# Project Map V1 — cross-run read projection

> Issue: [#1389](https://github.com/Q00/ouroboros/issues/1389)

## Status and delivery slices

Project Map is a read-only, replayable view over EventStore history. It does
not introduce a project state machine and cannot steer execution, dispatch a
provider, alter evidence, or declare acceptance.

The V1 delivery is intentionally split:

1. **Identity anchor (implemented)** — one canonical resolver and additive
   `orchestrator.session.started` fields.
2. **Projection (implemented)** — complete EventStore history plus frozen
   `ProjectRunSummary` and `ProjectRecord` values.
3. **Query surfaces (implemented)** — read-only MCP/CLI output with JSON parity.

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
malformed child marker cannot inherit a parent repository or publish an
identity while Git cannot validate that boundary.

Git owns every Git-format decision. The resolver invokes the installed `git`
binary with argv rather than a shell and asks it for:

- the absolute common directory via `rev-parse --path-format=absolute
  --git-common-dir`;
- bare status via `rev-parse --is-bare-repository`;
- the primary checkout via `worktree list --porcelain -z`; and
- the configured top level via `rev-parse --path-format=absolute
  --show-toplevel`.

This grammar requires Git 2.36.0 or newer. Every public resolver validates the
bounded `git --version` result before any topology query. An older or
unrepresentable version raises a typed, non-retryable configuration error;
spawn failures and timeouts remain retryable unavailability.

The process environment removes caller-supplied `GIT_*` overrides and gives Git
a fixed neutral `HOME`, so home-relative local includes cannot change identity
between start and resume. The central untrusted-project `.env` boundary rejects
platform home selectors and dynamic-loader controls (`LD_*`, `DYLD_*`, and
platform equivalents). Git queries disable global/system config and prompting
and set a five-second timeout. Only complete UTF-8 paths from successful
commands with at most 1 MiB of stdout are accepted. When a checkout marker is
present, it is passed back to Git explicitly as `--git-dir`; a malformed nested
marker therefore cannot be skipped in favor of a parent repository.
Each scalar path query returns one Git-owned value; the resolver removes only
Git's final LF terminator, so a legal POSIX newline inside a checkout path is
preserved rather than mistaken for a second record. An undecodable, truncated,
NUL-bearing, or otherwise unrepresentable successful response is transient
identity unavailability, never evidence for a fallback identity.
Fresh resolver inputs and every Git-reported ownership path must also still be
an actual directory at resolution time. Historical anchors may describe a path
that no longer exists, but a missing path can never be published as a new
identity. Immediately before returning, each public resolver revalidates the
complete input, checkout-root, and project-root directory population recorded
by topology resolution, including each canonical path's filesystem device and
inode generation. Deletion, same-path replacement, or symlink rebinding during
a Git query therefore cannot become a fresh durable anchor.
Primary-top-level discovery is likewise bound to the already validated common
directory, so a markerless reported path cannot fall through to an unrelated
ancestor checkout. Acceptance of that argument is not ownership proof: the
active checkout must also appear in Git's returned worktree population or equal
Git's configured top level for an explicit `core.worktree` owner.

Git availability is a separate outcome from positive topology proof. The
resolver first proves that the installed Git command can run without depending
on the candidate path. Spawn errors, timeouts, oversized output, and every
unproven nonzero query after a repository marker or bare-repository shape is
discovered raise a transient identity-unavailable error; Git has no portable
exit-code distinction between malformed topology and temporary repository I/O,
so neither may publish a fallback identity. The expected `symbolic-ref` miss for
a detached `HEAD` is accepted only when a second `rev-parse --verify
HEAD^{object}` succeeds. A paused public resume preserves its durable state and
live authority generation while releasing the exclusive claim for retry. A
genuine local directory is selected without a repository query only when
neither a checkout marker nor the standard bare `HEAD`/`objects`/`refs` shape
exists. Successful Git responses that do not positively prove ownership may
still use the conservative active-checkout boundary.

This deliberately does not reimplement `config.c`. BOM handling, whitespace,
comments, quoting, continuations, numeric booleans, later-value precedence,
`extensions.worktreeConfig`, includes, `core.worktree`, gitfiles, `commondir`,
`HEAD`, and submodule/worktree metadata all have exactly the semantics of the
installed Git version. A config or topology query that exits nonzero cannot
contribute any durable identity. A successful query whose returned worktree
population does not own the active checkout remains scoped to that checkout.

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
workspace outside its declared root fails before session publication. Both the
declared source root and generated worktree root must exactly equal the
checkout roots proven by Git; workspace scopes are then derived from those
proven roots rather than from caller-selected nested directories. The generated
`effective_cwd` is independently resolved as a direct checkout and must produce
the same canonical project root and workspace scope. A restored ordinary
directory, foreign checkout, moved worktree, or nested root that collapses a
real subdirectory to `.` therefore cannot reuse persisted `TaskWorkspace`
metadata to claim the source identity on a fresh run or resume. All source and
execution directories are revalidated together after both topology queries.
At publication, the provider cwd and frozen task cwd must name the same live
directory, not merely have equal raw strings; canonical-equivalent symlinked
worktree paths are then validated through the managed resolver using the actual
provider cwd.
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
event. Before the asynchronous publication check, the repository detaches one
sanitized contract snapshot; both validation and event persistence consume that
same snapshot, so caller mutation cannot split the top-level and nested anchors.
It then re-resolves the runner's concrete workspace through the same Git-backed
resolver at the persistence choke point and compares the complete identity. A
workspace deleted, replaced by a file, moved to another checkout, or rebound
through a symlink after contract construction cannot be published. This
subprocess-backed final resolution runs in a worker thread, so bounded Git
timeouts cannot block cancellation, heartbeats, or unrelated session work on
the orchestrator event loop.
Contract-free utility sessions remain valid; historical events are read without
being recreated through this API.

The shared provider-neutral worker cwd boundary normalizes direct runtimes,
leader-driven runtimes, and runner/executor `task_cwd` overrides to one absolute
path, resolving relative inputs against construction-time process cwd. If an
omitted provider cwd is unavailable, the boundary preserves `None`. Resolution
failure for an explicit cwd instead propagates and cannot silently select the
process cwd. The one resolution result, including resolved absence, is wrapped
and shared with every factory runtime and command dispatcher so downstream
constructors cannot reinterpret `None` independently. An explicit task path
does not replace an absent provider owner because the provider would still
execute with its retained value. Preparation instead requires task,
runtime-handle, and provider cwd owners to agree before publication, and it
cannot infer provider ownership from the runner's later process cwd. Persistent
Claude transport and runtime objects share the same normalized value for both
spawn and resume.
The resulting concrete workspace is therefore available before a runner-owned
session publishes its mandatory identity and remains the path passed to
provider subprocesses.

The start event is already the immutable run-ownership record. Adding the
project fields there avoids a second write and makes a crash immediately after
session publication attributable. Reconstruction copies these fields into the
immutable `_session_start_identity` snapshot; later progress cannot replace
them.

Historical session starts without top-level identity remain readable. The
projection slice may identify an older row from its nested execution contract
and must label that source explicitly. If top-level and nested identities
conflict, it must fail the complete project query rather than return a partial
map. Complete top-level anchors from the public low-level event producer also
remain readable when no execution contract exists; nested identity is compared
only when it is present.

### Legacy pre-anchor sessions (transitional)

Sessions started before the anchor landed carry no `project_id` on their
durable `orchestrator.session.started` event. Resume detects this from the
persisted start-identity snapshot itself (`has_project_anchor` is false) —
never from a version heuristic — and takes a preserved legacy path instead of
the resolver: the exact pre-anchor direct-cwd (or managed-task-workspace)
representation is reproduced for the workspace comparison. Rewriting these
sessions under the current resolver would make resume disagree with their own
immutable start events.

This dual representation is a transitional surface, not a second contract:

- **Implementation.** The legacy representation and its activation event live
  in `orchestrator/legacy_identity.py`; the consuming branch in the runner
  carries a comment pointing back here.
- **Observability.** Every activation emits the structured log event
  `project_map.legacy_identity_path` with `entry_point`
  `resume_workspace_comparison` and a `prepared_live_execution` context flag.
  An activation is counted only when a durable start-identity snapshot is
  present and still lacks the anchor; current prepared executions restore an
  intentionally anchorless contract-only snapshot and never count. The event
  is an advisory liveness signal only — the default file sink rotates away
  after `LoggingConfig.max_log_days` (seven) days, file logging may be
  disabled, and a sink that cannot be created is skipped — so absence from
  available logs can never prove inactivity and carries no removal
  authority.
- **Removal criterion.** The seam is a package-wide project-identity
  compatibility contract. EventStores are local and independently configured
  per installation, so no store inspection — maintainer-side or otherwise —
  can establish that other installations hold no pre-anchor sessions, and no
  such inspection authorizes removal. Removal is instead governed by a finite
  support window this RFC declares: every release published before 2027-07-29
  must retain the pre-anchor project-identity representation for sessions
  started before the anchor (2026-07-29). This window governs only the identity
  seam; it does not bypass independently versioned execution-contract,
  provider, permission, or workspace compatibility gates, all of which remain
  fail-closed. After the window ends, a release may delete
  `legacy_identity.py` and the runner's legacy branch only as a documented
  breaking change that simultaneously replaces the branch with a
  fail-closed rejection: when `_project_start_identity` finds a persisted
  start-identity snapshot without the complete anchor, resume must raise a
  typed error naming the last identity-compatible release — never silently
  rewrite the session under the current resolver, and never strand it without
  explanation. Operators who want to know whether the identity cutover affects
  their installation can run a per-installation inventory preflight before
  upgrading — enumerate sessions with `EventStore.get_all_sessions()`,
  read each persisted `_session_start_identity` snapshot, and treat any
  session that cannot be enumerated or read as pre-anchor — but this
  preflight is a verification tool for that one store only; it grants no
  package-wide removal authority.

## Authority boundary

Project identity is an indexing and attribution contract only:

- EventStore remains the source of truth.
- A `ProjectRecord` is rebuilt from events and never written back as
  execution state.
- `project_id` does not authenticate a caller or authorize a provider effect.
- Project Map cannot turn provisional route success into acceptance; the Final
  Gate remains the sole acceptance authority.
- Workspace filtering cannot hide conflicts and then claim a complete project
  map. Truncation and identity conflicts must be explicit.

## V1 projection

`ProjectMapBuilder` ships these invariants without a second state model:

1. `EventStore.get_all_sessions()` enumerates the complete lifecycle history;
2. `SessionRepository.reconstruct_session()` remains the only owner of status,
   while the projection opts into its strict related-event read so a storage
   failure cannot publish a lifecycle result from incomplete history;
3. frozen `ProjectRunSummary` and `ProjectRecord` values have deterministic
   ordering and JSON serialization;
4. compatible nested-only historical anchors are labeled
   `identity_source="execution_contract"`;
5. project conflicts are validated before workspace filtering can hide them;
6. an explicit limit below the attributable population raises a typed error
   instead of returning an unmarked recent window; and
7. projection performs no EventStore write and grants no execution authority.

## V1 query surfaces

`ouroboros_project_status` and `ouroboros status project` share the same
read-only handler. Both resolve the canonical Project Map identity from an
explicit project/workspace directory or the caller directory, optionally filter
by one canonical repository-relative workspace, and apply a positive complete-run
safety limit. A limit below the attributable population, an identity conflict,
or any reconstruction/storage failure returns an error with no partial
`ProjectRecord`.

The MCP tool publishes the serialized `ProjectRecord` as `structuredContent`;
`ouroboros status project --json` emits that exact object with deterministic key
ordering. Handler-owned EventStores are opened with SQLite `mode=ro` and
`initialize(create_schema=False)`. The composition root may inject its shared
EventStore, but the handler invokes only read methods and still skips schema
creation. Neither surface writes materialized project state, controls execution,
or contributes acceptance evidence.

## V1 non-goals

- Seed shard or child-Seed graphs;
- writable milestones or a project decision ledger;
- Auto/Ralph lineage integration before they emit the common anchor;
- materialized project tables or expression indexes;
- dashboard grouping;
- repository move/clone portability;
- cross-run route learning, trust reuse, or automatic guardrail enforcement.
