# RFC: UserLevel Plugin Layer

## Status

**Accepted (2026-05-07)**, supersedes the growth-oriented portions of #725.

This RFC pins the framing decisions converged on in #725 and the seven
contract decisions locked in [Q00/ouroboros-plugins](https://github.com/Q00/ouroboros-plugins)
issues #5–#11. Future debates about "should this go in core?" or "should we
add this to `ooo auto`?" should be answered against this document.

## Motivation

Ouroboros core risks expanding indefinitely as new operational workflows are
proposed. #689's GitHub-PR work was the inflection point: it crossed two
boundaries simultaneously — it was neither an OS primitive nor part of the
`ooo auto` product boundary, yet there was no third home. The defense-oriented
plugin layer described here is that third home.

The plugin layer exists to **keep core small**, not to grow ecosystem surface
area. Specifically:

- We do not pursue plugin count, marketplace dynamics, or "ecosystem health"
  as success metrics. The success metric for Ouroboros remains the strength
  of the spec-first discipline (Interview / Seed / Evolve / Provenance) and
  the quality of execution under that discipline.
- Reference plugins are deliberately few, high-quality, and maintained by
  core authors or co-maintainers — not a long-tail catalog.
- Lock-in for Ouroboros comes from the spec-first discipline and the
  durable substrate (ledger, provenance, seed history), not from how many
  plugins exist on top.
- The plugin layer is plumbing. It exists invisibly to prevent core bloat.
  It is not a product surface.

## Layer Model

```text
+-------------------------------------------------------------------+
|                Installable UserLevel Programs                      |
|                                                                   |
|  github-pr-ops   merge-assistant   jira-sync   linear-triage       |
|  slack-incident  release-coordinator  customer-debugger  ...       |
+-------------------------------+-----------------------------------+
                                |
                                | plugin contract / declared scopes
                                v
+-------------------------------------------------------------------+
|                First-party UserLevel Programs                      |
|                                                                   |
|  ooo auto     ooo run     ooo pm     ooo review?     ...           |
|                                                                   |
|  Product-level workflows maintained with Ouroboros, but still      |
|  programs above core rather than core itself.                      |
+-------------------------------+-----------------------------------+
                                |
                                | stable OS primitives
                                v
+-------------------------------------------------------------------+
|                         Ouroboros Core / OS                         |
|                                                                   |
|  Seed      Ledger      State      Runtime      MCP                 |
|  Provenance  Safety Boundaries  Progress/Status  Handoff           |
+-------------------------------+-----------------------------------+
                                |
                                | bounded adapters / external calls
                                v
+-------------------------------------------------------------------+
|                    External Systems / Runtimes                      |
|                                                                   |
|  GitHub   Jira   Linear   Slack   CI   Local repo   Agent CLIs      |
+-------------------------------------------------------------------+
```

The same diagram lives in
[Q00/ouroboros-plugins/docs/architecture.md](https://github.com/Q00/ouroboros-plugins/blob/main/docs/architecture.md)
and is the canonical reference for both repos.

## Why Defense-Oriented

Three reasons the plugin layer is plumbing, not a product:

1. **Lock-in for Ouroboros comes from the spec-first discipline, not from
   plugin count.** Interview → Seed → Evolve → Provenance is the unique
   value. GitHub PR ops, Jira sync, Slack incident response — every adjacent
   tool has those. Plugins are commodity; spec-first discipline is not.
2. **Ecosystem-driven lock-in is fragile at this scale.** A healthy
   ecosystem (governance, security review, breaking-change discipline,
   support) is operationally expensive. Maintainer cost grows with the
   ecosystem and the unique value gets diluted.
3. **A user-facing "AI workflow App Store" is not what `ooo` should sell.**
   The promise of `ooo auto "do X"` is "spec-first agent does the right
   thing." Adding "...but first, browse the marketplace and install the
   right plugin" makes the entry experience worse, not better.

The success metric is therefore **"the boundary holds"** — measurable, not
"plugin count" — vague.

## Manifest Schema

Authoritative source:
[Q00/ouroboros-plugins/schemas/0.1/plugin.schema.json](https://github.com/Q00/ouroboros-plugins/blob/main/schemas/0.1/plugin.schema.json).

Per the locked decision in
[Q00/ouroboros-plugins#6](https://github.com/Q00/ouroboros-plugins/issues/6),
the manifest carries **8 required + 2 optional** top-level fields:

- **Required (8)**: `schema_version`, `name`, `version`, `source`,
  `commands`, `capabilities`, `permissions`, `entrypoint`.
- **Optional (2)**: `description` (default `""`), `audit` (default
  `{events: [plugin.invoked, plugin.permission_used, plugin.completed,
  plugin.failed]}`).

Each required field is load-bearing for some part of the lifecycle, lockfile,
or firewall; each optional field has a sensible default the firewall provides
unconditionally.

The `source.type` enum is `local_path | plugin_home | first_party`. Per
[Q00/ouroboros-plugins#8](https://github.com/Q00/ouroboros-plugins/issues/8),
first-party programs share the manifest format and are registered at core
boot, bypassing the user-facing `discovered → installed → trusted` flow.

**First-party trust semantics.** Because first-party programs are shipped
inside the same release artifact as core (i.e. their manifests are not
attacker-controlled), all permissions they declare — including
`required: true` — are treated as **implicitly trusted at boot** by the
firewall (see Invocation Contract below). The boot-time registration step
populates the trust store with these grants in-process; there is no
user-visible "trust" prompt for first-party programs and no path for users
to revoke them short of disabling the program. This is the deliberate
contract: first-party programs MAY declare `required: true` permissions,
and conforming firewalls MUST NOT block them on the trust check. Plugins
that are not first-party never receive this treatment regardless of
`source.type`.

The manifest schema versions per
[Q00/ouroboros-plugins#11](https://github.com/Q00/ouroboros-plugins/issues/11):
SemVer-style `MAJOR.MINOR`. Each released `MAJOR.MINOR` lives in its own
directory under upstream `schemas/<MAJOR.MINOR>/` (e.g. `schemas/0.1/`,
`schemas/0.2/`, `schemas/1.0/`). The support window is *current MAJOR +
previous MAJOR*; older MAJORs may be retained for archival reading but are
out-of-window for compatibility guarantees.

### Vendoring strategy in core (resolves #736)

Ouroboros core vendors the schemas at
`src/ouroboros/plugin/schemas/<MAJOR.MINOR>/`, **mirroring the upstream
directory layout one-for-one** (so the URL `schemas/0.1/plugin.schema.json`
maps to vendored `src/ouroboros/plugin/schemas/0.1/plugin.schema.json`).
Each vendored directory contains a `_source.json` recording the upstream
git SHA at the time of the copy. The `scripts/sync-plugin-schemas.sh`
script copies all in-window MAJOR.MINOR directories from a pinned upstream
SHA. CI may surface drift as a warning until the schemas stabilize at v1;
this is intentionally less strict than a hard error to keep bring-up
smooth.

## Invocation Contract

Every UserLevel plugin command flows through one wrapper —
`src/ouroboros/plugin/firewall.py:invoke_plugin` (#729).

The wrapper's responsibilities, in order:

1. **Pre-invocation trust check.** If any `required: true` permission is not
   trusted, emit only `plugin.failed` with `result.status="blocked"` and a
   message naming the missing scope and the exact `ooo plugin trust ...`
   command to run (the canonical CLI entrypoint for the lifecycle commands;
   `ouroboros` is not a separate user-facing command). **No `plugin.invoked`
   is emitted** — the plugin never started.
2. **Confirmation gate.** If the resolved command has
   `requires_confirmation: true`, show a single confirmation prompt. Per
   [Q00/ouroboros-plugins#9 Q2](https://github.com/Q00/ouroboros-plugins/issues/9),
   this is the only confirmation; permission risk is handled at trust grant
   time.
3. **Emit `plugin.invoked`** before launching the entrypoint subprocess.
4. **Emit `plugin.permission_used`** for each `required: true` permission
   declared in the manifest. Optional permissions (`required: false`) are
   not emitted by default in v0; this is the deliberately coarse Option (a)
   from #729's spec. The path to graduate to per-call granular emission
   (stderr-line or sidecar file) is open but not implemented.
5. **Run entrypoint** out-of-process (subprocess via the manifest's declared
   command).
6. **Emit `plugin.completed` or `plugin.failed`** with `result.status` and
   the subprocess exit code.

Audit events conform to
[Q00/ouroboros-plugins/schemas/0.1/audit-event.schema.json](https://github.com/Q00/ouroboros-plugins/blob/main/schemas/0.1/audit-event.schema.json).
The compatibility surface between this schema and the existing core ledger
writer is tracked in #737.

### Audit-event compatibility (resolves #737)

The audit-event schema is the canonical shape for plugin-emitted events. The
core ledger writer accepts these events as-is, with any core-level envelope
(e.g. ledger-internal sequence numbers) added at a layer **above** the
schema's `additionalProperties: false` boundary. No silent field truncation
or expansion is permitted; mismatches produce errors, not warnings.

Bounded payloads: argv is recorded as-is, **including** any argv tokens —
the firewall does not redact, validate, or rewrite argv contents. The
"tokens, channel IDs, and free-form user messages are forbidden" rule
therefore applies to **plugin-defined audit fields** (i.e. fields the
plugin populates inside `plugin.invoked` / `plugin.permission_used` /
`plugin.completed` / `plugin.failed` event payloads), not to argv. The
contractual obligation to keep secrets out of argv is on the **caller**
(`ooo` CLI invocations and first-party programs that shell out to a
plugin); plugins MUST refuse to accept secrets via argv and MUST document
the secure path (env, file, OS keychain) instead. Provenance fields in
audit events are string-only per the
[`audit-event.schema.json`](https://github.com/Q00/ouroboros-plugins/blob/main/schemas/0.1/audit-event.schema.json)
constraint set (the schema is the canonical source; this RFC does not
introduce a separate `docs/audit.md` contract). Raw stdout/stderr is
**not** copied into the ledger; only a sha256 hash is recorded for
forensic comparison.

## UX

The user-facing install path is `ooo plugin add <repo-url>`. The repository
URL is the unit of distribution; the catalog inside the repository is the
unit of selection. Full UX details:
[Q00/ouroboros-plugins/docs/lifecycle.md](https://github.com/Q00/ouroboros-plugins/blob/main/docs/lifecycle.md).

**`add` vs `install`.** `add` is the **interactive entry point** intended
for humans: it accepts a repo URL, fetches the catalog, presents the
selection prompt, and then internally invokes `install` for each selected
plugin. `install` is the **non-interactive primitive** addressed by
plugin name (e.g. `ooo plugin install github-pr-ops`), used when the
repository is already known to the system or when scripts/CI need to
bypass the prompt. The two commands are layered, not redundant: `add`
calls `install`; `install` never calls `add`.

```bash
$ ooo plugin add https://github.com/Q00/ouroboros-plugins
Repository: Q00/ouroboros-plugins (b3a91f2)

Select plugins to install:

  [x] github-pr-ops      0.1.0   review and prepare PR merges

Press space to toggle, enter to confirm, esc to cancel.

$ ooo plugin trust github-pr-ops --scope github:read
$ ooo github-pr review https://github.com/Q00/ouroboros/pull/725
```

Anti-pattern install strings (e.g.
`git+https://github.com/Q00/ouroboros-plugins.git#plugins/github-pr-ops`)
are explicitly rejected because they leak repository layout into the
user-visible URL. Plugin authors must be free to refactor their repos
without breaking installs.

## Reference Plugin

[Q00/ouroboros-plugins](https://github.com/Q00/ouroboros-plugins) is the
**curated** reference repo, not a marketplace. It hosts the contract
artifacts (schemas, validator) and one v0 reference plugin —
`github-pr-ops` — whose purpose is to **prove the contract**, not populate
an ecosystem. Other plugins live in their authors' own repositories and
install via `ooo plugin add <author-repo-url>`.

`github-pr-ops` ships with one command: `review` (read-only). The
destructive `merge` command is intentionally absent from v0 per
[Q00/ouroboros-plugins#7](https://github.com/Q00/ouroboros-plugins/issues/7);
it returns when the destructive trust UX (#9) is in place.

## ooo auto Boundary

`ooo auto` is a **first-party UserLevel program**, not core. Its product
boundary is permanent:

```text
goal → clarification/interview → Seed → validation → execution handoff
```

Domain-specific operational workflows do not live here. They live in
plugins.

**Historical rationale (not an evergreen claim).** When this RFC was
drafted, `grep -nE 'github|pull_request' src/ouroboros/cli/commands/auto.py src/ouroboros/auto/pipeline.py`
returned empty, and the closed status of the #689 PR stack
(`#697`, `#707`, `#712`, `#715`, `#721`) showed the project had already
been rejecting domain-specific intrusions into `ooo auto` on a per-PR
basis. This RFC promotes that de facto rejection to a de jure boundary.

The **enforcement** of the boundary going forward is mechanical, not
documentary: #735 adds a CI lint guard that fails any future PR
re-introducing domain-specific keywords into the `ooo auto` code path.
That guard — not the snapshot above — is the evergreen control. If the
guard is ever removed or weakened, this RFC must be revisited.

## Deferred Decisions

These are intentionally postponed until a real plugin demonstrates the
need. Adding any of them speculatively violates the
"contract emerges from what the plugin actually exercises" principle.

- **Granular `plugin.permission_used` emission.** v0 emits one event per
  declared `required: true` permission at invocation start. Per-call
  emission via stderr-line or sidecar (Options (b)/(c) in #729) is open
  but unimplemented.
- **Per-repo trust grants.** v0 stores trust per-user. A future opt-in
  per-repo policy file is possible but not designed.
- **MCP-tool publication via plugins.** Partly resolved by the firewall
  (#729); remaining MCP-specific concerns to file separately if surfaced.
- **Plugin-update flow (`ooo plugin update`).** v0 ships the eight plugin
  commands locked in #731, split as follows:
  - **State-mutating** (write the trust store / lockfile / installed set):
    `add`, `install`, `trust`, `disable`, `remove`.
  - **Read-only** (no persistent state change): `discover`, `inspect`,
    `list`.
  The single deferred verb is the `update` *transition* — a separate
  in-place upgrade command. It lands when a real upgrade need surfaces;
  until then, `remove` + `add` is the documented upgrade path.
- **Automated migration scripts** for MAJOR-version manifest schema bumps.
  v0 → v1 (whenever it happens) ships with a manual migration guide.
- **Hosted catalog / index server.** Permanent non-goal: marketplace as a
  product surface is a non-goal of #725.

## Related Work

Sub-issues of #725, organized by phase:

| Phase | Issue | Title |
|---|---|---|
| 0 | #726 | Pin self-restraint in #725 body and draft this RFC |
| 0 | #727 | Resolve `PLUGIN LAYER` terminology collision in `docs/architecture.md` |
| 1 | #728 | Plugin manifest loader (`src/ouroboros/plugin/manifest.py`) |
| 1 | #729 | Plugin invocation firewall + audit-event emitter |
| 1 | #730 | Extend `src/ouroboros/plugin/skills/registry.py` for UserLevel programs |
| 2 | #731 | `ooo plugin {add,discover,inspect,install,trust,disable,remove,list}` CLI |
| 2 | #732 | Trust store + `~/.ouroboros/plugins.lock` |
| 3 | #733 | E2E contract proof with `github-pr-ops` (read-only path only) |
| 4 | #734 | Excise / verify-absent GitHub-PR domain branching from `ooo auto` |
| 4 | #735 | CI lint guard preventing domain keywords from leaking into `ooo auto` |
| Cross-repo | #736 | Schema vendoring strategy (vendor / submodule / PyPI) |
| Cross-repo | #737 | `audit-event.schema.json` compatibility with core ledger writer |

Contract repo issues at
[Q00/ouroboros-plugins](https://github.com/Q00/ouroboros-plugins):

| # | Title |
|---|---|
| #1 | `validate_contract.py` enforce JSON Schema (validator correctness) |
| #2 | CI workflow for the validator |
| #3 | LICENSE / CONTRIBUTING / CODEOWNERS (repo-metadata signals) |
| #4 | `ooo plugin add <repo-url>` UX docs in lifecycle.md |
| #5 | Rename `registry/` → `catalog/` |
| #6 | Manifest minimum schema (locked: 8 required + 2 optional) |
| #7 | `merge` removed from v0 reference plugin |
| #8 | first-party programs share the manifest format (locked: yes) |
| #9 | Destructive permission trust UX (locked: 6 answers) |
| #10 | `command.risk` and `permission.risk` enum alignment (locked: 3-value) |
| #11 | Schema versioning policy (locked: SemVer + dual-major + archived) |
