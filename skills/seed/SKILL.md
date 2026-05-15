---
name: seed
description: "Generate validated Seed specifications from interview results"
mcp_tool: ouroboros_generate_seed
mcp_args:
  session_id: "$1"
---

# /ouroboros:seed

Generate validated Seed specifications from interview results.

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

1. Use the `ToolSearch` tool to find and load the seed generation MCP tool:
   ```
   ToolSearch query: "+ouroboros seed"
   ```
2. The tool will typically be named `mcp__plugin_ouroboros_ouroboros__ouroboros_generate_seed` (with a plugin prefix). After ToolSearch returns, the tool becomes callable.
3. If ToolSearch finds the tool → proceed to **Path A**. If not → proceed to **Path B**.

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

### Path A: MCP Mode (Preferred)

If the `ouroboros_generate_seed` MCP tool is available (loaded via ToolSearch above):

1. Determine the interview session:
   - If `session_id` provided: Use it directly
   - If no session_id: Check conversation for a recent `ouroboros_interview` session ID
   - If none found: Ask the user

2. Call the MCP tool:
   ```
   Tool: ouroboros_generate_seed
   Arguments:
     session_id: <interview session ID>
   ```

3. The tool extracts requirements from the interview, calculates ambiguity score, and generates the Seed YAML.

4. Present the generated seed to the user.

**Advantages of MCP mode**: Automated ambiguity scoring (must be <= 0.2), structured extraction from persisted interview state, reproducible.

### Path B: Plugin Fallback (No MCP Server)

If the MCP tool is NOT available, fall back to agent-based generation:

1. Read `src/ouroboros/agents/seed-architect.md` and adopt that role
2. Extract structured requirements from the interview Q&A in conversation history
3. Generate a Seed YAML specification
4. Present the seed to the user

### QA Refinement Loop (Required after generation)

After Path A or Path B produces a seed, **do not present it as final yet**. Run a QA loop until the seed passes a high quality bar.

The first generation (Path A `ouroboros_generate_seed` or Path B agent role) runs **exactly once** and establishes the seed's ontology. From there on, **all revisions are direct YAML edits by you (main session)** — do not call `ouroboros_generate_seed` again. It does not accept revision hints, and re-running it would discard the established ontology.

**Threshold for seed**: `pass_threshold: 0.90` (stricter than default 0.80 — seeds are structural specs and must be precise).

**Max iterations**: 5. Track the highest-scoring seed across all iterations (the "best attempt"). If still not PASS after 5, present that best attempt with its QA verdict and ask the user: accept it as-is, do one more manual edit, or escalate to `ooo interview` / `ooo unstuck`.

The seed sits inside the **Define** diamond of Double Diamond — where expansion (Wonder) and convergence (Reflect/Refine/Restate) both happen in service of a single sharp specification. Expansion is not the enemy; **unchecked expansion that bypasses the user gate is.** The four-phase cycle plus User Adoption Gate is the only structural safeguard — no separate discipline rules are needed.

**Loop**:

1. Load the QA tool via `ToolSearch query: "+ouroboros qa"` if not already loaded. Fall back to the `ouroboros:qa-judge` agent role (read `src/ouroboros/agents/qa-judge.md`) if MCP is unavailable.

2. Call QA on the generated seed:
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

3. Branch on verdict:
   - **PASS (>= 0.90)**: Exit loop. Proceed to "After Seed Generation" below.
   - **REVISE (0.40–0.89)**: Run the **Wonder → Reflect → Refine → Restate** cycle below, then loop back to step 2.
   - **FAIL (< 0.40)**: Stop the loop. The seed has fundamental issues that regeneration likely won't fix. Show the full verdict and recommend `ooo interview` to revisit requirements, or `ooo unstuck` to challenge assumptions. Do not proceed to celebration.

4. On iteration N >= 3, briefly tell the user "Refining seed (iteration N/5)..." so they know progress is being made — but do not dump full verdicts each round; only deltas.

5. After PASS, show a one-line summary of the journey: `Seed passed QA at iteration N/5 with score X.XX.`

#### Wonder → Reflect → Refine → Restate (REVISE branch)

This revision loop mirrors the Double Diamond Define cycle: **diverge via multiple perspectives first, then converge through debate, user decision, and structural application.** Revisions must NEVER be auto-applied by the main session alone — *"No candidate is accepted by default."* (Symposium User Adoption Gate)

Four explicit phases per iteration:
- **Wonder** — diverge: collect raw proposals from independent sources
- **Reflect** — debate: surface where sources agree and where they conflict
- **Refine** — user gate: human picks which proposals enter the next seed
- **Restate** — apply: edit YAML in place with accepted items only

**Phase 1 — Wonder (diverge): collect raw proposals from three sources**

