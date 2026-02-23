#!/usr/bin/env node

/**
 * Magic Keyword Detector for Ouroboros
 *
 * Detects trigger keywords in user prompts and suggests
 * the appropriate Ouroboros skill to invoke.
 *
 * IMPORTANT: If MCP is not configured (ooo setup not run),
 * ALL ooo commands (except setup/help) redirect to setup first.
 *
 * Hook: UserPromptSubmit
 * Input: User prompt text via stdin (piped by Claude Code)
 * Output: JSON with detection result
 */

import { readFileSync, existsSync } from "fs";
import { join } from "path";
import { homedir } from "os";

// Skills that work without MCP setup (bypass the setup gate)
const SETUP_BYPASS_SKILLS = ["/ouroboros:setup", "/ouroboros:help"];

// Keyword → skill mapping
// "ooo <cmd>" prefix always works; natural language keywords also supported
const KEYWORD_MAP = [
  // ooo prefix shortcuts (checked first for priority)
  { patterns: ["ooo interview", "ooo socratic"], skill: "/ouroboros:interview" },
  { patterns: ["ooo seed", "ooo crystallize"], skill: "/ouroboros:seed" },
  { patterns: ["ooo run", "ooo execute"], skill: "/ouroboros:run" },
  { patterns: ["ooo eval", "ooo evaluate"], skill: "/ouroboros:evaluate" },
  { patterns: ["ooo evolve"], skill: "/ouroboros:evolve" },
  { patterns: ["ooo stuck", "ooo unstuck", "ooo lateral"], skill: "/ouroboros:unstuck" },
  { patterns: ["ooo status", "ooo drift"], skill: "/ouroboros:status" },
  { patterns: ["ooo ralph"], skill: "/ouroboros:ralph" },
  { patterns: ["ooo tutorial"], skill: "/ouroboros:tutorial" },
  { patterns: ["ooo welcome"], skill: "/ouroboros:welcome" },
  { patterns: ["ooo setup"], skill: "/ouroboros:setup" },
  { patterns: ["ooo help"], skill: "/ouroboros:help" },
  // Natural language triggers
  { patterns: ["interview me", "clarify requirements", "clarify my requirements", "socratic interview", "socratic questioning"], skill: "/ouroboros:interview" },
  { patterns: ["crystallize", "generate seed", "create seed", "freeze requirements"], skill: "/ouroboros:seed" },
  { patterns: ["ouroboros run", "execute seed", "run seed", "run workflow"], skill: "/ouroboros:run" },
  { patterns: ["evaluate this", "3-stage check", "three-stage", "verify execution"], skill: "/ouroboros:evaluate" },
  { patterns: ["evolve", "evolutionary loop", "iterate until converged"], skill: "/ouroboros:evolve" },
  { patterns: ["think sideways", "i'm stuck", "im stuck", "i am stuck", "break through", "lateral thinking"], skill: "/ouroboros:unstuck" },
  { patterns: ["am i drifting", "drift check", "session status", "check drift", "goal deviation"], skill: "/ouroboros:status" },
  { patterns: ["ralph", "don't stop", "must complete", "until it works", "keep going"], skill: "/ouroboros:ralph" },
  { patterns: ["ouroboros setup", "setup ouroboros"], skill: "/ouroboros:setup" },
  { patterns: ["ouroboros help"], skill: "/ouroboros:help" },
];

/**
 * Check if MCP server is registered in ~/.claude/mcp.json
 */
function isMcpConfigured() {
  try {
    const mcpPath = join(homedir(), ".claude", "mcp.json");
    if (!existsSync(mcpPath)) return false;
    const content = readFileSync(mcpPath, "utf-8");
    return content.includes("ouroboros");
  } catch {
    return false;
  }
}

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

  // Bare "ooo" (with no subcommand) → welcome (first-touch experience)
  if (lower === "ooo" || lower === "ooo?") {
    return {
      detected: true,
      keyword: "ooo",
      suggested_skill: "/ouroboros:welcome",
    };
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

// Check if this is the user's first interaction (welcome not yet shown)
function isFirstTime() {
  try {
    const prefsPath = join(homedir(), ".ouroboros", "prefs.json");
    if (!existsSync(prefsPath)) return true;
    const prefs = JSON.parse(readFileSync(prefsPath, "utf-8"));
    return !prefs.welcome_shown;
  } catch {
    return true;
  }
}

const result = detectKeywords(input);

// First-time user: trigger welcome on their first message (unless they typed an ooo command)
if (!result.detected && isFirstTime()) {
  console.log(
    `[MAGIC KEYWORD: /ouroboros:welcome] First time using Ouroboros! Starting welcome experience.`
  );
  process.exit(0);
}

// Output result as JSON for Claude Code hook consumption
if (result.detected) {
  // Gate check: if MCP not configured and skill requires it, redirect to setup
  if (!SETUP_BYPASS_SKILLS.includes(result.suggested_skill) && !isMcpConfigured()) {
    console.log(
      `[MAGIC KEYWORD: /ouroboros:setup] Ouroboros setup required. ` +
      `Run "ooo setup" first to register the MCP server. ` +
      `(You tried: "${result.keyword}" → ${result.suggested_skill})`
    );
  } else {
    console.log(`[MAGIC KEYWORD: ${result.suggested_skill}] Detected "${result.keyword}"`);
  }
} else {
  // No output when no keyword detected - keeps hook output clean
  console.log("Success");
}
