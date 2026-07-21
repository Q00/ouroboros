import { createHash } from "node:crypto";
import { appendFile, chmod, mkdir } from "node:fs/promises";
import { join } from "node:path";

import type { GraphAdvice, Operation } from "./contracts.js";
import type { InfraAnalysis } from "./infra-client.js";
import { normalizeInput, type GatewayInput } from "./policy.js";

export interface AnalysisClient {
  analyze(operation: Operation, input: GatewayInput): Promise<InfraAnalysis>;
}

export interface GatewayOptions {
  client: AnalysisClient;
  stateDir?: string;
}

export class Gateway {
  readonly #client: AnalysisClient;
  readonly #stateDir: string | undefined;
  readonly #cache = new Map<string, GraphAdvice>();

  constructor(options: GatewayOptions) {
    this.#client = options.client;
    this.#stateDir = options.stateDir;
  }

  async execute(
    operation: Operation,
    input: GatewayInput,
  ): Promise<GraphAdvice> {
    const normalized = normalizeInput(input);
    const keyHash = createHash("sha256")
      .update(JSON.stringify({ operation, ...normalized }))
      .digest("hex");
    const cached = this.#cache.get(keyHash);
    if (cached) {
      const result: GraphAdvice = {
        ...cached,
        provenance: { ...cached.provenance, cache: "hit" },
      };
      await this.#record(operation, keyHash, result.status, "hit");
      return result;
    }

    try {
      const analysis = await this.#client.analyze(operation, normalized);
      const advice: GraphAdvice = {
        status: "OK",
        operation,
        summary: analysis.signals.length > 0
          ? `InfraNodus returned ${analysis.signals.length} bounded graph signal${analysis.signals.length === 1 ? "" : "s"}.`
          : "InfraNodus returned no bounded graph signals for this input.",
        observations: analysis.signals.slice(0, 8).map((signal) => signal.slice(0, 280)),
        nextActions: actionsFor(operation),
        provenance: {
          provider: "infranodus",
          mode: "no-save",
          endpoint: analysis.endpoint,
          cache: "miss",
        },
      };
      this.#cache.set(keyHash, advice);
      await this.#record(operation, keyHash, "OK", "miss");
      return advice;
    } catch {
      const endpoint = operation === "graph_diagnose_stagnation"
        ? "/graphAndStatements"
        : "/graphsAndStatements";
      const advice: GraphAdvice = {
        status: "DEGRADED_NO_GRAPH",
        operation,
        summary: "Graph analysis is unavailable; continue with the local Ouroboros gate.",
        observations: [],
        nextActions: ["Continue the local gate and record that graph advice was unavailable."],
        provenance: {
          provider: "infranodus",
          mode: "no-save",
          endpoint,
          cache: "bypass",
        },
      };
      await this.#record(operation, keyHash, "DEGRADED_NO_GRAPH", "bypass");
      return advice;
    }
  }

  async #record(
    operation: Operation,
    keyHash: string,
    status: GraphAdvice["status"],
    cache: GraphAdvice["provenance"]["cache"],
  ): Promise<void> {
    if (!this.#stateDir) return;
    await mkdir(this.#stateDir, { recursive: true, mode: 0o700 });
    await chmod(this.#stateDir, 0o700);
    const ledgerPath = join(this.#stateDir, "ledger.jsonl");
    await appendFile(
      ledgerPath,
      `${JSON.stringify({
        timestamp: new Date().toISOString(),
        operation,
        keyHash,
        status,
        cache,
      })}\n`,
      { encoding: "utf8", mode: 0o600 },
    );
    await chmod(ledgerPath, 0o600);
  }
}

function actionsFor(operation: Operation): string[] {
  switch (operation) {
    case "graph_review_seed":
      return ["Review the graph signals before accepting the seed."];
    case "graph_diagnose_stagnation":
      return ["Test one graph signal as a new lateral hypothesis."];
    case "graph_compare_delivery":
      return ["Resolve material graph gaps before completing the delivery gate."];
  }
}
