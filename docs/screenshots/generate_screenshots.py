#!/usr/bin/env python3
"""Generate realistic TUI screenshots for Ouroboros documentation.

This script creates PNG images that simulate the TUI interface using Pillow.
Since we can't capture actual terminal screenshots in a headless environment,
we generate visual representations that match the TUI design.
"""

from __future__ import annotations

import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from dataclasses import dataclass
from typing import NamedTuple

# ═══════════════════════════════════════════════════════════════════════════════
# COLOR PALETTE (Catppuccin Mocha-inspired)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ColorPalette:
    """Terminal color palette for screenshots."""
    background: tuple[int, int, int] = (30, 30, 46)      # #1e1e2e
    surface: tuple[int, int, int] = (24, 24, 37)         # #181825
    panel: tuple[int, int, int] = (49, 50, 68)           # #313244
    primary: tuple[int, int, int] = (137, 180, 250)      # #89b4fa
    text: tuple[int, int, int] = (205, 214, 244)         # #cdd6f4
    text_muted: tuple[int, int, int] = (137, 137, 167)   # #89898a3
    success: tuple[int, int, int] = (166, 227, 161)      # #a6e3a1
    warning: tuple[int, int, int] = (249, 226, 175)      # #f9e2af
    error: tuple[int, int, int] = (243, 139, 168)        # #f38ba8
    border: tuple[int, int, int] = (69, 71, 90)          # #45475a
    accent: tuple[int, int, int] = (116, 199, 236)       # #74c7ec

COLORS = ColorPalette()


# ═══════════════════════════════════════════════════════════════════════════════
# TEXT RENDERING UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

class TextRenderer:
    """Handles text rendering with monospace fonts."""

    # Character cell dimensions (approximate for monospace fonts)
    CELL_WIDTH = 10
    CELL_HEIGHT = 18
    LINE_HEIGHT = 22
    PADDING = 16

    def __init__(self, image: Image.Image, draw: ImageDraw.ImageDraw):
        """Initialize renderer with PIL image and draw objects."""
        self.image = image
        self.draw = draw

        # Try to load a monospace font, fall back to default
        try:
            self.font_regular = ImageFont.truetype(
                "/System/Library/Fonts/Menlo.ttc", 14
            )
            self.font_bold = ImageFont.truetype(
                "/System/Library/Fonts/Menlo.ttc", 14
            )
            self.font_small = ImageFont.truetype(
                "/System/Library/Fonts/Menlo.ttc", 12
            )
        except Exception:
            self.font_regular = ImageFont.load_default()
            self.font_bold = ImageFont.load_default()
            self.font_small = ImageFont.load_default()

    def draw_text(
        self,
        x: int,
        y: int,
        text: str,
        color: tuple[int, int, int] = COLORS.text,
        font: ImageFont.ImageFont | None = None,
    ) -> int:
        """Draw text at position and return y offset for next line."""
        font = font or self.font_regular
        self.draw.text((x, y), text, fill=color, font=font)
        return y + self.LINE_HEIGHT

    def draw_line(self, y: int, color: tuple[int, int, int] = COLORS.border) -> None:
        """Draw a horizontal line."""
        width, _ = self.image.size
        self.draw.line([(self.PADDING, y), (width - self.PADDING, y)], fill=color, width=1)

    def draw_box(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        fill: tuple[int, int, int] | None = None,
        outline: tuple[int, int, int] = COLORS.border,
        border_width: int = 1,
    ) -> None:
        """Draw a rectangle box."""
        if fill:
            self.draw.rectangle([x, y, x + width, y + height], fill=fill, outline=outline, width=border_width)
        else:
            self.draw.rectangle([x, y, x + width, y + height], outline=outline, width=border_width)

    def draw_header(self, title: str, subtitle: str = "") -> int:
        """Draw application header bar."""
        height = 50
        self.draw_box(0, 0, self.image.width, height, fill=COLORS.panel, outline=COLORS.border)

        y = 16
        x = self.PADDING
        self.draw.text((x, y), title, fill=COLORS.primary, font=self.font_bold)
        if subtitle:
            self.draw.text((x + 200, y), subtitle, fill=COLORS.text_muted, font=self.font_small)

        return height

    def draw_footer(self, bindings: list[tuple[str, str]]) -> int:
        """Draw footer with key bindings."""
        height = 40
        y = self.image.height - height

        self.draw_box(0, y, self.image.width, height, fill=COLORS.panel, outline=COLORS.border)

        x = self.PADDING
        y_offset = y + 12
        for key, action in bindings:
            text = f"{key}: {action}"
            self.draw.text((x, y_offset), text, fill=COLORS.text_muted, font=self.font_small)
            x += 120

        return height