**Source 1 — QA Judge** (structural, external)
The `suggestions` from the QA verdict. These are gaps, contradictions, and quality issues in the YAML itself. QA cannot see the interview.

**Source 2 — Socrates** (dialectical, internal)
You are Socrates — the same Socratic facilitator who conducted the interview (see `skills/interview/SKILL.md` and `src/ouroboros/agents/socratic-interviewer.md`). You hold the only complete record of the dialectic in conversation memory: what the user actually said, what was agreed, what was explicitly rejected, what scope was carried through the Refine and Restate gates. Silently review the current seed YAML against that record and surface 2–4 items neither QA nor lateral personas can see:
- Did the user emphasize a constraint that got softened or dropped?
- Did something the user explicitly rejected sneak back in?
- Did the seed flatten nuance the user spent multiple turns clarifying?
- Are there silent assumptions the user never agreed to?
- Does wording contradict stated priorities (e.g., "MVP in a week" but 8 acceptance criteria)?

If QA and Socrates conflict, **Socrates wins** — QA does not know what the user actually said in the interview, you do.

**Source 3 — `ouroboros_lateral_think` (parallel personas)**
Call the MCP tool to fan out 5 independent perspectives, each running in its own Task pane with no cross-contamination:

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

Load via `ToolSearch query: "+ouroboros lateral_think"` if needed.

**Parsing persona outputs**: Each persona returns free-form prose, not a structured list. After the parallel call returns, read each persona's text and extract its concrete proposals into discrete candidates (one revision per candidate, not bundled). If a persona's output is purely abstract advice with no actionable revision, drop it from the candidate list rather than inventing one. Aim for 1–2 candidates per persona — if a persona produced 5, pick the 2 most concrete and discard the rest.

**Phase 2 — Reflect (debate): structure proposals by agreement and conflict**

Do not just dedupe. Read all proposals from Sources 1–3 and surface the *structure of the debate*:

- **Convergent signals (strong)**: same revision proposed by ≥2 independent sources. Example: QA says "criterion 3 is unmeasurable" AND simplifier says "drop criterion 3 or sharpen it" → strong signal to act on criterion 3.
- **Divergent signals (decisions)**: sources conflict. Example: researcher says "add User entity to ontology" but simplifier says "remove the User reference from goal — single-user implied". This is a decision the *user* must resolve, not the main session.
- **Singleton signals (weaker)**: one source only. Keep but mark as weaker.
- **Balance signal**: count expansion proposals (add) vs convergence proposals (sharpen/remove). Show the ratio above the user gate as information, not warning — e.g., `Balance: 4 expand / 2 sharpen / 1 remove`. Both directions are legitimate; the user decides what mix to accept.

Output of Reflect: a tagged candidate list with per-item metadata `(sources_backing, type=expand|sharpen|remove|resolve_conflict)`.

**Phase 3 — Refine (User Adoption Gate)**

Present the structured list via AskUserQuestion with multi-select. Convergent signals first, conflicts second, singletons last:

```
Iteration N/5 — QA score X.XX (REVISE)

Which revisions should enter the next seed?
(Nothing accepted by default. Multi-select.)

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

Balance line shown above the question: `Balance: 4 expand / 2 sharpen / 1 remove` (informational, not a warning).

Track all rejected candidates across iterations and pass them as `failed_attempts` to subsequent `ouroboros_lateral_think` calls so personas don't re-propose them.

**Phase 4 — Restate (apply accepted only)**

Edit the previous seed YAML in place. Apply ONLY user-accepted items. Do not start from scratch. Do not lose fields that were already correct. Do not call `ouroboros_generate_seed` again — that tool runs only at iter-0.

If the user picks "None", exit the loop with the current seed even though it's below threshold — user judgment overrides the threshold.

Common edit shapes (both expansion and convergence are legitimate when the user accepted them):
- Sharpen: replace vague phrase with measurable predicate (`"fast"` → `"p95 latency < 200ms"`)
- Tighten: harden a soft constraint (`"some kind of storage"` → `"SQLite, single file, no server"`)
- Make implicit explicit: surface a silent assumption as a constraint
- Remove: drop a contradicting or redundant criterion
- Expand (when accepted): add an ontology entity, criterion, or constraint that fills a gap the user confirmed

**Audit trail**

After each revision, append a brief audit block to `~/.ouroboros/seed-revisions/<session_id>.md` (create the directory if it doesn't exist) capturing: iteration N, QA score, all candidates with source tag, user's accept/reject decisions, and the resulting diff vs. previous iteration. This makes the convergence path inspectable and lets the user replay decisions later.

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

Then check `~/.ouroboros/prefs.json` for `star_asked`. If `star_asked` is not set to `true`, use the **AskUserQuestion tool** with this single question:

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
