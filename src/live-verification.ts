export interface LiveVerificationOptions {
  apiKey: string;
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  stateDir?: string;
}

export interface LiveVerificationResult {
  status: "PASS";
  inventory: {
    beforeCount: number;
    afterCount: number;
    beforeDigest: string;
    afterDigest: string;
    unchanged: true;
  };
  operations: Array<{
    operation: string;
    status: string;
    observationCount: number;
    endpoint: string;
    mode: "no-save";
  }>;
}

export async function runLiveVerification(
  options: LiveVerificationOptions,
): Promise<LiveVerificationResult> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const baseUrl = (options.baseUrl ?? "https://infranodus.com/api/v1").replace(/\/$/u, "");
  const before = await inventorySnapshot(baseUrl, options.apiKey, fetchImpl);
  const gateway = new Gateway({
    client: new InfraClient({
      apiKey: options.apiKey,
      baseUrl,
      fetchImpl,
      timeoutMs: 30_000,
    }),
    ...(options.stateDir ? { stateDir: options.stateDir } : {}),
  });
  const fixtures: ReadonlyArray<readonly [Operation, { objective: string; candidate: string }]> = [
    [
      "graph_review_seed",
      {
        objective: "A seed should state a measurable outcome, bounded scope, and verification gate.",
        candidate: "The proposed seed defines the outcome and scope, then requires a reproducible check.",
      },
    ],
    [
      "graph_diagnose_stagnation",
      {
        objective: "Identify a materially different hypothesis when progress repeats one assumption.",
        candidate: "The current approach keeps adjusting implementation details without testing a new model.",
      },
    ],
    [
      "graph_compare_delivery",
      {
        objective: "Delivery evidence should cover behavior, security boundaries, and an observable runtime check.",
        candidate: "The evidence includes passing tests, bounded permissions, and a live protocol invocation.",
      },
    ],
  ];
  const operations: LiveVerificationResult["operations"] = [];

  for (const [operation, input] of fixtures) {
    const advice = GraphAdviceSchema.parse(await gateway.execute(operation, input));
    if (advice.status !== "OK") {
      throw new Error(`Live verification failed safely for ${operation}`);
    }
    operations.push({
      operation,
      status: advice.status,
      observationCount: advice.observations.length,
      endpoint: advice.provenance.endpoint,
      mode: advice.provenance.mode,
    });
  }

  const after = await inventorySnapshot(baseUrl, options.apiKey, fetchImpl);
  if (before.count !== after.count || before.digest !== after.digest) {
    throw new Error("Live verification detected a graph inventory change");
  }

  return {
    status: "PASS",
    inventory: {
      beforeCount: before.count,
      afterCount: after.count,
      beforeDigest: before.digest,
      afterDigest: after.digest,
      unchanged: true,
    },
    operations,
  };
}

async function inventorySnapshot(
  baseUrl: string,
  apiKey: string,
  fetchImpl: typeof fetch,
): Promise<{ count: number; digest: string }> {
  const response = await fetchImpl(`${baseUrl}/listGraphs`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      modal: "mcp_server",
      source: "ouroboros-infranodus-gateway",
      tool: "live_inventory_guard",
    }),
  });
  if (!response.ok) throw new Error("Graph inventory check is unavailable");
  const payload: unknown = await response.json();
  if (!Array.isArray(payload)) throw new Error("Graph inventory response was invalid");
  const canonical = canonicalize(payload);
  return {
    count: payload.length,
    digest: createHash("sha256").update(canonical).digest("hex"),
  };
}

function canonicalize(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalize).sort().join(",")}]`;
  }
  if (typeof value === "object" && value !== null) {
    const entries = Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonicalize(item)}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}
import { createHash } from "node:crypto";

import { GraphAdviceSchema, type Operation } from "./contracts.js";
import { Gateway } from "./gateway.js";
import { InfraClient } from "./infra-client.js";
