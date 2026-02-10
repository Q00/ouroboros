# Onboarding Metrics & Success Tracking

Documenting the conversion funnel from first touch to active user.

---

## Conversion Funnel

Track users through each stage of onboarding:

```
Install → Welcome → Tutorial → Setup → First Project → Active User
   ↓         ↓         ↓        ↓         ↓            ↓
  100%     ????     ????     ????      ????         ???? (Target: 30%+)
```

---

## Stage 1: Installation (Entry Point)

**Definition:** User runs `claude /plugin install ouroboros@ouroboros`

**Success Metric:** Plugin installs without errors

**Tracking:**
- Plugin marketplace downloads
- Installation completion rate
- Installation errors by type

**Target:** 95%+ successful installation rate

**Drop-off Points:**
- Python version incompatibility (mitigation: clear error message with fix)
- Claude Code version issues (mitigation: version requirement in marketplace.json)
- Network issues (mitigation: retry instructions)

---

## Stage 2: Welcome Screen (First Touch)

**Definition:** User runs `ooo` or `/ouroboros:welcome`

**Success Metric:** User reads the welcome message and takes a next action

**Tracking:**
- Welcome skill invocation count
- Time spent in welcome (estimated by next command timing)
- Next action taken:
  - `ooo interview` (ideal - immediate engagement)
  - `ooo tutorial` (learning path)
  - `ooo help` (exploration)
  - No action (drop-off)

**Target:** 60%+ take a next action within 5 minutes

**Drop-off Points:**
- Welcome message too long (mitigation: keep under 60 seconds read time)
- Unclear next steps (mitigation: clear "Pick your path" CTAs)
- Overwhelming features (mitigation: progressive disclosure)

**Success Indicators:**
- User invokes another `ooo` command within 10 minutes
- User runs `ooo interview` with a real project idea

---

## Stage 3: Tutorial Engagement (Learning)

**Definition:** User runs `ooo tutorial`

**Success Metric:** User completes tutorial and starts first project

**Tracking:**
- Tutorial skill invocation count
- Tutorial completion rate (reaches Phase 6)
- Time to complete tutorial
- Next action after tutorial:
  - `ooo interview` with real idea (conversion)
  - `ooo setup` (power user path)
  - Drop-off (no further commands)

**Target:** 40%+ start first project after tutorial

**Tutorial Checkpoints:**
- [ ] Phase 0: User shares an idea
- [ ] Phase 2: User experiences "aha moment" of exposed assumptions
- [ ] Phase 3: User sees first mini-seed generated
- [ ] Phase 4: User requests more feature info
- [ ] Phase 6: User chooses next step

**Drop-off Points:**
- Tutorial too long (mitigation: mark with time estimates)
- Tutorial too generic (mitigation: use their actual idea)
- Tutorial doesn't lead to action (mitigation: clear CTAs at each phase)

---

## Stage 4: Setup Completion (Power User)

**Definition:** User runs `ooo setup` and completes configuration

**Success Metric:** User completes all 6 setup steps

**Tracking:**
- Setup skill invocation count
- Setup completion rate (reaches Step 5)
- Setup steps completed:
  - [ ] Step 1: Environment detected
  - [ ] Step 2: MCP server registered
  - [ ] Step 3: CLAUDE.md integrated
  - [ ] Step 4: Verification passed
  - [ ] Step 5: Success summary displayed
- Setup choices:
  - MCP enabled vs. skipped
  - CLAUDE.md integrated vs. skipped

**Target:** 70%+ complete setup (they chose to run it)

**Drop-off Points:**
- MCP registration fails (mitigation: clear error messages, alternative paths)
- Python version issues (mitigation: fallback to plugin-only mode)
- Setup too long (mitigation: mark as "~2 minutes", actual ~90 seconds)

**Celebration Checkpoints:**
Each step should have a moment of positive reinforcement:
- Step 1: "Great news! You're ready for Full Mode"
- Step 2: "MCP Server Registered! You can now run TUI execution"
- Step 3: "CLAUDE.md updated! Instant reference available"
- Step 5: Full success summary with next steps

