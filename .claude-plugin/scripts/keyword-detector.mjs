#!/usr/bin/env node

/**
 * Magic Keyword Detector for Ouroboros
 *
 * Detects trigger keywords in user prompts and suggests
 * the appropriate Ouroboros skill to invoke.
 *
 * Hook: UserPromptSubmit
 * Input: User prompt text via stdin (piped by Claude Code)
 * Output: JSON with detection result
 */

import { readFileSync } from "fs";

// Keyword → skill mapping
const KEYWORD_MAP = [
  { patterns: ["interview me", "clarify requirements", "clarify my requirements"], skill: "/ouroboros:interview" },
  { patterns: ["crystallize", "generate seed", "create seed"], skill: "/ouroboros:seed" },
  { patterns: ["ouroboros run", "execute seed", "run seed"], skill: "/ouroboros:run" },
  { patterns: ["evaluate this", "3-stage check", "three-stage"], skill: "/ouroboros:evaluate" },
  { patterns: ["think sideways", "i'm stuck", "im stuck", "i am stuck"], skill: "/ouroboros:unstuck" },
  { patterns: ["am i drifting", "drift check", "session status"], skill: "/ouroboros:status" },
  { patterns: ["ouroboros setup", "setup ouroboros"], skill: "/ouroboros:setup" },
  { patterns: ["ouroboros help"], skill: "/ouroboros:help" },
];

function detectKeywords(text) {
  const lower = text.toLowerCase().trim();

  for (const entry of KEYWORD_MAP) {
    for (const pattern of entry.patterns) {
      if (lower.includes(pattern)) {
        return {
          detected: true,
          keyword: pattern,
          suggested_skill: entry.skill,
        };
      }
    }
  }

  return {
    detected: false,
    keyword: null,
    suggested_skill: null,
  };
}

// Read user prompt from stdin (non-blocking)
let input = "";
try {
  input = readFileSync(0, "utf-8").trim();
} catch {
  // No stdin available (e.g., dry run) - exit cleanly
}

const result = detectKeywords(input);

// Output result as JSON for Claude Code hook consumption
if (result.detected) {
  console.log(`[MAGIC KEYWORD: ${result.suggested_skill}] Detected "${result.keyword}"`);
} else {
  // No output when no keyword detected - keeps hook output clean
  console.log("Success");
}
