# Getting Started with Ouroboros

Transform your vague ideas into validated specifications and execute them with confidence.

## Quick Start: 3 Commands in 60 Seconds

### Plugin Mode (No Python Required)
```bash
# Install (2 commands)
claude /plugin marketplace add Q00/ouroboros
claude /plugin install ouroboros@ouroboros

# Your first workflow (2 commands)
ooo interview "Build a task management CLI"
ooo seed
```

**Done!** You now have a validated specification ready for execution.

### Full Mode (Python 3.14+ Required)
```bash
# Setup
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync

# Configure
export ANTHROPIC_API_KEY="your-key"
ouroboros setup

# Execute
ouroboros run --seed ~/.ouroboros/seeds/latest.yaml
```

---

## Installation Guide

### Prerequisites
- **Claude Code** (for Plugin Mode)
- **Python 3.14+** (for Full Mode)
- **API Key** from OpenAI, Anthropic, or compatible provider

### Option 1: Plugin Mode (Recommended for Beginners)
```bash
# Install via Claude Code marketplace
claude /plugin marketplace add Q00/ouroboros
claude /plugin install ouroboros@ouroboros

# Verify installation
ooo help
```

### Option 2: Full Mode (For Developers)
```bash
# Clone repository
git clone https://github.com/Q00/ouroboros
cd ouroboros

# Install dependencies
uv sync

# Or using pip
pip install -e .

# Verify CLI
ouroboros --version
```

### Option 3: Standalone Binary
```bash
# Download from GitHub Releases
# macOS: brew install ouroboros
# Linux: snap install ouroboros

# Verify
ouroboros --version
```

---

## Configuration

### API Keys
```bash
# Set environment variables
export ANTHROPIC_API_KEY="your-anthropic-key"
# OR
export OPENAI_API_KEY="your-openai-key"

# Verify setup
ouroboros status health
```

### Configuration File
Create `~/.ouroboros/config.yaml`:
```yaml
# Model preferences
providers:
  default: anthropic/claude-3-5-sonnet
  frugal: anthropic/claude-3-haiku
  standard: anthropic/claude-3-5-sonnet
  frontier: anthropic/claude-3-opus

# TUI settings
tui:
  theme: dark
  refresh_rate: 100ms
  show_metrics: true

# Execution settings
execution:
  max_parallel_tasks: 5
  default_mode: standard
  auto_save: true
```

### Environment Variables
```bash
# Terminal customization
export TERM=xterm-256color
export OUROBOROS_THEME=dark

# MCP settings
export OUROBOROS_MCP_HOST=localhost
export OUROBOROS_MCP_PORT=8000
```

---

## Your First Workflow: Complete Tutorial

### Step 1: Start with an Idea
```bash
# Launch the Socratic interview
ooo interview "I want to build a personal finance tracker"
```

### Step 2: Answer Clarifying Questions
The interview will ask questions like:
- "What platforms do you want to track?" (Bank accounts, credit cards, investments)
- "Do you need budgeting features?" (Yes, with category tracking)
- "Mobile app or web-based?" (Desktop-only with web export)
- "Data storage preference?" (SQLite, local file)

Continue until the ambiguity score drops below 0.2.

### Step 3: Generate the Seed
```bash
# Create immutable specification
ooo seed
```

This generates a `seed.yaml` file like:
```yaml
goal: "Build a personal finance tracker with SQLite storage"
constraints:
  - "Desktop application only"
  - "Category-based budgeting"
  - "Export to CSV/Excel"
acceptance_criteria:
  - "Track income and expenses"
  - "Categorize transactions automatically"
  - "Generate monthly reports"
  - "Set and monitor budgets"
ontology_schema:
  name: "FinanceTracker"
  fields:
    - name: "transactions"
      type: "array"
      description: "All financial transactions"
metadata:
  ambiguity_score: 0.15
  seed_id: "seed_abc123"
```

### Step 4: Execute with TUI
```bash
# Launch visual execution
ouroboros run --seed finance-tracker.yaml --ui tui
```

### Step 5: Monitor Progress
Watch the TUI dashboard show:
- Double Diamond phases (Discover → Define → Design → Deliver)
- Task decomposition tree
- Parallel execution batches
- Real-time metrics (tokens, cost, drift)

### Step 6: Evaluate Results
```bash
# Run 3-stage evaluation
ooo evaluate
```

The evaluation checks:
1. **Mechanical** - Code compiles, tests pass, linting clean
2. **Semantic** - Meets acceptance criteria, aligned with goals
3. **Consensus** - Multi-model validation for critical decisions

---

## Common Workflows

### Workflow 1: New Project from Scratch
```bash
# 1. Clarify requirements
ooo interview "Build a REST API for a blog"

# 2. Generate specification
ooo seed

# 3. Execute with visualization
ouroboros run --seed blog-api.yaml --ui tui

# 4. Evaluate results
ooo evaluate

# 5. Monitor drift
ouroboros status drift
```

### Workflow 2: Bug Fixing
```bash
# 1. Analyze the problem
ooo analyze "User registration fails with email validation"

# 2. Generate fix seed
ooo seed

# 3. Execute
ouroboros run --seed bugfix-email.yaml

# 4. Verify fix
ooo evaluate
```

### Workflow 3: Feature Enhancement
```bash
# 1. Plan the enhancement
ooo plan "Add real-time notifications to the chat app"

# 2. Break into tasks
ooo seed

# 3. Execute with team coordination
ouroboros run --seed notifications.yaml --mode swarm

# 4. Review implementation
ooo review "notifications feature"
```

