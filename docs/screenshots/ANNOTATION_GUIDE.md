# Screenshot Annotation Guide for GitHub Stars

This guide provides templates and instructions for annotating screenshots to drive GitHub star conversions.

---

## Annotation Principles

### Goal
Each screenshot should:
1. Show a clear feature/benefit
2. Include a call-to-action to star the repo
3. Be visually appealing and professional
4. Work well in both light and dark themes

### Design System

**Colors** (high contrast for visibility):
- Accent: `#3b82f6` (blue for CTAs)
- Success: `#10b981` (green for completed items)
- Warning: `#f59e0b` (amber for in-progress)
- Error: `#ef4444` (red for issues)
- Background: Dark with `#1e1e2e` base

**Typography**:
- Title: 24px, bold
- Subtitle: 18px, medium
- Body: 14px, regular
- Captions: 12px, italic

**Layout**:
- Screenshot centered
- Annotation overlay (optional)
- Caption below image
- CTA button at bottom

---

## Screenshot 1: Hero Dashboard

### Purpose
Show the main TUI dashboard to demonstrate visual workflow tracking.

### Base Image
`dashboard.png` - Full TUI dashboard showing active execution

### Annotation Template

```
┌─────────────────────────────────────────────────────────────┐
│                    Ouroboros TUI Dashboard                  │
│                   See What's Happening                      │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│                    [SCREENSHOT HERE]                         │
│                                                              │
│  Annotations:                                                │
│  ├── Phase progress bar shows active execution phase        │
│  ├── Task tree displays parallel execution                   │
│  ├── Cost tracker shows real-time spend                     │
│  └── Drift metric indicates if on track                     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Caption Template
```markdown
**The Visual AI Workflow Engine**

See your workflows execute in real-time with our rich TUI dashboard:
- Active phase progress (Discover → Define → Design → Deliver)
- Parallel task tree visualization
- Real-time cost tracking
- Drift detection alerts

Stop flying blind with CLI-only tools.

