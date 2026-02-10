# Ouroboros Screenshots Guide

This directory contains screenshots for the Ouroboros documentation.

## Required Screenshots

### 1. Dashboard (`dashboard.png`)
- **Purpose**: Show the main TUI dashboard with workflow list
- **Content**:
  - Workflow tree on the left (showing available workflows)
  - Detail panel on the right
  - Help footer at the bottom
- **How to capture**: Run `ooo` and capture the initial state

### 2. Interview Mode (`interview.png`)
- **Purpose**: Show the interactive interview workflow
- **Content**:
  - Question displayed in the main area
  - Input field at the bottom
  - Progress indicator
- **How to capture**: Run `ooo interview` and answer a few questions

### 3. Seed Mode (`seed.png`)
- **Purpose**: Show the workflow seeding interface
- **Content**:
  - Seed configuration form
  - Context preview
  - Confirmation prompt
- **How to capture**: Run `ooo seed` and navigate through the prompts

### 4. Evaluate Mode (`evaluate.png`)
- **Purpose**: Show the evaluation/results interface
- **Content**:
  - Execution results
  - Metrics/stats display
  - Pass/fail indicators
- **How to capture**: Run `ooo evaluate` after completing a workflow

## Capturing Screenshots

### Method 1: Using the Demo Script
```bash
./docs/screenshots/capture-demo.sh
```

### Method 2: Manual Capture with Terminal Recording
1. Install `terminalizer` or `asciinema`:
   ```bash
   npm install -g terminalizer
   # or
   brew install asciinema
   ```

2. Record the TUI session:
   ```bash
   terminalizer record ouroboros-demo
   # or
   asciinema rec ouroboros-demo.cast
   ```

3. Export individual frames as PNG

### Method 3: Direct Screenshot (macOS)
1. Run the TUI in a dedicated terminal window
2. Press `Cmd + Shift + 4` then `Space` for window capture
3. Click the terminal window

### Method 4: Using iTerm2 Shell Integration
1. Enable Shell Integration in iTerm2
2. Use `Save Current Screen` feature from the menu

## Styling the Terminal for Screenshots

For consistent, professional screenshots, configure your terminal:

**Recommended Settings**:
- Font: SF Mono, JetBrains Mono, or Fira Code (14-16pt)
- Theme: Dark background with high contrast
- Window size: 1200x800 or similar
- Transparency: 0% (solid background)
- Blur: Disabled

**Color Palette (Reference)**:
- Background: `#1e1e2e` (Catppuccin Mocha base)
- Text: `#cdd6f4`
- Primary: `#89b4fa`
- Success: `#a6e3a1`
- Warning: `#f9e2af`
- Error: `#f38ba8`

## Placeholder Images

Current files are placeholders:
- `dashboard.png` - 1200x800 empty placeholder
- `interview.png` - 1200x800 empty placeholder
- `optimize.png` - 1200x800 empty placeholder
- `evaluate.png` - 1200x800 empty placeholder

Replace these with actual TUI screenshots following the guide above.

## Quick Reference

```bash
# Run the TUI
uv run python -m ouroboros.tui.app

# Run interview mode
uv run python -m ouroboros.cli interview

# Run seed mode
uv run python -m ouroboros.cli seed

# Run evaluate mode
uv run python -m ouroboros.cli evaluate
```
