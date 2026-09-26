# /threat-model bootstrap

> **Re-read note:** If you need this file mid-session and the Read tool
> reports "file unchanged", the prior result was evicted from context; reload
> with `cat .claude/skills/threat-model/bootstrap.md` via Bash.

Derive a threat model from **code + past vulnerabilities** when no application
owner is available. Five stages: spawn a parallel research swarm, synthesize
its findings into sections 1-3 and a vuln working table, generalize vulns into threat
classes, gap-fill with STRIDE, emit `THREAT_MODEL.md` per `schema.md`.

This mode is read-only static analysis and is **language-agnostic**: the same
stages apply whether the target is C/C++, Rust, Go, Python, Java/Kotlin,
JavaScript/TypeScript, or polyglot. Do not build, run, or fuzz the target. The
Bash tool is permitted **only** for `git` (history mining), `find`/`ls`
(layout), `gh api` (public advisory lookup), and `cat` (re-reading skill
files). Do not execute anything from inside `<target-dir>`. The same
restriction applies to every subagent you spawn: pass it verbatim in each
prompt.

---

## Inputs

- `<target-dir>` (required): local checkout.
- `--vulns <path>` (optional): past vulnerabilities. Any of:
  - newline-separated CVE IDs (`CVE-2026-29022`)
  - CSV with columns `id,title,component,description` (extra columns ignored)
  - markdown pentest report (parse headings + body for finding descriptions)
  - JSON array of objects with at least `id` and `description` keys
- `--depth recon|full` (optional, default `full`): `recon` runs stages 1-2
  only. Still write all eight sections (schema requires sections 1-7; section 8 optional);
  leave section 4, section 5, and section 8 as header + empty table, and put "run with --depth full
  to populate" in section 6.
  Use for fast context-building before a deeper pass.

If `--vulns` is absent, the Vuln-file parser agent is skipped; the History
miner and Advisory fetcher agents in the Stage-1 swarm cover the same ground
from `<target-dir>`'s own git history and public advisories.

- `--fresh` (optional): ignore any existing checkpoint in
  `./.threat-model-state/` and start from Stage 1.

---

## Checkpointing (runs before Stage 1 and after every stage)

On large codebases the Stage-1 swarm can exhaust context or hit rate limits
before Stage 5 emits `THREAT_MODEL.md`. Stage state persists to
`./.threat-model-state/` (in the **current working directory**, not
`<target-dir>`) so a fresh `/threat-model bootstrap` session can resume
without re-spawning the swarm. The state dir is cwd-relative because
`checkpoint.py` confines all paths to cwd as a guard against prompt-injected
writes outside the repo.

All checkpoint I/O goes through `python3 .claude/skills/_lib/checkpoint.py`
(atomic writes, JSON-validated). Never use the Write tool for `progress.json`
directly. Never pass payload via heredoc or stdin; target-derived strings
could collide with the heredoc delimiter and break out to shell. The
Write→`--from` pattern keeps repo-derived bytes out of Bash argv.

State files in `./.threat-model-state/`:
- `progress.json` — **single source of truth** for resume position:
  `{"status": "running"|"complete", "stage_done": N}`. Resume decisions read
  ONLY this file.
- `stageN.json` — data payload for stage N (schemas at the tail of each stage
  below).
- `_chunk.tmp` — transient payload buffer; overwritten before every
  `save`/`append` call.

**Start of run — resume check.** Bash:
`python3 .claude/skills/_lib/checkpoint.py load ./.threat-model-state`

- `status == "absent"` OR `"complete"`, OR `--fresh` in `$ARGUMENTS` →
  **fresh start.** Bash:
  `python3 .claude/skills/_lib/checkpoint.py reset ./.threat-model-state`,
  then proceed to Stage 1.
- `status == "running"` with `stage_done == N` → **resume.** Read
  `stage1.json` through `stageN.json` **in order**, merging keys into working
  state (later files override earlier — checkpoints may be deltas). Print
  `Resuming from checkpoint: Stage N complete`, and **skip directly to
  Stage N+1**.

**End of every stage N.** Two tool calls:
1. Write tool → `./.threat-model-state/_chunk.tmp` containing the
   stage's output JSON.
2. Bash → `python3 .claude/skills/_lib/checkpoint.py save ./.threat-model-state <N> <name> --key stage --from ./.threat-model-state/_chunk.tmp`

