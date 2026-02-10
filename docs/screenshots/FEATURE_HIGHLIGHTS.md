# Ouroboros Feature Highlights Guide

This guide outlines the key features to highlight in screenshots and demo videos to drive GitHub stars.

---

## Core Features to Highlight

### 1. TUI Dashboard - The Visual Advantage

**Why This Matters**: Most AI workflow tools are CLI-only black boxes. Users can't see what's happening.

**Screenshot Elements**:
- Workflow tree showing active execution
- Phase progress indicators (Double Diamond: Discover → Define → Design → Deliver)
- Real-time cost tracking display
- Drift detection metrics
- Parallel task execution visualization

**Caption Template**:
```
"See your AI workflows execute in real-time with our rich TUI dashboard.
Stop flying blind with CLI-only tools."
```

**CTA**:
```
"Want visual workflow tracking? Star us on GitHub!"
```

---

### 2. Cost Savings - The 85% Advantage

**Why This Matters**: Cost is the #1 concern for AI development. 85% is a specific, verifiable claim.

**Screenshot Elements**:
- PAL Router tier selection display
- Cost breakdown panel (Frugal vs Standard vs Frontier)
- Before/after cost comparison
- Cost prediction interface

**Caption Template**:
```
"Why pay $300 when you can pay $45? PAL Router achieves 85% cost reduction
through automatic model tier selection based on task complexity."
```

**CTA**:
```
"Save money on AI development. Star us on GitHub!"
```

**Math to Include**:
```
Base Cost (Claude Opus for everything): $300/month
With PAL Router: $45/month
Savings: 85%

Algorithm:
complexity = 0.3 * norm_tokens + 0.3 * norm_tools + 0.4 * norm_depth
- < 0.4: Frugal tier (Haiku) - $0.25/1M tokens
- 0.4-0.7: Standard tier (Sonnet) - $3/1M tokens
- >= 0.7: Frontier tier (Opus) - $15/1M tokens
```

---

### 3. Socratic Interview - The Quality Advantage

**Why This Matters**: Solves the #1 problem with AI tools: "build me X" results in wrong output.

**Screenshot Elements**:
- Question display with Socratic prompts
- Progress indicator showing ambiguity reduction
- Answer input field with context
- Ambiguity score trending down

**Caption Template**:
```
"Stop debugging requirements. Our Socratic interview exposes hidden
assumptions BEFORE writing code. Ambiguity score drops from 0.8 to 0.15."
```

**CTA**:
```
"Build the right thing the first time. Star us on GitHub!"
```

**Sample Questions to Show**:
```
Q: "Should completed tasks be deletable or archived?"
Q: "What happens when two tasks have the same priority?"
Q: "Is this for teams or solo use?"
→ 12 hidden assumptions exposed
→ Seed generated. Ambiguity: 0.15
```

---

### 4. Event Sourcing - The Debugging Advantage

**Why This Matters**: Full replay capability is unique. No other tool offers this.

**Screenshot Elements**:
- Event timeline display
- Replay controls
- State inspection panel
- Audit trail viewer

**Caption Template**:
```
"Debug any session with full event replay. Every decision, state change,
and agent action is recorded. Time-travel debugging for AI workflows."
```

**CTA**:
```
"Debuggable AI workflows. Star us on GitHub!"
```

---

### 5. 3-Stage Evaluation - The QA Advantage

**Why This Matters**: Quality assurance is missing from most AI tools.

**Screenshot Elements**:
- Mechanical test results (pass/fail)
- Semantic analysis scores
- Consensus evaluation output
- Overall quality grade

**Caption Template**:
```
"Three-stage evaluation ensures quality:
1. Mechanical (automated tests)
2. Semantic (requirements matching)
3. Consensus (multi-agent validation)

Catch issues before deployment."
```

**CTA**:
```
"Quality assurance for AI development. Star us on GitHub!"
```

---

### 6. Execution Modes - The Flexibility Advantage

**Why This Matters**: Different tasks need different approaches.

**Screenshot Elements**:
- Mode selection interface
- Mode comparison table
- Performance metrics per mode
- Mode descriptions

**Caption Template**:
```
"7 execution modes for any workflow:
- Autopilot: Full autonomous execution
- Ultrawork: Maximum parallelism
- Ralph: 'The boulder never stops' persistence
- Ecomode: 85% cost reduction
- And 3 more...

Choose the right mode for the job."
```

**CTA**:
```
"Flexible AI workflows. Star us on GitHub!"
```

---

## Screenshot Order for README Gallery

Optimize for visual flow and value proposition:

1. **Dashboard.png** (Hero image)
   - Shows: Full TUI with active execution
   - Caption: "The Visual AI Workflow Engine"
   - CTA: "See what's happening"

2. **Interview.png**
   - Shows: Socratic questioning in progress
   - Caption: "Stop debugging requirements"
   - CTA: "Build the right thing"

3. **Execution.png**
   - Shows: Parallel task execution tree
   - Caption: "Watch tasks execute in real-time"
   - CTA: "Visual workflow tracking"

4. **Evaluation.png**
   - Shows: 3-stage evaluation results
   - Caption: "Quality assurance built-in"
   - CTA: "Catch issues early"

---

## Video Script: 30-Second "Wow" Demo

**Goal**: Convert viewers to stars in 30 seconds