# ═══════════════════════════════════════════════════════════════════════════════
# SCREEN GENERATORS
# ═══════════════════════════════════════════════════════════════════════════════

def create_base_image(width: int = 1200, height: int = 800) -> tuple[Image.Image, ImageDraw.ImageDraw, TextRenderer]:
    """Create a base image with background color."""
    image = Image.new("RGB", (width, height), COLORS.background)
    draw = ImageDraw.Draw(image)
    renderer = TextRenderer(image, draw)
    return image, draw, renderer


def generate_dashboard() -> Image.Image:
    """Generate the main TUI dashboard screenshot."""
    image, draw, renderer = create_base_image()

    # Header
    footer_h = renderer.draw_header("Ouroboros TUI", "Workflow Monitor")

    # Double Diamond Phase Bar
    y = footer_h + 20
    renderer.draw_box(renderer.PADDING, y, image.width - 2 * renderer.PADDING, 40, fill=COLORS.surface)
    phase_text = "  Discover  →  Define  →  Design  →  Deliver"
    renderer.draw.text((renderer.PADDING + 20, y + 10), phase_text, fill=COLORS.text_muted, font=renderer.font_small)

    # Main content area split
    content_y = y + 60
    panel_width = (image.width - 3 * renderer.PADDING) // 2
    panel_height = image.height - content_y - footer_h - renderer.PADDING

    # Left Panel - AC Tree
    renderer.draw_box(
        renderer.PADDING,
        content_y,
        panel_width,
        panel_height,
        fill=COLORS.surface,
        outline=COLORS.primary,
        border_width=2,
    )
    renderer.draw.text((renderer.PADDING + 16, content_y + 16), "AC EXECUTION TREE", fill=COLORS.primary, font=renderer.font_bold)

    # Tree content
    tree_x = renderer.PADDING + 40
    tree_y = content_y + 50
    tree_items = [
        (" Seed", COLORS.primary),
        ("  AC1: Initialize project", COLORS.warning),
        ("    SubAC1: Create directory", COLORS.success),
        ("    SubAC2: Setup config", COLORS.success),
        ("  AC2: Implement core logic", COLORS.text),
        ("    SubAC1: Define models", COLORS.warning),
        ("    SubAC2: Add validation", COLORS.text_muted),
        ("  AC3: Write tests", COLORS.text),
        ("    SubAC1: Unit tests", COLORS.text_muted),
        ("    SubAC2: Integration tests", COLORS.text_muted),
        ("  AC4: Documentation", COLORS.text_muted),
    ]

    for text, color in tree_items:
        tree_y = renderer.draw_text(tree_x, tree_y, text, color, renderer.font_regular)

    # Right Panel - Node Detail
    right_x = renderer.PADDING * 2 + panel_width
    renderer.draw_box(
        right_x,
        content_y,
        panel_width,
        panel_height,
        fill=COLORS.surface,
        outline=COLORS.primary,
        border_width=2,
    )
    renderer.draw.text((right_x + 16, content_y + 16), "NODE DETAIL", fill=COLORS.primary, font=renderer.font_bold)

    # Detail content
    detail_x = right_x + 24
    detail_y = content_y + 60
    details = [
        ("ID:", "ac_1_subac2", COLORS.text),
        ("Status:", "Executing", COLORS.warning),
        ("Depth:", "2", COLORS.text),
        ("", "", COLORS.text),
        ("Content:", "", COLORS.primary),
        ("Setup configuration with", COLORS.text_muted),
        ("environment variables and", COLORS.text_muted),
        ("database connection...", COLORS.text_muted),
    ]

    for label, value, color in details:
        if label:
            renderer.draw.text((detail_x, detail_y), label, fill=COLORS.text_muted, font=renderer.font_small)
            renderer.draw.text((detail_x + 80, detail_y), value, fill=color, font=renderer.font_regular)
        else:
            renderer.draw.text((detail_x, detail_y), value, fill=color, font=renderer.font_regular)
        detail_y += renderer.LINE_HEIGHT

    # Footer
    bindings = [("q", "Quit"), ("p", "Pause"), ("r", "Resume"), ("d", "Debug"), ("l", "Logs")]
    renderer.draw_footer(bindings)

    return image


