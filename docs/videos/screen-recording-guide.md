# Screen Recording Guide

This guide covers step-by-step instructions for recording high-quality demo videos of Ouroboros.

## Table of Contents
1. [macOS Screen Recording](#macos-screen-recording)
2. [Linux Screen Recording](#linux-screen-recording)
3. [OBS Studio (Cross-platform)](#obs-studio)
4. [Best Practices](#best-practices)
5. [Post-Production](#post-production)

---

## macOS Screen Recording

### Built-in Screen Recorder

**Setup:**
```bash
# 1. Set up your terminal before recording
# Recommended: iTerm2 or Terminal.app with these settings:
# - Profile: Dark (High contrast)
# - Font: SF Mono 14pt or MesloLGS NF 14pt
# - Hide scrollbar and toolbar for clean view
```

**Recording:**
1. Press `Cmd + Shift + 5` to open screen capture tools
2. Choose "Record Selected Portion"
3. Drag to select your terminal window (recommended: 1280x720 minimum)
4. Click "Record"
5. Perform your demo
6. Press `Cmd + Shift + 5` again, then "Stop Recording"
7. Video saves to `~/Movies/Screen Recording`

**Optimization:**
```bash
# Use ffmpeg to compress and optimize
ffmpeg -i input.mov -c:v libx264 -crf 23 -preset medium \
  -vf "scale=1280:trunc(ow/a/2)*2" -c:a aac -b:a 128k \
  output.mp4
```

---

## Linux Screen Recording

### SimpleScreenRecorder

**Install:**
```bash
# Ubuntu/Debian
sudo apt install simplescreenrecorder

# Fedora
sudo dnf install simplescreenrecorder

# Arch
sudo pacman -S simplescreenrecorder
```

**Settings:**
| Option | Value |
|--------|-------|
| Video container | MP4 |
| Video codec | H.264 |
| Audio codec | AAC (optional) |
| Frame rate | 30 fps |
| Bitrate | 5000 kbps |
| Resolution | 1280x720 (or higher) |

### Using ffmpeg Directly

```bash
# Record specific window (find window ID with xwininfo)
ffmpeg -f x11grab -framerate 30 -video_size 1280x720 \
  -i :0.0+100,100 -c:v libx264 -preset ultrafast \
  -crf 23 -pix_fmt yuv420p output.mp4

# Record specific display
ffmpeg -f x11grab -framerate 30 -video_size 1920x1080 \
  -i :0.0 -c:v libx264 -preset ultrafast \
  -crf 23 -pix_fmt yuv420p output.mp4
```

---

## OBS Studio (Cross-platform)

### Installation

```bash
# macOS
brew install --cask obs

# Ubuntu/Debian
sudo apt install obs-studio

# Fedora
sudo dnf install obs-studio

# Arch
sudo pacman -S obs
```

### Scene Setup for Terminal Demos

**1. Create a new Scene**
- Name: "Terminal Demo"
- Add Source: "Window Capture" or "Display Capture"

**2. Configure Source**
- Select your terminal application
- Crop to remove window decorations
- Transform > Fit to screen (if needed)

**3. Output Settings**
- Settings > Output > Recording
- Format: MP4 (or MKV for safety during recording)
- Encoder: x264
- Rate Control: CBR
- Bitrate: 5000 Kbps (1080p), 3000 Kbps (720p)
- Keyframe Interval: 2

**4. Video Settings**
- Settings > Video
- Base Resolution: 1920x1080 or 1280x720
- Output Resolution: Same as base
- FPS: 30

**5. Audio (Optional)**
- Add Audio Input Capture for microphone
- Add Audio Output Capture for system sounds
- Set levels to avoid clipping

---

## Best Practices

### Before Recording

1. **Environment Setup**
   ```bash
   # Clean terminal history
   history -c

   # Set up demo workspace
   cd ~/demo-workspace
   rm -rf *  # Clean slate for demo
   ```

2. **Terminal Appearance**
   ```bash
   # For bash prompt, set a clean prompt
   export PS1="$ "
   export PS2="> "

   # Or for a more styled prompt
   export PS1="\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ "
   ```

3. **Resolution Guidelines**
   | Platform | Min Resolution | Recommended |
   |----------|---------------|-------------|
   | GitHub README | 800x600 | 1280x720 |
   | YouTube | 1280x720 | 1920x1080 |
   | Twitter/X | 640x360 | 1280x720 |
   | Presentations | 1920x1080 | 1920x1080 |

### During Recording

1. **Pacing**
   - Pause 2 seconds before each major action
   - Speak clearly and at moderate pace
   - Wait for command completion before moving on

2. **Visibility**
   - Use high contrast colors
   - Increase font size for readability
   - Avoid decorative elements that distract

3. **Audio**
   - Use a quality microphone if narrating
   - Reduce background noise
   - Do a test recording first

### Common Issues and Fixes

| Issue | Solution |
|-------|----------|
| Terminal text too small | Increase font size to 14-16pt |
| Screen looks blurry | Record at higher resolution, scale down |
| Commands execute too fast | Practice pacing, add intentional pauses |
| Color contrast issues | Use high-contrast terminal theme |

---

## Post-Production

### ffmpeg Commands

```bash
# Basic conversion (MOV to MP4)
ffmpeg -i input.mov -c:v libx264 -c:a aac output.mp4

# Compress while maintaining quality
ffmpeg -i input.mp4 -c:v libx264 -crf 23 -preset medium output.mp4

# Add fade in/out
ffmpeg -i input.mp4 -vf "fade=t=in:st=0:d=0.5,fade=t=out:st=29:d=1" output.mp4

# Trim video (keep 00:05 to 00:30)
ffmpeg -i input.mp4 -ss 00:00:05 -to 00:00:30 -c copy output.mp4

# Scale to specific resolution
ffmpeg -i input.mp4 -vf scale=1280:720 -c:v libx264 output.mp4

# Add text overlay
ffmpeg -i input.mp4 -vf "drawtext=text='Ouroboros Demo':fontsize=24:fontcolor=white:x=10:y=10" output.mp4
```

### Creating GIF for README

```bash
# Generate GIF from MP4 (short clips only)
ffmpeg -i input.mp4 -vf "fps=10,scale=640:-1:flags=lanczos" -c:v gif output.gif

# Or use gifsicle for better compression
ffmpeg -i input.mp4 -vf "fps=10,scale=640:-1:flags=lanczos" -f gif - | \
  gifsicle --optimize=3 --delay=10 > output.gif
```

### Automation Script

```bash
#!/bin/bash
# demo-render.sh - Quick render pipeline

INPUT=$1
OUTPUT=${2:-"${INPUT%.*}_final.mp4"}

# 1. Trim first 2 seconds (pre-roll)
# 2. Scale to 720p if needed
# 3. Compress with CRF 23

ffmpeg -i "$INPUT" \
  -ss 00:00:02 \
  -vf "scale=iw*min(1\,720/iw):ih*min(1\,720/ih)" \
  -c:v libx264 -crf 23 -preset medium \
  -c:a aac -b:a 128k \
  -movflags +faststart \
  "$OUTPUT"

echo "Rendered: $OUTPUT"
```

---

## Publishing Checklist

- [ ] Video exported in correct format (MP4, H.264)
- [ ] Resolution matches target platform
- [ ] Audio levels normalized (-16 LUFS recommended)
- [ ] File size optimized (under 50MB for web, under 5MB for GIF)
- [ ] Tested on target platform (GitHub, YouTube, etc.)
- [ ] Thumbnail or poster frame generated
- [ ] Captioning/subtitles added if needed
- [ ] Accessible alternative text provided