**End of run.** After writing `<target-dir>/THREAT_MODEL.md`, Bash:
`python3 .claude/skills/_lib/checkpoint.py done ./.threat-model-state 5 --key stage`

---

## Stage 1 — Research swarm

Goal: gather everything needed to fill sections 1-3 and the vuln working table, in
parallel. Spawn the agents below **in a single batch** with the Task tool so
they run concurrently. Each agent gets a narrow brief, the absolute path to
`<target-dir>`, and the read-only restriction verbatim. Each returns a
structured text block; you synthesize in Stage 2.

Skip the swarm and run the briefs yourself sequentially if `<target-dir>` is
small (<50 source files) or `--depth recon` is set; the parallelism isn't
worth the overhead there.

| Agent | Brief | Returns |
|---|---|---|
| **Docs reader** | Read `README*`, `SECURITY.md`, `CHANGELOG*`, top-level `docs/`, and the build manifest (`setup.py` / `Cargo.toml` / `package.json` / `CMakeLists.txt`). Summarize what the project says it is, who uses it, and any security claims or fix entries it documents. | Prose system description; list of self-documented security fixes. |
| **Surface mapper** | Grep the source tree for entry-point signatures (table below). For each hit, name the surface, the file:function, and what crosses it. Include supply-chain surfaces (lockfiles, vendored deps, `curl \| sh` in build scripts). Bound the scan: exclude `vendor/`, `node_modules/`, `third_party/`, generated code; cap at ~5 representative hits per surface row. | Candidate section 3 rows: `{entry_point, description, trust_boundary, file_refs}`. |
| **Infra reader** | Read deploy-time config: `*.tf`/`*.tfvars`, k8s manifests (`*.yaml` under `k8s/`/`deploy/`/`manifests/`), `Dockerfile*`, CI workflows, and any IAM/service-account/dataset-ACL files. For each, name (a) the identity it runs as and what that identity can reach, (b) any access grant not managed in this tree (ad-hoc IAM, hand-created SAs, missing column/policy tags), (c) credentials or principals that survive a migration or teardown. | Candidate section 3 rows for infra surfaces + candidate section 4 rows: `{threat, surface, asset}` where the config itself is the finding. |
| **Asset finder** | Identify what the code protects or produces: sensitive data it reads/writes (secrets, keys, user records, DBs), process integrity (always present for native code), service availability, and downstream embedder assets if it's a library. | Candidate section 2 rows: `{asset, description, sensitivity}`. |
| **History miner** | Two steps. **(a)** Glance at the build manifest and file extensions to identify language **and domain**, then derive 6-10 commit-message keywords specific to that stack on top of the base set `CVE- security vuln fix exploit`. Derive from what the code does, not from a lookup table; the three examples below illustrate the specificity bar, not coverage: native parser → `overflow OOB UAF integer`; web service → `injection SSRF IDOR traversal`; crypto → `timing constant-time nonce`. **(b)** `git -C <target-dir> log --all -i --grep='<base ∪ derived, \|-joined>' --oneline`, then read the full message + diff of each hit. Also grep any `issues/` or `bugs/` export in-tree. | Vuln rows: `{id (commit hash), title, component, class, vector}`. |
| **Advisory fetcher** | If `git -C <target-dir> remote get-url origin` is GitHub and `gh` is on PATH: `gh api /repos/{owner}/{repo}/security-advisories`. Otherwise return "no public advisory source". | Vuln rows: `{id (CVE/GHSA), title, component, class, vector}`. |
| **Vuln-file parser** | Only spawn if `--vulns <path>` was provided. Parse the file (newline CVE list, CSV, markdown report, or JSON array) into normalized rows. | Vuln rows: `{id, title, component, class, vector}`. |

Surface-mapper grep targets (pass this table in its prompt). Treat the
"Look for" column as a **seed, not a checklist**: one concrete token per row
to set the specificity bar, then extend with the idioms of whatever
language/framework the target actually uses.

| Surface | Look for |
|---|---|
| Network | socket `listen`/`accept`/`bind`; HTTP route definitions (e.g., `@app.route`); RPC/gRPC/GraphQL service defs |
| File / format parsing | file-open calls (e.g., `open(`); format magic-byte checks; "parse"/"decode"/"load"/"unmarshal" function names |
| CLI / env | argv parsers (e.g., `argparse`); env reads (e.g., `getenv`) |
| Deserialization | language-native deserializers on external data (e.g., `pickle`, `ObjectInputStream`) |
| DB / query | raw query-string construction; ORM `.raw()`/`.query()` escapes |
| IPC / plugins | dynamic load (e.g., `dlopen`); subprocess spawn; `eval`/`exec` on config; dynamic import |
| Supply chain | dependency lockfiles; vendored libs; `curl \| sh` in build scripts |
| Infra / IAM | terraform `google_*_iam_*`/`aws_iam_*`; k8s `serviceAccountName`/WIF annotations; BigQuery dataset/table `access{}` blocks; secrets mounts |

