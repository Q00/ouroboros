# Shared MCP Serve with Authenticated Project Contexts

## Status

Proposed, 2026-09-25. The owner decided three things:

- There is one shared `ouroboros mcp serve` per machine, for every MCP client
  (Claude Code, Codex, …).
- The command that clients already launch starts that server when it is
  missing and attaches to it when it is running.
- Attachment is authenticated, so the same model can later extend to other
  users and machines.

Refs #2325.

## Problem

Every MCP client session spawns its own `ouroboros mcp serve`. Each process holds its own
SQLite handles and its own copy of every bridged upstream. Production machines
reached 48–119 serve processes, SQLite write-lock contention and gigabytes of
duplicated upstream trees (#2325).

Four security advisories share one cause with this. At import,
`config/loader.py` pours the current directory's `.env`, which ships with
whatever repository the user cloned, into the process-wide `os.environ`. A
denylist (`config/untrusted_env.py`: 82 names plus 11 prefixes) then filters it. Every advisory so far was a name the
denylist missed: `*_CLI_PATH`, the `CODEX_HOME` family, `OUROBOROS_CLI` and
`PYTHONPATH`.

## Root cause

**Values that belong to a project are held as process-global state.**

- **Project directory.** 55 reads of `Path.cwd()` or `os.getcwd()` outside
  `cli/`. Only a handful decide anything. At least 25 are `x or os.getcwd()`
  fallbacks, and provider constructors add more of the form
  `cwd if cwd is not None else os.getcwd()`. `parallel_executor.py` repeats
  `self._task_cwd or self._adapter.working_directory or os.getcwd()` 18
  times. `create_ouroboros_server` bakes the launch cwd into the default
  runtime, every stage model adapter, the fan-out workspace root and
  `ProjectStatusHandler`. Detached jobs start in `Path.cwd()` with no override
  (`mcp/tools/background.py`).
- **Environment.** The project `.env` is imported into `os.environ`. The login
  shell env is merged in, and the first launcher's values win. Subprocess
  spawn sites derive the child env from `os.environ`.
- **Runtime and backend.** They are resolved once at composition and stored on
  handler fields. Per-call resolvers read `OUROBOROS_*` from `os.environ`.

With one session per process these defaults happen to be right. A shared
server makes them wrong, and the untrusted `.env` makes them exploitable.

## Concepts

| Concept | Meaning | Existing anchor |
|---|---|---|
| **Principal** | Who is calling, the result of authentication | `AuthContext` from `Authenticator` (`mcp/server/security.py`, `mcp/server/auth.py`) |
| **ProjectContext** | Which project the work acts on: canonical project dir, runtime, LLM backend, and an env overlay | New. Replaces process cwd and `os.environ` as the source of project-scoped values. Not named "Workspace": `mcp/server/workspace.py` already uses that name for the `--workspace-root` confinement policy |
| **Node** | Which machine produced an event | New id, recorded on events |

A session is an attachment of one Principal to one ProjectContext on one Node.

## Design

### 1. ProjectContext is resolved once, at the boundary, and is required below it

- A ProjectContext is built when a session attaches and cached by its key:
  `(realpath(project_dir), runtime, llm_backend, trusted env digest, project
  .env digest)`.
- Downstream signatures take `cwd: Path` and `env: Mapping[str, str]` as
  **required** values. Every `or os.getcwd()` and `else os.getcwd()` fallback
  is deleted. A missed call site becomes a mypy error in CI, not a silent run
  in the wrong repository.
- Composition-time bindings (stage adapters, default runtime, fan-out root,
  project status) move from `create_ouroboros_server` to per-ProjectContext
  construction.
- As a tripwire, the shared server `chdir`s at startup into an empty,
  read-only directory it owns. Any read of the process cwd that survives then
  points at nothing, so it fails loudly instead of acting on another project.

### 2. Project `.env` becomes data, filtered by an allowlist

- The shared server never imports a project `.env` into `os.environ` and never
  mutates `os.environ` after startup. A test pins that invariant.
- A ProjectContext's env is built from three layers with today's precedence
  (`config/loader.py` never overrides an already-set value): the session's
  trusted env (section 3), then the **allowlisted** keys of the project
  `.env`, then `~/.ouroboros/.env`. Keys outside the allowlist are ignored and
  logged by name only. A new key is inert until someone adds it on purpose. The
  allowlist lists names explicitly, never by suffix or pattern.
- Child processes (Claude/Codex workers, detached jobs, verify shells) are
  spawned with `build_child_env(base_env=project_context.env)`, never with
  `os.environ.copy()`. That child env always carries `_OUROBOROS_NESTED=1`, as
  children inherit it today from `mcp serve`, so a worker's own
  `ouroboros mcp serve` still exits instead of attaching; the existing
  exceptions that pop it (mechanical verification) keep doing so.
- Runtime and config reads go through the ProjectContext env view and not through
  `os.environ`.

The allowlist is a ceiling added **above** the existing denylist, which stays
as the floor:

- `UNTRUSTED_ENV_DENYLIST` and `UNTRUSTED_ENV_DENIED_PREFIXES` keep their
  meaning. No allowlist entry may be a key they deny, and a test pins
  `allowlist ∩ denied == ∅`. The trusted-only cost and routing controls
  (`OUROBOROS_EXECUTION_MODEL`, `OUROBOROS_AGENT_REASONING_EFFORT`,
  `OUROBOROS_MODEL_TIER_ROUTING`, …) therefore stay unavailable to a project
  `.env`.
- The initial allowlist is `OUROBOROS_LOG_MODE` only. Any key that changes cost
  or execution behavior, including per-stage model selection, is added only by
  an explicit maintainer decision recorded in PR 1a.

**Open decision: provider API keys** (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`OPENROUTER_API_KEY`, `GOOGLE_API_KEY`). A cloned repository that supplies its
own key routes the user's prompts, which contain their code, to an account it
controls. The recommendation is to deny them and document moving keys to
`~/.ouroboros/.env`. Base URLs stay denied.

This is a behavior change: today every project `.env` key the denylist does
not name takes effect. The
docs do not advertise project `.env` configuration. The release notes must
state the change.

### 3. Attach or spawn (client configs unchanged)

The stdio `ouroboros mcp serve` that Claude Code and Codex already launch does
the following:

1. Computes its **compatibility key**: protocol version, exact package
   version and the resolved EventStore path. Discovery is keyed by a digest of
   it: record `~/.ouroboros/run/serve-<digest>.json` (`{protocol, version, db,
   pid, start_time, socket}`), lock via `file_lock` on base path `~/.ouroboros/locks/serve-<digest>`. Servers
   with different keys never see or replace each other's record. The sequence
   version A → version B → version A therefore finds the still-live A server
   and never starts a second one.
2. When no live server exists for its key, it takes that lock with
   `core.file_lock.file_lock(..., stable_parent_authority=True)`, checks again,
   and spawns the shared server in a new session, in its empty cwd, with an **explicit**
   env: trusted process basics (`PATH`, `HOME`, `TMPDIR`, locale) plus
   `~/.ouroboros/.env`. It never passes its own `os.environ`, which carries the
   login-shell merge, `_OUROBOROS_NESTED` (checked by `mcp serve` at startup, so
   the spawned server would refuse to start) and host markers such as
   `CLAUDECODE`. It holds the lock until the new server's record is published
   or the spawn times out, so two launchers can never both spawn for one key.
3. The server publishes its record with `core.owner_only.write_owner_only`
   (0600, atomic) only after its socket is listening, and removes it with the
   same compare-and-delete on `(pid, start_time)` that the existing PID
   registry uses. A record whose `(pid, start_time)` is no longer live is stale:
   a launcher holding the lock replaces it. The shared server also keeps
   registering in the existing PID registry (`~/.ouroboros/mcp-servers/`), so
   the existing stale-record sweep and `mcp doctor` see it unchanged; the discovery record
   adds only what attaching needs (version, db, socket).
4. Connects to the server's unix socket, which lives in a per-key directory
   under `~/.ouroboros/run/` with a short name because of the macOS `sun_path`
   limit. The server creates it with `core.owner_only.secure_directory` and
   refuses to listen unless its mode is verified as 0700.
5. Authenticates (section 4), then sends the attach request: `project_dir`
   (its cwd), `client`, the per-session inputs from the table below and the
   session's trusted env. The trusted env
   is the launcher's own process env **minus every key the `.env` loader
   injected** (PR 1a makes the loader record them). It is what this session already sees
   today from its client (for example the runtime selectors a Codex
   `[mcp_servers.ouroboros.env]` sets), and it never contains project `.env`
   content. The server reads the project `.env` itself, through the allowlist.
6. Passes its own stdin/stdout descriptors over `SCM_RIGHTS`
   (`socket.send_fds`). The server then serves the session directly on the
   client's pipes, using `stdio_server(stdin=…, stdout=…)` with explicit
   streams (mcp==2.0.0 skips its fd claim in that case). The launcher relays
   nothing, never reads or probes stdin again (the stdin-peer probe and
   watchdog move to the server, which now sees the client's EOF directly), and
   waits only on its control connection. It exits when the server reports the
   session end, or when the server dies, so the client sees a disconnect.

The launcher must stay alive because the client treats its child's exit as a
server disconnect. The per-process orphan watchdog keeps running in the
launcher, tethered to the client ancestor it already resolves. The shared
server does not run that watchdog: its parent is whichever launcher spawned
it, and that launcher's exit must not end other sessions. Its lifetime is
governed only by the rule below, and a session ends on its pipes' EOF or
when its launcher's control connection closes.

Every current `mcp serve` option is accounted for:

| Option | Shared mode |
|---|---|
| `--transport` | Only `stdio` attaches. `sse` and `streamable-http` keep today's behavior unchanged |
| `--host`, `--port`, `--auth-token`, `--allow-remote`, `--allowed-host`, `--allowed-origin` | Network-only. Under stdio they already have no effect (`--auth-token` is ignored with a warning), which is unchanged |
| `--runtime`, `--llm-backend` | Per session, in the attach request; part of the ProjectContext key |
| `--workspace-root` | Per session, in the attach request (section 4) |
| `--idle-timeout` | Per session: the server ends that session after the timeout without a tool call, and the launcher exits, which is what the client sees today. Default stays disabled for stdio |
| `--db` | Server-global: part of the compatibility key, so a launcher with a different resolved EventStore path gets its own server and never writes into another one's store |

**Server lifetime:** the server exits after it has had zero attached sessions
and zero live jobs for a grace period. The activity clock is attachments and
jobs, not tool calls, so a long `start_auto` job survives its client closing.
Each compatibility key ends on its own clock; a new package version never
drains an older server that still has sessions or jobs.

**Fallback** to today's per-session in-process serve applies on Windows (no
`AF_UNIX` fd passing), when `OUROBOROS_SHARED_SERVE=0` (added to `UNTRUSTED_ENV_DENYLIST` in the
PR that introduces it, because the launcher still loads the project `.env`
through `config/loader.py`), or when attach
fails within a bounded timeout **before** descriptors are passed. After the
descriptors are passed, a failure is a disconnect, never a second server on
the same pipes.

### 4. Authentication and authorization

- **One credential authority.** Attaching presents a credential that the
  existing `Authenticator` verifies with `BEARER_TOKEN` semantics: the local
  owner secret is its `token_secret`, the launcher sends a signed
  `client_id:timestamp:signature` token, and verification uses
  `hmac.compare_digest`. The secret itself never crosses the socket. This
  yields an `AuthContext` (Principal). `SecurityLayer` keeps checking every tool call
  against the Principal. Local and future remote transports share this code;
  only the carrier differs.
- **Local owner credential.** It is generated on first use and stored in
  `~/.ouroboros/credentials/local-owner` (0600). It is **never** placed in an
  environment variable, because env propagates to children and has already
  leaked once (#2404). The unix socket's 0700 directory is defense in depth,
  not the authentication itself.
- **Project scope.** A Principal carries the ProjectContexts it may attach to. The
  local owner may attach to any. Principals added later (other users, machines,
  robots) default to none. Attaching to a ProjectContext outside the scope is
  refused before any ProjectContext is built.
- **Confinement.** The process-global `--workspace-root` policy
  (`set_workspace_policy`) becomes per session. The session's effective roots
  are its launcher's `--workspace-root` values intersected with the
  Principal's project scope, so one launcher's flag never widens or narrows
  another session. For the local owner, whose scope is unrestricted, this is
  exactly today's behavior, including "unset means any directory".
- Descriptor passing is accepted only on an authenticated connection, and only
  for exactly two descriptors.

### 5. Provenance on events

Events record `principal_id` and `node_id` next to the existing session and job
handles. Nothing consumes them yet. They exist so that sharing results across
nodes later (for example, federated exchange of verified outcomes rather than
raw data) can attribute every record.

## Security invariants (each backed by a test)

1. The shared server does not mutate `os.environ` after startup.
2. No project `.env` key outside the allowlist reaches any ProjectContext env or any
   child process.
3. No child process is spawned from `os.environ.copy()`. This is enforced by
   an AST spawn-site test; the one proposed in the open PR #2407 is extended
   if it lands first, otherwise it is added here.
4. Code below the attach boundary has no `os.getcwd()` / `Path.cwd()`
   fallback. An AST test covers this, and the empty-cwd tripwire backs it at
   runtime.
5. An unauthenticated connection cannot build a ProjectContext, pass descriptors or
   call a tool.
6. The credential and the session's trusted env never appear in argv, logs
   or events, and the credential never appears in any env.
7. A ProjectContext of repository B cannot change the env, cwd or config seen by a
   session in repository A.
8. The shared server is spawned with an explicit env and never inherits the
   launcher's `os.environ`. A test on the spawn call covers this.
9. A session's trusted env and per-session options apply only to that session:
   two launchers with different values each see their own.
10. Launchers with different compatibility keys never share a server.
11. Every child env carries `_OUROBOROS_NESTED=1` unless a spawn site pops it
    on purpose, and a nested `mcp serve` exits instead of attaching.
12. The exit of the launcher that spawned the shared server does not end the
    shared server or any other session.

## Pull request sequence

| PR | Delivers | Useful on its own |
|---|---|---|
| 1a | Project `.env` as allowlisted data above the existing denylist floor; `build_child_env(base_env=…)` at every spawn site; no `os.environ` mutation after startup | Yes: closes the untrusted-`.env` advisory class structurally. Label `security` |
| 1b | `ProjectContext` in a new module; required cwd, fallbacks deleted (mypy-driven). Grandfathered modules only shrink | Yes: removes silent wrong-directory fallbacks |
| 2 | Env and config reads moved from `os.environ` to the ProjectContext view; composition-time bindings made per-ProjectContext | Yes: prerequisite for sharing |
| 3 | Shared server: attach-or-spawn, descriptor passing, lifetime, authenticated attach, Project scope, provenance fields. Behind `OUROBOROS_SHARED_SERVE=1` | Opt-in |
| 4 | Default on after a soak; bridge upstreams run once per server | — |

## SDK assumptions (verified or to verify)

- Verified in mcp==2.0.0: the "second concurrent `stdio_server()`" guard
  raises inside `_claim_fd`, which explicit streams skip. Several sessions can
  therefore run in one process. `MCPServerAdapter.serve` must use the explicit
  path and not `run_stdio_async()`.
- To verify in the PR 3 harness: `Server.run()` running concurrently for many
  sessions (the SDK's own streamable-HTTP manager drives sessions through
  `serve_loop`), and `anyio.wrap_file` over a received **socket** descriptor.
  Claude Code hands its server sockets, not pipes. If `stdio_server` does not
  fit, build the small reader/writer stream pair directly.

## Non-goals

- Remote transport (TCP+TLS), a Principal registry and management UI,
  cross-node or federated exchange. The design leaves room for these, but none
  of them is built here.
- Changing client `.mcp.json` or plugin launch commands.
- Fixing exact contents in this RFC. The allowlist entries, the record and
  attach wire formats, and module placement are delivered by PRs 1a and 3,
  bound by the invariants above.

## Risks

- **Blast radius:** a crash of the shared server disconnects every session.
  Detached jobs survive because they are separate processes. In-process jobs
  do not.
- **Behavior change:** project `.env` keys outside the allowlist stop working.
- **Refactor breadth:** PRs 1–2 touch many call sites. The mypy-required
  signatures and AST invariant tests are what keep this safe.

## Evidence plan

Extend the socketpair harness from #2433. It lives in the PR body, not in
pytest, because pytest must not spawn real CLIs.

- Two clients in the same project: one server; one ProjectContext when their
  trusted envs are identical.
- Two projects: one server, two ProjectContexts, each child in its own directory.
- Repository B's `.env` sets a denied key: absent from B's and A's child env.
- An unauthenticated connection is refused before descriptor passing.
- All clients closed with no jobs: the server exits after the grace and no
  `mcp serve` or upstream processes remain.
- A job still running after its client closed: the server stays until the job
  reaches a terminal state.
- Mixed package versions, in the order A → B → A: one server per version, and
  the second A attaches to the first A.
- Different `--db`, `--workspace-root`, `--idle-timeout` or runtime selector
  env per launcher: each session sees its own values.
- Repository B's `.env` sets `OUROBOROS_EXECUTION_MODEL`: denied, as today.
- `SIGKILL` of the server: launchers exit, and the next attach respawns it.
