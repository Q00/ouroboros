import { createServer, type Server } from "node:http";

import { afterEach, describe, expect, it } from "vitest";

import { runLiveVerification } from "../src/live-verification.js";

const servers: Server[] = [];
afterEach(async () => {
  await Promise.all(servers.splice(0).map(
    (server) => new Promise<void>((resolve, reject) =>
      server.close((error) => error ? reject(error) : resolve()),
    ),
  ));
});

describe("live no-save verifier", () => {
  it("proves three calls leave graph inventory unchanged", async () => {
    const paths: string[] = [];
    const server = createServer((request, response) => {
      paths.push(request.url ?? "");
      response.writeHead(200, { "content-type": "application/json" });
      if (request.url === "/api/v1/listGraphs") {
        response.end(JSON.stringify([
          { id: 7, contextName: "private-name-never-reported", createdAt: "2026-01-01" },
        ]));
      } else {
        response.end(JSON.stringify({
          extendedGraphSummary: { contentGaps: ["A bounded verification signal"] },
        }));
      }
    });
    servers.push(server);
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") throw new Error("missing port");

    const result = await runLiveVerification({
      apiKey: "fake-live-key",
      baseUrl: `http://127.0.0.1:${address.port}/api/v1`,
    });

    expect(result).toMatchObject({
      status: "PASS",
      inventory: { beforeCount: 1, afterCount: 1, unchanged: true },
    });
    expect(result.inventory.beforeDigest).toBe(result.inventory.afterDigest);
    expect(result.operations).toHaveLength(3);
    expect(paths.filter((path) => path === "/api/v1/listGraphs")).toHaveLength(2);
    expect(paths.filter((path) => path.includes("doNotSave=true"))).toHaveLength(3);
    expect(JSON.stringify(result)).not.toContain("private-name-never-reported");
    expect(JSON.stringify(result)).not.toContain("A bounded verification signal");
  });
});