**Checkpoint:** Write tool → `./.threat-model-state/_chunk.tmp`:

```json
{
  "stage": 1,
  "swarm": {
    "docs_reader": "<returned text block>",
    "surface_mapper": [ {entry_point, description, trust_boundary, file_refs} ],
    "infra_reader": { "surfaces": [...], "threats": [...] },
    "asset_finder": [ {asset, description, sensitivity} ],
    "history_miner": [ {id, title, component, class, vector} ],
    "advisory_fetcher": [ {id, title, component, class, vector} ],
    "vuln_file_parser": [ {id, title, component, class, vector} ]
  }
}
```

Then Bash:
`python3 .claude/skills/_lib/checkpoint.py save ./.threat-model-state 1 swarm --key stage --from ./.threat-model-state/_chunk.tmp`

This is the most expensive checkpoint — the swarm is the majority of the
mode's token spend. Agents that were skipped (no `--vulns`, no GitHub remote,
`--depth recon`) get an empty list/null. If the swarm was run inline
(small target), populate the same keys from your own sequential passes.

---

## Stage 2 — Synthesize

Goal: turn the swarm returns into `## 1-3` of the schema plus a vuln working
table. This stage runs in the orchestrating agent, not a subagent; it's the
join.

**Section 1: System context.** From the Docs reader's summary plus your own glance at
the tree layout, write 1-2 paragraphs: what it is, language, rough size, who
would embed or deploy it, where it would run.

**Section 2: Assets.** Take the Asset finder's rows. Dedupe, fill any obvious gaps
(native code without "host process integrity" → add it), assign `sensitivity`.

