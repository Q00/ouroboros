# Demo Preparation Checklist

Use this checklist before recording any Ouroboros demo video.

## Environment Setup

- [ ] **Clean workspace**: Navigate to a fresh demo directory
  ```bash
  cd ~/demo-workspace
  rm -rf *  # Start clean
  ```

- [ ] **Plugin installed**: Verify Ouroboros is installed
  ```bash
  claude /plugin list | grep ouroboros
  ```

- [ ] **Version check**: Note the version for reference
  ```bash
  claude /plugin info ouroboros
  ```

- [ ] **Terminal configured**: Set optimal appearance
  - Font: SF Mono, MesloLGS NF, or JetBrains Mono (14-16pt)
  - Theme: Dark with high contrast
  - Size: Minimum 1280x720 pixels
  - Hide: Scrollbar, toolbar decorations

- [ ] **Prompt styling**: Clean, minimal prompt
  ```bash
  export PS1="$ "
  ```

## Demo Content

- [ ] **Script reviewed**: Read and rehearse the script from `script.md`
- [ ] **Commands tested**: Run all commands at least once before recording
- [ ] **Expected output verified**: Know what the output should look like
- [ ] **Timing noted**: Plan pauses for key moments (2-3 seconds)
- [ ] **Backup commands ready**: Have recovery commands if something goes wrong

## Recording Software

### For macOS Built-in Recorder
- [ ] Screen capture tested (`Cmd + Shift + 5`)
- [ ] Selected region marked
- [ ] Microphone connected (if narrating)
- [ ] Storage space available (at least 1GB free)

### For OBS Studio
- [ ] Scene configured for terminal window
- [ ] Output settings verified (MP4, H.264, 30fps)
- [ ] Audio levels tested (not clipping)
- [ ] Recording destination set

### For asciinema
- [ ] asciinema installed (`asciinema --version`)
- [ ] Config file set (`~/.config/asciinema/config`)
- [ ] API token authenticated (for uploads)
- [ ] cols/rows configured (120x30 recommended)

## Technical Verification

- [ ] **Quick test run**: Execute the main demo command
  ```bash
  # Example:
  ooo run Create a simple counter component
  ```

- [ ] **Duration confirmed**: Demo fits target duration (30s / 2min / 5min)
- [ ] **Dependencies installed**: All required tools available
- [ ] **Network stable**: For API calls and downloads
- [ ] **Disk space**: At least 5GB free for recording files

## Visual Quality

- [ ] **Code readable**: Font size large enough on recorded resolution
- [ ] **Colors accessible**: High contrast, visible in both light/dark themes
- [ ] **No clutter**: Close unused applications and browser tabs
- [ ] **Desktop clean**: Minimal icons, neutral wallpaper

## Audio (If Narrating)

- [ ] **Microphone tested**: Record sample and listen back
- [ ] **Quiet environment**: Minimize background noise
- [ ] **Script printed**: Have paper copy for reference during recording
- [ ] **Water ready**: Keep water nearby for voice clarity

## Pre-Recording Checklist (Run 5 minutes before)

- [ ] Terminal history cleared (`history -c`)
- [ ] Fresh shell started (`exec bash` or similar)
- [ ] Recording software open and ready
- [ ] Demo commands in muscle memory (practiced 3+ times)
- [ ] Notifications disabled (Do Not Disturb mode)

## During Recording Reminders

- [ ] Pause 2 seconds before typing first command
- [ ] Type deliberately, no typos
- [ ] Wait for output to complete before next action
- [ ] Highlight key moments with verbal emphasis (if narrating)
- [ ] Keep eye contact with camera (if face visible)

## Post-Recording Checklist

- [ ] **Review the footage**: Watch the entire recording
- [ ] **Check audio**: Clear and at consistent level
- [ ] **Verify timing**: Fits within target duration
- [ ] **Export settings correct**: Resolution, format, bitrate
- [ ] **File size reasonable**: Under 50MB for web demos
- [ ] **Backup created**: Keep original recording file

## Publishing Checklist

- [ ] **Filename standardized**: `{demo-type}_v{version}_{date}.{ext}`
- [ ] **Platform-tested**: Uploaded to target platform and verified
- [ ] **Thumbnail prepared**: For video platforms
- [ ] **Description written**: Title, summary, and key points
- [ ] **Metadata added**: Tags, categories, timestamps
- [ ] **Embed code tested**: Works in README or documentation

## Quick Start Checklist (For 30s Demo)

```
[ ] Clean terminal
[ ] Run: ooo run [simple-task]
[ ] Verify completion
[ ] Start screen recording
[ ] Execute same command
[ ] Stop recording
[ ] Review and export
```

## Emergency Recovery Commands

```bash
# If demo goes wrong, quickly reset
clear
history -c
cd ~/demo-workspace && rm -rf *

# Have these ready to paste if needed:
echo "Let me try that again..."
```

## Sign-off

**Demo Type**: _____________________

**Date**: _____________________

**Recorder**: _____________________

**Duration Target**: _____________________

**Status**: [ ] Ready to Record  [ ] Needs Practice  [ ] Complete

---

*Last updated: 2025-01-11*
