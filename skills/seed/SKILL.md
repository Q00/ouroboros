---
name: seed
description: "Generate validated Seed specifications from interview results"
mcp_tool: ouroboros_generate_seed
mcp_args:
  session_id: "$1"
---

# /ouroboros:seed

Generate validated Seed specifications from interview results.

## Required Skill Capabilities

- `ask_user` — ask human-judgment questions through the active runtime's user-question surface.
- `inspect_code` — read repo-local agent roles and recover exact context from local files before guessing.
- `call_mcp` — use available Ouroboros MCP tools directly, including runtime tool discovery when a deferred MCP surface must be loaded.
- `run_shell` — run bounded local commands for audit-trail writes and setup steps.
- `refine_answer` — confirm free-form user decisions before treating them as accepted seed revisions.
- `maintain_ledger` — keep QA scores, candidate decisions, rejected proposals, and audit trail keys visible.

## Usage

```
ooo seed [session_id]
/ouroboros:seed [session_id]
```

**Trigger keywords:** "crystallize", "generate seed"

## Instructions

When the user invokes this skill:

### Load MCP Tools (Required before Path A/B decision)

The Ouroboros MCP tools are often registered as **deferred tools** that must be explicitly loaded before use. **You MUST perform this step before deciding between Path A and Path B.**

1. Use the active runtime's `call_mcp` capability to find and load the seed generation MCP tool through runtime tool discovery when needed:
   ```
   tool discovery query: "+ouroboros seed"
   ```
2. The tool will typically be named `mcp__plugin_ouroboros_ouroboros__ouroboros_generate_seed` (with a plugin prefix). After runtime tool discovery returns, the tool becomes callable through the active runtime's `call_mcp` capability.
3. If runtime tool discovery finds the tool → proceed to **Path A**. If not → proceed to **Path B**.

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

### Path A: MCP Mode (Preferred)

If the `ouroboros_generate_seed` MCP tool is available (loaded via runtime tool discovery above):

1. Determine the interview session:
   - If `session_id` provided: Use it directly
   - If no session_id: Check conversation for a recent `ouroboros_interview` session ID
   - If none found: Ask the user

2. Call the MCP tool through the active runtime's `call_mcp` capability:
   ```
   Tool: ouroboros_generate_seed
   Arguments:
     session_id: <interview session ID>
   ```

3. The tool extracts requirements from persisted interview state, calculates ambiguity score, and generates the Seed YAML.

   **Seed generation response shapes**: Branch only after an actual Seed YAML artifact is available.
   - If the response has `status: "delegated_to_subagent"` and `dispatch_mode: "plugin"`, keep the returned `session_id`, wait for the plugin-managed subagent result, then extract the Seed YAML from that result. Do not enter the QA loop using the delegation envelope as the artifact.
   - If the response directly contains Seed YAML, extract that YAML directly.
   - If neither shape yields Seed YAML, stop and ask the user to resume generation or provide the missing artifact; do not fabricate a seed just to satisfy the QA loop.

4. Continue immediately into the required QA Refinement Loop. Do not present the seed as final, ask for acceptance, or proceed to "After Seed Generation" until QA exits with PASS or the user explicitly accepts a below-threshold best attempt at the loop boundary.

**Advantages of MCP mode**: Automated ambiguity scoring (must be <= 0.2), structured extraction from persisted interview state, reproducible.

### Path B: Plugin Fallback (No MCP Server)

If the MCP tool is NOT available, fall back to agent-based generation:

1. Read `src/ouroboros/agents/seed-architect.md` and adopt that role.
2. Recover the interview requirements before drafting; do not invent missing context:
   - If the current conversation contains the complete interview Q&A or accepted post-interview corrections, use that conversation context as the freshest source of truth.
   - If conversation context is incomplete and `session_id` was provided, use the active runtime's `inspect_code` / `run_shell` capabilities to look for persisted interview artifacts under the Ouroboros data directory (for example `~/.ouroboros/data/`), exported session artifacts, or other exact local records for that ID.
   - If both conversation context and a persisted artifact are available, merge them conservatively: keep the persisted transcript as evidence, but let explicit in-thread user corrections or clarifications supersede older persisted wording.
   - If neither local artifacts nor conversation history provide enough requirements, ask the user for the missing interview transcript / concise requirement summary, or ask them to run or resume `ooo interview`. Do not generate a seed from an absent transcript.