| Time | Scene | Voiceover | Screen |
|------|-------|-----------|--------|
| 0-5s | Terminal with plugin installed | "Ouroboros is a visual AI workflow engine." | Type `ooo interview` |
| 5-15s | Interview mode running | "It asks Socratic questions to expose hidden assumptions." | Questions appearing, answering |
| 15-25s | Dashboard with execution | "Then watch your workflow execute in real-time with our rich TUI dashboard." | Dashboard showing progress |
| 25-30s | Final results + CTA | "85% cost savings. Visual feedback. Quality assurance. Star us on GitHub to try it yourself." | Star button on screen |

**Key Moments**:
- 5s: First question appears (hook)
- 15s: Dashboard visual (wow factor)
- 25s: Cost savings mentioned (value)
- 30s: CTA (conversion)

---

## Video Script: 2-Minute Feature Deep Dive

**Goal**: Convince interested users to star

| Time | Section | Content |
|------|---------|---------|
| 0:00-0:10 | Intro | "Ouroboros is the visual alternative to CLI-only AI workflow tools" |
| 0:10-0:30 | Problem | "Most tools are black boxes. Type prompt, wait, hope. No visibility, no cost control, no quality assurance" |
| 0:30-1:00 | Solution: TUI | "Our rich TUI dashboard shows execution in real-time. See phases, tasks, costs, drift" |
| 1:00-1:30 | Solution: Cost | "PAL Router achieves 85% cost reduction. Simple tasks get Haiku, complex get Opus" |
| 1:30-2:00 | Solution: Quality | "Socratic interview prevents wrong output. 3-stage evaluation catches issues" |
| 2:00-2:30 | CTA | "Free, open source, works as Claude Code plugin. Star us on GitHub" |

---

## Comparison Graphic: TUI vs CLI

Create a side-by-side comparison image:

```
┌─────────────────────────────────────────────────────────────┐
│                     OuroBOROS TUI                           │
├─────────────────────────────────────────────────────────────┤
│  Phase: Define    │    AC Tree (Expandable)                │
│  Progress: ████████░░ 80%    ├─ Task A (completed)          │
│  Cost: $12.34     │    ├─ Task B (in progress)         │
│  Drift: 0.15 OK   │    │  ├─ Subtask B.1 (running)    │
│                   │    │  └─ Subtask B.2 (pending)    │
│  [Events] [Replay]    └─ Task C (pending)               │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│              CLI-ONLY TOOLS (Generic)                       │
├─────────────────────────────────────────────────────────────┤
│ $ run workflow                                               │
│ Executing task A...                                         │
│ Executing task B...                                         │
│ Executing task C...                                         │
│ Done.                                                       │
│                                                             │
│ $                                                            │
└─────────────────────────────────────────────────────────────┘
```

**Caption**:
```
"Stop flying blind. See your AI workflows execute in real-time."
```

---

## Social Media Assets

### Twitter/X Cards

1. **Cost Savings Card**:
   - Image: Cost comparison chart
   - Text: "85% cost reduction on AI workflows"
   - CTA: "Learn how: github.com/Q00/ouroboros"

2. **TUI Demo Card**:
   - Image: Dashboard screenshot
   - Text: "Visual AI workflow engine"
   - CTA: "Star on GitHub"

3. **Before/After Card**:
   - Image: CLI vs TUI comparison
   - Text: "Stop prompting. Start specifying."
   - CTA: "See the difference"

### LinkedIn Post Images

1. **Feature Overview** (1080x1080):
   - 4 quadrants: TUI, Cost, Quality, Flexibility
   - Clean, professional design
   - Star button prominent

2. **Comparison Table** (1920x1080):
   - Ouroboros vs Competitors
   - Green checkmarks for Ouroboros features
   - Red X for missing competitor features

---

## Screenshot Production Checklist

For each screenshot:

- [ ] Terminal configured (dark theme, SF Mono 14pt)
- [ ] Window sized appropriately (1280x720 or larger)
- [ ] TUI running with meaningful data (not empty state)
- [ ] Colors are high contrast and readable
- [ ] No sensitive information visible
- [ ] File saved as PNG (lossless)
- [ ] File size optimized (under 500KB)
- [ ] Added caption explaining the feature
- [ ] Added CTA to star on GitHub
- [ ] Tested in README preview

---

## Video Production Checklist

For each video:

- [ ] Script finalized
- [ ] Screen resolution set to minimum 1280x720
- [ ] Audio quality tested (if narrating)
- [ ] Practice run completed
- [ ] Recording at 30fps minimum
- [ ] Exported as MP4 (H.264)
- [ ] File size under 50MB (for web)
- [ ] Thumbnail created
- [ ] Closed captions added (if narrated)
- [ ] Tested on target platform

---

## Distribution Checklist

After creating assets:

- [ ] Update README with new screenshots
- [ ] Add video to GitHub Releases
- [ ] Post to HackerNews with compelling title
- [ ] Share on Reddit (r/programming, r/devtools)
- [ ] Post on LinkedIn with video demo
- [ ] Tweet thread with screenshots
- [ ] Add to documentation
- [ ] Submit to AI tools directories
- [ ] Update plugin marketplace listing

---

## Measuring Success

Track these metrics:

- **GitHub stars growth rate** (stars/week)
- **README views** (via GitHub insights)
- **Demo video watch time** (via YouTube analytics)
- **Plugin install count** (via marketplace analytics)
- **Referral traffic** (via GitHub referral sources)

**Target**: 100 stars in first 30 days, 500 stars in 90 days.

**Conversion Funnel**:
1. Visitor lands on GitHub repo
2. Sees compelling hero image
3. Reads "Why Ouroboros?" section
4. Views screenshots/videos
5. Clicks "Star Us" button
6. Stars the repo

**Optimization**:
- A/B test different hero images
- Test CTA button text
- Test screenshot order
- Track which assets get most engagement
