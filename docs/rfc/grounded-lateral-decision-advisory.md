# RFC: Grounded Lateral — Evidence-Backed Decision Advisory

## Status

Draft — for owner review. Not scheduled; no code changes yet.

## Problem

Telemetry (PostHog, 14d window, 2026-08) shows the adoption funnel's real
bottleneck is not installs but the first tool call: 1,883 users attached the
MCP server, only 709 (38%) ever invoked a tool, and 60% of converters did so
within one hour of first attach. 563 users attach the server every day for 3+
days without a single call — the host model never finds a reason to invoke us.

The interview — our core funnel entry — is perceived as heavy. The wedge
features that need no upfront spec are `lateral_think` (multi-persona fan-out)
and `qa`. But `lateral_think` today has two limits:

1. **Its only trigger is stagnation.** The server-instructions copy routes to
   it on "stuck or repeated failures" — a rare in-session moment. Consequential
   decisions (architecture choice, library selection, trade-off calls, "the
   user doesn't know what to pick") happen far more often and need the same
   multi-perspective treatment, but nothing routes them to us.
2. **It is opinion-only.** Persona payloads carry no tool grant
   (`build_lateral_multi_subagent` attaches no `allowed_tools`), so a persona
   debate is a bundle of ungrounded LLM opinions — indistinguishable from what
   the host model produces alone. Divergence without evidence and without
   convergence leaves an unsure user *more* unsure.

## Goal

Upgrade `ouroboros_lateral_think` from "unstuck helper" into a grounded
decision advisory: multi-persona fan-out whose claims are backed by verifiable
evidence (web when available), converging on **one recommendation with flip
conditions**, persisted as a decision record that later pre-answers interview
questions.

## Non-goals

- No change to the interview flow itself (the interview-diet is a separate
  track; this RFC only builds the bridge into it).
- No new subsystem ownership: grounding reuses the existing tool-envelope
  policy machinery and the existing evidence-schema vocabulary.
- No blocking gates on the user: evidence verification is withhold-only
  (a failed citation is dropped/flagged, never a hard failure of the tool).

## Design

### D1. Trigger expansion (server instructions + tool description)

`render_mcp_server_instructions()` (`src/ouroboros/backends/capabilities.py:749`)
currently routes: `stuck or repeated failures → ouroboros_lateral_think`.

Replace with (staying inside the ~2KB host instructions budget):

> stuck, repeated failures, or a consequential choice with no clear winner
> (architecture, library, trade-off, "which option?") → `ouroboros_lateral_think`

Mirror the same expansion in the tool's own description
(`evaluation_handlers.py:1556-1571`), which already says "call proactively".
This is the highest-leverage, lowest-cost change: it ships to the 563
attach-only daily users on their next plugin update with zero new machinery.

### D2. `mode` parameter: `unstuck` (default) | `decision`

New optional enum on the tool schema. `unstuck` is byte-for-byte today's
behavior — existing callers and the skill are untouched.

`decision` mode changes the **output contract** of the fan-out:

- Per-persona subagent prompt (extend the "Task for you (subagent)" block in
  `build_lateral_multi_subagent`, `subagent.py:1932-1943`): position on the
  decision, strongest argument, strongest argument *against* own position,
  and — when a tool envelope is granted (D3) — evidence entries.
- Synthesis contract (skill + `subagent_orchestration_instruction`): the
  debate MUST converge to
  1. one recommendation,
  2. its grounds (cited where evidence exists),
  3. the strongest dissent, and
  4. **flip conditions** ("choose B instead if <condition>").

Note: `skills/unstuck/SKILL.md` today explicitly forbids auto-emitting a
verdict in debate mode. That stays true for `unstuck` mode; `decision` mode
deliberately inverts it — an unsure user asked for a decision, not a debate
transcript. This is a contract change and is why the mode is a new enum value
rather than a behavior change of the existing path.

Two speed tiers, because a user at a decision point is in a hurry:

- **quick** (default): personas only, no web — bounded by today's fan-out
  latency.
- **deep** (explicit opt-in via arg or `ooo lateral deep ...`): web-grounded
  research per persona. Never silently escalate quick → deep.

### D3. Tiered grounding via the existing tool envelope

The envelope machinery already exists and already names the web tools:
`policy.py:97-124` gives INTERVIEW and EVALUATION
`("Read","Grep","Glob","WebFetch","WebSearch")` at `max_mutation_class=READ_ONLY`,
and both `WebFetch`/`WebSearch` are classified READ_ONLY/BUILTIN/SIDECAR in the
capability model. `lateral_think` simply never calls
`allowed_runtime_builtin_tool_names`.

Change: add a LATERAL role profile (same read-only tuple as INTERVIEW) and
attach the resolved envelope to persona payloads in
`build_lateral_multi_subagent` — but **only** when
`backend_supports_tool_envelope` holds for the session backend (same guard the
interview path uses in `authoring_handlers.py:226`). Where the backend cannot
enforce an envelope, payloads ship exactly as today (prose-only personas).

This yields the fail-safe tiering for free:

| layer | condition | experience |
|---|---|---|
| base | always | persona debate, opinions only (today's behavior) |
| grounded | envelope-capable backend + deep tier | personas cite live sources |

The first-call experience can therefore never regress to an error: web access
is an upgrade, never a dependency.

### D4. Deterministic citation gate (withhold-only)

Hallucinated citations are this feature's biggest trust risk: one dead link
under "recommendation grounds" and the advisory is worse than no advisory.

- **Vocabulary**: reuse the existing evidence contract — personas in deep tier
  emit the fenced JSON block that `evidence_schema.extract_evidence` already
  parses, with the `research.yaml` field vocabulary (`external_sources`,
  `claims`; `rejected_if: external_sources == []` applies only to the deep
  tier where evidence was promised).
- **New (small) piece**: a URL liveness/consistency checker — nothing like it
  exists in `src/` today. Bounded stdlib fetch (timeout ≤ 4s, HEAD then
  ranged GET, no redirect chains beyond 3, response capped), run over cited
  URLs before synthesis. Location: alongside `evidence_schema.py`
  (`orchestrator/`), not a new package.
- **Enforcement is withhold-only**, matching the house verify-gate invariant:
  a dead or unreachable citation demotes the claim to "unverified" (rendered
  as such in the synthesis) or drops the citation; it never fails the tool
  call and never blocks the recommendation. Offline machines degrade to the
  base tier silently.

### D5. Decision records — the bridge to the core funnel

**Reuse `ouroboros_record_conductor_decision` / the `conductor_decision`
aggregate** (owner decision, 2026-08-31). The aggregate already persists
bounded free text — `verification_summary` goes through
`bounded_conductor_text` (byte-capped, secret-rejecting,
`core/conductor.py:86`) and `conductor_directive` stores its full event data;
only `action_arguments` is digest-only. That is enough to carry a compact ADR:
recommendation in `selected_action`, grounds + surviving citations in
`verification_summary`, flip conditions in the directive payload.

Constraints to accommodate (implementation notes, not blockers):

- `phase=selected` requires `attention_event_id` / `evidence_event_ids`
  (`conductor_handler.py`): the lateral fan-out records its own trigger event
  and passes that id — no schema relaxation needed.
- The mutating budgets (`_MAX_MUTATING_PER_ATTENTION=1`,
  `_MAX_MUTATING_PER_ROOT_JOB=2`) cap only mutating effects; an advisory
  decision uses a non-mutating `ConductorEffect`, so the budgets do not bind.
- Citation lists must fit the `verification_summary` byte cap — the citation
  gate (D4) already bounds what survives.

Consumer (the actual point): the interview context-pack path reads prior
decision records for the project and treats them as committed context —
questions already answered by a past `ooo lateral` run are pre-filled instead
of re-asked. Every decision made through the wedge makes the eventual
interview one question lighter. This is the structural answer to "the
interview is heavy": not shrinking the interview, but pre-answering it.

### D6. Interview-less seed crystallization (the ladder's last rung)

When a session has already settled the key decisions — after a `decision`-mode
lateral run, or simply because the user and host converged on goal,
constraints, and success criteria in conversation — the host should be able to
offer: **"this is basically a spec now; crystallize it into a Seed?"** without
routing through the interview. A Seed is valuable even if `ooo run` never
happens: it is a reviewable spec artifact, an AC checklist, and (via
`ooo publish`) a shareable GitHub issue — the run is the upsell, not the
prerequisite.

Current blocker: `ouroboros_generate_seed` requires a completed interview
`session_id` (`authoring_handlers.py:1157-1204`) — it is hard-coupled to
`InterviewState`. Change:

- **New input path**: accept host-supplied session context (settled goal,
  constraints, candidate ACs, decision-record ids from D5) as an alternative
  to an interview session. Prior art:
  `docs/rfc/context-first-inverted-interview.md` — harvest what the session
  already established, ask only what is missing.
- **The ambiguity gate stays, but its failure mode changes.** Host-supplied
  context is scored exactly like interview output (≤ 0.2 to pass; `force`
  still audited). But instead of the binary "blocked — go do the interview /
  force", a too-ambiguous submission returns the **specific gap questions**
  (1–3, targeting only the unclear components of the score breakdown). The
  interview doesn't disappear; it shrinks to exactly the gaps the session
  left open. Combined with D5 records pre-answering questions, this is the
  structural interview diet.
- **Quality valves already exist and stay**: the deterministic seed preflight
  gate (fictional scripts, unbound vars, conceptual paths) blocks garbage
  before RUN, and seed QA is advisory-only per the standing owner decision —
  no new blocking loop on the user.
- **Trigger copy** (server instructions, same budget): after the D1 sentence,
  add: "when the session has already settled goal + constraints + success
  criteria → offer `ouroboros_generate_seed` directly (no interview needed)."
- **Determinism contract** (honest split — generation is LLM-based and is
  not, and must not pretend to be, reproducible):
  1. *Settled inputs are anchored verbatim.* Anything the session already
     committed — D5 decision records, host-supplied goal/constraints/AC
     candidates — is carried into the seed byte-for-byte, never re-worded by
     the LLM. This is the #1488 refiner-anchoring pattern
     (`committed_decisions` + deterministic backstop) applied to seed
     composition; it also prevents the trust-killing failure where a decision
     the user made in lateral reappears paraphrased in the seed.
     **The backstop lives in `SeedGenerator` itself, not per entry point**
     (owner decision, 2026-08-31): interview, auto (its ledger's
     `committed_decisions()`, `auto/interview_driver.py:1672`), and the D6
     host-context path all converge on the same composition step, so the
     verbatim guarantee is enforced at that one chokepoint — the same
     single-path principle that fixed the per-provider verification drift in
     the codex run parity work. Note #1488's verified scope is round-to-round
     ledger reuse; whether the ledger→seed-YAML conversion already preserves
     committed answers verbatim is unverified — auditing `SeedGenerator`'s
     consumption of committed material is the first task of P4, and any gap
     found there is fixed for all three entry points at once.
  2. *Composition is non-deterministic.* The LLM only fills structure and
     wording around the anchors.
  3. *Acceptance is deterministic.* Schema validation, the preflight
     executability gate, and the ambiguity threshold judge the output the
     same way every time — whatever the LLM produced, a bad seed is rejected
     deterministically.

### Observability

`unstuck` already lands in the `command_run` funnel vocabulary. Add `mode`
(`unstuck`/`decision`, closed enum) as an allowed `command_run` dimension so
the wedge's conversion can be read directly — following the closed-vocabulary
/ fail-closed rules from the #1908 telemetry review. Measure: decision-mode
calls per week, deep-tier share, and (later) interview sessions that consumed
≥1 decision record.

## Phasing

| phase | ships | new machinery |
|---|---|---|
| P1 | D1 trigger copy + D2 `mode=decision` output contract (quick tier only) | none — prompt/copy/schema-enum only |
| P2 | D3 envelope wiring + D4 citation gate (deep tier) | LATERAL role profile, URL checker (~1 small module) |
| P3 | D5 decision records + interview consumption | conductor-decision reuse + one interview read path |
| P4 | D6 interview-less seed (context input path + gap-question gate) | generate_seed input variant; reuses ambiguity scorer, preflight, inverted-interview prior art |

P1 alone is shippable and measurable: trigger expansion reaches every
attach-only user on the next update, and the decision-mode output contract
needs no runtime capability at all. P4 is independently valuable and could be
pulled ahead of P2/P3 — it monetizes *any* converged session, not just
lateral-originated ones.

## Rollout channels (verified 2026-08-31)

Changes reach the installed base through two channels with very different
latency, which dictates what D1 can promise:

- **Fast channel — PyPI via uvx.** The plugin's `.mcp.json` launches
  `uvx --isolated --from "ouroboros-ai[mcp]"` **unpinned**, so the Python
  package (server instructions, tool schemas, persona prompts, telemetry)
  refreshes on uvx re-resolution independently of any plugin update.
  Empirically fleet-wide within hours: 0.52.0 reached 449 serve users the day
  it was released; the 0.51.14→0.51.15 migration completed in ~1 day.
  **D1–D4 all ship on this channel.**
- **Plugin channel — marketplace auto-update.** Carries `skills/`,
  `plugin.json`, `.mcp.json` itself. Verified working: a local
  `installed_plugins.json` shows ouroboros auto-updated to 0.52.0
  (gitCommitSha = the release commit) hours after the release. The historical
  `claude plugin install` no-op gap is closed (install.sh runs
  `plugin update`). Skill-file changes (the `ooo lateral` arg parsing for
  decision mode) ride this slower channel — keep the MCP tool
  backward-compatible so a stale skill + new server never breaks.

## Resolved decisions (owner, 2026-08-31)

1. **Skill surface**: no new `ooo decide` command — decision mode lives inside
   `ooo lateral` (one more command surface would raise the learning cost).
2. **Decision store**: reuse `record_conductor_decision` (see D5), not a new
   aggregate.

## Open questions (owner decisions)

1. **Deep-tier default**: should `decision` mode default to deep when the
   envelope is available, or always require explicit opt-in? (This RFC says
   opt-in, for latency predictability.)
2. **Decision-record scope key**: per-project (cwd/repo) or per-session?
   Per-project is what makes the interview bridge valuable, but needs the
   same project-scoping rules the brownfield scanner uses.

## Prior art in-repo

- `docs/rfc/interview-milestone-lateral-contract.md` — bounded lateral
  triggering from interviews (contract-only precedent for D1).
- `docs/rfc/symposium-deliberation.md` — multi-persona deliberation shape.
- `src/ouroboros/profiles/research.yaml` — the citation evidence vocabulary
  D4 reuses.
- Verify-gate withhold-only invariant (#2180–#2182) — the enforcement stance
  D4 inherits.