3. Generate a Seed YAML specification from the recovered requirements.
4. Continue immediately into the required QA Refinement Loop. Do not present the seed as final, ask for acceptance, or proceed to "After Seed Generation" until QA exits with PASS or the user explicitly accepts a below-threshold best attempt at the loop boundary.

### QA Refinement Loop (Required after generation)

After Path A or Path B produces a seed, **do not present it as final yet**. Run a QA loop until the seed passes a high quality bar.

The first generation (Path A `ouroboros_generate_seed` or Path B agent role) runs **exactly once** and establishes the seed's ontology. From there on, **all revisions are direct YAML edits by you (main session)** — do not call `ouroboros_generate_seed` again. It does not accept revision hints, and re-running it would discard the established ontology.

**Threshold for seed**: `pass_threshold: 0.90` (stricter than default 0.80 — seeds are structural specs and must be precise).

**Max iterations**: 5. Track the highest-scoring seed across all iterations (the "best attempt"). If still not PASS after 5, present that best attempt with its QA verdict and ask the user: accept it as-is, request one final manual edit, or escalate to `ooo interview` / `ooo unstuck`. If the user accepts a below-threshold attempt, present the complete accepted Seed YAML in a fenced `yaml` block before proceeding to "After Seed Generation". If the user requests one final manual edit, apply exactly that edit, present the edited Seed YAML, ask for explicit accept/escalate, and do not start a sixth QA iteration unless the user explicitly asks to rerun QA despite the max-iteration cap.

The seed sits inside the **Define** diamond of Double Diamond — where expansion (Wonder) and convergence (Reflect/Refine/Restate) both happen in service of a single sharp specification. Expansion is not the enemy; **unchecked expansion that bypasses the user gate is.** The four-phase cycle plus User Adoption Gate is the workflow's primary safeguard.

**Loop**:

1. Establish the QA evaluator for this run:
   - **MCP QA mode**: Load the QA tool via the active runtime's `call_mcp` capability using runtime tool discovery query `"+ouroboros qa"` if not already loaded.
   - **Fallback QA mode**: If MCP is unavailable, read `src/ouroboros/agents/qa-judge.md`, adopt that evaluator role, and return its exact JSON schema: lowercase `verdict` (`pass`/`revise`/`fail`), numeric `score`, `dimensions`, `differences`, `suggestions`, and `reasoning`. In this mode there is no MCP-owned `qa_session_id`; track iteration history in the audit block and local loop ledger instead.

2. Obtain a QA verdict using the available mode:

   **MCP QA mode** — call QA on the generated seed through the active runtime's `call_mcp` capability:
   ```
   Tool: ouroboros_qa
   Arguments:
     artifact: <the seed YAML>
     quality_bar: "Seed must be internally consistent, acceptance_criteria must be measurable and testable, constraints must be concrete (no vague terms), ontology_schema must cover all entities referenced in goal/criteria, and there must be no contradictions between fields."
     artifact_type: "document"
     pass_threshold: 0.90
     seed_content: <the seed YAML>
     qa_session_id: <reuse across iterations>
     iteration_history: <accumulated>
   ```

   **Fallback QA mode** — skip the tool call and evaluate the current seed text under the QA Judge role from step 1, using the same quality bar and threshold. Treat the locally produced verdict exactly like the MCP verdict for the PASS/REVISE/FAIL branch below.

   **QA response shapes**: Branch only after a usable verdict is available.
   - In MCP QA mode, if the response has `status: "delegated_to_subagent"` and no verdict payload, keep the returned `qa_session_id`, wait for the plugin-managed subagent result, then parse that result as the QA verdict. Do not treat the delegation envelope itself as PASS/REVISE/FAIL.
   - In MCP QA mode, if the response already includes a scored verdict, parse that inline verdict directly.
   - In fallback QA mode, parse the exact QA Judge JSON. Normalize `verdict` to uppercase only for the branch labels below (`pass`→PASS, `revise`→REVISE, `fail`→FAIL). Treat `differences` as blocking/revision issues and `suggestions` as proposed fixes; do not add non-schema fields such as `loop_action`.
   - In all modes, append the parsed verdict plus applied/rejected revision decisions to `iteration_history` before the next QA pass.

