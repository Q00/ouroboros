---
name: interview
description: "Use when the user wants to build or change something but the request is vague or open-ended: 'help me figure out what to build', 'I'm not sure what it should do', 'add X' with no details, or no clear definition of done. Runs a short Socratic interview that turns the idea into goals, constraints, and acceptance criteria before any code is written. Not for simple questions, small clear edits, or an existing Seed."
aliases: [socratic]
mcp_tool: ouroboros_interview
mcp_args:
  initial_context: "$1"
  cwd: "$CWD"
---

# /ouroboros:interview

Socratic interview that turns a vague request into the goals, constraints, and acceptance criteria a Seed needs.

## Required Skill Capabilities

- `ask_user`: ask human-judgment questions through the active runtime's `ask_user` capability.
- `inspect_code`: answer repo-local factual questions from exact local files before asking the user.
- `call_mcp`: use Ouroboros MCP tools for persistent interview state and seed generation.
- `orchestrate_subagents`: run advisory lanes and the Seed-ready Acceptance Guard lanes.
- `run_lateral_review`: invoke lateral thinking subagents before milestone turns and direct-answer synthesis.
- `web_research`: fetch current external facts only when the interview genuinely depends on them.
- `refine_answer`: confirm structured interpretations of free-text answers before forwarding them (Refine gate).
- `maintain_ledger`: keep ambiguity, gates, and unresolved decisions visible in the main session.
- `run_closure_gate`: run the Seed-ready Acceptance Guard even when MCP reports seed-ready.
- `restate_goal`: restate the goal and require explicit approval before seed generation (Restate gate).

## Non-Skippable Gates

- Refine gate on free-text answers that carry scope, constraints, or decisions (step 4).
- A visible ambiguity ledger in the main session (step 6).
- MCP seed-ready (`meta.seed_ready=true`) is permission to run the Seed-ready Acceptance Guard (step 8), not completion.
- Restate gate and explicit user approval before suggesting or running seed generation (step 9).

The server names the last two `seed_ready_acceptance_guard` and `restate_goal_approved` in `required_client_gates`.

## Usage

```
ooo interview [topic]
/ouroboros:interview [topic]
```

The topic is the skill arguments, or the user's own request when there are none. Start from it at once; do not ask for the topic again or explain Ouroboros first. Updates are checked in the background; if your context carries an Ouroboros update notice, mention `ooo update` once after the first question.

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros interview"` for `ouroboros_interview`; `"+ouroboros lateral"` for `ouroboros_lateral_think`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the fallback only when the tool is genuinely absent.

If `ouroboros_interview` is callable, use Path A. Use Path B only when it is genuinely absent (no Ouroboros MCP server).

## Path A: MCP Mode

MCP generates the questions, keeps state, and scores ambiguity; it cannot read code, browse, or call tools. You (the main session) answer what code or research can answer and route to the user only what needs human judgment: goals, acceptance criteria, business logic, preferences. You never decide for the user; auto-confirm is for facts only.

1. **Start the interview.**
   ```
   Tool: ouroboros_interview
   Arguments:
     initial_context: <user's topic or idea>
     cwd: <current working directory>
     confused_terms: <optional explicit terms the user does not understand>
     references: <optional [{reference_id, label, origin, url?, excerpt?}]>
   ```
   Returns a session ID and the first question. `confused_terms` and `references` are structured adapter context, not requirements. They MUST NOT alter the first question; later, glossary help covers only explicitly confused terms and references serve only contrast questions. Do not infer them from vocabulary density or fetch referenced URLs/files. On a later answer, include them only when the user introduces a new reference or asks what a term means; product decisions stay in the answer.

