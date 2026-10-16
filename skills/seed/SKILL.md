---
name: seed
description: "Use when goal, constraints, and success criteria are already settled and the user wants them frozen into a formal spec an agent can execute and verify: 'turn this into a spec', 'write it up as a Seed', or right after an interview. Returns an immutable Seed with acceptance criteria. Not for vague requests (use interview first)."
aliases: [crystallize]
mcp_tool: ouroboros_generate_seed
mcp_args:
  session_id: "$1"
---

# /ouroboros:seed

Generate a validated Seed specification from an interview or from requirements this conversation already settled.

## Required Skill Capabilities

`ask_user` (the active runtime's user-question surface), `inspect_code`, `call_mcp`, `orchestrate_subagents` (lateral persona payloads), `run_shell` (audit-trail and prefs writes), `refine_answer` (confirm free-form decisions), `maintain_ledger` (QA scores, candidate decisions, rejected proposals).

## Usage

```
ooo seed [session_id]
/ouroboros:seed [session_id]
```

## Instructions

### Python Runtime (Required)

Define this resolver in the same shell before running any shell snippet below, and call `ouroboros_python` with every argument quoted. It accepts only Python 3.12 or newer and falls back to uv.

<!-- ouroboros-python-resolver:start -->
```bash
ouroboros_python() {
  if command -v python3 >/dev/null 2>&1 &&
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))') >/dev/null 2>&1
  then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python3 "$@")
    return
  fi
  if command -v python >/dev/null 2>&1 &&
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python -c 'import sys; raise SystemExit(sys.version_info < (3, 12))') >/dev/null 2>&1
  then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python "$@")
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command uv run --no-project --quiet --python '>=3.12' python "$@")
    return
  fi
  printf '%s\n' 'Ouroboros skills require Python >= 3.12 or uv on PATH.' >&2
  return 127
}
```
<!-- ouroboros-python-resolver:end -->

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros seed"` for `ouroboros_generate_seed`; `"+ouroboros qa"` for `ouroboros_qa`; `"+ouroboros lateral"` for `ouroboros_lateral_think`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the fallback only when the tool is genuinely absent.

Load `ouroboros_generate_seed` before choosing a path: if it is callable, use **Path A**; use **Path B** only when it is genuinely absent (no Ouroboros MCP server).

**Delegated responses are not artifacts.** If any tool below returns `status: "delegated_to_subagent"` (with `dispatch_mode: "plugin"` and possibly an `_subagents` array), keep the returned session ID, wait for the plugin-managed subagent results, and parse the artifact (Seed YAML, QA verdict, or persona texts) from those results, never from the envelope or its prompts.

### Path A: MCP Mode (Preferred)

1. Pick the entrance:
   - `session_id` given, or a recent `ouroboros_interview` session ID in the conversation: use the interview session (step 2).
   - No interview, but THIS conversation already settled the goal, the constraints, and verifiable success criteria (a lateral decision, a brownfield scan, or plain discussion that converged): use the interview-less path below. Do not send the user to `ooo interview` to repeat what they already said.
   - Otherwise ask the user which interview or requirements to seed.

   **Interview-less path** (`session_context`):
   ```
   Tool: ouroboros_generate_seed
   Arguments:
     session_context:
       goal: <the user's own settled wording, verbatim, never your paraphrase>
       acceptance_criteria: [<verifiable checks: a command, a visible behaviour, a measurable state>]
       constraints: [<optional>]
       decisions: [<optional; each becomes a constraint>]
       project_type: greenfield | brownfield
   ```
   Every value enters the Seed byte-for-byte, so shell chains in an AC (`ruff check && pytest`) are allowed. If the response has `status: "gap_questions_required"`, ask the user exactly the listed questions, merge the answers into `session_context`, and call again; that is the whole interview.

2. Interview session:
   ```
   Tool: ouroboros_generate_seed
   Arguments:
     session_id: <interview session ID>
   ```
   The tool extracts requirements from persisted interview state, scores ambiguity (must be <= 0.2), and returns Seed YAML.

3. If no Seed YAML results, stop and ask the user to resume generation or supply the artifact. Never fabricate a Seed.

4. Run the Advisory QA Check below, then go to "After Seed Generation".

### Path B: No MCP Server

1. Read the `seed-architect` prompt and adopt that role: `$OUROBOROS_AGENTS_DIR/seed-architect.md` when that override is set, else `seed-architect.md` in the installed `ouroboros.agents` package, else `src/ouroboros/agents/seed-architect.md` in a source checkout. Resolve every agent prompt this skill names the same way.
2. Recover the requirements before drafting; never invent missing context:
   - With a `session_id`, use current-thread Q&A and corrections only when they belong to that session; if incomplete, use `inspect_code` / `run_shell` to find persisted or exported artifacts for that ID (for example under `~/.ouroboros/data/`). Keep a persisted transcript as evidence, but let explicit same-thread corrections supersede it.
   - Without a `session_id`, use current-thread Q&A only when it forms one coherent interview.
   - Otherwise ask for the transcript or a requirement summary, or suggest `ooo interview`. Never seed from an absent or mismatched transcript.
3. Generate the Seed YAML, run the Advisory QA Check, then go to "After Seed Generation".

### Advisory QA Check (single pass, non-blocking)

Run QA **exactly once** on the generated Seed. The verdict is advisory and never blocks: the Seed is presented as final regardless of score, and there is no QA-until-PASS loop.

Generation (Path A tool or Path B role) runs **exactly once** and fixes the Seed's ontology. Any later revision is a direct YAML edit by you; never call `ouroboros_generate_seed` again (it takes no revision hints and would discard the ontology).

**MCP QA mode:**
   ```
   Tool: ouroboros_qa
   Arguments:
     artifact: <the seed YAML>
     quality_bar: "Seed must be internally consistent, acceptance_criteria must be measurable and testable, constraints must be concrete (no vague terms), ontology_schema must cover all entities referenced in goal/criteria, and there must be no contradictions between fields. acceptance_criteria must also be parsimonious in the ontological sense: a criterion names a state of the finished work a user can see is true, while an implementation step names a means of reaching it, and only the first belongs in the list. Read each criterion beside its siblings — one intelligible only as a move toward a sibling is that sibling's means and belongs merged into the outcome it serves, and flagging that is as important as flagging a missing piece, since it commits the seed to an unverified path. How many criteria a goal has follows from that judgment, so weigh each criterion against its siblings."
     artifact_type: "document"
     pass_threshold: 0.90
     seed_content: <the seed YAML>
     qa_session_id: <reuse across passes>
     iteration_history: <accumulated across passes>
   ```
The 0.90 bar (stricter than the default 0.80) labels the verdict; it gates nothing.

**Fallback QA mode** (QA tool absent): read the `qa-judge` prompt (resolved as in Path B step 1), adopt that role, and evaluate the Seed with the same quality bar and threshold. It returns JSON with lowercase `verdict` (`pass`/`revise`/`fail`), `score`, `dimensions`, `differences`, `suggestions`, `reasoning`; uppercase the verdict only for display and add no non-schema fields. There is no `qa_session_id`; keep pass history in the audit block.

Present, always in this order:
1. `QA advisory: <PASS|REVISE|FAIL>, score X.XX (bar 0.90).`
2. Below the bar: the top 2-3 suggestions as short advisory bullets (findings, not tasks). Apply none automatically.
3. The complete Seed YAML in a fenced `yaml` block.
4. For REVISE or FAIL, one opt-in line such as `Want a refinement pass on these findings? Otherwise the seed stands as-is.` For FAIL (< 0.40) also mention `ooo interview` (revisit requirements) or `ooo unstuck` (challenge assumptions). Then continue to "After Seed Generation"; the Seed is final unless the user opts in.

Only on an explicit opt-in, run one refinement pass (below), re-run QA once for an updated advisory line, and present the revised YAML. Each further pass needs a fresh explicit opt-in; never chain passes. Before each later QA call, append the previous verdict and the applied/rejected decisions to `iteration_history`.

#### Refinement pass: Wonder → Reflect → Refine → Restate (opt-in)

Diverge across independent sources, debate, let the user decide, then apply. No candidate is accepted by default, and you never apply a revision on your own (User Adoption Gate).

**Wonder: collect proposals.**
- **QA**: the verdict's `suggestions` (structural gaps in the YAML; QA cannot see the interview).
- **Socratic lens** (`socratic-interviewer` prompt, resolved as in Path B step 1): check the Seed against interview evidence (persisted session state first, then this conversation) for 2-4 items: dropped or softened user constraints, rejected scope that crept back, flattened nuance, unagreed assumptions. Without evidence, record `no Socratic proposal: dialectic context unavailable`; never invent preferences. A QA/Socratic conflict goes to Reflect; neither side wins automatically.
- **Lateral personas** (only if `ouroboros_lateral_think` is callable):
  ```
  Tool: ouroboros_lateral_think
  Arguments:
    problem_context: |
      User opted into a refinement pass (QA advisory score X.XX, bar 0.90).
      Current seed YAML:
      <YAML>
      QA suggestions:
      - <suggestion 1>
      Original user goal from interview: <recall>
    current_approach: "The seed as currently drafted (above)."
    persona: "all"
    failed_attempts:
      - <candidates rejected in earlier passes>
  ```
  Extract at most 1-2 concrete, discrete revisions from each persona's prose and drop abstract advice. If the response carries the `<!-- ouroboros-lateral-inline-dispatch-v1 base64 ... -->` sentinel and the runtime can dispatch isolated subagents, decode it and send each `payload.prompt` + `payload.context` through `orchestrate_subagents`; otherwise read the visible persona sections (split on section headers, not bare `---`). If the tool is absent, record `no lateral proposals: MCP lateral tool unavailable` and do not emulate personas.

**Reflect: structure the debate.** Tag each candidate with its backing sources and a type (`expand`, `sharpen`, `remove`, `resolve_conflict`): convergent (two or more sources agree, strong), divergent (sources conflict; the user resolves it), singleton (weaker). Show a balance line such as `Balance: 4 expand / 2 sharpen / 1 remove` as information, not a warning.

**Refine: user gate.** Ask single-choice `ask_user` questions only, conflicts first: one per conflict group (one option per resolution plus "Leave unchanged"), then strong revisions ("apply all, review one by one, or skip"), then singletons ("review one by one or skip all"). Every batch offers "keep current seed"; confirm free-form answers with `refine_answer`. Pass rejected candidates as `failed_attempts` on later lateral calls.

**Restate: apply accepted items only.** Edit the existing YAML in place, keep fields that were already correct, and do not regenerate. If everything was skipped, the Seed stands unchanged.

**Audit trail.** After each pass, append to `~/.ouroboros/seed-revisions/<revision_key>.md` (create the directory if needed): pass number, QA score, every candidate with its source tag and accepted/rejected decision, and the diff against the previous YAML. `revision_key` is the `session_id`, or, without one, `<slugified-goal>-YYYYMMDDTHHMMSSZ` derived once and reused for every pass. If the write fails, include the audit block in the response.

## After Seed Generation

Announce `Your seed has been crystallized!`

Then read `~/.ouroboros/prefs.json`. If `star_asked` is not `true`, ask one single-choice question with `ask_user`: "If Ouroboros helped clarify your thinking, a GitHub star supports continued development. Ready to unlock Full Mode?" with options:

- **⭐ Star & Setup**: run `gh api -X PUT /user/starred/Q00/ouroboros`, record `star_asked`, then read and execute `../setup/SKILL.md`.
- **Just Setup**: record `star_asked`, then read and execute `../setup/SKILL.md`.
- **Other** (custom text): record `star_asked` and skip setup.

Record `star_asked` with this snippet, which preserves existing keys such as `welcomeShown`, `welcomeCompleted`, and `welcomeVersion`:

```bash
ouroboros_python - <<'PY'
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

If `star_asked` is already `true`, skip the question; the footer points to `ooo run` (which requires `ooo setup` first).

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