3. Branch on verdict:
   - **PASS (>= 0.90)**: Exit loop. Present the final validated Seed YAML to the user, then proceed to "After Seed Generation" below.
   - **REVISE (0.40–0.89)**: Run the **Wonder → Reflect → Refine → Restate** cycle below, then loop back to step 2.
   - **FAIL (< 0.40)**: Stop the loop. The seed has fundamental issues that regeneration likely won't fix. Show the full verdict and recommend `ooo interview` to revisit requirements, or `ooo unstuck` to challenge assumptions. Do not proceed to celebration.

4. On iteration N >= 3, briefly tell the user "Refining seed (iteration N/5)..." so they know progress is being made — but do not dump full verdicts each round; only deltas.

5. After PASS, show a one-line summary of the journey: `Seed passed QA at iteration N/5 with score X.XX.`

6. Immediately after that PASS summary, present the complete final validated Seed YAML in a fenced `yaml` block. This must happen before any "After Seed Generation" celebration, star prompt, setup prompt, or next-step text.

#### Wonder → Reflect → Refine → Restate (REVISE branch)

This revision loop mirrors the Double Diamond Define cycle: **diverge via multiple perspectives first, then converge through debate, user decision, and structural application.** Revisions must NEVER be auto-applied by the main session alone — *"No candidate is accepted by default."* (Symposium User Adoption Gate)

Four explicit phases per iteration:
- **Wonder** — diverge: collect raw proposals from independent sources
- **Reflect** — debate: surface where sources agree and where they conflict
- **Refine** — user gate: human picks which proposals enter the next seed
- **Restate** — apply: edit YAML in place with accepted items only

**Phase 1 — Wonder (diverge): collect raw proposals from available sources**

**Source 1 — QA Judge** (structural, external)
The `suggestions` from the QA verdict. These are gaps, contradictions, and quality issues in the YAML itself. QA cannot see the interview.

**Source 2 — Socrates** (dialectical, user-intent evidence)
You are Socrates — the Socratic facilitator lens from `skills/interview/SKILL.md` and `src/ouroboros/agents/socratic-interviewer.md`. Review the current seed YAML against verifiable interview evidence, in this order:

1. If a `session_id` exists, first use available persisted interview/session state for that session. Path A may run from `ooo seed <session_id>` in a fresh conversation, so persisted state can be the only reliable dialectic record.
2. Use conversation memory when it is available in the current thread.
3. If no persisted state or conversation evidence is available for a point, mark Socrates output as `no Socrates-only proposal: dialectic context unavailable` for that point. Do not invent user preferences, rejected scope, or interview nuance.

From the available evidence, surface 2–4 items neither QA nor lateral personas can see:
- Did the user emphasize a constraint that got softened or dropped?
- Did something the user explicitly rejected sneak back in?
- Did the seed flatten nuance the user spent multiple turns clarifying?
- Are there silent assumptions the user never agreed to?
- Does wording contradict stated priorities (e.g., "MVP in a week" but 8 acceptance criteria)?

If QA and Socrates conflict, do not resolve the conflict silently in Wonder. Carry both candidates into Reflect as a divergent signal, cite the available evidence for each side, and let the Refine user gate choose the resolution. Do not assume the Socratic lens is automatically authoritative; QA can be correct when no user-intent evidence contradicts it.

**Source 3 — `ouroboros_lateral_think` (independent perspectives, MCP-only when available)**
Attempt to load the MCP tool with the active runtime's `call_mcp` capability using runtime tool discovery query `"+ouroboros lateral"` if needed. If the tool loads, call it through the active runtime's `call_mcp` capability to collect 5 independent MCP personas or isolated perspectives:

