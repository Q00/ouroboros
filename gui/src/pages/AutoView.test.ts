// Run with `bun test` in gui/ (excluded from the page's type-check and bundle).
import { beforeAll, describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import type { AutoView, ChatMessage, ChatSession } from "../core/types";

// The page modules read the browser at import (language, token, route); give them just enough.
const scope = globalThis as unknown as Record<string, unknown>;
scope.window ??= { location: { search: "", hash: "" }, addEventListener() {}, setTimeout, clearTimeout };
scope.document ??= { documentElement: {}, querySelector: () => null };
Object.defineProperty(globalThis, "navigator", { value: { languages: ["en"], language: "en", platform: "" }, configurable: true });

type Module = typeof import("./AutoView");
type RunsModule = typeof import("./Runs");
let autoSteps: Module["autoSteps"];
let autoActivity: Module["autoActivity"];
let autoEnding: Module["autoEnding"];
let AutoStepper: Module["AutoStepper"];
let runPreparing: RunsModule["runPreparing"];

beforeAll(async () => {
  ({ autoSteps, autoActivity, autoEnding, AutoStepper } = await import("./AutoView"));
  ({ runPreparing } = await import("./Runs"));
});

// A translator that shows which sentence was chosen, and with what.
const t = ((key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key)) as Parameters<Module["autoSteps"]>[2];

const phases = (...names: string[]) => names.map((phase) => ({ phase, at: null }));

function view(overrides: Partial<AutoView> = {}): AutoView {
  return {
    v: 3,
    auto_session_id: "auto_1",
    phase: "complete",
    message: null,
    phases: phases("interview", "seed_generation", "run", "complete"),
    rounds: [],
    rounds_dropped: 0,
    max_rounds: 50,
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
    expect(steps[2]).toMatchObject({ id: "run", state: "current", caption: "auto.caption.preparing" });
  });

  test("once tasks exist, the caption counts them", () => {
    const run = { id: "r", kind: "run", run_status: "running", progress: progress(3, 1) } as Partial<ChatMessage>;
    const steps = autoSteps(session("running", [run]), view(), t);
    expect(steps[2].caption).toBe('auto.caption.criteriaDone {"done":1,"total":3}');
  });

  test("before the run card reads its board, it is still starting", () => {
    const run = { id: "r", kind: "run", run_status: "running", progress: null } as Partial<ChatMessage>;
    expect(autoSteps(session("running", [run]), view(), t)[2].caption).toBe("auto.caption.starting");
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
    autoSteps(session("done", [{ id: "r", kind: "run", run_status: "completed", progress: progress(3, 3) }, { id: "x", kind: "result", source: "auto", outcome: "completed", in_repo: true, ...result }]), view({ outcome: "completed" }), t)[4];

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

// ── Five steps, for new chats and for chats saved while Auto still reviewed its spec ──

const FIVE = ["interview", "spec", "run", "verify", "result"];

/** A view as a console before #3064 saved it: the Seed review's fields, and its phases. */
function reviewedView(overrides: Partial<AutoView> = {}): AutoView {
  const old = {
    ...view({ v: 2, phases: phases("interview", "seed_generation", "review", "repair", "review", "run", "complete"), outcome: "completed" }),
    max_repair_rounds: 5,
    grade: "A",
    grades: [
      { grade: "B", repair_round: 0, at: null },
      { grade: "A", repair_round: 1, at: null },
    ],
    review_message: "Seed grade A",
    findings: ["Success criteria are vague"],
    repair_round: 1,
  };
  return { ...old, ...overrides } as AutoView;
}

const seed = { id: "s", kind: "seed", source: "auto", text: "goal", acceptance_criteria: ["a", "b"], acceptance_total: 2 } as Partial<ChatMessage>;
const finished = { id: "r", kind: "run", run_status: "completed", progress: progress(2, 2) } as Partial<ChatMessage>;

/** The stepper as the page renders it, in English. */
function rendered(chat: ChatSession, data: AutoView): { items: string[]; html: string } {
  const html = renderToStaticMarkup(createElement(AutoStepper, { session: chat, view: data }));
  const items = [...html.matchAll(/<li class="auto-step" data-state="([a-z]+)"/g)].map((match) => match[1]);
  return { items, html };
}

describe("the stepper has five steps", () => {
  test("a new chat: interview, spec, run, verify, result", () => {
    const steps = autoSteps(session("done", [seed, finished]), view({ outcome: "completed" }), t);
    expect(steps.map((step) => step.id)).toEqual(FIVE);
    expect(steps.every((step) => step.state === "done")).toBe(true);
    expect(steps[1].caption).toBe('auto.caption.criteria {"count":2}');
  });

  test("a chat saved with review data shows the same five steps and none of the review", () => {
    const steps = autoSteps(session("done", [seed, finished]), reviewedView(), t);
    expect(steps.map((step) => step.id)).toEqual(FIVE);
    expect(steps.every((step) => step.state === "done")).toBe(true);
    const said = JSON.stringify(steps);
    for (const word of ["grade", "repair", "review", "Seed grade A", "Success criteria are vague"]) expect(said).not.toContain(word);
  });

  test("both render as five marks, with no review label or grade", () => {
    for (const data of [view({ outcome: "completed" }), reviewedView()]) {
      const { items, html } = rendered(session("done", [seed, finished]), data);
      expect(items).toEqual(["done", "done", "done", "done", "done"]);
      expect(html).toContain("Step 5 of 5");
      expect(html).not.toMatch(/Review|Grade|Revision|grade A/);
    }
  });

  test("a live chat from an engine that still reviews renders five marks, the spec one current", () => {
    const live = reviewedView({ phase: "repair", phases: phases("interview", "seed_generation", "review", "repair"), outcome: null, execution_id: null });
    const { items, html } = rendered(session("running", [seed]), live);
    expect(items).toEqual(["done", "current", "todo", "todo", "todo"]);
    expect(html).toContain("Step 2 of 5");
  });
});

describe("pipeline phases on the five steps", () => {
  const at = (phase: string, history: string[]) => {
    const steps = autoSteps(session("running", []), view({ phase, phases: phases(...history), outcome: null, execution_id: null }), t);
    return steps.map((step) => step.state);
  };

  test.each([
    ["interview", ["interview"], 0],
    ["seed_generation", ["interview", "seed_generation"], 1],
    ["run", ["interview", "seed_generation", "run"], 2],
    ["ralph_handoff", ["interview", "seed_generation", "run", "ralph_handoff"], 3],
    ["evaluate", ["interview", "seed_generation", "run", "evaluate"], 3],
    ["unstuck_lateral", ["interview", "seed_generation", "run", "unstuck_lateral"], 3],
    // A chat from before #3064: its review and repair were part of writing the spec.
    ["review", ["interview", "seed_generation", "review"], 1],
    ["repair", ["interview", "seed_generation", "review", "repair"], 1],
  ])("%s is on step %#", (phase, history, index) => {
    const states = at(phase as string, history as string[]);
    expect(states).toHaveLength(5);
    expect(states.indexOf("current")).toBe(index as number);
    expect(states.slice(0, index as number).every((state) => state === "done")).toBe(true);
  });

  test("a review or repair phase says the spec is being written", () => {
    for (const phase of ["seed_generation", "review", "repair"]) {
      expect(autoActivity(session("running", [seed]), view({ phase, outcome: null }))).toBe("auto.activity.seed");
    }
  });

  test("an old chat that failed in its review ends on the spec step", () => {
    const failed = reviewedView({ phase: "blocked", phases: phases("interview", "seed_generation", "review", "blocked"), outcome: "failed", execution_id: null, grade: "C" } as Partial<AutoView>);
    const chat = session("failed", [seed]);
    const steps = autoSteps(chat, failed, t);
    expect(steps.map((step) => step.state)).toEqual(["done", "failed", "todo", "todo", "failed"]);
    expect(autoEnding(chat, failed, t)).toEqual({ title: "auto.end.failed.title", body: "auto.end.other" });
  });
});

describe("the completion pass after the criteria pass", () => {
  const passing = (stage: string, extra: Record<string, unknown> = {}) =>
    ({
      id: "r",
      kind: "run",
      run_status: "completed",
      progress: { ...progress(2, 2), completion: { stage, round: null, defects: null, status: null, done: false, steps: [], shots: null, ...extra } },
    }) as Partial<ChatMessage>;

  test("the Verify step names the stage the pass is on", () => {
    const fixing = autoSteps(session("running", [seed, passing("fix", { defects: 16 })]), view(), t);
    expect(fixing[3]).toMatchObject({ id: "verify", state: "current", caption: 'auto.caption.pass.fix {"count":16}' });
    const trying = autoSteps(session("running", [seed, passing("play_2", { round: 2 })]), view(), t);
    expect(trying[3].caption).toBe('auto.caption.pass.play {"n":2}');
  });

  test("the line under the thread leaves the pass to the card until it is done", () => {
    expect(autoActivity(session("running", [seed, passing("fix", { defects: 16 })]), view())).toBeNull();
    expect(autoActivity(session("running", [seed, passing("done", { done: true, status: "fixed" })]), view())).toBe("auto.activity.finishing");
    expect(autoActivity(session("running", [seed, finished]), view())).toBe("auto.activity.finishing");
  });
});
