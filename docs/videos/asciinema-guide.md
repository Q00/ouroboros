# Terminal Recording Guide (asciinema)

This guide covers setting up and using asciinema for recording terminal sessions, ideal for embedding in documentation and README files.

## Table of Contents
1. [Installation](#installation)
2. [Basic Usage](#basic-usage)
3. [Configuration](#configuration)
4. [Embedding](#embedding)
5. [Advanced Tips](#advanced-tips)

---

## Installation

### macOS
```bash
brew install asciinema
```

### Linux
```bash
# Ubuntu/Debian
sudo apt install asciinema

# Fedora
sudo dnf install asciinema

# Arch
sudo pacman -S asciinema
```

### Verify Installation
```bash
asciinema --version
# Expected output: asciinema 2.x.x
```

---

## Basic Usage

### Quick Record
```bash
# Start recording (saves locally)
asciinema rec demo.cast

# Press Ctrl+D or exit the shell to stop recording
```

### Play Recording
```bash
# Play locally
asciinema play demo.cast

# Play at 2x speed
asciinema play -s 2 demo.cast
```

### Upload to asciinema.org
```bash
# Upload and get shareable URL
asciinema upload demo.cast

# Record and upload in one command
asciinema rec demo-upload.cast
# When prompted, authenticate and upload
```

---

## Configuration

### Config File Location
```bash
# macOS/Linux
~/.config/asciinema/config
```

### Recommended Config
```toml
# ~/.config/asciinema/config
[api]
token = YOUR_API_TOKEN_HERE

[rec]
# Command to record (default: $SHELL)
command = /bin/bash

# Record at idle timeout (seconds)
idle_time_limit = 2.0

# Yes/No - prompt before uploading
yes = true

# Record with additional metadata
cols = 120
rows = 30
```

### Environment Variables
```bash
# Set terminal size for recording
export ASCIINEMA_REC_COLS=120
export ASCIINEMA_REC_ROWS=30

# Set idle time limit (auto-pause)
export ASCIINEMA_REC_IDLE_TIME_LIMIT=2

# Disable recording idle time
export ASCIINEMA_REC_IDLE_TIME_LIMIT=0
```

---

## Recording for Ouroboros Demos

### Preparation Script
```bash
#!/bin/bash
# pre-record.sh - Prepare terminal for recording

# Clean terminal
clear

# Set optimal terminal size
echo -ne "\e[8;30;120t"

# Set a clean prompt
export PS1="$ "

# Display welcome message
cat << 'EOF'
Ouroboros Demo - Terminal Recording
===================================
Ready to record. Type 'asciinema rec' when ready.
EOF
```

### Demo Recording Template
```bash
#!/bin/bash
# record-ooo-demo.sh - Record Ouroboros demo

asciinema rec \
  --cols 120 \
  --rows 30 \
  --idle-time-limit 1.5 \
  --command "bash demo-scene.sh" \
  ouroboros-demo.cast

# Preview
asciinema play ouroboros-demo.cast

# Offer to upload
echo "Upload to asciinema.org? (y/n)"
read -r answer
if [ "$answer" = "y" ]; then
  asciinema upload ouroboros-demo.cast
fi
```

### Demo Scene Script
```bash
#!/bin/bash
# demo-scene.sh - The actual demo commands

# Add pauses for dramatic effect
pause() {
  sleep 1.5
}

echo "Starting Ouroboros Quick Demo..."
pause

echo "$ ooo run Create a simple counter component"
pause

# Simulate the command
echo "[Planning phase...] Analyzing requirements..."
sleep 1
echo "[Execution phase...] Creating component files..."
sleep 1
echo "[Verification phase...] Testing component..."
sleep 1

echo "Done! Component created at src/components/Counter.tsx"
pause

echo "$ cat src/components/Counter.tsx"
pause

# Show the component
cat << 'EOF'
// Counter component with increment/decrement
const Counter = () => {
  const [count, setCount] = useState(0);
  return (
    <div>
      <button onClick={() => setCount(c => c - 1)}>-</button>
      <span>{count}</span>
      <button onClick={() => setCount(c => c + 1)}>+</button>
    </div>
  );
};
EOF

pause
echo "Demo complete!"
```

---

## Embedding

### In Markdown (GitHub)

```markdown
<!-- Upload to asciinema.org first, then use this format -->

[![asciicast](https://asciinema.org/a/IMAGE_ID.svg)](https://asciinema.org/a/IMAGE_ID)
```

### In HTML

```html
<!-- Interactive player -->
<script src="https://asciinema.org/a/IMAGE_ID.js" id="asciicast-IMAGE_ID" async></script>

<!-- Or use the player directly -->
<asciinema-player src="demo.cast" cols="120" rows="30"></asciinema-player>
```

### Converting to GIF

```bash
# Install agg (asciinema to GIF converter)
# macOS
brew install agg

# Linux
cargo install agg

# Convert CAST to GIF
agg input.cast output.gif --theme monokai --font-size 18
```

### Converting to MP4

```bash
# Using asciinema-ffmpeg (custom script)
# Requires ffmpeg

#!/bin/bash
cast_to_mp4() {
  local input=$1
  local output="${2:-${input%.*}.mp4}"

  asciinema play "$input" | \
    ffmpeg -y -i - -vf "scale=1280:-1" -c:v libx264 -preset slow \
    -crf 23 -pix_fmt yuv420p "$output"
}

cast_to_mp4 demo.cast demo.mp4
```

---

## Advanced Tips

### Speed Control
```bash
# Record at normal speed, play faster
asciinema rec demo.cast
asciinema play -s 2 demo.cast  # 2x speed

# Record with idle limit to remove dead time
asciinema rec --idle-time-limit=1.5 demo.cast
```

### Multiple Takes
```bash
# Record multiple versions
for i in {1..3}; do
  asciinema rec "demo-take-${i}.cast"
  # Review each take
  asciinema play "demo-take-${i}.cast"
done

# Choose best take, rename
mv demo-take-2.cast demo-final.cast
```

### Adding Title/Comments
```bash
# Edit CAST file to add metadata
# CAST files are JSON, easily editable

cat > demo.cast << 'EOF'
{"version": 2, "width": 120, "height": 30, "timestamp": 1234567890, "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"}}
[0.123, "o", "Ouroboros Demo\n"]
[0.234, "o", "$ ooo run Create counter\n"]
...
EOF
```

### Keyboard Sound Effects
```bash
# Record with TTYrec for sound support
# Then convert to asciinema

# Install ttyrec and tty2cast
brew install ttyrec

# Record with ttyrec
ttyrec demo.ttyrec

# Convert to asciinema
tty2cast demo.ttyrec > demo.cast
```

---

## Best Practices for Ouroboros

### Recommended Settings
| Setting | Value | Reason |
|---------|-------|--------|
| Columns | 120-140 | Wide enough for code output |
| Rows | 30-40 | Show sufficient context |
| Idle limit | 1.5-2.0 | Remove thinking pauses |
| Font | Monospace 14-16pt | Readable code display |

### Content Guidelines
1. **Keep it short**: Under 60 seconds for demos
2. **Clear commands**: No typos, no backspacing
3. **Add pauses**: Let viewer read output
4. **Show results**: Verify the output is correct

### Example Full Workflow
```bash
# 1. Prepare environment
./pre-record.sh

# 2. Practice the demo (dry run)
./demo-scene.sh

# 3. Record for real
asciinema rec --idle-time-limit=2 ooo-demo.cast

# 4. Review
asciinema play ooo-demo.cast

# 5. If good, upload
asciinema upload ooo-demo.cast

# 6. Add to README
# Copy the embed code from asciinema.org
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Terminal size wrong | Set `ASCIINEMA_REC_COLS` and `ROWS` |
| Colors look wrong | Ensure TERM is set to `xterm-256color` |
| Too much dead time | Use `--idle-time-limit` |
| File too large | Edit CAST to remove unnecessary frames |
| Playback issues | Validate CAST file format |

### Validate CAST File
```python
#!/usr/bin/env python3
# validate_cast.py - Check CAST file format

import json
import sys

if len(sys.argv) < 2:
    print("Usage: validate_cast.py file.cast")
    sys.exit(1)

with open(sys.argv[1]) as f:
    header = json.loads(f.readline())
    required = ['version', 'width', 'height']
    for field in required:
        if field not in header:
            print(f"Missing required field: {field}")
            sys.exit(1)

    print(f"Valid CAST file: {header['width']}x{header['height']}")
```