2. **Route each question** through the PATHs below. First check the response:
   - `meta.status="parent_question_required"` or `meta.ask_user_directly=true`: a normal turn, not a failure. Follow the response text: ask one Socratic question yourself, never mention MCP, provider failures, or `reason_code`, and send the user's answer with `last_question` set to the exact question you asked.
   - `meta.question_advisory_request`: run the question-first advisory fan-out below.
   - `meta.lateral_review_required=true`: run the milestone lateral-review dispatch below.
   - Errors and `meta.reason == "initial_context_too_large"`: see Retry on failure.

   **Question-first advisory fan-out**: show the interview question first. The advisory helps the human answer; it never hides, replaces, or delays the question, which stays visible while lanes run.

   When `meta.question_advisory_subagents` is present you MUST process every
   payload: each entry (`title`, `agent`, `prompt`, `context`) is spawn-ready work, and its prompt passes unchanged. Never reconstruct prompts from prose. Obey `meta.question_advisory_host_action`: `spawn_subagents` means parallel support was declared; `dispatch_subagents_if_supported` means native parallel dispatch when available and sequential processing otherwise; `process_payloads_sequentially` requires ordered processing. The stamped `dispatch_mode` (`host_driven`, `dispatch_mode="host_decides"`, `sequential`) says the same. The payloads are the work contract, while the host action selects the execution strategy. For parallel dispatch, start one child per payload through the active runtime's `orchestrate_subagents` capability in a single fan-out turn. Wait for every result, then synthesize.

   Lanes: `code_context` (repo-local facts; reuses `meta.code_investigation_request`), `web_context` (only when current external facts affect the answer), `data_context` (takes measurements; see Data measurements), `ambiguity_contrarian` (hidden assumptions, vague terms, missing decisions, risky defaults), `answer_simplifier` (2-3 easy choices or one concise draft), `architecture_implications` (ownership, interfaces, rollout, system shape). Synthesize a compact helper: 2-3 answer options, one recommended draft, or a short ambiguity note. Do not forward advisory output to `ouroboros_interview` until the user approves, edits, or explicitly asks you to auto-confirm a safe answer.

   **Submitting fan-out results (re-entry)**: when the meta carries a `fanout_id` (`meta.question_advisory_fanout_id`, or a lateral persona panel's), call `ouroboros_submit_fanout_results` after all children return, with:
   - `session_id`: the session the fan-out was issued under; refused if omitted when the producer had one.
   - `fanout_id`: the stamped id.
   - `correlation_key`: the stamped `result_correlation_key` (`context.lane_id`, `context.persona`, or `code_facts`); refused if omitted when the fan-out recorded one.
   - `results`: one entry per lane, either `{ "key": <correlation value>, "content": <child output> }` or exactly `{ "key": <lane>, "undispatched": true }` (the literal `true`, no `content`). A lane that ran and found nothing still submits its output. A lane you could not spawn at all (no capability, the child died, the user cancelled) is `undispatched`. Never invent output for a lane you did not run. Bad entries come back together as `status="invalid_result_entry"` with `invalid_keys`.

   A complete set returns a bounded artifact envelope. Call `ouroboros_fetch_artifact` with its `contract_id`, then continue from the correlated synthesis in the fetched `body`; this MCP fetch is required even when the host has no shell. A partial set returns `status="partial"` with `missing_required_keys`: **retry with every lane you hold, not only the missing ones**, because no submitted output is kept between calls. Sequential hosts accumulate outputs and send the growing set. Only lanes marked `required: true` block completion. Continue the interview from the fetched synthesis.

   **Data measurements**: the `data_context` lane discovers the host's data tools, takes the measurement itself, and returns the aggregate it read. Nothing is confirmed before it runs and nothing is run after it; the user registering a tool is the consent to have it called. When its output carries measurements:
   - Show the numbers **beside** the question as material for the user's
     judgment, never as the answer. The user answers on the ordinary
     `[from-user]` path; there is no `[from-data]` answer to forward.
   - Carry the aggregate as the lane reported it, with its `metric` and the
     decision it informs. Do not re-derive, re-scale, or combine numbers.
   - If the user has already answered the question by the time the measurement
     arrives, drop it. Do not re-open the decision with it: evidence informs a
     decision, it does not revisit one.

   When `data_needed` is false, the reason describes the lane, never the user's infrastructure; say it in your own words rather than pasting the constant. `not_a_measurement`, `question_too_ambiguous_to_measure`, `answer_would_not_be_an_aggregate`: move on. `no_data_store_described`: mention it only if you know a store exists and the lane was not told about it. `store_described_but_not_callable`: yours to handle, not the user's to hear; check whether the tool is available to you, take the read yourself, or re-dispatch the lane. Do not attach read-only or cost disclaimers to measurements; nothing performs that check.

   **Milestone lateral-review dispatch**: `meta.lateral_review_required=true` (older responses carry only `meta.lateral_review_recommended=true`) means the interview crossed an ambiguity milestone (`initial -> progress`, `progress -> refined`, `refined -> ready`). The server raises it at most once per milestone per session. Treat it as a required lightweight subagent review for that turn, and run it silently: after showing the question, call `ouroboros_lateral_think` with `meta.lateral_review_tool_args` and dispatch its persona payloads in the same fan-out turn as the advisory lanes. Do not announce it. Its personas are only those the advisory lanes do not already run, so fold its findings into the same helper (the options or the recommended draft), never a separate block, and mention it only when a finding changes the recommended answer or the next question. If only legacy advisory fields are present, use `personas=["researcher","contrarian","simplifier"]`, a problem context with the session, milestone, and question, and a current approach describing the next interview-routing decision. If lateral tooling is unavailable, continue the interview and say the review could not be run. Lateral review never changes requirements or marks the interview complete by itself.

   **Main-session direct-answer assistance**: when you would otherwise answer the MCP question yourself or compress the user's free text into a decision, the advisory lanes are your lateral review: `ambiguity_contrarian` and `answer_simplifier` run the contrarian and simplifier personas on every question, and `architecture_implications` runs the architect. Build 2-3 concrete options or one recommended draft from their output, then let the user approve, tweak, or switch to auto. Do not call `ouroboros_lateral_think` again for a question that carried advisory lanes. Only when the response carried none (for example a `meta.ask_user_directly=true` turn where you write the question yourself) and you are about to synthesize a product, UX, or architecture answer from partial input, call it at most once for that question with one persona: `contrarian` for tradeoffs, priorities, non-goals, risk, or success criteria; `architect` when the answer changes system shape or ownership; `simplifier` when the user seems busy or terse. The same silence rule applies.

   **PATH 1: Code Answer** (current state: stack, frameworks, dependencies, patterns, architecture, file layout). Use `inspect_code`. Description, not prescription: "the project uses JWT" is a fact; "the new feature should use JWT" is a decision, so PATH 2.
   - **PATH 1a: Auto-confirm** only when ALL hold: the answer is an exact match in a manifest or config file (`pyproject.toml`, `package.json`, `go.mod`, `Dockerfile`, `.env.example`, lock files, CI config), purely descriptive, and unambiguous (one candidate). Send `[from-code][auto-confirmed]` at once, show a non-blocking note (`ℹ️ Auto-confirmed: Python 3.12, FastAPI framework (pyproject.toml)`), and resend a correction if the user later says it is wrong.
   - **PATH 1b: Code Confirmation** otherwise (inferred from patterns, several candidates, no manifest match): use the confirmation pattern with `[from-code]`.

   **PATH 2: Human Judgment** (goals, vision, acceptance criteria, business logic, preferences, tradeoffs, scope, behavior of new features): ask through `ask_user` with suggested options; prefix `[from-user]`.

   **PATH 3: Code + Judgment** (facts exist but need interpretation, e.g. "I see a saga pattern in orders/. Should payments use the same?"): read the code first, show the findings with the question, and route the entire question to the user; prefix `[from-user]`.

   **PATH 4: Research Interlude** (third-party APIs, pricing, library capabilities, version compatibility, security advisories, standards the codebase cannot answer): use `web_research`, then the confirmation pattern with `[from-research]`. Facts, not decisions: "Stripe's rate limit is 100 req/s" is research; "we should use Stripe" is PATH 2.

   **When in doubt, use PATH 2.**

   **Confirmation pattern (PATH 1b, PATH 4)**: ask through `ask_user`: "MCP asks: <question>. I found: <finding and its source>. Is this correct?" with options `Yes, correct` and `No, let me correct`.
   - `Yes, correct`: send the concise fact with the path's prefix; no Refine gate.
   - `No, let me correct`, or a correction typed directly: collect the corrected answer as free text (ask for any reasoning, constraints, or scope to keep), run the Refine gate, and send it as `[from-user][refined]`.

3. **Send the answer.**
   ```
   Tool: ouroboros_interview
   Arguments:
     session_id: <session ID>
     answer: <prefixed answer>
     last_question: <exact question text, required when you rather than MCP asked it (parent handoff, Restate reopen)>
   ```
   MCP sees only the text you send, so never compress reasoning into a label. Free-text answers that carry reasoning, constraints, or scope go as this multi-section payload, after the Refine gate:
   ```
   [from-user][refined]
   Decision: Stripe Billing.

   Reasoning:
   - Subscription is the core business model.

   Constraints (user-stated):
   - 30% Korean MAU, KRW required.

   Out of scope (user-stated):
   - Refund policy changes, tax-invoice issuance.

   Codebase context (main session verified):
   - src/payments/toss_adapter.py is one-shot KRW only.
   ```
   Single lines are fine for PATH 1a facts, accepted confirmations, and short PATH 2 answers with nothing attached (`[from-user] Yes`).

   Prefixes: `[from-code]`, `[from-user]`, `[from-research]` name the source; `[auto-confirmed]` marks PATH 1a. Append `[refined]` only after the Refine gate passed; MCP treats refined answers as high-confidence ground truth. When the refined answer explicitly closes the interview, add the `[closure]` marker (`[from-user][refined][closure]`); never encode closure by matching natural-language phrases.

4. **Refine gate** (free-text answers that carry reasoning, constraints, or scope). Structure the answer into the payload above, then ask through `ask_user`: "I structured your answer as follows before sending it to MCP: <payload>. Is anything missing or misrepresented?" Options: `Send as-is`, `Add to Constraints`, `Add to Out of scope`, `Add context`, `Rewrite`.
   - For any option other than `Send as-is`, never infer the missing text from the label: ask one follow-up for the exact text, apply it, and ask the Refine gate once more. If the second answer again asks for a change, ask a targeted PATH 2 follow-up and withhold the MCP answer until the user supplies the text or accepts the payload. Never forward a payload the user has called incomplete.
   - Skip it for PATH 1a facts, accepted confirmations, and short PATH 2 answers with no reasoning attached. Restate corrections (step 9) never skip it, however short.

5. **Dialectic Rhythm Guard.** Count consecutive questions answered without direct user judgment (PATH 1a auto-confirms and accepted PATH 1b / PATH 4 confirmations). After 3, the next question MUST go to PATH 2, even if code or research could answer it. Reset the count to 0 on any direct user answer (PATH 2, PATH 3, or a correction sent as `[from-user][refined]`).

6. **Ambiguity ledger.** Keep independent tracks (scope, constraints, outputs, verification) visible; do not let the interview collapse onto one subtopic.

7. **Repeat steps 2-6** until the user closes the interview (send `[from-user][refined][closure]`) or the response carries `meta.seed_ready=true`.

8. **Seed-ready Acceptance Guard.** When MCP signals seed-ready, do not relay completion. Before announcing it or suggesting `ooo seed`, run three lanes through `orchestrate_subagents` (one after another when the runtime has no parallel primitive), correlated by `context.lane_id`, each given the code, research, and brownfield context MCP did not see:
   - `closer`: apply the criteria in `src/ouroboros/agents/seed-closer.md`. Its verdict gates.
   - `contrarian`: hidden assumptions, overloaded terms, decisions the interview skipped.
   - `gap_hunter`: missing requirements, unlisted constraints, unhandled edge cases, unverifiable acceptance criteria.

   Each lane returns only JSON:
   - `closer`: `{"lane_id": "closer", "verdict": "seed_ready" | "not_ready", "reason": "...", "blocking_question": "..." | null}`
   - `contrarian` / `gap_hunter`: `{"lane_id": "<lane>", "severity": "high" | "medium" | "low", "finding": "...", "question": "..." | null}`; `high` only when the gap would materially change the implementation.

   Closure is blocked when the `closer` verdict is not `seed_ready` or any `contrarian` / `gap_hunter` severity is `high`. Then override the signal ("MCP says seed-ready, but I am not accepting it yet because <gap>."), explain the gap briefly, and ask the single highest-impact blocking question through PATH 2 or PATH 3.

9. **Restate gate** (after the Seed-ready Acceptance Guard passes). Restate the agreed goal as one sentence and ask through `ask_user`: "goal: <one-sentence restatement>. If someone else read only this line, would they arrive at the outcome you have in mind?" Options: `Yes, generate seed`, `Adjust wording`, `Missing scope`.
   - `Yes, generate seed`: suggest `ooo seed`.
   - `Adjust wording` / `Missing scope`: ask for the replacement wording or the missing condition or boundary. This is a real interview correction: run the Refine gate on it, with Decision set to the corrected goal line verbatim, Reasoning carrying the user's correction text unparaphrased, and any constraints, out-of-scope items, or verified codebase context it introduces. Send it as `[from-user][refined]` with `last_question` set to the exact Restate follow-up you asked; the handler rejects a reopen without `last_question`. Return to step 7 so MCP updates its state and step 8 runs again; never go to `ooo seed` from pre-correction state.
   - Then ask the Restate gate again with the corrected line. Do not loop more than twice; if alignment is still not reached, ask a targeted PATH 2 question instead of forcing a goal line.

#### Retry on failure

- `is_error=true` with `meta.recoverable=true`: tell the user you are retrying and call `ouroboros_interview` with the `session_id` to resume (state is persisted; at most 2 retries). If it still fails, say you are switching to direct interview mode and continue in Path B from where you left off.
- `meta.reason == "initial_context_too_large"` (`is_error=false`): the text is a directive to you, not an interview question; never route it to `ask_user`. Summarize the original `initial_context` (goal, constraints, success criteria) within `meta.max_chars` and resend it as `answer` with the `session_id`. The next response carries the real first question.

## Path B: Fallback (No MCP Server)

1. Read `src/ouroboros/agents/socratic-interviewer.md` and adopt that role.
2. Pre-scan config files (`pyproject.toml`, `package.json`, `go.mod`, ...) with `inspect_code`, and ask confirmation-style questions ("I see X. Should I assume Y?") instead of open discovery.
3. Ask each question through `ask_user` with suggested answers; use `inspect_code` and `web_research` as needed.
4. Keep the ambiguity ledger: track independent threads, revisit unresolved ones every few rounds, and do not let one subtopic crowd out the original request.
5. Apply the Refine gate to free-text answers, the Seed-ready Acceptance Guard before closure, and the Restate gate before suggesting `ooo seed`. With no MCP state, apply Restate corrections to your local ledger and goal line, rerun the guard locally, and ask the Restate gate again.
6. Results live only in the conversation (not persisted).

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
