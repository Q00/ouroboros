#!/usr/bin/env bash
# produce-demo.sh - Automated demo production for Ouroboros
# Usage: ./produce-demo.sh [quickstart|nextjs|fastapi|debug]

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
OUTPUT_DIR="$SCRIPT_DIR/../output"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# Demo configurations
declare -A DEMO_COMMANDS=(
    ["quickstart"]="Create a simple counter component with increment/decrement buttons"
    ["nextjs"]="Create a Next.js 14 app with shadcn/ui and a dark mode toggle"
    ["fastapi"]="Create a FastAPI project with PostgreSQL, SQLAlchemy, and pytest"
    ["debug"]="Investigate and fix the failing test in tests/api/test_users.py"
)

declare -A DEMO_TITLES=(
    ["quickstart"]="Ouroboros Quick Start Demo"
    ["nextjs"]="Ouroboros Next.js App Demo"
    ["fastapi"]="Ouroboros FastAPI Project Demo"
    ["debug"]="Ouroboros Debugging Demo"
)

# Functions
log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

show_usage() {
    cat << EOF
${BLUE}Ouroboros Demo Production${NC}

Usage: $0 [demo_type]

Demo Types:
  quickstart    30-second Quick Start demo (default)
  nextjs        Next.js app creation demo
  fastapi       FastAPI project creation demo
  debug         Debugging workflow demo

Options:
  -h, --help    Show this help message

Examples:
  $0 quickstart
  $0 nextjs

EOF
}

check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check asciinema
    if ! command -v asciinema &> /dev/null; then
        log_error "asciinema not found. Install with: brew install asciinema"
        exit 1
    fi
    log_success "asciinema found: $(asciinema --version | head -1)"

    # Check ffmpeg (optional)
    if command -v ffmpeg &> /dev/null; then
        log_success "ffmpeg found: $(ffmpeg -version 2>&1 | head -1)"
    else
        log_warning "ffmpeg not found. MP4 conversion will not be available."
        log_warning "Install with: brew install ffmpeg"
    fi

    # Check ooo command
    if ! command -v ooo &> /dev/null; then
        log_error "ooo command not found. Please ensure Ouroboros is installed."
        exit 1
    fi
    log_success "ooo command found"

    # Create output directory
    mkdir -p "$OUTPUT_DIR"
}

prepare_environment() {
    log_info "Preparing environment..."

    # Set terminal size environment variables
    export ASCIINEMA_REC_COLS=120
    export ASCIINEMA_REC_ROWS=30

    # Clean prompt
    export PS1="$ "

    log_success "Environment prepared"
}

record_demo() {
    local demo_type="$1"
    local demo_command="${DEMO_COMMANDS[$demo_type]}"
    local demo_title="${DEMO_TITLES[$demo_type]}"
    local output_file="$OUTPUT_DIR/${demo_type}_${TIMESTAMP}.cast"

    log_info "Recording demo: $demo_type"
    log_info "Command: ooo run $demo_command"
    log_info "Output: $output_file"

    # Create temporary scene script
    local scene_script="/tmp/ouroboros_demo_scene_$$.sh"

    cat > "$scene_script" << SCENE_SCRIPT
#!/bin/bash
# Demo scene for $demo_type

clear
echo "$demo_title"
echo "===================================="
echo ""
sleep 1

echo "Starting demo..."
sleep 1

echo "$ ooo run $demo_command"
sleep 1

# Execute the actual command
ooo run $demo_command

echo ""
echo "Demo complete!"
sleep 1
SCENE_SCRIPT

    chmod +x "$scene_script"

    # Record with asciinema
    log_info "Starting recording... (Press Ctrl+D to stop)"
    sleep 2

    asciinema rec \
        --cols 120 \
        --rows 30 \
        --idle-time-limit 2.0 \
        --command "bash $scene_script" \
        "$output_file"

    # Cleanup
    rm -f "$scene_script"

    log_success "Recording saved to: $output_file"

    # Create symlink to latest
    ln -sf "$(basename "$output_file")" "$OUTPUT_DIR/${demo_type}_latest.cast"
}

convert_to_mp4() {
    local demo_type="$1"
    local cast_file="$OUTPUT_DIR/${demo_type}_latest.cast"
    local mp4_file="$OUTPUT_DIR/${demo_type}_latest.mp4"

    if ! command -v ffmpeg &> /dev/null; then
        log_warning "ffmpeg not available, skipping MP4 conversion"
        return 1
    fi

    if [ ! -f "$cast_file" ]; then
        log_error "CAST file not found: $cast_file"
        return 1
    fi

    log_info "Converting to MP4..."

    # Use asciinema to play and ffmpeg to capture
    asciinema play "$cast_file" | \
        ffmpeg -y -i - \
        -vf "scale=1280:-1:flags=lanczos" \
        -c:v libx264 -preset medium \
        -crf 23 -pix_fmt yuv420p \
        -movflags +faststart \
        "$mp4_file" 2>/dev/null

    if [ $? -eq 0 ]; then
        log_success "MP4 created: $mp4_file"

        # Get file size
        local size=$(du -h "$mp4_file" | cut -f1)
        log_info "File size: $size"

        # Check if under 50MB for web
        local size_bytes=$(stat -f%z "$mp4_file" 2>/dev/null || stat -c%s "$mp4_file" 2>/dev/null)
        if [ "$size_bytes" -gt 52428800 ]; then
            log_warning "File exceeds 50MB. Consider compressing further."
        fi
    else
        log_error "MP4 conversion failed"
        return 1
    fi
}

show_results() {
    local demo_type="$1"
    local cast_file="$OUTPUT_DIR/${demo_type}_latest.cast"
    local mp4_file="$OUTPUT_DIR/${demo_type}_latest.mp4"

    echo ""
    log_success "Demo production complete!"
    echo ""
    echo "Output files:"
    echo "  CAST:  $cast_file"
    if [ -f "$mp4_file" ]; then
        echo "  MP4:   $mp4_file"
    fi
    echo ""
    echo "Next steps:"
    echo "  1. Review the demo: asciinema play $cast_file"
    if [ -f "$mp4_file" ]; then
        echo "  2. View the MP4: open $mp4_file"
    fi
    echo "  3. Upload to asciinema.org: asciinema upload $cast_file"
    echo ""
}

# Main
main() {
    local demo_type="${1:-quickstart}"

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help)
                show_usage
                exit 0
                ;;
            *)
                demo_type="$1"
                shift
                ;;
        esac
    done

    # Validate demo type
    if [[ ! -v "DEMO_COMMANDS[$demo_type]" ]]; then
        log_error "Unknown demo type: $demo_type"
        show_usage
        exit 1
    fi

    echo -e "${BLUE}"
    cat << "EOF"
╔════════════════════════════════════════════╗
║     Ouroboros Demo Production Script      ║
║                                            ║
║  Automated demo recording and conversion   ║
╚════════════════════════════════════════════╝
EOF
    echo -e "${NC}"

    check_prerequisites
    prepare_environment
    record_demo "$demo_type"
    convert_to_mp4 "$demo_type"
    show_results "$demo_type"
}

main "$@"
