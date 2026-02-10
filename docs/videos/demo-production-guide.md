# Demo Production Guide

This guide provides step-by-step instructions for creating the 30-second Quick Start demo for Ouroboros.

## Prerequisites

### Required Tools

| Tool | Purpose | Install Command |
|------|---------|-----------------|
| asciinema | Terminal recording | `brew install asciinema` |
| ffmpeg | Video conversion | `brew install ffmpeg` |
| tmux | Session management (optional) | `brew install tmux` |

### Verify Installation

```bash
asciinema --version  # Should output 3.x.x
ffmpeg -version | head -1  # Should show version
```

## Quick Start (30-Second Demo)

### Option A: Automated Script Production (Recommended)

Use the provided automated scripts for consistent results:

```bash
cd /Users/jaegyu.lee/Project/ouroboros/docs/videos

# Run the automated demo production
./produce-demo.sh quickstart
```

### Option B: Manual Recording

Follow these steps for manual recording:

#### Step 1: Prepare Environment

```bash
# Clean terminal and set optimal size
export PS1="$ "
clear

# Set terminal size (120 columns x 30 rows)
# In iTerm2: View -> Set Font Size -> 14pt
# In Terminal.app: Terminal -> Preferences -> Profiles -> Text -> Font 14
```

#### Step 2: Create Demo Workspace

```bash
# Create a clean demo directory
mkdir -p ~/ouroboros-demo && cd ~/ouroboros-demo
```

#### Step 3: Test Run (Before Recording)

```bash
# Test the command first
ooo run Create a simple counter component with increment/decrement buttons
```

#### Step 4: Record Demo

```bash
# Start recording
asciinema rec quickstart-demo.cast

# In the recording session, execute:
echo "Ouroboros Quick Start Demo"
echo "==========================="
echo ""
ooo run Create a simple counter component with increment/decrement buttons

# Press Ctrl+D when complete
```

#### Step 5: Review and Export

```bash
# Review the recording
asciinema play quickstart-demo.cast

# Convert to MP4 (requires ffmpeg)
./cast-to-mp4.sh quickstart-demo.cast quickstart-demo.mp4
```

## Script Details

### 30-Second Quick Start Timeline

| Time | Action | Expected Output |
|------|--------|-----------------|
| 0-5s | Show clean terminal | Ouroboros intro message |
| 5-15s | Type `ooo run` command | Planning phase starts |
| 15-25s | Show execution progress | Agent activity and results |
| 25-30s | Display completion | Summary and CTA |

### Demo Commands

#### Simple Counter (Default)
```bash
ooo run Create a simple counter component with increment/decrement buttons
```

#### Next.js App (Node.js)
```bash
ooo run Create a Next.js 14 app with shadcn/ui and a dark mode toggle
```

#### FastAPI Project (Python)
```bash
ooo run Create a FastAPI project with PostgreSQL, SQLAlchemy, and pytest
```

## Post-Production

### Export Settings

```bash
# For web embedding (720p, optimized)
ffmpeg -i input.cast -vf "scale=1280:-1" -c:v libx264 -preset medium \
  -crf 23 -pix_fmt yuv420p output.mp4

# For high quality (1080p)
ffmpeg -i input.cast -vf "scale=1920:-1" -c:v libx264 -preset slow \
  -crf 20 -pix_fmt yuv420p output.mp4
```

### Adding Overlays

```bash
# Add title overlay
ffmpeg -i input.mp4 -vf "drawtext=text='Ouroboros Quick Start':\
  fontsize=32:fontcolor=white:x=(w-text_w)/2:y=20" output.mp4
```

## File Organization

```
docs/videos/
├── output/                    # Generated demo files
│   ├── quickstart.cast       # asciinema cast files
│   ├── quickstart.mp4        # MP4 videos
│   └── quickstart.gif        # GIF for README
├── scripts/                   # Demo automation scripts
│   ├── produce-demo.sh       # Main production script
│   ├── cast-to-mp4.sh        # Conversion script
│   └── demo-scene.sh         # Scene scripts
├── script.md                 # Original 30s script
└── demo-production-guide.md  # This file
```

## Best Practices

1. **Clean Environment**: Start with a fresh terminal
2. **Pre-tested Commands**: Run commands before recording
3. **Consistent Sizing**: Use 120x30 terminal size
4. **Short Duration**: Keep under 30 seconds
5. **High Contrast**: Use dark terminal with bright text

## Troubleshooting

| Issue | Solution |
|-------|----------|
| asciinema not found | Run `brew install asciinema` |
| Terminal size wrong | Set `ASCIINEMA_REC_COLS=120` and `ROWS=30` |
| Colors incorrect | Set `TERM=xterm-256color` |
| File too large | Use `--idle-time-limit=2` to remove dead time |

## Publishing Checklist

- [ ] Demo tested and verified working
- [ ] Recording completed without errors
- [ ] Video reviewed for quality
- [ ] Exported in correct format (MP4, H.264)
- [ ] File size optimized (<50MB for web)
- [ ] Added to docs/videos/output/
- [ ] README.md updated with demo link
- [ ] Upload to asciinema.org (optional)

## Advanced: TTY Recording with Sound

For keyboard sound effects and more authentic feel:

```bash
# Install ttyrec
brew install ttyrec

# Record with ttyrec
ttyrec demo.ttyrec

# Convert to asciinema
tty2cast demo.ttyrec > demo.cast
```

---

*Last updated: 2025-02-11*
