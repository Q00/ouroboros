---
name: threat-model
description: >-
  Build a threat model for a target codebase. Three modes: "interview" walks an
  application owner through the four-question framework and produces a threat
  model from their answers; "bootstrap" derives a threat model from the code
  plus past vulnerabilities (CVEs, git history, pentest reports) when no owner
  is available; "bootstrap-then-interview" chains the two when both owner and
  codebase are present. All write THREAT_MODEL.md in a shared schema. Use
  when asked to "threat
  model", "build a threat model", "map the attack surface", or "what should we
  be worried about in this codebase".
argument-hint: "[bootstrap-then-interview|bootstrap|interview] <target-dir> [--vulns <file>] [--design-doc <file>] [--seed <THREAT_MODEL.md>] [--fresh]"
allowed-tools:
  - Read
  - Glob
  - Bash(python3 .claude/skills/_lib/checkpoint.py:*)
  - Grep
  - Write
  - Bash(git:*)
  - Bash(gh api:*)
  - Bash(find:*)
  - Bash(ls:*)
  - Bash(cat:*)
  - AskUserQuestion
  - Task
---

# threat-model

A threat model answers **"what could go wrong with this system, who would do
it, and what should we do about it?"** independently of whether any specific
bug has been found yet. It is the map; vulnerability discovery is the metal
detector. A good threat model tells the pipeline where to look and tells triage
which findings matter.

**Litmus test:** If patching one line of code makes an entry disappear, it was
a vulnerability, not a threat. A threat ("attacker achieves RCE via untrusted
media parsing") still stands after every known bug is fixed; a vulnerability
("`dr_wav.h:412` doesn't bounds-check `chunk_size`") does not. This skill
produces threats. Vulnerabilities appear only as **evidence** that raises a
threat's likelihood score.

**Invocation:** `/threat-model [bootstrap-then-interview|bootstrap|interview] <target-dir> [flags]`

---

## Step 0 — Safety preamble (always runs first)

This skill performs **static analysis only**. It reads source, git history,
and any vulnerability reports the user supplies, and writes a single output
file (`<target-dir>/THREAT_MODEL.md`). It does not build, execute, fuzz, or
modify the target, and does not make network requests against the target's
infrastructure.

Before proceeding, confirm and state in your first response:

1. The target directory exists and is a local checkout you can read.
2. You will not execute any code from the target directory.
3. If `--vulns` points at a URL or you are asked to "fetch CVEs", you will
   query only public advisory databases (NVD, GitHub Security Advisories, the
   project's own issue tracker) and never the target's live deployment.

If the user asks you to validate a threat by running an exploit, decline.
This repository vendors only the static skills; there is no execution
pipeline here (Ouroboros adaptation).

---

## Step 1 — Route to a mode

Parse `$ARGUMENTS`:

| First token | Route to |
|---|---|
| `interview` | Read `interview.md` in this directory and follow it. |
| `bootstrap` | Read `bootstrap.md` in this directory and follow it. |
| `bootstrap-then-interview` | Bootstrap first, then interview seeded from the draft. See below. |
| anything else, or empty | Ask the user: **"Is someone who owns or built this system available to answer questions in this session?"** Yes and the codebase is checked out → recommend `bootstrap-then-interview`. Yes but no codebase → `interview.md`. No → `bootstrap.md`. |

All modes write the same artifact (`THREAT_MODEL.md`, schema in `schema.md`)
so downstream consumers (pipeline `recon`/`judge`, verifier agents) do not need
to know which mode produced it.

| | `interview` | `bootstrap` |
|---|---|---|
| **Needs** | An application owner present in the session | A local checkout; optionally past vulns |
| **Method** | Four-question framework: conversational walk through *what are we working on → what can go wrong → what are we going to do about it → did we do a good job* | Five stages: parallel research swarm → synthesize sections 1-3 + vuln table → generalize vulns into threat classes → STRIDE gap-fill → emit |
| **Best for** | New systems, design reviews, systems where the risk lives in business logic the code doesn't show | Inherited systems, third-party code, OSS dependencies, anything with a CVE history |
| **Provenance tag** | `interview` | `bootstrap` |

**Context durability.** Interview mode is multi-turn; tool results from early
reads may be evicted before you need them. To stay resilient:

- Do **not** read `interview.md` or `bootstrap.md` in full up front. Read the
  mode file (or the relevant section of it) **at the point you need it**, one
  question or stage at a time.
- If a re-read via the Read tool is refused as "file unchanged", the prior
  result was evicted; reload with `cat <path>` via Bash instead.

**Interview backbone** (so you can proceed even if `interview.md` is
unavailable mid-session):

| Q | Question | Fills schema sections |
|---|---|---|
| Q1 | What are we working on? | section 1 context, section 2 assets, section 3 entry points |
| Q2 | What can go wrong? | section 4 threat rows (id, threat, actor, surface, asset) |
| Q3 | What are we going to do about it? | section 4 impact/likelihood/status/controls; section 5 deprioritized; section 8 recommended mitigations |
| Q4 | Did we do a good job? | validate ranking, coverage check, section 6 open questions |

### `bootstrap-then-interview` mode

When the owner is available *and* the codebase is checked out, this is the
recommended path: the owner's time goes to refining a code-grounded draft
instead of describing the system from scratch.

1. Tell the owner: "I'll read the code first and come back with a draft
   (about 5-10 min), then we'll walk it together. Want that, or would you
   rather start cold?" Only proceed if they opt in; otherwise fall back to
   `interview.md`.
2. Read `bootstrap.md` and follow it end-to-end. Write
   `<target-dir>/THREAT_MODEL.md`.
3. Immediately continue into interview mode: read `interview.md` and follow
   it with `--seed <target-dir>/THREAT_MODEL.md` in effect. The section 6 open
   questions from bootstrap become your Q1-Q4 prompts; the owner confirms,
   corrects, and adds rather than starting from nothing.
4. Overwrite `<target-dir>/THREAT_MODEL.md` with the refined model. Set
   provenance `mode: bootstrap-then-interview`.

The same flow is available manually: run `bootstrap` first, then
`interview --seed <THREAT_MODEL.md>` in a later session.

---

## Step 2 — Shared output contract

All modes MUST emit `<target-dir>/THREAT_MODEL.md` conforming to `schema.md`
in this directory. **Read `schema.md` immediately before you write the file**,
not at routing time; in interview mode the gap between routing and emit can be
many turns, and an early read will be evicted before it's used.

After writing the file, print to the user:

1. The path to `THREAT_MODEL.md`.
2. The top 5 threats by likelihood × impact (id, one-line description, L×I).
3. For `bootstrap`: any open questions the code could not answer (these seed a
   later `interview` pass).
4. For `interview`: any owner statements that could not be verified in code
   (these seed follow-up code review).

---

## References

- [docs/security/README.md](../../../docs/security/README.md) for how this
  skill fits the Ouroboros security loop (Ouroboros adaptation; the upstream
  `docs/security.md` / `docs/prompting.md` live in the reference harness).
