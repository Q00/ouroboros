---
name: tutorial
description: "Interactive tutorial teaching Ouroboros hands-on"
triggers: "ooo tutorial"
---

# /ouroboros:tutorial

Interactive tutorial that teaches Ouroboros through hands-on experience.

## Usage

```
ooo tutorial
/ouroboros:tutorial
```

## Tutorial Flow

When this skill is invoked, guide the user through a learn-by-doing tutorial with progressive disclosure.

---

## Phase 1: The Hook (30 seconds)

Start with enthusiasm and immediate value:

```
Welcome to the Ouroboros Tutorial!

In the next 5 minutes, you'll:
- Transform a vague idea into a precise specification
- See Ouroboros expose hidden assumptions you didn't know you had
- Generate a validated Seed ready for AI execution

Let's start with YOUR idea. What's something you've been wanting to build?

[Wait for user response - any project idea works]
```

---

## Phase 2: The Interview Experience (2 minutes)

Once they share an idea, immediately start a simplified interview:

```
Great choice! Let me show you how Ouroboros thinks about this.

[Ask 3-4 targeted Socratic questions about their idea]
- "What's the primary problem this solves?"
- "Who are the main users?"
- "What's the one feature that would make this useful immediately?"

Notice something? Each question exposed an assumption you hadn't articulated.
This is the "aha moment" - requirements clarity BEFORE writing code.
```

---

## Phase 3: The Quick Win (1 minute)

Generate a mini-seed to show immediate value:

```
Based on your answers, here's what we've uncovered:

[Display a simplified seed structure]

Goal: [their goal refined]
Constraints: [3 key constraints they mentioned]
Success Criteria: [2-3 clear metrics]

Ambiguity Score: 0.XX [show progress from vague to clear]

This seed is now ready for AI execution. No more "build me X" and hoping for the best!
```

---

## Phase 4: Feature Discovery (1 minute)

Progressive disclosure of features:

```

Now that you've seen the core magic, here's what else Ouroboros can do:

Immediate Wins (Available Now):
- ooo unstuck    - 5 lateral thinking personas when you're blocked
- ooo seed       - Generate full specifications from interviews
- ooo evolve     - Evolutionary loop until ontology converges

Advanced Features (Setup Required):
- ooo run        - Execute with visual TUI dashboard
- ooo evaluate   - 3-stage quality verification
- ooo status     - Track project drift in real-time

Want to try any of these now? Or shall I show you the full workflow?
```

---

## Phase 5: The Real Workflow (30 seconds)

Show the complete pipeline:

```

The Complete Ouroboros Workflow:

Idea → Interview → Seed → Route → Execute → Evaluate
  ↓        ↓         ↓       ↓        ↓         ↓
Vague    Clear    Frozen   Right    Build    Verified
      Assumptions   Spec    Model    Visibly     Quality

5 commands from idea to validated result:
1. ooo interview "your idea"        # Expose assumptions
2. ooo seed                        # Crystallize spec
3. ooo run                         # Execute with TUI
4. ooo evaluate                    # 3-stage verification
5. ooo status                      # Check drift

Ready to try your first real project?
```

---

## Phase 6: Call to Action

```

Your First Real Project awaits!

Pick a path:

Path A: Start Fresh
  ooo interview "your actual project idea"

Path B: Learn More
  ooo help    # Full command reference

Path C: Setup Full Features
  ooo setup   # Enable TUI, evaluation, drift tracking

Which path interests you?
```

---

## Tutorial Principles

1. **Immediate Value** - Show something useful in the first 30 seconds
2. **Learn by Doing** - Don't just explain, have them experience it
3. **Progressive Disclosure** - Reveal features gradually, not all at once
4. **Quick Wins** - Celebrate small milestones throughout
5. **Clear Next Steps** - Always give them a specific action to take

---

## Tutorial Checkpoints

Track progress through these checkpoints:

- [ ] User shares an idea
- [ ] User experiences the "aha moment" of exposed assumptions
- [ ] User sees their first mini-seed generated
- [ ] User understands the value proposition
- [ ] User chooses a next step (path A, B, or C)

---

## Common Tutorial Responses

### "I don't have a project idea"
```
No problem! Let's use a classic example: a task management CLI.

[Continue tutorial with task management CLI example]
```

### "This seems complicated"
```
It's actually simpler than it sounds. Let me show you the 3 commands you'll use 90% of the time:

1. ooo interview "idea"  # Clarify what you want
2. ooo seed             # Generate the spec
3. ooo run              # Build it

Everything else is optional power-user features.
```

### "How is this different from just prompting Claude?"
```
Great question! The difference is:

Without Ouroboros:
  "Build me a task CLI" → Claude guesses → You realize it's wrong → Rewrite prompt → Repeat

With Ouroboros:
  Interview → Hidden assumptions exposed → Precise spec → Claude builds exactly what you want → First try

The interview saves you hours of iteration.
```

---

## Success Metrics

A successful tutorial ends when:
- User has experienced the interview process
- User understands the seed specification concept
- User knows their next step
- User feels confident to try their real project

## Tutorial Variations

### For Technical Users
- Emphasize the TUI dashboard and evolutionary loop
- Show cost optimization features (PAL Router)
- Highlight evaluation pipeline

### For Non-Technical Users
- Focus on the interview and clarification process
- Emphasize the "translate ideas to specs" value
- Minimize technical jargon

### For OMC Users
- Highlight key differences (TUI, specs, cost optimization)
- Show migration path from OMC commands
- Compare feature sets side-by-side
