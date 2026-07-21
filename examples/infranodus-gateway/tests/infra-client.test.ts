import { describe, expect, it, vi } from "vitest";

import { InfraClient } from "../src/infra-client.js";

describe("InfraClient allowlisted no-save contract", () => {
  it.each([
    ["graph_review_seed", "/graphsAndStatements", "contexts"],
    ["graph_compare_delivery", "/graphsAndStatements", "contexts"],
    ["graph_diagnose_stagnation", "/graphAndStatements", "text"],
  ] as const)("maps %s to its only allowed endpoint", async (operation, endpoint, bodyField) => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          extendedGraphSummary: {
            contentGaps: ["Missing recovery evidence"],
            mainTopics: ["onboarding", "resilience"],
          },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const client = new InfraClient({
      apiKey: "test-key-never-logged",
      baseUrl: "https://infranodus.test/api/v1",
      fetchImpl,
    });

    const result = await client.analyze(operation, {
      objective: "Validate onboarding requirements.",
      candidate: "The delivery has login but lacks recovery evidence.",
    });

    expect(fetchImpl).toHaveBeenCalledOnce();
    const [url, init] = fetchImpl.mock.calls[0] ?? [];
    const requestUrl = new URL(String(url));
    expect(requestUrl.pathname).toContain(endpoint);
    expect(requestUrl.searchParams.get("doNotSave")).toBe("true");
    expect(requestUrl.searchParams.has("save")).toBe(false);
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("authorization")).toBe(
      "Bearer test-key-never-logged",
    );
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    expect(body).toHaveProperty(bodyField);
    expect(body).toMatchObject({
      modal: "mcp_server",
      source: "ouroboros-infranodus-gateway",
      tool: operation,
    });
    expect(result.endpoint).toBe(endpoint);
    expect(result.signals).toEqual([
      "Missing recovery evidence",
      "onboarding",
      "resilience",
    ]);
  });

  it("does not leak an upstream error body", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("private upstream diagnostic", { status: 500 }),
    );
    const client = new InfraClient({ apiKey: "test-key", fetchImpl });

    await expect(
      client.analyze("graph_review_seed", {
        objective: "Review",
        candidate: "Safe prose.",
      }),
    ).rejects.not.toThrow(/private upstream diagnostic/);
  });

  it("aborts a request at the configured deadline", async () => {
    const fetchImpl = vi.fn<typeof fetch>().mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const client = new InfraClient({ apiKey: "test-key", fetchImpl, timeoutMs: 5 });

    await expect(
      client.analyze("graph_diagnose_stagnation", {
        objective: "Find a missing perspective.",
        candidate: "The current attempt repeats the same assumption.",
      }),
    ).rejects.toThrow(/unavailable/i);
  });
});