def generate_interview() -> Image.Image:
    """Generate the interview mode screenshot."""
    image, draw, renderer = create_base_image()

    # Header
    footer_h = renderer.draw_header("Ouroboros Interview", "Big Bang Phase")

    # Main content
    content_y = footer_h + 30

    # Progress section
    renderer.draw.text((renderer.PADDING, content_y), "Round 3", fill=COLORS.primary, font=renderer.font_bold)
    renderer.draw.text((renderer.PADDING + 100, content_y), "Interview Session: interview_20250211_120000", fill=COLORS.text_muted, font=renderer.font_small)

    content_y += 40

    # Question section
    renderer.draw_box(
        renderer.PADDING,
        content_y,
        image.width - 2 * renderer.PADDING,
        120,
        fill=COLORS.surface,
        outline=COLORS.primary,
        border_width=2,
    )
    renderer.draw.text((renderer.PADDING + 16, content_y + 16), "Q:", fill=COLORS.warning, font=renderer.font_bold)
    question = "What specific features should the task management system support?"
    content_y += 50
    renderer.draw.text((renderer.PADDING + 16, content_y), question, fill=COLORS.text, font=renderer.font_regular)

    # Input section
    content_y += 150
    renderer.draw_box(
        renderer.PADDING,
        content_y,
        image.width - 2 * renderer.PADDING,
        80,
        fill=COLORS.panel,
        outline=COLORS.border,
    )
    renderer.draw.text((renderer.PADDING + 16, content_y + 16), "Your response:", fill=COLORS.text_muted, font=renderer.font_small)
    renderer.draw.text((renderer.PADDING + 16, content_y + 40), "Tasks should support: create, read, update, delete operations...", fill=COLORS.text, font=renderer.font_regular)

    # Info section
    content_y += 120
    info_lines = [
        "Press Enter to submit your response",
        "Press Ctrl+C to exit and save progress",
        "",
        "Context: Building a task management CLI tool",
    ]
    for line in info_lines:
        content_y = renderer.draw_text(renderer.PADDING, content_y, line, COLORS.text_muted, renderer.font_small)

    # Footer
    bindings = [("Ctrl+C", "Exit"), ("Enter", "Submit"), ("Ctrl+J", "Newline")]
    renderer.draw_footer(bindings)

    return image


def generate_seed() -> Image.Image:
    """Generate the seed mode screenshot."""
    image, draw, renderer = create_base_image()

    # Header
    footer_h = renderer.draw_header("Ouroboros Seed Generator", "Specification Crystallization")

    # Main content
    content_y = footer_h + 30

    # Title
    renderer.draw.text((renderer.PADDING, content_y), "Generating Seed Specification", fill=COLORS.primary, font=renderer.font_bold)
    content_y += 40

    # Ambiguity Score
    renderer.draw_box(
        renderer.PADDING,
        content_y,
        image.width - 2 * renderer.PADDING,
        60,
        fill=COLORS.surface,
        outline=COLORS.success,
        border_width=2,
    )
    renderer.draw.text((renderer.PADDING + 16, content_y + 16), "Ambiguity Score:", fill=COLORS.text_muted, font=renderer.font_small)
    renderer.draw.text((renderer.PADDING + 150, content_y + 16), "0.18 / 1.0", fill=COLORS.success, font=renderer.font_bold)
    renderer.draw.text((renderer.PADDING + 16, content_y + 38), "Status: Ready for seed generation", fill=COLORS.success, font=renderer.font_small)

    content_y += 80

    # Seed preview
    renderer.draw_box(
        renderer.PADDING,
        content_y,
        image.width - 2 * renderer.PADDING,
        300,
        fill=COLORS.panel,
        outline=COLORS.border,
    )
    renderer.draw.text((renderer.PADDING + 16, content_y + 16), "SEED PREVIEW", fill=COLORS.primary, font=renderer.font_bold)

    preview_y = content_y + 50
    preview_lines = [
        ("goal:", COLORS.primary),
        ("  Build a CLI task management tool", COLORS.text),
        ("", COLORS.text),
        ("constraints:", COLORS.primary),
        ("  - Python 3.14+", COLORS.text),
        ("  - No external database", COLORS.text),
        ("  - SQLite for persistence", COLORS.text),
        ("", COLORS.text),
        ("acceptance_criteria:", COLORS.primary),
        ("  - Tasks can be created", COLORS.text),
        ("  - Tasks can be listed", COLORS.text),
        ("  - Tasks can be marked complete", COLORS.text),
    ]

    for line, color in preview_lines:
        preview_y = renderer.draw_text(renderer.PADDING + 24, preview_y, line, color, renderer.font_regular)

    # Footer
    bindings = [("Enter", "Confirm"), ("n", "New interview"), ("q", "Quit")]
    renderer.draw_footer(bindings)

    return image


