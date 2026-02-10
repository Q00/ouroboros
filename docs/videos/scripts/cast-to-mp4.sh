#!/usr/bin/env bash
# cast-to-mp4.sh - Convert asciinema cast files to MP4 video
# Usage: ./cast-to-mp4.sh input.cast [output.mp4]

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

show_usage() {
    cat << EOF
Convert asciinema CAST files to MP4 video.

Usage: $0 <input.cast> [output.mp4]

Arguments:
  input.cast    Path to input asciinema CAST file
  output.mp4    Path to output MP4 file (default: input.mp4)

Options:
  -h, --help    Show this help message
  -q, --quality QUALITY    Output quality (low|medium|high, default: medium)

Examples:
  $0 demo.cast
  $0 demo.cast demo.mp4
  $0 demo.cast -q high

EOF
}

check_dependencies() {
    if ! command -v ffmpeg &> /dev/null; then
        log_error "ffmpeg not found. Install with: brew install ffmpeg"
        exit 1
    fi

    if ! command -v asciinema &> /dev/null; then
        log_error "asciinema not found. Install with: brew install asciinema"
        exit 1
    fi
}

# Quality presets
declare -A QUALITY_PRESETS=(
    ["low"]="23"
    ["medium"]="23"
    ["high"]="18"
)

# Resolution presets
declare -A RESOLUTION_PRESETS=(
    ["low"]="960"
    ["medium"]="1280"
    ["high"]="1920"
)

convert_cast() {
    local input_file="$1"
    local output_file="$2"
    local quality="${3:-medium}"

    # Verify input file exists
    if [ ! -f "$input_file" ]; then
        log_error "Input file not found: $input_file"
        exit 1
    fi

    # Set default output filename
    if [ -z "$output_file" ]; then
        output_file="${input_file%.*}.mp4"
    fi

    # Get quality settings
    local crf="${QUALITY_PRESETS[$quality]}"
    local width="${RESOLUTION_PRESETS[$quality]}"
    local preset="medium"
    if [ "$quality" = "high" ]; then
        preset="slow"
    fi

    log_info "Converting: $input_file -> $output_file"
    log_info "Quality: $quality (CRF: $crf, Width: ${width}px)"

    # Convert using asciinema play piped to ffmpeg
    asciinema play "$input_file" | \
        ffmpeg -y -i - \
        -vf "scale=${width}:-1:flags=lanczos" \
        -c:v libx264 \
        -preset "$preset" \
        -crf "$crf" \
        -pix_fmt yuv420p \
        -movflags +faststart \
        "$output_file" < /dev/null

    if [ $? -eq 0 ]; then
        log_success "Conversion complete!"

        # Show file info
        local size=$(du -h "$output_file" | cut -f1)
        local duration=$(ffprobe -v error -show_entries format=duration \
            -of default=noprint_wrappers=1:nokey=1 "$output_file" 2>/dev/null)

        echo ""
        echo "Output details:"
        echo "  File:     $output_file"
        echo "  Size:     $size"
        echo "  Duration: ${duration}s"
    else
        log_error "Conversion failed"
        exit 1
    fi
}

# Main
main() {
    local input_file=""
    local output_file=""
    local quality="medium"

    while [[ $# -gt 0 ]]; do
        case $1 in
            -h|--help)
                show_usage
                exit 0
                ;;
            -q|--quality)
                quality="$2"
                shift 2
                ;;
            -*)
                log_error "Unknown option: $1"
                show_usage
                exit 1
                ;;
            *)
                if [ -z "$input_file" ]; then
                    input_file="$1"
                elif [ -z "$output_file" ]; then
                    output_file="$1"
                else
                    log_error "Too many arguments"
                    show_usage
                    exit 1
                fi
                shift
                ;;
        esac
    done

    if [ -z "$input_file" ]; then
        log_error "Missing input file"
        show_usage
        exit 1
    fi

    check_dependencies
    convert_cast "$input_file" "$output_file" "$quality"
}

main "$@"
