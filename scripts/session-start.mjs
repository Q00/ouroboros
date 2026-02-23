#!/usr/bin/env node

/**
 * Session Start Hook for Ouroboros
 *
 * On first ever session after plugin install, triggers welcome experience.
 * Checks ~/.ouroboros/prefs.json for welcome_shown flag.
 *
 * Hook: SessionStart
 */

import { readFileSync, existsSync } from "fs";
import { join } from "path";
import { homedir } from "os";

const PREFS_PATH = join(homedir(), ".ouroboros", "prefs.json");

function getPrefs() {
  try {
    if (!existsSync(PREFS_PATH)) return {};
    return JSON.parse(readFileSync(PREFS_PATH, "utf-8"));
  } catch {
    return {};
  }
}

const prefs = getPrefs();

if (!prefs.welcome_shown) {
  console.log(
    `[MAGIC KEYWORD: /ouroboros:welcome] First time using Ouroboros! Starting welcome experience.`
  );
} else {
  console.log("Success");
}
