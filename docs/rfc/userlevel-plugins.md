# RFC: UserLevel Plugin Layer

## Status

**Accepted (2026-05-07)**, supersedes the growth-oriented portions of #725.

This RFC pins the framing decisions converged on in #725 and the seven
contract decisions locked in [Q00/ouroboros-plugins](https://github.com/Q00/ouroboros-plugins)
issues #5–#11. Future debates about "should this go in core?" or "should we
add this to `ooo auto`?" should be answered against this document.

### Implementation Status

"Accepted" means the **design** is locked; it does **not** mean every
artifact named below already exists in the repository. This RFC is the
**target contract** — implementer-facing prose throughout this document
uses present tense for that contract, and readers SHOULD interpret
unbuilt artifacts as RFC-2119 **MUST** (the implementation must conform
when it lands), not as a description of `main` today.

The matrix below tracks where each artifact stands at the moment this RFC
is merged. Concrete paths and commands referenced later in the document
(`src/ouroboros/plugin/firewall.py:invoke_plugin`, `scripts/sync-plugin-schemas.sh`,
`ooo plugin add`, etc.) are **target paths**, not current paths, unless
this matrix marks them as shipped.

| Artifact | Tracking issue | Status at RFC merge |
|---|---|---|
| Plugin manifest schemas under upstream `schemas/0.1/` (incl. `audit-event.schema.json`) | upstream Q00/ouroboros-plugins #6, #11 | **Shipped upstream** |
| Vendored copy at `src/ouroboros/plugin/schemas/0.1/` + `_source.json` | #736 | Not yet present in core |
| `scripts/sync-plugin-schemas.sh` | #736 | Not yet present |
| `src/ouroboros/plugin/manifest.py` (loader) | #728 | Not yet present |
| `src/ouroboros/plugin/firewall.py:invoke_plugin` (invocation contract) | #729 | Not yet present |
| `ooo plugin {add,install,trust,disable,remove}` (state-mutating CLI) | #731 | Not yet present |
| `ooo plugin {discover,inspect,list}` (read-only CLI) | #731 | Not yet present |
| `~/.ouroboros/plugins.lock` + trust store | #732 | Not yet present |
| `ooo auto` domain-keyword CI lint guard | #735 | Not yet present |
| `github-pr-ops` E2E contract proof | #733 | Not yet present |

Two consequences flow from this matrix that other sections of this
document refer back to:

1. **Boundary enforcement is currently documentary, not mechanical.** The
   "ooo auto Boundary" section describes #735 as the durable, evergreen
   control. That control activates only when #735 lands. Until then, the
   boundary is held by review discipline plus the historical evidence
   captured in that section. The clause "this RFC must be revisited if
   the guard is ever removed or weakened" therefore takes effect from the
   moment #735 ships, not from the moment this RFC merges.
2. **Implementation PRs that build the unbuilt rows above MUST conform to
   this RFC.** Drift between this contract and what those PRs ship is a
   bug in the PR, not a license to amend the RFC silently — amendments
   require a follow-up RFC change against this document.

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

> **Note.** Ouroboros-core's own `docs/architecture.md` still presents
> the older `PLUGIN LAYER` framing at the time this RFC merges. That
> divergence is intentional and tracked in #727; the diagram above is
> the authoritative one for new work, and the core architecture doc
> will be updated to match as part of #727. Until that update lands,
> any apparent disagreement between the two diagrams MUST be resolved
> in favor of this RFC.

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
user-visible "trust" prompt for first-party programs by default. This is
the deliberate contract: first-party programs MAY declare `required: true`
permissions, and conforming firewalls MUST NOT block them on the trust
check. Plugins that are not first-party never receive this treatment
regardless of `source.type`.

**First-party persistence model — one rule, three references aligned.**
The user lockfile (`~/.ouroboros/plugins.lock`) holds **zero** first-
party records, ever, in any state (trusted or disabled). All durable
state for first-party programs lives in a sibling override file
`~/.ouroboros/first-party-overrides.json`, owned by core. The override
file contains one entry per `(first-party name, artifact_digest)` pair
that the user has explicitly disabled; nothing else is persisted there.

The boot/disable/re-enable cycle works against that single file:

1. **Boot.** For every first-party program present in the release
   artifact, the firewall computes the program's `artifact_digest`
   and looks up `(name, digest)` in the override file. If absent, the
   program is implicitly trusted in-process (no lockfile write, no
   override write — the trust is fully derived from "release artifact
   present" + "no override saying otherwise"). If present, the
   program starts disabled: required permissions are stripped from
   the in-process trust table and invocation is refused.
2. **`ooo plugin disable <name>` for a first-party program.** Writes
   the `(name, current artifact_digest)` entry into the override
   file. No lockfile changes. Effective immediately and on every
   subsequent boot.
3. **`ooo plugin trust …` for a disabled first-party program.**
   Removes the matching entry from the override file. No lockfile
   write occurs — first-party trust grants are never persisted in
   the lockfile and the in-process implicit grant re-attaches at the
   next boot (or immediately, in the same process). The CLI presents
   this re-trust as an explicit confirmation prompt for audit
   symmetry, but the persistence target is the override file, not
   the lockfile.

Two follow-on consequences this model bakes in: (a) a release that
renames or rotates a first-party program produces a new
`artifact_digest`, so any stale override entry from an older release
no longer matches and the new program starts trusted by default — the
user can disable again if desired. (b) the lockfile remains a clean,
third-party-only artifact, so any tooling that audits "what
third-party plugins do I trust?" never has to filter first-party rows
out.

Disabled first-party programs remain installed (they're part of the
core release artifact, so they cannot be uninstalled separately) but
are not invocable until the override entry is cleared.

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

**Bounded payloads — argv handling.** The "tokens, channel IDs, and
free-form user messages are forbidden" rule applies to **plugin-defined
audit fields** (fields the plugin populates inside `plugin.invoked` /
`plugin.permission_used` / `plugin.completed` / `plugin.failed` event
payloads). For `argv` specifically the contract is **defense in depth**;
treating argv as either fully trusted or fully redacted is unsafe.

1. **Plugins MUST NOT accept secrets via argv.** Plugin authors MUST
   document a secure path (env var, file, OS keychain) for any
   credential a command needs and MUST reject argv-supplied secrets at
   parse time when feasible. This is the primary control.
2. **The firewall MUST apply a built-in argv redaction policy before
   ledger write**, as a safety net for the case where rule (1) is
   violated by accident. The minimum policy redacts:
   - Values of well-known secret flags by name match
     (e.g. `--token=…`, `--password=…`, `--api-key=…`, `--secret=…`,
     and the value position immediately following those flags),
   - Tokens with high-confidence formats (`Bearer …`, `gh[oprsu]_…`,
     `sk-…`, AWS-style `AKIA…`, JWT-shaped strings with three
     dot-separated base64url segments).
   Redacted positions are replaced with the literal string `[redacted]`
   in the ledger record. The hash of the original argv (sha256 over the
   un-redacted form) MAY be recorded alongside for forensic
   reconciliation, but the original value MUST NOT.
3. **Plugins MAY tighten the policy per command.** A plugin MAY declare
   additional flags or positional indexes to redact via a future
   manifest extension; the v0 manifest does not yet expose this, so v0
   redaction is exactly the built-in policy in (2). Adding the
   per-command redaction list is tracked alongside the granular
   permission emission work in the Deferred Decisions section.
4. **Caller responsibility persists.** The firewall's safety net does
   not absolve callers (`ooo` CLI, first-party programs, scripts/CI) of
   the obligation to keep secrets out of argv in the first place; the
   safety net exists to limit blast radius, not to make argv a
   sanctioned secret channel.

Provenance fields in audit events are string-only per the
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
plugin. `install` is the **non-interactive primitive** used when scripts
or CI need to bypass the selection prompt. The two commands are layered,
not redundant: `add` calls `install`; `install` never calls `add`.

`install` MUST be unambiguous about which `(source_identity, digest)`
it is targeting:

- **Default form** — `ooo plugin install <name>` — succeeds **only if
  exactly one** known catalog (a previously `add`-ed `plugin_home`
  repository or a previously registered `local_path` source — see
  below) exposes a plugin with that `name`. If two or more sources
  expose the same `name`, the command MUST exit with an "ambiguous
  plugin name" error listing the candidate sources; it MUST NOT pick
  one heuristically.
- **Qualified form** — `ooo plugin install <name> --from <repo-url>`
  (or `--from <local-path>`) — selects an explicit source and is
  required whenever the default form would be ambiguous. CI / scripts
  SHOULD prefer this form unconditionally, because catalog membership
  can change over time and the qualified form is stable across that
  drift.
- **No catalog match** — `ooo plugin install <name>` with no known
  source providing `<name>` MUST instruct the user to either
  `ooo plugin add <repo-url>` first (for a `plugin_home` source) **or**
  re-run `install` in the qualified form
  `ooo plugin install <name> --from <local-path>` (for a `local_path`
  source — that form is itself the registration verb, per the catalog-
  registration rules above). The error message MUST mention both
  paths so users with a local checkout are not misdirected to `add`.
  In no case may `install` silently search the network.

**How sources enter the known catalog.** v0 has exactly two registration
paths, one per `source.type` that can produce a user trust record:

- `plugin_home` sources are registered by `ooo plugin add <repo-url>`
  (the repo URL becomes a known catalog at that moment, regardless of
  whether the user proceeds to install anything from the selection
  prompt). Subsequent `install`s can address that `name` without
  re-fetching.
- `local_path` sources are registered the first time the user runs
  `ooo plugin install <name> --from <local-path>` against an absolute
  path. The qualified form is therefore both a register-on-first-use
  and an install in the same command; there is no separate
  `register-local` verb in v0. The path is recorded as a known catalog
  in the trust store; later `install <name>` calls can address it via
  the default form if it is unambiguous.

`first_party` sources do not go through registration at all — they are
populated at boot from the core release artifact, as documented under
the Manifest Schema section.

**Plugin name → command-namespace mapping.** Every installed plugin's
manifest `name` field IS the user-facing command namespace, with no
aliasing: a plugin named `github-pr-ops` is invoked as
`ooo github-pr-ops <command> [args...]`, where `<command>` is one of
the entries declared in the manifest's `commands` array (each `commands`
entry's own `name` is the subcommand). Aliases and short names are
explicitly out of scope for v0.

`ooo plugin install` MUST refuse a new install whose manifest `name`
collides with **any** name already occupying the top-level `ooo`
command namespace, not just other installed third-party plugins. The
reserved set at the moment of the check is the union of:

- every first-party UserLevel program currently registered at boot
  (`auto`, `run`, `pm`, `plugin` itself, and any other first-party
  program shipped in the same release artifact);
- every built-in `ooo` subcommand or top-level option that is not a
  first-party program (e.g. `help`, `version`, `--version`); and
- every other third-party plugin currently installed.

A name collision MUST produce an explicit error naming the conflicting
occupant — never silently shadow it. This prevents a third-party plugin
from hijacking dispatch for a name like `auto` or `run`. Renaming on
the plugin side (manifest `name` change ⇒ new `artifact_digest` ⇒ fresh
trust subject) is the only path to install such a plugin.

**Collisions detected at boot** (the upgrade case). The same uniqueness
invariant MUST be enforced at boot, because a new core release can
introduce a first-party program whose name collides with an
already-installed third-party plugin from a previous release. When the
firewall detects such a collision at boot:

1. **First-party wins** for command dispatch — the new first-party
   program is registered under that name. This preserves the release
   contract: a core release is allowed to ship new commands.
2. The conflicting third-party plugin is **auto-disabled** (its
   `disabled: true` flag is set) and its required permissions are
   stripped from the in-process trust table. It remains installed on
   disk so the user does not lose data.
3. The firewall MUST emit an explicit `plugin.failed` event with
   `result.status="name_collision_with_first_party"` for the disabled
   plugin, and `ooo plugin list` MUST surface the disabled state with
   the reason. The plugin is not invocable until the user resolves the
   conflict by `ooo plugin remove` (and, if desired, reinstalling
   under a different name from a re-published catalog).

There is no silent shadowing in either direction — install-time and
boot-time both refuse the ambiguous state explicitly.

**Trust identity is NOT the manifest `name`.** Manifest `name` controls
the CLI namespace and the install-time uniqueness check; it does **not**
identify the trust subject. Trust records — and the lockfile entries in
`~/.ouroboros/plugins.lock` — MUST be keyed by the tuple

```text
( source.type , source_identity , artifact_digest )
```

where `source_identity` is dispatched on `source.type` so the key works
across all enum values (this matters because `source.type` is
`local_path | plugin_home | first_party`, none of which is necessarily
URL-shaped):

| `source.type` | `source_identity` | Notes |
|---|---|---|
| `plugin_home` | normalized repo URL the plugin was added from | Normalization is **strict and conservative**: it strips a trailing `.git`, the URL fragment, and any embedded `userinfo` (`user:pass@`), but **preserves the scheme exactly** (so `http://…` and `https://…` are distinct trust subjects) and preserves the host case-insensitively. Aliasing across schemes, hosts, or paths produces a different `source_identity` and forces fresh trust |
| `local_path` | the absolute, resolved filesystem path of the plugin directory at install time | Symlinks are resolved; relative paths are rejected. Two installs of the same path resolve to the same `source_identity` |
| `first_party` | the manifest `name` (the program is shipped inside the core release artifact) | First-party programs do not produce trust records in the user lockfile; their required permissions are populated as implicitly trusted at boot per the Manifest Schema section. The triple is recorded in core's in-process trust table for audit symmetry, not in `~/.ouroboros/plugins.lock` |

`artifact_digest` is the sha256 of the **complete installed artifact**,
not just the manifest, computed at install time and re-verified before
each invocation. The hashing input is dispatched per source type so it
covers all executable bytes the plugin will run:

| `source.type` | `artifact_digest` input | Re-verification rule |
|---|---|---|
| `plugin_home` | sha256 of the **canonical tree hash** of the installed plugin subtree under `~/.ouroboros/plugins/<...>/`, computed independently of any tar dialect (see "Canonical tree hash" below) so arbitrary path lengths and link targets are covered without relying on `ustar`/`pax` quirks | Recorded at `install` / `add` time AND **recomputed before every invocation** against the on-disk subtree. If the recomputed digest does not match the trusted record (e.g. a user or another process edited the installed bytes), the firewall MUST emit `plugin.failed` with `result.status="trust_subject_changed"` and refuse to run; the user must re-issue `ooo plugin trust ...` to re-confirm the new artifact. There is no "recompute only at install" shortcut |
| `local_path` | the same canonical tree hash, computed against the absolute path on disk | Same per-invocation re-verification rule as `plugin_home`. The path is mutable in place, so the firewall MUST recompute the digest before each invocation and fail closed on drift, as above |
| `first_party` | the same canonical tree hash applied to the entrypoint file (or the program's subtree if it ships as a directory), computed at boot | Rolls with each core release; restart picks up the new digest and the boot-time grant attaches to the new triple. Per-invocation re-verification is not required because the core release artifact is the unit of trust here — tampering with it is out of scope for the plugin firewall |

**Canonical tree hash.** To avoid `ustar`/`pax` lossiness for long paths
or extended attributes, the digest is computed without going through a
tarball at all:

1. Walk the subtree depth-first, collecting an entry for each regular
   file and each symlink (directories are implicit). Reject other file
   types (devices, FIFOs, sockets) at install time.
2. For each entry, build a record `<mode>\0<path>\0<sha256-of-content
   or link-target>\0`, where:
   - `<mode>` is the octal POSIX mode masked to the executable bit
     (`0o755` or `0o644` for files; `0o777` for symlinks);
   - `<path>` is the path relative to the subtree root, with `/` as
     separator and **no length cap** (this is what `ustar` couldn't do);
   - `<sha256-of-content>` is the hex sha256 of the file's bytes; for
     symlinks it's the hex sha256 of the link target string.
3. Sort the records lexicographically by `<path>` (NUL is sorted as
   raw byte 0x00).
4. The canonical tree hash is `sha256(concat(sorted records))`.

This serialization is deterministic, covers the entire executable
surface (including symlinks), and has no implementation-defined
truncation. Implementations MAY cache the hash but MUST recompute it
on the cadence specified in the table above.

Manifest-only binding is **insufficient** and explicitly rejected: an
attacker (or a careless edit) that swaps the entrypoint while leaving
the manifest untouched would otherwise inherit the prior trust. Binding
to the artifact digest closes that code-substitution path.

This collectively closes the two permission-escalation paths the trust
subject is designed to defeat:

1. **Same-name reinstall under a different source** — `remove` + `add`
   from a different repository produces a new `source_identity`, hence
   a new triple, hence un-trusted required scopes.
2. **Code substitution under the same source** — modifying the
   entrypoint without touching the manifest produces a new
   `artifact_digest`, hence a new triple, hence un-trusted required
   scopes. For `plugin_home` and `local_path` this is checked on
   **every invocation** (the firewall recomputes the canonical tree
   hash defined above against the on-disk subtree before each call
   and fails closed on drift, exactly as the per-source-type rule
   above specifies — note that the digest is **not** a tarball hash;
   the tar-independent serialization is what makes long paths and
   symlinks safe). For `first_party` it is checked at **boot only**,
   on the explicit grounds that the core release artifact is the unit
   of trust there and tampering with it is out of scope for the
   plugin firewall.

Concrete obligations on the lifecycle commands:

- `ooo plugin remove <name>` MUST delete **every trust record for the
  install subject — past and present**, not only records bound to the
  currently-active triple. The lockfile entry shape includes the
  plugin's manifest `name` alongside its `(source.type,
  source_identity, artifact_digest)`, so the deletion scope is records
  matching `(name, source.type, source_identity)` for any historical
  `artifact_digest` — explicitly **scoped to this plugin name**, never
  to other sibling plugins installed from the same `plugin_home` repo
  URL or the same `local_path` directory. (A catalog repo can host
  multiple plugins; removing one MUST NOT de-trust its siblings.)
  After clearing those records, `remove` also removes the installed
  snapshot for the `plugin_home` plugin (its own subdirectory under
  `~/.ouroboros/plugins/<...>/`, not the parent catalog) or, for
  `local_path`, removes only this plugin's catalog registration while
  leaving the on-disk path untouched. This closes the otherwise-silent
  regrant path where a user could downgrade or reinstall back to an
  old digest and inherit prior trust, while preserving siblings. No
  "tombstone with implicit re-grant" behavior is permitted — uninstall
  fully revokes, including for any earlier version of the same install
  subject.
- `ooo plugin install` MUST compute the new triple (recomputing
  `artifact_digest` from the just-installed bytes, not copying it from
  upstream metadata) and, if any field differs from a previously-trusted
  record for the same `name`, MUST treat the install as a fresh trust
  subject (all `required: true` permissions begin un-trusted).
- `ooo plugin trust ...` writes a record bound to the current triple of
  the named plugin. Trust does NOT carry across triple changes, ever,
  including version bumps from the same source (digest changes ⇒ new
  trust subject). `trust` also clears the `disabled` flag below.
- `ooo plugin disable <name>` is the **revocation primitive** for both
  third-party plugins and first-party programs. It MUST:
  - delete every trust record bound to the plugin's current triple
    (so the firewall's pre-invocation trust check refuses on the next
    invocation, exactly as if the user had never run `trust`);
  - set a durable `disabled: true` flag in the lockfile (or, for
    first-party, in core's in-process disabled set persisted to a
    sibling override file) — this prevents boot-time implicit grants
    from re-attaching on the next start;
  - leave the installed bytes and manifest in place, so re-enabling
    is cheap and identity-preserving.
  Re-enabling is performed by re-running `ooo plugin trust …` on the
  same plugin: that clears the `disabled` flag and writes fresh trust
  records bound to the *current* triple. There is no separate `enable`
  verb in v0; the trust prompt is the only re-grant entrypoint, which
  keeps every grant decision explicit and recorded.
- The deferred `update` flow, when it lands, MUST surface the digest
  change to the user and require explicit re-confirmation of any
  permission whose risk class changed; until `update` exists, the
  documented upgrade path is `remove` + `add`, which by construction
  forces fresh trust grants.

```bash
$ ooo plugin add https://github.com/Q00/ouroboros-plugins
Repository: Q00/ouroboros-plugins (b3a91f2)

Select plugins to install:

  [x] github-pr-ops      0.1.0   review and prepare PR merges

Press space to toggle, enter to confirm, esc to cancel.

$ ooo plugin trust github-pr-ops --scope github:read
$ ooo github-pr-ops review https://github.com/Q00/ouroboros/pull/725
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

The **future enforcement** of the boundary is mechanical, not
documentary: #735 will add a CI lint guard that fails any PR
re-introducing domain-specific keywords into the `ooo auto` code path.
Once that guard ships (status tracked in the Implementation Status
matrix), it — not the historical snapshot above — becomes the evergreen
control, and the "must be revisited if weakened" clause takes effect from
that point. Until #735 lands, the boundary is held by review discipline
plus the evidence captured here.

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
