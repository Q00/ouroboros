// Run with `bun test` in gui/ (excluded from the page's type-check and bundle).
import { beforeAll, describe, expect, test } from "bun:test";

import type { CompletionPass } from "../core/types";

const scope = globalThis as unknown as Record<string, unknown>;
scope.window ??= { location: { search: "", hash: "" }, addEventListener() {}, setTimeout, clearTimeout };
scope.document ??= { documentElement: {}, querySelector: () => null };
Object.defineProperty(globalThis, "navigator", { value: { languages: ["en"], language: "en", platform: "" }, configurable: true });

type Module = typeof import("./CompletionPass");
let completionLine: Module["completionLine"];
let completionCaption: Module["completionCaption"];
let shotName: Module["shotName"];

beforeAll(async () => {
  ({ completionLine, completionCaption, shotName } = await import("./CompletionPass"));
});

const t = ((key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)) as Parameters<Module["completionLine"]>[0];

function pass(overrides: Partial<CompletionPass> = {}): CompletionPass {
  return { stage: "review", round: null, defects: null, status: null, done: false, steps: [], shots: null, ...overrides };
}

describe("the completion pass on the run card", () => {
  test("each stage says only what its event says", () => {
    expect(completionLine(t, pass())).toBe("completion.stage.review");
    expect(completionLine(t, pass({ stage: "play_plan" }))).toBe("completion.stage.play_plan");
    expect(completionLine(t, pass({ stage: "play", round: 1 }))).toBe('completion.stage.play {"n":1}');
    expect(completionLine(t, pass({ stage: "play_review_2", round: 2 }))).toBe('completion.stage.play_review {"n":2}');
    expect(completionLine(t, pass({ stage: "fix", defects: 16 }))).toBe('completion.stage.fix {"count":16}');
    expect(completionLine(t, pass({ stage: "repair", defects: 3 }))).toBe('completion.stage.repair {"count":3}');
    expect(completionLine(t, pass({ stage: "fix" }))).toBe("completion.stage.other");
    expect(completionLine(t, pass({ stage: "recheck" }))).toBe("completion.stage.recheck");
    expect(completionLine(t, pass({ stage: "pick_1_swapped" }))).toBe("completion.stage.pick");
    expect(completionLine(t, pass({ stage: "something_new" }))).toBe("completion.stage.other");
  });

  test("a finished pass says how it ended", () => {
    expect(completionLine(t, pass({ stage: "done", done: true, status: "fixed" }))).toBe("completion.done.fixed");
    expect(completionLine(t, pass({ stage: "done", done: true, status: "clean" }))).toBe("completion.done.clean");
    expect(completionLine(t, pass({ stage: "done", done: true, status: "fix_failed" }))).toBe("completion.done.fix_failed");
    expect(completionLine(t, pass({ stage: "done", done: true }))).toBe("completion.done.other");
  });

  test("the Verify step gets a short caption", () => {
    expect(completionCaption(t, pass({ stage: "play_3", round: 3 }))).toBe('auto.caption.pass.play {"n":3}');
    expect(completionCaption(t, pass({ stage: "fix", defects: 16 }))).toBe('auto.caption.pass.fix {"count":16}');
    expect(completionCaption(t, pass({ stage: "pick_2" }))).toBe("auto.caption.pass.pick");
    expect(completionCaption(t, pass({ stage: "recheck" }))).toBe("auto.caption.pass.check");
  });

  test("a screenshot is named by its step", () => {
    expect(shotName("02-after-dragging-to-rotate.png")).toBe("after dragging to rotate");
    expect(shotName("07-after_reset.png")).toBe("after reset");
  });
});
