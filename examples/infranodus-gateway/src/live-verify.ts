#!/usr/bin/env node
import { mkdir, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

import { runLiveVerification } from "./live-verification.js";

const apiKey = process.env.INFRANODUS_API_KEY ?? "";
if (!apiKey) {
  process.stderr.write("INFRANODUS_API_KEY is required for live verification\n");
  process.exitCode = 2;
} else {
  const result = await runLiveVerification({
    apiKey,
    ...(process.env.GATEWAY_STATE_DIR
      ? { stateDir: process.env.GATEWAY_STATE_DIR }
      : {}),
  });
  const serialized = `${JSON.stringify(result, null, 2)}\n`;
  if (process.env.LIVE_EVIDENCE_PATH) {
    await mkdir(dirname(process.env.LIVE_EVIDENCE_PATH), { recursive: true });
    await writeFile(process.env.LIVE_EVIDENCE_PATH, serialized, {
      encoding: "utf8",
      mode: 0o600,
    });
  }
  process.stdout.write(serialized);
}