---

## Stage 5: First Project (Activation)

**Definition:** User runs `ooo interview` with a real project idea

**Success Metric:** User generates their first seed specification

**Tracking:**
- First interview invocation (after welcome/tutorial)
- Interview completion rate (reaches ambiguity < 0.2)
- Time to complete interview
- Seed generation (`ooo seed`)
- Seed quality metrics:
  - Ambiguity score achieved
  - Number of acceptance criteria
  - Constraint coverage

**Target:** 80%+ generate first seed

**Drop-off Points:**
- Interview too long (mitigation: aim for 5-10 questions)
- Interview too abstract (mitigation: ground in their specific idea)
- User doesn't have a project (mitigation: offer example projects)

**Success Indicators:**
- User runs `ooo seed` after interview
- User expresses satisfaction with the specification
- User proceeds to execution (`ooo run`)

---

## Stage 6: Active User (Retention)

**Definition:** User completes first full workflow (interview → seed → run/evaluate)

**Success Metric:** User returns within 7 days for another project

**Tracking:**
- First workflow completion
- Second project started (within 7 days)
- Commands used over time:
  - `ooo interview` frequency
  - `ooo seed` frequency
  - `ooo run` frequency
  - `ooo evaluate` frequency
  - `ooo unstuck` frequency

**Target:** 40%+ return within 7 days

**Engagement Patterns:**
- **Power User:** Runs setup, uses TUI, multiple projects
- **Specification User:** Uses interview + seed, manual execution
- **Triage User:** Uses unstuck frequently
- **Casual User:** Occasional interview + seed

---

## Key Performance Indicators (KPIs)

### Primary KPIs
| Metric | Current | Target | Measurement |
|:-------|:--------|:-------|:------------|
| Installation Success Rate | ? | 95%+ | Plugin install success / total |
| Welcome → Next Action | ? | 60%+ | Second command within 10min / welcome invocations |
| Tutorial → First Project | ? | 40%+ | Interview after tutorial / tutorial completions |
| Setup Completion Rate | ? | 70%+ | Reaches Step 5 / setup invocations |
| First Project → Seed | ? | 80%+ | Seed generation / first interviews |
| 7-Day Retention | ? | 40%+ | Second project in 7 days / first workflow completions |

### Secondary KPIs
| Metric | Current | Target | Measurement |
|:-------|:--------|:-------|:------------|
| Average Interview Questions | ? | 5-10 | Questions per interview |
| Average Interview Duration | ? | 3-5 min | Time from interview start to seed |
| Average Seed Ambiguity | ? | <0.2 | Ambiguity score in generated seeds |
| Tutorial Completion Time | ? | <5 min | Time from tutorial start to Phase 6 |
| Setup Completion Time | ? | <2 min | Time from setup start to Step 5 |

---

## Star Conversion Tracking

GitHub stars are a key community growth metric. Track:

### Star Conversion Points
1. **After Welcome** - "Found this useful? Star us on GitHub!"
2. **After Setup** - Success summary includes star CTA
3. **After First Project** - "Built something amazing? Star us!"

**Tracking Method:**
- GitHub API: Monitor star growth
- Correlate star growth with feature releases
- Survey new stars: "Where did you discover Ouroboros?"

**Target:** 10% of engaged users (Stage 5+) convert to stars

---

## Drop-off Analysis

### Critical Drop-off Points

#### Point 1: After Installation (Welcome not invoked)
**Symptom:** User installs but never runs `ooo`
**Hypothesis:** Installation confirmation doesn't prompt usage
**Solution:** Post-install message: "Run `ooo` to get started!"

#### Point 2: During Welcome (No next action)
**Symptom:** User reads welcome but doesn't invoke another command
**Hypothesis:** Welcome message doesn't create urgency
**Solution:** Add time-bound motivation: "In the next 5 minutes..."

#### Point 3: During Tutorial (Abandoned mid-tutorial)
**Symptom:** User starts tutorial but doesn't complete
**Hypothesis:** Tutorial too generic or too long
**Solution:** Use user's actual idea, add progress indicators

