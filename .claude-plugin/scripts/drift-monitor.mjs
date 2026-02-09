#!/usr/bin/env node

/**
 * Drift Monitor for Ouroboros
 *
 * Monitors file changes (Write/Edit tool calls) and checks
 * if there's an active Ouroboros session that may be drifting.
 *
 * Hook: PostToolUse (Write|Edit)
 * Output: Advisory message if active session detected
 *
 * This is a lightweight check - actual drift measurement
 * requires calling /ouroboros:status with the MCP server.
 */

import { readFileSync, existsSync, readdirSync, statSync } from "fs";
import { join } from "path";
import { homedir } from "os";

function checkActiveSession() {
  // Check for active session markers
  const ouroborosDir = join(homedir(), ".ouroboros", "data");

  if (!existsSync(ouroborosDir)) {
    return { active: false };
  }

  // Look for recent session state files
  try {
    // Session files are stored as JSON in the data directory
    // Check if any were modified in the last hour
    const files = readdirSync(ouroborosDir).filter(
      (f) => f.endsWith(".json") && f.startsWith("interview-")
    );

    if (files.length === 0) {
      return { active: false };
    }

    // Find the most recent session
    let newest = null;
    let newestTime = 0;

    for (const file of files) {
      const stat = statSync(join(ouroborosDir, file));
      if (stat.mtimeMs > newestTime) {
        newestTime = stat.mtimeMs;
        newest = file;
      }
    }

    // Only consider sessions modified in the last hour
    const oneHourAgo = Date.now() - 60 * 60 * 1000;
    if (newestTime < oneHourAgo) {
      return { active: false };
    }

    return {
      active: true,
      session_file: newest,
      last_modified: new Date(newestTime).toISOString(),
    };
  } catch {
    return { active: false };
  }
}

// Lightweight check - don't block the hook
const session = checkActiveSession();

if (session.active) {
  console.log(
    `Ouroboros session active (${session.session_file}). ` +
    `Use /ouroboros:status to check drift.`
  );
} else {
  // No output when no active session - keeps hook clean
  console.log("Success");
}