```
Tool: ouroboros_lateral_think
Arguments:
  problem_context: |
    Seed is in REVISE state (QA score X.XX, threshold 0.90).
    Current seed YAML:
    <YAML>
    QA suggestions:
    - <suggestion 1>
    - <suggestion 2>
    Original user goal from interview: <recall>
  current_approach: "The seed as currently drafted (above)."
  persona: "all"
  failed_attempts:
    - <previously rejected candidate from earlier iterations>
    - ...
```

The 5 personas return distinct revision angles:
- **hacker**: unconventional workarounds (e.g., reframe a constraint instead of adding criteria)
- **researcher**: knowledge the seed assumes but doesn't pin down
- **simplifier**: criteria/constraints to *remove* for sharper convergence
- **architect**: structural reorganization without expansion
- **contrarian**: challenges to assumptions the seed treats as settled

**Parsing persona outputs when lateral MCP is available**: Each persona returns free-form prose, not a structured list. After the parallel call returns, read each persona's text and extract its concrete proposals into discrete candidates (one revision per candidate, not bundled). If a persona's output is purely abstract advice with no actionable revision, drop it from the candidate list rather than inventing one. Aim for 1–2 candidates per persona — if a persona produced 5, pick the 2 most concrete and discard the rest.

**Lateral response shapes**: `ouroboros_lateral_think` does not have one universal synchronous shape. After calling it with all personas, branch on the returned shape before extracting candidates:

- **Plugin delegation**: If the response has `status: "delegated_to_subagent"`, `dispatch_mode: "plugin"`, and an `_subagents` array, wait for every plugin-managed subagent result. Extract concrete revision candidates from those returned persona texts. Do not attempt to parse candidates from the envelope prompts themselves.
- **Inline fallback with dispatch block**: If the response returns markdown `content` plus the hidden sentinel `<!-- ouroboros-lateral-inline-dispatch-v1 base64 ... -->`, keep the visible markdown as the lateral scaffold. If the active runtime can dispatch isolated subagents, decode the sentinel JSON (`dispatch_mode`, `persona_count`, `payloads`) and send each `payload.prompt` + `payload.context` through that isolated subagent surface, then extract candidates from the returned persona texts. If the runtime cannot dispatch subagents, synthesize candidates directly from the visible inline persona sections.
- **Inline fallback without dispatch block**: Treat the returned markdown as the complete lateral output and synthesize candidates directly from the visible persona sections. Do not split solely on `---` if doing so would corrupt user-provided content; prefer section headers and visible persona boundaries.

If runtime tool discovery cannot load `ouroboros_lateral_think`, do not emulate lateral personas or read persona files directly. Record `no lateral proposals: MCP lateral tool unavailable` as Source 3 output and proceed with QA plus Socrates/available sources. The QA refinement loop remains required, and the User Adoption Gate still applies to any proposed revision.

**Phase 2 — Reflect (debate): structure proposals by agreement and conflict**

Do not just dedupe. Read all proposals from the available Wonder sources (Sources 1–2, plus Source 3 only when `ouroboros_lateral_think` loaded successfully) and surface the *structure of the debate*:

- **Convergent signals (strong)**: same revision proposed by ≥2 independent sources. Example: QA says "criterion 3 is unmeasurable" AND simplifier says "drop criterion 3 or sharpen it" → strong signal to act on criterion 3.
- **Divergent signals (decisions)**: sources conflict. Example: researcher says "add User entity to ontology" but simplifier says "remove the User reference from goal — single-user implied". This is a decision the *user* must resolve, not the main session.
- **Singleton signals (weaker)**: one source only. Keep but mark as weaker.
- **Balance signal**: count expansion proposals (add) vs convergence proposals (sharpen/remove). Show the ratio above the user gate as information, not warning — e.g., `Balance: 4 expand / 2 sharpen / 1 remove`. Both directions are legitimate; the user decides what mix to accept.

Output of Reflect: a tagged candidate list with per-item metadata `(sources_backing, type=expand|sharpen|remove|resolve_conflict)`.

**Phase 3 — Refine (User Adoption Gate)**

