import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";

import { GraphAdviceSchema, type Operation } from "./contracts.js";
import type { Gateway } from "./gateway.js";
import { PolicyError } from "./policy.js";

const InputSchema = z.object({
  objective: z.string().describe("Goal, requirement, or acceptance criteria in safe prose"),
  candidate: z.string().describe("Seed, current approach, or delivery evidence in safe prose"),
});

const TOOL_DESCRIPTIONS: Record<Operation, string> = {
  graph_review_seed: "Compare safe requirements prose with a proposed Ouroboros seed before approval.",
  graph_diagnose_stagnation: "Find graph-based gaps in a stalled approach without reading or writing a saved graph.",
  graph_compare_delivery: "Compare safe acceptance prose with delivery evidence before the QA completion gate.",
};

export function createServer(gateway: Gateway): McpServer {
  const server = new McpServer({
    name: "ouroboros-infranodus-gateway",
    version: "0.1.0",
  });

  for (const operation of Object.keys(TOOL_DESCRIPTIONS) as Operation[]) {
    server.registerTool(
      operation,
      {
        title: operation,
        description: TOOL_DESCRIPTIONS[operation],
        inputSchema: InputSchema,
        outputSchema: GraphAdviceSchema,
        annotations: {
          readOnlyHint: true,
          destructiveHint: false,
          idempotentHint: true,
          openWorldHint: false,
        },
      },
      async ({ objective, candidate }) => {
        try {
          const advice = await gateway.execute(operation, { objective, candidate });
          return {
            content: [{ type: "text", text: JSON.stringify(advice) }],
            structuredContent: advice,
          };
        } catch (error) {
          const message = error instanceof PolicyError
            ? error.message
            : "Gateway request failed safely";
          return {
            content: [{ type: "text", text: message }],
            isError: true,
          };
        }
      },
    );
  }

  return server;
}
