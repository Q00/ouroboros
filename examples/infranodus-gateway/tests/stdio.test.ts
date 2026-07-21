import { mkdtemp, rm } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { afterEach, describe, expect, it } from "vitest";

import { GraphAdviceSchema } from "../src/contracts.js";

const cleanupDirectories: string[] = [];
const cleanupServers: Server[] = [];

afterEach(async () => {
  await Promise.all(cleanupServers.splice(0).map(
    (server) => new Promise<void>((resolve, reject) =>
      server.close((error) => error ? reject(error) : resolve()),
    ),
  ));
  await Promise.all(
    cleanupDirectories.splice(0).map((path) => rm(path, { recursive: true })),
  );
});

describe("real stdio MCP surface", () => {
  it("exposes and executes exactly three read-only tools", async () => {
    const upstream = createServer((_request, response) => {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({
        extendedGraphSummary: { contentGaps: ["Bounded fake signal"] },
      }));
    });
    cleanupServers.push(upstream);
    await new Promise<void>((resolve) => upstream.listen(0, "127.0.0.1", resolve));
    const address = upstream.address();
    if (!address || typeof address === "string") throw new Error("missing test port");
    const stateParent = await mkdtemp(join(tmpdir(), "oig-stdio-test-"));
    cleanupDirectories.push(stateParent);

    const transport = new StdioClientTransport({
      command: process.execPath,
      args: [join(process.cwd(), "dist/stdio.js")],
      env: {
        INFRANODUS_API_KEY: "stdio-test-key",
        NODE_OPTIONS: `--import=${join(process.cwd(), "tests/rewrite-fetch.mjs")}`,
        OIG_TEST_BASE: `http://127.0.0.1:${address.port}`,
        GATEWAY_STATE_DIR: join(stateParent, "state"),
      },
      stderr: "pipe",
    });
    const client = new Client({ name: "stdio-contract-test", version: "1.0.0" });
    await client.connect(transport);

    try {
      const listed = await client.listTools();
      const names = listed.tools.map((tool) => tool.name).sort();
      expect(names).toEqual([
        "graph_compare_delivery",
        "graph_diagnose_stagnation",
        "graph_review_seed",
      ]);
      for (const tool of listed.tools) {
        expect(tool.annotations).toMatchObject({
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false,
        });
      }

      for (const name of names) {
        const result = await client.callTool({
          name,
          arguments: {
            objective: "Validate the bounded acceptance criteria.",
            candidate: "The safe delivery summary covers the main flow.",
          },
        });
        expect(result.isError).not.toBe(true);
        expect(GraphAdviceSchema.parse(result.structuredContent)).toMatchObject({
          status: "OK",
          operation: name,
          provenance: { mode: "no-save" },
        });
      }
    } finally {
      await client.close();
    }
  });
});
