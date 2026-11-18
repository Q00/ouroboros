// Run with `bun test` in gui/ (excluded from the page's type-check and bundle).
import { beforeAll, describe, expect, test } from "bun:test";

import type { AutoView, ChatMessage, ChatSession } from "../core/types";

// The page modules read the browser at import (language, token, route); give them just enough.
const scope = globalThis as unknown as Record<string, unknown>;
scope.window ??= { location: { search: "", hash: "" }, addEventListener() {}, setTimeout, clearTimeout };
scope.document ??= { documentElement: {}, querySelector: () => null };
Object.defineProperty(globalThis, "navigator", { value: { languages: ["en"], language: "en", platform: "" }, configurable: true });

type Module = typeof import("./AutoView");
type RunsModule = typeof import("./Runs");
let autoSteps: Module["autoSteps"];
let runPreparing: RunsModule["runPreparing"];

beforeAll(async () => {
  ({ autoSteps } = await import("./AutoView"));
  ({ runPreparing } = await import("./Runs"));
});

// A translator that shows which sentence was chosen, and with what.
const t = ((key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)) as Parameters<Module["autoSteps"]>[2];

function view(overrides: Partial<AutoView> = {}): AutoView {
  return {
    v: 2,
    auto_session_id: "auto_1",
    phase: "complete",
    message: null,
    phases: [
      { phase: "interview", at: null },
      { phase: "seed_generation", at: null },
      { phase: "review", at: null },
      { phase: "run", at: null },
      { phase: "complete", at: null },
    ],
    rounds: [],
    rounds_dropped: 0,
    max_rounds: 50,
    max_repair_rounds: 5,
    grade: "A",
    grades: [],
    review_message: null,
    findings: [],
    repair_round: 0,
    execution_id: "exec_1",
    seed_loaded: true,
    outcome: null,
    skipped: 0,
    read_error: null,
    ...overrides,
  };
}

function session(status: ChatSession["status"], messages: Partial<ChatMessage>[]): ChatSession {
  return { id: "c", title: "t", goal: "t", mode: "auto", backend: null, model: null, status, cwd: "/p", updated_at: 0, created_at: 0, version: 1, messages: messages as ChatMessage[] };
}

const counts = { pending: 0, executing: 0, completed: 0, failed: 0 };
const progress = (total: number, done = 0) => ({ tiles: [], status: "running", counts: { ...counts, completed: done, pending: total - done }, total, done, phase: null, activity: null, key: "" });

describe("the run step while the run plans its tasks", () => {
  test("a live run with no tasks yet says it is preparing them", () => {
    const run = { id: "r", kind: "run", run_status: "running", progress: progress(0) } as Partial<ChatMessage>;
    const steps = autoSteps(session("running", [run]), view(), t);
    expect(steps[3]).toMatchObject({ id: "run", state: "current", caption: "auto.caption.preparing" });
  });

  test("once tasks exist, the caption counts them", () => {
    const run = { id: "r", kind: "run", run_status: "running", progress: progress(3, 1) } as Partial<ChatMessage>;
    const steps = autoSteps(session("running", [run]), view(), t);
    expect(steps[3].caption).toBe('auto.caption.criteriaDone {"done":1,"total":3}');
  });

  test("before the run card reads its board, it is still starting", () => {
    const run = { id: "r", kind: "run", run_status: "running", progress: null } as Partial<ChatMessage>;
    expect(autoSteps(session("running", [run]), view(), t)[3].caption).toBe("auto.caption.starting");
  });

  test("a run that ended with no tasks is not preparing", () => {
    expect(runPreparing({ run_status: "completed", progress: progress(0) as ChatMessage["progress"] })).toBe(false);
    expect(runPreparing({ run_status: "interrupted", progress: progress(0) as ChatMessage["progress"] })).toBe(false);
    expect(runPreparing({ run_status: "running", progress: null })).toBe(false);
    expect(runPreparing({ run_status: "running", progress: progress(0) as ChatMessage["progress"] })).toBe(true);
  });
});

describe("the result step's changed files", () => {
  const done = (result: Partial<ChatMessage>) =>
    autoSteps(session("done", [{ id: "r", kind: "run", run_status: "completed", progress: progress(3, 3) }, { id: "x", kind: "result", source: "auto", outcome: "completed", in_repo: true, ...result }]), view({ outcome: "completed" }), t)[5];

  test("project files and Ouroboros state are counted apart, both said", () => {
    const step = done({ file_count: 3, internal_count: 2 });
    expect(step.caption).toBe('auto.caption.filesInternal {"count":3,"internal":2}');
    expect(step.title).toBe('auto.caption.filesInternal.title {"count":3,"internal":2}');
  });

  test("without state or bytecode the caption is the plain count", () => {
    expect(done({ file_count: 3, internal_count: 0 }).caption).toBe('auto.caption.files {"count":3}');
    // A result saved before the split has no internal count.
    expect(done({ file_count: 5 }).caption).toBe('auto.caption.files {"count":5}');
  });

  test("only state or bytecode changed: the step says how it ended", () => {
    expect(done({ file_count: 0, internal_count: 2 }).caption).toBe("chat.result.outcome.completed");
  });

  test("outside a repository nothing is counted", () => {
    expect(done({ in_repo: false, file_count: 0, internal_count: 0 }).caption).toBe("chat.result.outcome.completed");
  });
});
