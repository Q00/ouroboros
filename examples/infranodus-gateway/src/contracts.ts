import { z } from "zod";

export const OperationSchema = z.enum([
  "graph_review_seed",
  "graph_diagnose_stagnation",
  "graph_compare_delivery",
]);

export type Operation = z.infer<typeof OperationSchema>;

export const GraphAdviceSchema = z.object({
  status: z.enum(["OK", "DEGRADED_NO_GRAPH"]),
  operation: OperationSchema,
  summary: z.string().max(280),
  observations: z.array(z.string().max(280)).max(8),
  nextActions: z.array(z.string().max(280)).max(5),
  provenance: z.object({
    provider: z.literal("infranodus"),
    mode: z.literal("no-save"),
    endpoint: z.enum(["/graphsAndStatements", "/graphAndStatements"]),
    cache: z.enum(["hit", "miss", "bypass"]),
  }),
});

export type GraphAdvice = z.infer<typeof GraphAdviceSchema>;
