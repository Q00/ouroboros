import type { Operation } from "./contracts.js";
import type { GatewayInput } from "./policy.js";

export interface InfraClientOptions {
  apiKey: string;
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface InfraAnalysis {
  endpoint: "/graphsAndStatements" | "/graphAndStatements";
  signals: string[];
}

export class InfraUnavailableError extends Error {
  readonly code = "INFRANODUS_UNAVAILABLE";

  constructor() {
    super("InfraNodus analysis is temporarily unavailable");
    this.name = "InfraUnavailableError";
  }
}

export class InfraClient {
  readonly #apiKey: string;
  readonly #baseUrl: string;
  readonly #fetch: typeof fetch;
  readonly #timeoutMs: number;

  constructor(options: InfraClientOptions) {
    if (!options.apiKey.trim()) {
      throw new Error("INFRANODUS_API_KEY is required");
    }
    this.#apiKey = options.apiKey;
    this.#baseUrl = (options.baseUrl ?? "https://infranodus.com/api/v1").replace(
      /\/$/u,
      "",
    );
    this.#fetch = options.fetchImpl ?? fetch;
    this.#timeoutMs = options.timeoutMs ?? 20_000;
  }

  async analyze(
    operation: Operation,
    input: GatewayInput,
  ): Promise<InfraAnalysis> {
    const isStagnation = operation === "graph_diagnose_stagnation";
    const endpoint = isStagnation
      ? "/graphAndStatements"
      : "/graphsAndStatements";
    const query = new URLSearchParams(
      isStagnation
        ? {
            doNotSave: "true",
            addStats: "true",
            includeGraphSummary: "false",
            extendedGraphSummary: "true",
            includeGraph: "false",
            includeStatements: "false",
            aiTopics: "true",
          }
        : {
            doNotSave: "true",
            addStats: "true",
            includeStatements: "false",
            includeGraphSummary: "false",
            extendedGraphSummary: "true",
            includeGraph: "false",
            compactGraph: "true",
            compactStatements: "true",
            aiTopics: "true",
            optimize: "develop",
            compareMode: "difference",
          },
    );
    const analysisBody = isStagnation
      ? { text: `${input.objective}\n\nCurrent approach:\n${input.candidate}` }
      : {
          contexts: [
            { text: input.objective, modifyAnalyzedText: "none" },
            { text: input.candidate, modifyAnalyzedText: "none" },
          ],
          aiTopics: "true",
        };
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeoutMs);

    try {
      const response = await this.#fetch(`${this.#baseUrl}${endpoint}?${query}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${this.#apiKey}`,
        },
        body: JSON.stringify({
          ...analysisBody,
          modal: "mcp_server",
          source: "ouroboros-infranodus-gateway",
          tool: operation,
        }),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new InfraUnavailableError();
      }
      const data: unknown = await response.json();
      return { endpoint, signals: extractSignals(data) };
    } catch {
      throw new InfraUnavailableError();
    } finally {
      clearTimeout(timer);
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractSignals(payload: unknown): string[] {
  const root = isRecord(payload) && isRecord(payload.entriesAndGraphOfContext)
    ? payload.entriesAndGraphOfContext
    : payload;
  if (!isRecord(root) || !isRecord(root.extendedGraphSummary)) {
    return [];
  }

  const summary = root.extendedGraphSummary;
  const candidates = [
    summary.contentGaps,
    summary.mainTopics,
    summary.mainConcepts,
    summary.conceptualGateways,
    summary.topRelations,
  ];
  const signals: string[] = [];
  const collect = (value: unknown): void => {
    if (signals.length >= 8) return;
    if (typeof value === "string") {
      const normalized = value.replace(/\s+/gu, " ").trim().slice(0, 280);
      if (normalized && !signals.includes(normalized)) signals.push(normalized);
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) collect(item);
      return;
    }
    if (isRecord(value)) {
      for (const item of Object.values(value)) collect(item);
    }
  };
  for (const candidate of candidates) collect(candidate);
  return signals;
}
