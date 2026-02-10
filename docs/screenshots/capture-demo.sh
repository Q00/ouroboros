#!/bin/bash
# Ouroboros TUI Screenshot Capture Script
#
# This script helps capture screenshots of the TUI for documentation.
# It uses terminal recording tools and extracts frames as images.
#
# Usage:
#   ./capture-demo.sh              # Capture all screenshots
#   ./capture-demo.sh dashboard    # Capture specific screenshot
#
# Requirements (one of):
#   - terminalizer (npm install -g terminalizer)
#   - asciinema (brew install asciinema)
#   - screencapture (macOS built-in)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCREENSHOTS_DIR="$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check for screenshot tools
check_tools() {
    local tools_available=()

    if command -v terminalizer &> /dev/null; then
        tools_available+=("terminalizer")
    fi

    if command -v asciinema &> /dev/null; then
        tools_available+=("asciinema")
    fi

    if [[ "$OSTYPE" == "darwin"* ]]; then
        tools_available+=("screencapture")
    fi

    if [ ${#tools_available[@]} -eq 0 ]; then
        echo -e "${RED}Error: No screenshot tools found${NC}"
        echo "Install one of:"
        echo "  - terminalizer: npm install -g terminalizer"
        echo "  - asciinema:   brew install asciinema"
        echo "  - Or use macOS built-in screencapture"
        exit 1
    fi

    echo -e "${GREEN}Available tools: ${tools_available[*]}${NC}"
}

# Wait for user to prepare window
wait_for_window() {
    local window_name="$1"
    echo ""
    echo -e "${YELLOW}→ Prepare your terminal window:${NC}"
    echo "  1. Open a new terminal window"
    echo "  2. Set window size to 1200x800 or similar"
    echo "  3. Navigate to: cd $PROJECT_ROOT"
    echo "  4. Run the command below when ready"
    echo ""
    echo -e "${GREEN}Command to run:${NC} $2"
    echo ""
    read -p "Press Enter when window is ready and TUI is visible..."
}

# Capture using macOS screencapture (most reliable)
capture_screenshot_macos() {
    local output_file="$1"
    local prompt="$2"

    echo ""
    echo -e "${YELLOW}→ Screenshot Capture${NC}"
    echo "  Output: $output_file"
    echo "  $prompt"
    echo ""
    echo "Steps:"
    echo "  1. Run the TUI command in a separate terminal"
    echo "  2. Position the window as desired"
    echo "  3. Press Cmd+Shift+4, then Space (window capture mode)"
    echo "  4. Click the terminal window"
    echo ""

    read -p "Press Enter after taking the screenshot, or 's' to skip: " response
    if [[ "$response" == "s" ]]; then
        echo -e "${YELLOW}Skipped${NC}"
        return 1
    fi

    # Check if file exists
    if [ -f "$HOME/Desktop/$output_file" ]; then
        mv "$HOME/Desktop/$output_file" "$SCREENSHOTS_DIR/$output_file"
        echo -e "${GREEN}✓ Moved screenshot to $SCREENSHOTS_DIR/$output_file${NC}"
    elif [ -f "$SCREENSHOTS_DIR/$output_file" ]; then
        echo -e "${GREEN}✓ Screenshot exists at $SCREENSHOTS_DIR/$output_file${NC}"
    else
        echo -e "${YELLOW}⚠ Screenshot not found. Please move it manually to $SCREENSHOTS_DIR/$output_file${NC}"
    fi
}

# Capture dashboard screenshot
capture_dashboard() {
    echo -e "\n${GREEN}=== Capturing Dashboard ===${NC}"
    capture_screenshot_macos "dashboard.png" "Show the main TUI dashboard with workflow tree"
}

# Capture interview screenshot
capture_interview() {
    echo -e "\n${GREEN}=== Capturing Interview Mode ===${NC}"
    capture_screenshot_macos "interview.png" "Run 'uv run python -m ouroboros.cli interview' and answer a question"
}

# Capture seed screenshot
capture_seed() {
    echo -e "\n${GREEN}=== Capturing Seed Mode ===${NC}"
    capture_screenshot_macos "seed.png" "Run 'uv run python -m ouroboros.cli seed' and navigate through prompts"
}

# Capture evaluate screenshot
capture_evaluate() {
    echo -e "\n${GREEN}=== Capturing Evaluate Mode ===${NC}"
    capture_screenshot_macos "evaluate.png" "Run 'uv run python -m ouroboros.cli evaluate' and show results"
}

# Main execution
main() {
    check_tools

    local target="${1:-all}"

    case "$target" in
        dashboard)
            capture_dashboard
            ;;
        interview)
            capture_interview
            ;;
        seed)
            capture_seed
            ;;
        evaluate)
            capture_evaluate
            ;;
        all)
            capture_dashboard
            capture_interview
            capture_seed
            capture_evaluate
            ;;
        *)
            echo -e "${RED}Unknown target: $target${NC}"
            echo "Usage: $0 [dashboard|interview|seed|evaluate|all]"
            exit 1
            ;;
    esac

    echo -e "\n${GREEN}=== Screenshot capture complete ===${NC}"
    echo "Files in $SCREENSHOTS_DIR:"
    ls -lh "$SCREENSHOTS_DIR"/*.png 2>/dev/null || echo "No PNG files found"
}

main "$@"
