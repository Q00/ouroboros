#!/bin/bash
# MCP Progress Visualization Demo Recording Script

set -e

echo "============================================"
echo "MCP Progress Visualization Demo"
echo "============================================"
echo ""
echo "This demo will show:"
echo "  1. Real-time AC status tracking (⏳ → 🔄 → ✅)"
echo "  2. Session metrics (cost, tokens, duration)"
echo "  3. Live progress bar"
echo ""
echo "Press Enter to start recording..."
read

# Start asciinema recording
asciinema rec mcp_progress_demo.cast \
    --overwrite \
    --title "Ouroboros MCP - Real-time Progress Visualization" \
    --command "bash -c '
        cd /Users/seung-gali/Documents/code/ouroboros

        echo \"═══════════════════════════════════════════════════\"
        echo \"   Ouroboros MCP - Execute Seed with Progress\"
        echo \"═══════════════════════════════════════════════════\"
        echo \"\"

        echo \"📋 Seed file: demo_seed.yaml\"
        echo \"🎯 Goal: Create a simple hello world Python script\"
        echo \"✅ Acceptance Criteria: 4\"
        echo \"\"

        sleep 2

        echo \"Starting execution with real-time progress tracking...\"
        echo \"\"

        sleep 1

        # Execute the seed with orchestrator
        # This will show the Rich progress display
        ~/.local/bin/uv run ouroboros run workflow --orchestrator demo_seed.yaml

        echo \"\"
        echo \"✅ Demo completed!\"
        echo \"\"
        echo \"Key features shown:\"
        echo \"  • Real-time AC status updates\"
        echo \"  • Live session metrics (cost/tokens)\"
        echo \"  • Progress bar visualization\"
        echo \"  • Event-driven updates at 4Hz\"
    '"

echo ""
echo "============================================"
echo "Recording saved to: mcp_progress_demo.cast"
echo "============================================"
echo ""
echo "To replay: asciinema play mcp_progress_demo.cast"
echo "To upload: asciinema upload mcp_progress_demo.cast"
echo ""