Use the active runtime's `ask_user` capability with executable single-choice questions only. Do not ask one multi-select question or present options that can be selected contradictorily.

Ask sequential single-choice questions in this order:

1. For each conflict group, ask one question with exactly one option per mutually exclusive resolution plus "Leave unchanged"; handle the runtime's free-form "Other" response if available. Record the chosen option as accepted and mark the other options in that group rejected.
2. For non-conflicting convergent signals, ask one single-choice batch question: "Apply all strong non-conflicting revisions, review one by one, or skip them?" If the user chooses review, ask each revision as a Yes/No/Other single-choice question.
3. For singleton signals, ask one single-choice batch question: "Review singleton revisions one by one, skip all singleton revisions, or other?" If the user chooses review, ask each revision as a Yes/No/Other single-choice question.
4. Always include an exit option at the batch level: "None of the above / keep current seed". If selected, exit the loop with the current seed.

Convergent signals still appear first in summaries, conflicts second, singletons last. Conflict questions must be asked before any non-conflicting batch is applied so contradictory revisions cannot both enter the next seed.

```
Iteration N/5 — QA score X.XX (REVISE)

Which revisions should enter the next seed?
(Nothing accepted by default. Questions are single-choice and may be sequential.)

Strong (multiple sources agree):
A. [QA + Simplifier] Criterion 3 "easy to use" — sharpen to measurable predicate
B. [QA + Socrates] Re-add "single-user only" constraint dropped from iter-0

Conflicts (mutually exclusive — pick at most one per group):
C1. [Researcher] Add User entity to ontology
C2. [Simplifier] Remove User reference from goal (single-user implied)
C3. Neither — leave ontology untouched on this point

Singletons:
D. [Contrarian] Constraint "no external DB" contradicts criterion 7
E. [Architect] Group 3 user-management criteria under one parent
F. [Hacker] Replace "user authentication" with "device-local key file"

Other:
G. None of the above (exit loop with current seed)
H. Other — describe a different change
```

Portable gate example:

```json
{
  "questions": [{
    "question": "Conflict: how should the seed handle User in the ontology?",
    "header": "Conflict C",
    "options": [
      {"label": "Add User", "description": "Accept C1 and reject C2/C3"},
      {"label": "Remove User", "description": "Accept C2 and reject C1/C3"},
      {"label": "Leave unchanged", "description": "Accept C3 and reject C1/C2"}
    ],
    "multiSelect": false
  }]
}
```

Balance line shown above the question: `Balance: 4 expand / 2 sharpen / 1 remove` (informational, not a warning).

Track all rejected candidates across iterations and pass them as `failed_attempts` to subsequent `ouroboros_lateral_think` calls when the MCP lateral tool is available, so personas don't re-propose them.

**Phase 4 — Restate (apply accepted only)**

Edit the previous seed YAML in place. Apply ONLY user-accepted items. Do not start from scratch. Do not lose fields that were already correct. Do not call `ouroboros_generate_seed` again — that tool runs only at iter-0.

If the user picks "None", exit the loop with the current seed even though it's below threshold — user judgment overrides the threshold. Before proceeding to "After Seed Generation", present the complete current Seed YAML in a fenced `yaml` block so the accepted artifact is explicit.

Common edit shapes (both expansion and convergence are legitimate when the user accepted them):
- Sharpen: replace vague phrase with measurable predicate (`"fast"` → `"p95 latency < 200ms"`)
- Tighten: harden a soft constraint (`"some kind of storage"` → `"SQLite, single file, no server"`)
- Make implicit explicit: surface a silent assumption as a constraint
- Remove: drop a contradicting or redundant criterion
- Expand (when accepted): add an ontology entity, criterion, or constraint that fills a gap the user confirmed

**Audit trail**

