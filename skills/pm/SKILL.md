---
name: prd
description: "Generate a PRD through guided PM-focused interview with automatic question classification"
---

# /ouroboros:prd

Generate a Product Requirements Document through a PM-focused Socratic interview.

## Usage

```
ooo prd [topic]
/ouroboros:prd [topic]
```

**Trigger keywords:** "prd", "product requirements", "write prd"

## Instructions

When the user invokes this skill:

### Step 1: Brownfield Detection

1. Check if the current directory contains code using `detect_brownfield(cwd)`:
   - Use Glob to check for config files (`pyproject.toml`, `package.json`, `go.mod`, etc.)
   - If found, ask via `AskUserQuestion`:
     ```json
     {
       "questions": [{
         "question": "Existing codebase detected. Should I scan it for context?",
         "header": "Context",
         "options": [
           {"label": "Yes, scan it", "description": "Scan the codebase to understand existing patterns and tech stack"},
           {"label": "No, start fresh", "description": "Treat this as a greenfield project (no existing code context)"}
         ],
         "multiSelect": false
       }]
     }
     ```
   - If no code found: skip to Step 2

2. If user says "Yes, scan it":
   - Check `~/.ouroboros/brownfield.json` for registered repos
   - If repos exist: present multi-select via `AskUserQuestion` to choose which repos to reference
   - If empty or file doesn't exist: ask for repo path(s), scan with Glob/Read to determine name/desc, save to `~/.ouroboros/brownfield.json`
   - Use Read, Glob, Grep to scan selected repos (config files, key types, directory structure) and build brownfield context summary

### Step 2: Initial Question

Ask the user via `AskUserQuestion`:
```json
{
  "questions": [{
    "question": "What do you want to build? Describe the product or feature in a few sentences.",
    "header": "Product",
    "options": [
      {"label": "New feature for existing product", "description": "Add functionality to an existing system"},
      {"label": "New product from scratch", "description": "Build something entirely new"}
    ],
    "multiSelect": false
  }]
}
```

The user's response (selected option + any custom text) becomes the `initial_context`.

### Step 3: Start PRD Interview Loop

Now begin the Socratic interview loop. Read `agents/socratic-interviewer.md` and adopt that role, BUT with these PRD-specific modifications:

#### Question Classification

For EACH question the interviewer would ask, BEFORE presenting it to the user, classify it:

**(a) PM Pass-through**: Questions about business goals, user needs, success metrics, scope, priorities, user stories, workflows, constraints (budget, timeline, regulations).
- Present these directly to the user via `AskUserQuestion`

**(b) Reframe**: Questions that PMs should answer but are phrased too technically.
- Examples: "optimistic vs pessimistic locking?" -> "When multiple people edit the same thing simultaneously, should the first save win, or should we warn the later person?"
- Present the REFRAMED question to the user
- Internally track the original technical question + PM's answer for the interview context

**(c) Decide-Later (DEV)**: Pure development/implementation questions that PMs cannot answer.
- Examples: "Which database?", "Caching strategy?", "API pagination approach?"
- Do NOT ask the user. Instead:
  1. Display a brief notification: `[DEV -> decide-later] "original question text"`
  2. Add the question to the `decide_later` list
  3. Continue to the next question automatically

#### Scoring

After each round, mentally assess the PM-level ambiguity:
- **Only score what PMs should know**: business goals, user needs, success criteria, scope
- **Do NOT penalize** decide-later items — they are intentional deferrals
- When PM-level ambiguity is low (all business/product questions are clear), the interview can conclude

#### Interview Flow

```
while pm_ambiguity > threshold:
    question = generate_next_question(history)
    classification = classify(question, history, brownfield_context)

    if classification == "pass_through":
        answer = ask_user(question)
    elif classification == "reframe":
        reframed = reframe_for_pm(question)
        answer = ask_user(reframed)
        # Track: original_question + pm_answer
    elif classification == "decide_later":
        display("[DEV -> decide-later] {question}")
        decide_later_list.append(question)
        continue  # Skip to next question

    record_answer(question, answer)
```

### Step 4: Interview Completion

When PM-level ambiguity is resolved:

1. **Display decide-later summary**:
   ```
   ## Deferred to Development Phase

   The following decisions will be made during the development interview:
   1. <question 1>
   2. <question 2>
   ...
   ```

2. **Generate PRD document** (prd.md):
   - Use the full Q&A history + brownfield context to generate a natural language PRD
   - Sections: Goal, Target Users, User Stories, Success Criteria, Constraints, Deferred Decisions
   - Save to `.ouroboros/prd.md`

3. **Generate PRD Seed** (YAML):
   - Extract structured requirements into a PRDSeed
   - Include `deferred_decisions` (original question texts)
   - Include `referenced_repos` (selected brownfield repos)
   - Save to `~/.ouroboros/seeds/prd_seed_{id}.yaml`

4. **Present next step**:
   ```
   Your PRD has been generated!

   Artifacts:
   - PRD Document: .ouroboros/prd.md
   - PRD Seed: ~/.ouroboros/seeds/prd_seed_{id}.yaml

   Deferred decisions: {N} items (to be resolved in development interview)

   Next: `ooo interview` to start the development interview based on this PRD
   ```

## Interviewer Behavior (PRD Mode)

The interviewer in PRD mode:
- Focuses on BUSINESS and PRODUCT questions, not technical ones
- Targets PM-level ambiguity (goals, users, success criteria, scope)
- Automatically defers technical questions without bothering the PM
- Reframes necessary technical questions into PM-friendly language
- Always ends responses with a question
- NEVER writes code, edits files, or runs commands

## Example Session

```
User: ooo prd

[Brownfield detected] Existing codebase found.
> Yes, scan it

[Scanning repos...]
Brownfield context loaded: Python/FastAPI backend

What do you want to build?
> A notification system for our mobile app

Q1: Who are the primary users of this notification system?
> End users of our fitness tracking app

Q2: What events should trigger notifications?
> Workout reminders, achievement unlocks, friend activity

[DEV -> decide-later] "Should notifications use push (APNs/FCM), in-app, or email delivery?"
[DEV -> decide-later] "What message queue system for async notification processing?"

Q3: How time-sensitive are these notifications?
> Workout reminders must be on time, others can be delayed up to an hour

Q4 (reframed): When a user has many unread notifications, should we group them or show each one separately?
> Group similar ones, like "3 friends completed workouts"

...

## Deferred to Development Phase
1. "Should notifications use push (APNs/FCM), in-app, or email delivery?"
2. "What message queue system for async notification processing?"
3. "Database schema for notification storage and read/unread state?"

Your PRD has been generated!
Next: `ooo interview` to start the development interview based on this PRD
```

## Next Steps

After PRD completion, `ooo interview` will auto-detect the PRD seed and offer to use it as initial context for a development-focused interview.