---

## Understanding the TUI Dashboard

The TUI provides real-time visibility into your workflow:

### Main Dashboard View
```
┌──────────────────────────────────────────────────────┐
│  🎯 OUROBOROS DASHBOARD                              │
├──────────────────────────────────────────────────────┤
│  Phase: 🟢 DESIGN                                    │
│  Progress: 65% [████████████░░░░░░░░░░░]              │
│  Cost: $2.34 (85% saved)                             │
│  Drift: 0.12 ✅                                      │
├──────────────────────────────────────────────────────┤
│  Task Tree                                          │
│  ├─ 🟢 Define API endpoints (100%)                    │
│  ├─ 🟡 Implement auth service (75%)                 │
│  └─ ○ Create database schema (0%)                    │
├──────────────────────────────────────────────────────┤
│  Active Agents: 3/5                                  │
│  ├── executor [Building auth service]                │
│  ├── researcher [Analyzing best practices]           │
│  └── verifier [Waiting results]                      │
└──────────────────────────────────────────────────────┘
```

### Key Components
1. **Phase Indicator** - Shows current Double Diamond phase
2. **Progress Bar** - Overall completion percentage
3. **Metrics Panel** - Cost, drift, and agent status
4. **Task Tree** - Hierarchical view of all tasks
5. **Agent Activity** - Live status of working agents

### Interactive Features
- **Click** on tasks to see details
- **Press Space** to pause/resume execution
- **Press D** to view drift analysis
- **Press C** to see cost breakdown

---

## Troubleshooting

### Installation Issues

#### Plugin not recognized
```bash
# Check plugin is installed
claude /plugin list

# Reinstall if needed
claude /plugin install ouroboros@ouroboros --force

# Restart Claude Code
```

#### Python dependency errors
```bash
# Check Python version
python --version  # Must be 3.14+

# Reinstall with uv
uv sync --all-groups

# Or with pip
pip install --force-reinstall ouroboros-ai
```

### Configuration Issues

#### API key not found
```bash
# Set environment variable
export ANTHROPIC_API_KEY="your-key"

# Or use .env file
echo 'ANTHROPIC_API_KEY=your-key' > ~/.ouroboros/.env

# Verify
ouroboros status health
```

#### MCP server issues
```bash
# Re-register MCP server
ouroboros mcp register

# Check server status
ouroboros mcp status
```

### Execution Issues

#### TUI not displaying
```bash
# Check terminal capabilities
echo $TERM

# Set proper TERM
export TERM=xterm-256color

# Try CLI mode
ouroboros run --seed project.yaml --ui cli
```

#### High costs
```bash
# Check predictions
ouroboros predict --seed project.yaml

# Review cost breakdown
ouroboros cost breakdown
```

#### Stuck execution
```bash
# Check status
ouroboros status --events

# Use unstuck mode
ooo unstuck

# Or restart from checkpoint
ouroboros run --seed project.yaml --resume
```

### Performance Issues

#### Slow startup
```bash
# Clear cache
rm -rf ~/.ouroboros/cache/

# Check resource usage
ps aux | grep ouroboros

# Reduce parallel tasks
export OUROBOROS_MAX_PARALLEL=2
```

#### Memory issues
```bash
# Enable compression
export OUROBOROS_COMPRESS=true

# Check memory limits
ouroboros config get limits
```

---

## Best Practices

### For Better Interviews
1. **Be specific** - Instead of "build a social app" say "build a Twitter clone with real-time messaging"
2. **Consider constraints** - Think about budget, timeline, and technical limitations
3. **Define success** - Clear acceptance criteria help generate better specs

### For Effective Seeds
1. **Include non-functional requirements** - Performance, security, scalability
2. **Define boundaries** - What's in scope and what's not
3. **Specify integrations** - APIs, databases, third-party services

### For Successful Execution
1. **Monitor drift** - Check status regularly to catch deviations early
2. **Use evaluation** - Always run evaluation to ensure quality
3. **Iterate with evolve** - Use evolutionary loops to refine specs

---

## Next Steps

### After Your First Project
1. **Explore Modes** - Try different execution modes for various scenarios
2. **Custom Skills** - Create your own skills for repetitive workflows
3. **Team Work** - Use swarm mode for team-based development

### Advanced Topics
1. **Custom Agents** - Define specialized agents for your domain
2. **MCP Integration** - Connect to external tools and services
3. **Event Analysis** - Use replay to learn from past executions

### Community
- 📚 [Documentation](https://github.com/Q00/ouroboros/docs)
- 💬 [Discord Community](https://discord.gg/ouroboros)
- 🐛 [GitHub Issues](https://github.com/Q00/ouroboros/issues)
- 💡 [Feature Requests](https://github.com/Q00/ouroboros/discussions)

---

## Troubleshooting Reference

| Issue | Solution | Command |
|-------|----------|---------|
| Plugin not loaded | Reinstall plugin | `claude /plugin install ouroboros@ouroboros` |
| CLI not found | Install Python package | `pip install ouroboros-ai` |
| API errors | Check API key | `export ANTHROPIC_API_KEY=...` |
| TUI blank | Check terminal | `export TERM=xterm-256color` |
| High costs | Reduce seed scope | `ooo interview` to refine |
| Execution stuck | Use unstuck | `ooo unstuck` |
| Drift detected | Review spec | `ouroboros status drift` |

Need more help? Check our [FAQ](docs/faq.md) or join our [Discord](https://discord.gg/ouroboros).