**Section 3: Entry points & trust boundaries.** Merge Surface mapper + Infra reader
rows. Dedupe, name the trust boundary for each ("untrusted file → process
memory", "unauth HTTP → application logic", "namespace workload → WIF
identity"), and for each list which section 2 assets are reachable from it.
Supply-chain, build-time, and infra/IAM surfaces **are** entry points even
though no runtime input crosses them. **Every row here must get at least one
threat in Stage 3 or 4**; that's the coverage invariant the emit-time check
enforces.

**Vuln working table.** Concatenate rows from History miner + Advisory
fetcher + Vuln-file parser. Dedupe by `id`. For each row, decide which section 3
entry point it traversed; read the relevant source to confirm (e.g.,
"CVE-2026-29022 is in `drwav__read_smpl`, reached via the WAV file-parsing
entry point"). If a vuln's entry point isn't in section 3, the Surface mapper missed
one; add it now. Hold this table in your working notes; it does **not** go
into `THREAT_MODEL.md` verbatim. It becomes the `evidence` column in Stage 3.

**Checkpoint:** Write tool → `./.threat-model-state/_chunk.tmp`:

```json
{
  "stage": 2,
  "section1_context": "<markdown prose>",
  "section2_assets": [ {asset, description, sensitivity} ],
  "section3_entry_points": [ {entry_point, description, trust_boundary, reachable_assets} ],
  "vuln_table": [ {id, title, component, class, vector, entry_point} ]
}
```

Then Bash:
`python3 .claude/skills/_lib/checkpoint.py save ./.threat-model-state 2 synthesize --key stage --from ./.threat-model-state/_chunk.tmp`

---

## Stage 3 — Generalize: vulns → threats

Goal: cluster Stage-2 vulns into threat rows at the right abstraction level.

### 3a. Cluster

Group the Stage-2 vuln table by `(entry point, bug class, asset reached)`.
Each cluster becomes **one** candidate threat. Examples:

- 3 heap overflows + 1 integer overflow, all in WAV/FLAC parsers, all
  reaching process memory → **one** threat: "Memory corruption leading to RCE
  via untrusted audio file parsing". Evidence: all 4 IDs.
- 2 SQL injections in different endpoints → **one** threat: "Data exfiltration
  / tampering via SQL injection in HTTP API". Evidence: both IDs.

Apply the litmus test to each cluster's threat statement: would it still be
true after every listed evidence item is patched? If not, you're still at vuln
level; zoom out.

### 3b. Variant scan (raises likelihood)

For each cluster, look for **siblings**: code paths with the same shape that
weren't in the vuln list. Grep for the same pattern (other format parsers,
other endpoints calling the same unsafe helper, other size fields multiplied
without overflow checks). You are not trying to prove these are exploitable;
you are estimating how much of the surface shares the pattern. More siblings →
higher likelihood.

Keep sibling locations in your working notes and surface them in the hand-back
(Stage 5, item 4). Do **not** put `file:func` references in the section 4 `evidence`
cell; evidence is for confirmed past vulns only. Sibling counts inform the
likelihood score, not the evidence column.

### 3c. Score

For each cluster, assign:

- `actor`: from the entry point (file parsing → whoever supplies the file;
  network endpoint → `remote_unauth` or `remote_auth` depending on whether
  auth precedes it).
- `impact`: from the asset and the bug class (memory corruption on a network
  service → `critical`; info leak of non-sensitive data → `low`).
- `likelihood`: start from the evidence. ≥1 confirmed past vuln in this exact
  surface → at least `likely`. Public exploit or active exploitation →
  `almost_certain`. No evidence, but siblings found and technique is well
  known → `possible`. Adjust down for controls.
- `controls`: grep for mitigations relevant to the stack (size caps, input
  validation, sandboxing/seccomp; ASLR/stack-protector/CFI in native builds;
  parameterized queries / ORM; auth middleware / CSRF tokens / CSP; rate
  limiting; SecurityManager/JEP-411 replacements in Java; etc.). `none` if
  none found.
- `status`: `unmitigated` unless you found a control that fully closes it.
- `recommended_mitigation` (working notes, not a section 4 column): for each cluster,
  name **one class-level control** that would close or materially shrink the
  whole threat regardless of which instance is found next (e.g., "sandbox the
  decoder process", "parameterized queries everywhere", "drop `pickle` for
  `json`", "enable CSP `default-src 'self'`", "size-cap all length fields
  before allocation"). Prefer a control that survives the next bug over a
  patch for the last one. These become section 8 rows in Stage 5.

Write each cluster as a section 4 row.

**Checkpoint:** Write tool → `./.threat-model-state/_chunk.tmp`:

```json
{
  "stage": 3,
  "section1_context": "...",
  "section2_assets": [...],
  "section3_entry_points": [...],
  "section4_threats": [ {threat, actor, surface, asset, impact, likelihood, status, controls, evidence} ],
  "mitigation_notes": [ {cluster, recommended_mitigation} ],
  "sibling_locations": [ {threat, locations: ["file:func", ...]} ]
}
```

Then Bash:
`python3 .claude/skills/_lib/checkpoint.py save ./.threat-model-state 3 generalize --key stage --from ./.threat-model-state/_chunk.tmp`

---

## Stage 4 — Gap-fill (the part past vulns can't give you)

Past vulnerabilities are biased toward what's already been found. A threat
model must also cover what hasn't. For **every section 3 entry point that has no section 4
row yet**, walk STRIDE and add at least the plausible ones:

| | For this entry point, could an attacker… |
|---|---|
| Spoofing | …pretend to be a trusted source? |
| Tampering | …modify data in transit or at rest? |
| Repudiation | …act without leaving attributable logs? |
| Info disclosure | …read data they shouldn't? |
| DoS | …exhaust a resource (CPU, memory, disk, connections)? |
| Elevation | …end up with more privilege than they started with? |

Also walk the entry points that **do** have rows: is the existing row the only
plausible threat, or are other STRIDE categories live too? (A file parser with
an RCE threat probably also has a DoS threat.)

For **infra/IAM entry points** (from the Infra reader), STRIDE maps less
cleanly than for code. Walk these instead:

- **Over-grant**: does the identity reach more than the app needs (whole
  dataset vs one table; project-level vs resource-level)?
- **Lateral identity**: can a co-located workload (same namespace, same node,
  same SA) assume this identity?
- **Drift**: is any grant managed outside this tree (click-ops IAM, ad-hoc
  ACL, unmanaged SA), so it won't be reviewed or torn down with the code?
- **Residual access**: do credentials or principals from a predecessor system
  survive the migration?
- **Column exposure**: does a broad table read expose identity/PII columns
  the app doesn't need?
- **Scope enforcement**: where an automated approval, merge, or write path
  exists, what bounds it to its intended scope (path allowlist, label,
  reviewer set)?

Threats added in this stage have empty `evidence`. That's fine; score
`likelihood` from technique prevalence and surface reachability alone. **The
final section 4 table must contain at least one row with empty evidence**, or this
stage didn't run.

Populate `## 5. Deprioritized` with STRIDE categories you considered and
ruled out, with the reason ("Repudiation: not applicable, no multi-user
actions").

**Checkpoint:** Write tool → `./.threat-model-state/_chunk.tmp`:

```json
{
  "stage": 4,
  "section1_context": "...",
  "section2_assets": [...],
  "section3_entry_points": [...],
  "section4_threats": [ {...stage3 rows + gap-fill rows with empty evidence} ],
  "section5_deprioritized": [ {threat, reason} ],
  "mitigation_notes": [...],
  "sibling_locations": [...]
}
```

Then Bash:
`python3 .claude/skills/_lib/checkpoint.py save ./.threat-model-state 4 gap-fill --key stage --from ./.threat-model-state/_chunk.tmp`

---

## Stage 5 — Emit

**Coverage check (do this before writing the file).** For every section 3 entry
point, confirm at least one section 4 row names it in the `surface` column. Match on
the entry-point's name string, not the concept; the downstream scorer is a
text match. Any section 3 row with zero section 4 coverage means Stage 4 was incomplete; go
back and add the missing threat now.

Sort section 4 by (impact desc, likelihood desc). Assign `id` = `T1`, `T2`, … in
sorted order.

Populate `## 6. Open questions` with everything the code couldn't tell you:

- Deployment context ("Is this exposed to the network or only local?")
- Intended actors ("Who supplies the input files in practice?")
- Controls you couldn't verify ("Is there a WAF / sandbox / size limit
  upstream of this?")
- Risk appetite ("Is DoS acceptable for this use case?")

These seed a later `/threat-model interview --seed THREAT_MODEL.md`
pass.

Populate `## 8. Recommended mitigations` from the Stage-3c working notes: one
row per class-level `mitigation`, listing the `threat_ids` it covers, whether
it `closes_class` (yes/partial), and a rough `effort` (S/M/L). If two
clusters are closed by the same control, emit one row with both clusters'
ids. Gap-fill threats from Stage 4 get rows here too where an obvious
class-level control exists.

Assemble the file **incrementally** in `./.threat-model-state/THREAT_MODEL.md`
(one chunk per `## N.` section; a stalled chunk loses that section, not the
file), then copy the assembled result to `<target-dir>/THREAT_MODEL.md` in
one Write. The assembly happens in cwd because `checkpoint.py append` is
cwd-confined; the final Write tool call is not.

1. Write tool → `./.threat-model-state/THREAT_MODEL.md` (clobbers any prior
   file) containing only the title line and `## 1. Context` section.
2. For each remaining section (`## 2. Assets`, `## 3. Entry points`,
   `## 4. Threats`, `## 5. Deprioritized`, `## 6. Open questions`,
   `## 7. Provenance`, `## 8. Recommended mitigations`):
   - Write tool → `./.threat-model-state/_chunk.tmp` containing that ONE
     section's markdown.
   - Bash:
     `python3 .claude/skills/_lib/checkpoint.py append ./.threat-model-state/THREAT_MODEL.md --from ./.threat-model-state/_chunk.tmp`
3. Read tool → `./.threat-model-state/THREAT_MODEL.md`, then Write tool →
   `<target-dir>/THREAT_MODEL.md` with the same content.

Set `## 7. Provenance`:

```
- mode: bootstrap
- date: <today>
- target: <target-dir> @ <git rev-parse --short HEAD or "not a git repo">
- inputs: <--vulns path, or "git-log + CHANGELOG mined">
- owner: unset
```

**Checkpoint (final):** After the file is on disk, Bash:
`python3 .claude/skills/_lib/checkpoint.py done ./.threat-model-state 5 --key stage`

Hand back to the user:

1. Path to the file.
2. Top 5 threats (id, threat, impact × likelihood).
3. Count of threats with evidence vs without (shows gap-fill ran).
4. Stage-3b sibling locations as candidate leads for `/vuln-scan` or the
   pipeline `find` stage.
5. The section 8 recommended mitigations, top 3 by (closes_class, effort asc).
6. The section 6 open questions, framed as "ask the owner".