[Star us on GitHub](https://github.com/Q00/ouroboros)
```

### Key Elements to Highlight
1. Phase progress bar (top section)
2. Task tree with expand/collapse
3. Cost display (bottom right)
4. Drift indicator (bottom left)
5. Event log (scrollable area)

---

## Screenshot 2: Socratic Interview

### Purpose
Show the requirements gathering process that prevents wrong output.

### Base Image
`interview.png` - Interview mode with question and answer

### Annotation Template

```
┌─────────────────────────────────────────────────────────────┐
│                   Socratic Interview                        │
│              Stop Debugging Requirements                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│                    [SCREENSHOT HERE]                         │
│                                                              │
│  Example Questions:                                          │
│  ├── "Should tasks be deletable or archived?"               │
│  ├── "What happens with duplicate priorities?"              │
│  └── "Is this for teams or solo use?"                       │
│                                                              │
│  Result: 12 hidden assumptions exposed                       │
│          Ambiguity score: 0.8 → 0.15                         │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Caption Template
```markdown
**Build the Right Thing the First Time**

Before writing code, Ouroboros asks targeted questions:
- "Should completed tasks be deletable or archived?"
- "What happens when two tasks have the same priority?"
- "Is this for teams or solo use?"

12 questions later:
- Hidden assumptions exposed: 12
- Ambiguity score: 0.8 → 0.15
- Specification generated (the "Seed")

No more "build me X" disasters.

[Star us on GitHub](https://github.com/Q00/ouroboros)
```

### Key Elements to Highlight
1. Current question display (main area)
2. Answer input field (bottom)
3. Progress indicator (questions answered)
4. Ambiguity score trend (decreasing)
5. Context panel (right side)

---

## Screenshot 3: Execution View

### Purpose
Show parallel task execution and progress tracking.

### Base Image
`execution.png` or `seed.png` - Workflow execution with task tree

### Annotation Template

```
┌─────────────────────────────────────────────────────────────┐
│                   Parallel Execution                         │
│              Watch Tasks Decompose & Run                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│                    [SCREENSHOT HERE]                         │
│                                                              │
│  Task Tree Structure:                                        │
│  ├── Feature A (completed)                                 │
│  ├── Feature B (in progress)                               │
│  │   ├── Subtask B.1 (running)                            │
│  │   └── Subtask B.2 (pending)                            │
│  └── Feature C (pending)                                   │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Caption Template
```markdown
**Watch Your Code Build Itself**

See tasks decompose and execute in real-time:
- Double Diamond phases (Discover → Define → Design → Deliver)
- Parallel task execution with dependency tracking
- Status indicators (pending, in progress, completed)
- Real-time metrics (tokens, time, cost)

Know exactly what's happening, always.

[Star us on GitHub](https://github.com/Q00/ouroboros)
```

### Key Elements to Highlight
1. Task tree with hierarchy
2. Status icons (pending/running/completed)
3. Progress bars for active tasks
4. Token usage display
5. Time elapsed indicator

---

## Screenshot 4: Cost Dashboard

### Purpose
Show the 85% cost savings achieved through PAL Router.

### Base Image
Create new: Cost breakdown panel showing tier usage

### Annotation Template

```
┌─────────────────────────────────────────────────────────────┐
│                   PAL Router Cost Savings                   │
│                    85% Cost Reduction                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│                    [SCREENSHOT HERE]                         │
│                                                              │
│  Tier Distribution:                                          │
│  ├── Frugal (Haiku): 65% @ $0.25/1M tokens                 │
│  ├── Standard (Sonnet): 30% @ $3/1M tokens                 │
│  └── Frontier (Opus): 5% @ $15/1M tokens                   │
│                                                              │
│  Total Cost: $12.34 (vs $82.00 without PAL Router)          │
│  Savings: $69.66 (85%)                                      │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Caption Template
```markdown
**Stop Overpaying for AI Development**

PAL Router automatically selects the right model tier:
- Simple queries → Haiku (frugal)
- Medium tasks → Sonnet (standard)
- Complex tasks → Opus (frontier)

The result: 85% cost reduction with no quality loss.

Why pay $300 when you can pay $45?

[Star us on GitHub](https://github.com/Q00/ouroboros)
```

### Key Elements to Highlight
1. Tier breakdown (pie chart or bar graph)
2. Cost comparison (before/after)
3. Complexity algorithm display
4. Per-tier token counts
5. Total savings amount

---

## Screenshot 5: Evaluation Results

### Purpose
Show the 3-stage evaluation pipeline that ensures quality.

### Base Image
`evaluate.png` - Evaluation results with pass/fail indicators

### Annotation Template

```
┌─────────────────────────────────────────────────────────────┐
│                   3-Stage Evaluation                        │
│              Quality Assurance Built-In                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│                    [SCREENSHOT HERE]                         │
│                                                              │
│  Evaluation Stages:                                          │
│  ├── 1. Mechanical: Automated tests                         │
│  ├── 2. Semantic: Requirements matching                     │
│  └── 3. Consensus: Multi-agent validation                   │
│                                                              │
│  Overall Grade: A (92%)                                     │
│  Issues Found: 3 (all fixed)                                │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Caption Template
```markdown
**Quality Assurance for AI Development**

Three-stage evaluation catches issues before deployment:
1. Mechanical (automated tests): Syntax, types, linting
2. Semantic (requirements matching): Did we build what we specified?
3. Consensus (multi-agent): Cross-validation for confidence

Catch issues early. Ship with confidence.

[Star us on GitHub](https://github.com/Q00/ouroboros)
```

### Key Elements to Highlight
1. Three-stage pipeline visualization
2. Pass/fail indicators per stage
3. Overall quality grade
4. Issues found and fixed count
5. Confidence score

---

## Screenshot 6: Event Replay

### Purpose
Show the debugging capability through event sourcing.

### Base Image
Create new: Event timeline with replay controls

### Annotation Template

```
┌─────────────────────────────────────────────────────────────┐
│                   Event Replay Debugging                    │
│              Time-Travel for AI Workflows                   │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│                    [SCREENSHOT HERE]                         │
│                                                              │
│  Event Timeline:                                             │
│  ├── 14:32:01 Phase started: Discover                       │
│  ├── 14:32:15 Task created: "User auth"                     │
│  ├── 14:32:45 Task completed: "User auth"                   │
│  └── 14:33:02 Phase started: Define                         │
│                                                              │
│  Replay from any point. Debug any session.                  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Caption Template
```markdown
**Debug Any Session, Any Time**

Full event sourcing means:
- Every decision recorded
- Every state change tracked
- Every agent action logged
- Complete replay capability

Time-travel debugging for AI workflows.

[Star us on GitHub](https://github.com/Q00/ouroboros)
```

### Key Elements to Highlight
1. Event timeline with timestamps
2. Event types color-coded
3. Replay controls (scrubber, jump to)
4. State inspection panel
5. Export/save event log button

---

## Annotation Tools

### For Quick Annotations
- **macOS Preview**: Built-in, basic markup
- **Skitch**: Simple, free screenshot annotation
- **CleanShot X**: Powerful, paid (worth it)

### For Professional Graphics
- **Figma**: Full design control
- **Canva**: Templates and elements
- **Photoshop**: Industry standard

### For Terminal Screenshots
- **iTerm2**: Built-in screen capture
- **Terminalizer**: Animated terminal recordings
- **asciinema**: Terminal session recording

### Recommended Workflow

1. **Capture** screenshot with appropriate content
2. **Open** in annotation tool
3. **Add** callout boxes for key features
4. **Label** with clear, concise text
5. **Add** star button overlay
6. **Export** as PNG (high quality)
7. **Optimize** file size (under 500KB)
8. **Test** in README preview

---

## README Gallery Layout

```markdown
## 📸 Screenshots

### Visual Workflow Tracking
![Dashboard](docs/screenshots/dashboard.png)
*See your workflows execute in real-time with our rich TUI dashboard*

### Requirements Gathering
![Interview](docs/screenshots/interview.png)
*Socratic interview exposes hidden assumptions before coding*

### Parallel Execution
![Execution](docs/screenshots/execution.png)
*Watch tasks decompose and execute with dependency tracking*

### Quality Assurance
![Evaluation](docs/screenshots/evaluate.png)
*Three-stage evaluation catches issues before deployment*

**Want to see more?** [Star us on GitHub](https://github.com/Q00/ouroboros)
```

---

## CTA Variations

### Button Style
```markdown
[![Star Us](https://img.shields.io/badge/Star%20Us-GitHub-blue?style=for-the-badge&logo=github)](https://github.com/Q00/ouroboros)
```

### Link Style
```markdown
**Found this useful?** [Star us on GitHub](https://github.com/Q00/ouroboros)
```

### Text Style
```markdown
> ⭐ If you find Ouroboros useful, please consider starring us on GitHub!
> https://github.com/Q00/ouroboros
```

### Embedded Style
```markdown
<a href="https://github.com/Q00/ouroboros">
  <img src="https://img.shields.io/badge/Star%20on%20GitHub-black?style=for-the-badge&logo=github" alt="Star on GitHub">
</a>
```

---

## File Optimization

### Before Uploading
```bash
# Optimize PNG file size
pngquant --quality=85-95 dashboard.png

# Or use ImageMagick
convert dashboard.png -quality 95 -strip dashboard_optimized.png

# Check file size
ls -lh dashboard.png
```

### Target Sizes
- Hero image: Under 1MB
- Feature screenshots: Under 500KB each
- Thumbnails: Under 100KB

### Format Guidelines
- Use PNG for screenshots (lossless)
- Use JPG for photos (smaller size)
- Use WebP for modern browsers (best compression)
- Never use GIF for static images

---

## Accessibility

### Alt Text Templates
```markdown
![TUI dashboard showing active workflow execution with phase progress,
task tree, cost tracker, and drift metrics](docs/screenshots/dashboard.png)

![Socratic interview mode displaying a question about task deletion
with answer input field and progress indicator](docs/screenshots/interview.png)
```

### Color Contrast
- Ensure text overlays have background
- Use high contrast colors (WCAG AA minimum)
- Test in both light and dark modes
- Avoid red/green combinations (colorblindness)

---

## Quality Checklist

For each screenshot:
- [ ] Content is meaningful (not empty state)
- [ ] Resolution is 1280x720 or higher
- [ ] File size is under 500KB
- [ ] Colors are high contrast
- [ ] No sensitive information visible
- [ ] Annotations are clear and readable
- [ ] CTA to star on GitHub included
- [ ] Alt text provided
- [ ] Tested in README preview
- [ ] Works in both light and dark themes

---

## Next Steps

1. **Capture** all 6 screenshots using the guide
2. **Annotate** with clear feature callouts
3. **Add** star button CTAs
4. **Optimize** file sizes
5. **Update** README with gallery
6. **Test** in GitHub preview
7. **Iterate** based on feedback

**Remember**: Screenshots are often the first thing visitors see. Make them count!