#### Point 4: During Setup (Setup abandoned)
**Symptom:** User starts setup but doesn't complete
**Hypothesis:** Setup error or unclear progress
**Solution:** Better error messages, progress bar

#### Point 5: After First Interview (No seed generated)
**Symptom:** User completes interview but doesn't run `ooo seed`
**Hypothesis:** Interview doesn't lead naturally to seed
**Solution:** Auto-suggest seed generation at interview completion

---

## A/B Testing Ideas

### Welcome Message Variants
- **Variant A (Current):** Feature-focused table
- **Variant B:** Problem-solution narrative
- **Variant C:** Interactive "What do you want to build?" prompt

### Tutorial Format
- **Variant A (Current):** Text-based tutorial
- **Variant B:** Example project walkthrough
- **Variant C:** Interactive quiz-style

### Setup Flow
- **Variant A (Current):** 6-step wizard
- **Variant B:** One-click setup with defaults
- **Variant C:** Guided setup with video

---

## Measurement Implementation

### Client-Side Tracking (Future)
Add anonymous telemetry to track:
- Skill invocation counts
- Command sequences
- Error rates
- Feature usage

### Server-Side Tracking
- GitHub stars growth
- Clone/download counts
- Issue/discussion engagement

### Manual Tracking
- User surveys
- Discord engagement
- GitHub discussion analysis

---

## Success Celebration Points

Design moments of positive reinforcement throughout onboarding:

1. **Welcome Complete:** "You're ready to transform vague ideas into precise specs!"
2. **Tutorial Aha Moment:** "Notice how each question exposed an assumption?"
3. **Setup Complete:** Visual success summary with checklist
4. **First Seed:** "Your specification is ready for AI execution. First try."
5. **First Execution:** "You've just completed your first Ouroboros workflow!"

Each celebration should:
- Be visually distinct (emoji, formatting)
- Reinforce the value proposition
- Lead naturally to the next step
- Create positive emotional association

---

## Onboarding Optimization Checklist

### Friction Reduction
- [ ] Minimize setup steps (target: <3 steps to value)
- [ ] Clear error messages with specific fixes
- [ ] Progress indicators for multi-step processes
- [ ] Skip/advanced options for power users

### Motivation Enhancement
- [ ] Immediate value in first 60 seconds
- [ ] Clear "why" for each step
- [ ] Social proof (user counts, success stories)
- [ ] Urgency without pressure

### Learning Support
- [ ] Progressive disclosure (don't overwhelm)
- [ ] Learn-by-doing (not just reading)
- [ ] Quick wins (early success moments)
- [ ] Clear next steps after each stage

### Community Building
- [ ] Star CTAs at key moments
- [ ] Discord/community links
- [ ] Contribution opportunities
- [ ] Share success stories

---

## Dashboard Goals

Future onboarding dashboard should show:

```
Ouroboros Onboarding Metrics
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This Week                     This Month                    All Time
Installs:     127             Installs:        523          Installs:        2,847
Welcome:       89 (70%)       Welcome:         367 (70%)   Welcome:        1,988 (70%)
Tutorial:      45 (36%)       Tutorial:        189 (36%)   Tutorial:        1,024 (36%)
Setup:         31 (24%)       Setup:           128 (24%)   Setup:            717 (25%)
First Seed:    23 (18%)       First Seed:       97 (19%)   First Seed:       542 (19%)
Active Users:  9  (7%)        Active Users:     41 (8%)    Active Users:     227 (8%)

Funnel Health: [████████░░] 70% to welcome, 7% to active
Target: 60% to welcome, 40% to active

Top Drop-off: Setup (31% loss, consider simplification)
```

---

## Continuous Improvement

### Weekly Review
- Review conversion funnel metrics
- Identify new drop-off points
- A/B test one optimization

### Monthly Review
- Analyze retention patterns
- Survey churned users
- Update onboarding flow

### Quarterly Review
- Major onboarding redesign based on data
- Update success targets
- Review competitive landscape