def generate_evaluate() -> Image.Image:
    """Generate the evaluation mode screenshot."""
    image, draw, renderer = create_base_image()

    # Header
    footer_h = renderer.draw_header("Ouroboros Evaluation", "3-Stage Verification Pipeline")

    # Main content
    content_y = footer_h + 30

    # Final result
    renderer.draw_box(
        renderer.PADDING,
        content_y,
        image.width - 2 * renderer.PADDING,
        80,
        fill=COLORS.surface,
        outline=COLORS.success,
        border_width=3,
    )
    renderer.draw.text((renderer.PADDING + 24, content_y + 20), "Final Approval:", fill=COLORS.text, font=renderer.font_bold)
    renderer.draw.text((renderer.PADDING + 160, content_y + 20), "APPROVED", fill=COLORS.success, font=renderer.font_bold)
    renderer.draw.text((renderer.PADDING + 24, content_y + 50), "Highest Stage Completed: 2 (Semantic Evaluation)", fill=COLORS.text_muted, font=renderer.font_small)

    content_y += 110

    # Stage 1
    renderer.draw_box(
        renderer.PADDING,
        content_y,
        image.width - 2 * renderer.PADDING,
        100,
        fill=COLORS.panel,
        outline=COLORS.border,
    )
    renderer.draw.text((renderer.PADDING + 16, content_y + 16), "Stage 1: Mechanical Verification", fill=COLORS.primary, font=renderer.font_bold)
    renderer.draw.text((renderer.PADDING + 350, content_y + 16), "[PASS]", fill=COLORS.success, font=renderer.font_bold)

    stage1_y = content_y + 45
    stage1_items = [
        ("[PASS] lint:", "No issues found"),
        ("[PASS] build:", "Build successful"),
        ("[PASS] test:", "12/12 tests passing"),
        ("[PASS] coverage:", "85% code coverage"),
    ]
    for status, detail in stage1_items:
        renderer.draw.text((renderer.PADDING + 32, stage1_y), status, fill=COLORS.success, font=renderer.font_small)
        renderer.draw.text((renderer.PADDING + 140, stage1_y), detail, fill=COLORS.text, font=renderer.font_small)
        stage1_y += 20

    content_y += 120

    # Stage 2
    renderer.draw_box(
        renderer.PADDING,
        content_y,
        image.width - 2 * renderer.PADDING,
        120,
        fill=COLORS.panel,
        outline=COLORS.border,
    )
    renderer.draw.text((renderer.PADDING + 16, content_y + 16), "Stage 2: Semantic Evaluation", fill=COLORS.primary, font=renderer.font_bold)
    renderer.draw.text((renderer.PADDING + 350, content_y + 16), "[PASS]", fill=COLORS.success, font=renderer.font_bold)

    stage2_y = content_y + 45
    stage2_items = [
        ("Score:", "0.85", COLORS.text),
        ("AC Compliance:", "YES", COLORS.success),
        ("Goal Alignment:", "0.90", COLORS.text),
        ("Drift Score:", "0.08", COLORS.success),
    ]
    for label, value, color in stage2_items:
        renderer.draw.text((renderer.PADDING + 32, stage2_y), label, fill=COLORS.text_muted, font=renderer.font_small)
        renderer.draw.text((renderer.PADDING + 140, stage2_y), value, fill=color, font=renderer.font_small)
        stage2_y += 20

    # Footer
    bindings = [("q", "Quit"), ("r", "Re-evaluate"), ("v", "View details")]
    renderer.draw_footer(bindings)

    return image


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Generate all screenshots."""
    output_dir = Path(__file__).parent

    print("Generating Ouroboros TUI screenshots...")
    print(f"Output directory: {output_dir}")
    print()

    generators = [
        ("dashboard.png", generate_dashboard, "Main TUI Dashboard"),
        ("interview.png", generate_interview, "Interview Mode"),
        ("seed.png", generate_seed, "Seed Generation"),
        ("evaluate.png", generate_evaluate, "Evaluation Results"),
    ]

    for filename, generator, description in generators:
        output_path = output_dir / filename
        print(f"Generating {description}...", end=" ")

        try:
            image = generator()
            image.save(output_path, "PNG", optimize=True)
            print(f"[OK] {output_path}")
        except Exception as e:
            print(f"[FAILED] {e}")

    print()
    print("Screenshot generation complete!")
    print()
    print("Generated files:")
    for filename, _, description in generators:
        output_path = output_dir / filename
        if output_path.exists():
            size = output_path.stat().st_size
            print(f"  - {filename} ({size:,} bytes) - {description}")


if __name__ == "__main__":
    main()