After each revision, append a brief audit block to `~/.ouroboros/seed-revisions/<revision_key>.md` (create the directory if it doesn't exist) capturing: iteration N, QA score, all candidates with source tag, user's accept/reject decisions, and the resulting diff vs. previous iteration. This makes the convergence path inspectable and lets the user replay decisions later.

Choose `revision_key` deterministically:
- If `session_id` exists, use that exact `session_id`.
- If no `session_id` exists (common in Path B), derive a stable seed label from the seed goal or project name plus the current UTC timestamp, for example `<slugified-goal>-YYYYMMDDTHHMMSSZ`. Once derived, reuse the same key for every iteration in the current seed run.
- If the filesystem write is unavailable, include the same audit block in the assistant response instead of silently dropping it.

Format:
```markdown
## Iteration N — score X.XX

### Candidates
- [A] [QA+Simplifier] sharpen criterion 3 — **accepted**
- [B] [Socrates] re-add single-user constraint — **accepted**
- [C1] [Researcher] add User entity — rejected
- [C2] [Simplifier] remove User from goal — **accepted**
- [D] [Contrarian] resolve no-DB / criterion-7 conflict — rejected
- ...

### Diff vs. iteration N-1
- criteria[2]: "easy to use" → "first-time user completes flow in < 3 clicks"
- constraints: + "single-user only"
- goal: "...for users..." → "...for the single operator..."
```

## Seed Components

The seed contains:

- **GOAL**: Clear primary objective
- **CONSTRAINTS**: Hard limitations (e.g., Python >= 3.12, no external DB)
- **ACCEPTANCE_CRITERIA**: Measurable success criteria
- **ONTOLOGY_SCHEMA**: Data structure definition (name, fields, types)
- **EVALUATION_PRINCIPLES**: Quality principles with weights
- **EXIT_CONDITIONS**: When the workflow should terminate
- **METADATA**: Version, timestamp, ambiguity score, interview ID

## Example Output

```yaml
goal: Build a CLI task management tool
constraints:
  - Python >= 3.12
  - No external database
  - SQLite for persistence
acceptance_criteria:
  - Tasks can be created
  - Tasks can be listed
  - Tasks can be marked complete
ontology_schema:
  name: TaskManager
  description: Task management domain model
  fields:
    - name: tasks
      type: array
      description: List of tasks
    - name: title
      type: string
      description: Task title
metadata:
  ambiguity_score: 0.15
```

## After Seed Generation

On successful seed generation, first announce:

```
Your seed has been crystallized!
```

Then check `~/.ouroboros/prefs.json` for `star_asked`. If `star_asked` is not set to `true`, use the active runtime's `ask_user` capability with this single question:

```json
{
  "questions": [{
    "question": "If Ouroboros helped clarify your thinking, a GitHub star supports continued development. Ready to unlock Full Mode?",
    "header": "Next step",
    "options": [
      {
        "label": "\u2b50 Star & Setup",
        "description": "Star on GitHub + run ooo setup to enable run, evaluate, status"
      },
      {
        "label": "Just Setup",
        "description": "Skip star, go straight to ooo setup for Full Mode"
      }
    ],
    "multiSelect": false
  }]
}
```

- **Star & Setup**: Run `gh api -X PUT /user/starred/Q00/ouroboros`, merge `{"star_asked": true}` into `~/.ouroboros/prefs.json`, then read and execute `skills/setup/SKILL.md`
- **Just Setup**: Merge `{"star_asked": true}` into `~/.ouroboros/prefs.json`, then read and execute `skills/setup/SKILL.md`
- **Other** (user provides custom text): Merge `{"star_asked": true}` into `~/.ouroboros/prefs.json`, skip setup

Create `~/.ouroboros/` directory if it doesn't exist. Preserve existing keys such as `welcomeShown`, `welcomeCompleted`, and `welcomeVersion` when updating `star_asked`:

```bash
python3 - <<'PY'
import json, os
path = os.path.expanduser('~/.ouroboros/prefs.json')
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, encoding='utf-8') as f:
        prefs = json.load(f)
    if not isinstance(prefs, dict):
        prefs = {}
except Exception:
    prefs = {}
prefs['star_asked'] = True
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, indent=2)
    f.write('\n')
PY
```

If `star_asked` is already `true`, skip the question and just announce:

```
Your seed has been crystallized!
📍 Next: `ooo run` to execute this seed (requires `ooo setup` first)
```
