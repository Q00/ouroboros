import { mkdtemp, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { GraphAdviceSchema } from "../src/contracts.js";
import { Gateway } from "../src/gateway.js";

const cleanup: string[] = [];
afterEach(async () => {
  await Promise.all(cleanup.splice(0).map((path) => rm(path, { recursive: true })));
});

describe("Gateway resilience and metadata-only state", () => {
  it("rejects sensitive input before invoking the analysis client", async () => {
    const analyze = vi.fn();
    const gateway = new Gateway({ client: { analyze } });

    await expect(
      gateway.execute("graph_review_seed", {
        objective: "Review the seed.",
        candidate: "API_KEY=super-secret-value-123456",
      }),
    ).rejects.toThrow(/rejected/i);
    expect(analyze).not.toHaveBeenCalled();
  });

  it("deduplicates identical analysis in memory and marks the cache hit", async () => {
    const analyze = vi.fn().mockResolvedValue({
      endpoint: "/graphsAndStatements",
      signals: ["Recovery evidence is absent", "resilience"],
    });
    const gateway = new Gateway({ client: { analyze } });
    const input = {
      objective: "Validate the recovery requirement.",
      candidate: "The delivery includes login only.",
    };

    const first = await gateway.execute("graph_review_seed", input);
    const second = await gateway.execute("graph_review_seed", input);

    expect(analyze).toHaveBeenCalledOnce();
    expect(first.provenance.cache).toBe("miss");
    expect(second.provenance.cache).toBe("hit");
    expect(GraphAdviceSchema.parse(first)).toEqual(first);
    expect(first.observations).toEqual([
      "Recovery evidence is absent",
      "resilience",
    ]);
  });

  it("returns bounded degraded advice when InfraNodus is unavailable", async () => {
    const gateway = new Gateway({
      client: { analyze: vi.fn().mockRejectedValue(new Error("private failure")) },
    });

    const advice = await gateway.execute("graph_diagnose_stagnation", {
      objective: "Find a new perspective.",
      candidate: "The current attempt repeats one assumption.",
    });

    expect(advice).toMatchObject({
      status: "DEGRADED_NO_GRAPH",
      observations: [],
      provenance: {
        provider: "infranodus",
        mode: "no-save",
        endpoint: "/graphAndStatements",
        cache: "bypass",
      },
    });
    expect(JSON.stringify(advice)).not.toContain("private failure");
    expect(GraphAdviceSchema.parse(advice)).toEqual(advice);
  });

  it("writes only hashed metadata with private filesystem modes", async () => {
    const parent = await mkdtemp(join(tmpdir(), "oig-state-test-"));
    cleanup.push(parent);
    const stateDir = join(parent, "state");
    const objective = "Validate a private acceptance criterion.";
    const candidate = "Safe delivery summary without identifiers.";
    const gateway = new Gateway({
      client: {
        analyze: vi.fn().mockResolvedValue({
          endpoint: "/graphsAndStatements",
          signals: ["One bounded observation"],
        }),
      },
      stateDir,
    });

    await gateway.execute("graph_compare_delivery", { objective, candidate });

    const ledgerPath = join(stateDir, "ledger.jsonl");
    const [dirStats, fileStats, ledger] = await Promise.all([
      stat(stateDir),
      stat(ledgerPath),
      readFile(ledgerPath, "utf8"),
    ]);
    expect(dirStats.mode & 0o777).toBe(0o700);
    expect(fileStats.mode & 0o777).toBe(0o600);
    expect(ledger).not.toContain(objective);
    expect(ledger).not.toContain(candidate);
    expect(ledger).not.toContain("One bounded observation");
    expect(JSON.parse(ledger.trim())).toMatchObject({
      operation: "graph_compare_delivery",
      status: "OK",
      cache: "miss",
    });
    expect(JSON.parse(ledger.trim()).keyHash).toMatch(/^[a-f0-9]{64}$/u);
  });
});
