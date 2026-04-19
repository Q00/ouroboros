// Bun tests for pure helpers in ouroboros-bridge.ts.
// Run: bun test  (from this directory)
//
// Covers: cfg, rand62, id, fnv, build, parse, readText, stamp, notify, dupe,
//         base, childOutput.
// I/O + runtime-orchestration (patch/resolveMid/attempt/run/bridge) are
// covered by Python installer tests and live integration; untested here
// because they require a live client.
import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import {
  B62,
  DEDUPE_MS,
  ID_LEN,
  MAX_BYTES,
  MAX_FANOUT,
  MAX_SEEN,
  _resetDedupe,
  base,
  build,
  buildEnvelope,
  cfg,
  childOutput,
  dupe,
  fnv,
  id,
  notify,
  num,
  parse,
  rand62,
  readText,
  stamp,
} from "./ouroboros-bridge.ts"
import {
  _resolveMid,
  _dispatch,
  _patch,
  _PATCH_RETRIES,
  _RESOLVE_RETRIES,
} from "./ouroboros-bridge.ts"

const ENV_BACKUP = { ...process.env }
const PLATFORM_BACKUP = process.platform
const restoreEnv = () => {
  for (const k of Object.keys(process.env)) if (!(k in ENV_BACKUP)) delete process.env[k]
  for (const k of Object.keys(ENV_BACKUP)) process.env[k] = ENV_BACKUP[k]
  Object.defineProperty(process, "platform", { value: PLATFORM_BACKUP })
}

afterEach(restoreEnv)

describe("cfg — platform config dir", () => {
  test("linux prefers XDG_CONFIG_HOME", () => {
    process.env.HOME = "/home/u"
    process.env.XDG_CONFIG_HOME = "/custom/xdg"
    delete process.env.APPDATA
    Object.defineProperty(process, "platform", { value: "linux" })
    expect(cfg()).toBe("/custom/xdg/opencode")
  })

  test("linux falls back to $HOME/.config when XDG_CONFIG_HOME unset", () => {
    process.env.HOME = "/home/u"
    delete process.env.XDG_CONFIG_HOME
    Object.defineProperty(process, "platform", { value: "linux" })
    expect(cfg()).toBe("/home/u/.config/opencode")
  })

  test("darwin uses Library/Application Support", () => {
    process.env.HOME = "/Users/u"
    Object.defineProperty(process, "platform", { value: "darwin" })
    expect(cfg()).toBe("/Users/u/Library/Application Support/OpenCode")
  })

  test("win32 uses APPDATA when present", () => {
    process.env.APPDATA = "C:\\Users\\u\\AppData\\Roaming"
    process.env.USERPROFILE = "C:\\Users\\u"
    delete process.env.HOME
    Object.defineProperty(process, "platform", { value: "win32" })
    expect(cfg()).toContain("OpenCode")
    expect(cfg()).toContain("Roaming")
  })

  test("win32 falls back to USERPROFILE/AppData/Roaming when APPDATA unset", () => {
    delete process.env.APPDATA
    process.env.USERPROFILE = "C:\\Users\\u"
    delete process.env.HOME
    Object.defineProperty(process, "platform", { value: "win32" })
    const r = cfg()
    expect(r).toContain("AppData")
    expect(r).toContain("Roaming")
    expect(r).toContain("OpenCode")
  })
})

describe("num — env parse guard", () => {
  test("returns default when undefined", () => {
    expect(num(undefined, 42)).toBe(42)
  })
  test("parses valid integer string", () => {
    expect(num("1500", 42)).toBe(1500)
  })
  test("parses valid float", () => {
    expect(num("3.14", 0)).toBeCloseTo(3.14)
  })
  test("rejects NaN string, returns default", () => {
    expect(num("abc", 99)).toBe(99)
  })
  test("rejects empty string, returns default", () => {
    expect(num("", 7)).toBe(7)
  })
  test("rejects negative, returns default", () => {
    expect(num("-5", 10)).toBe(10)
  })
  test("accepts zero", () => {
    expect(num("0", 99)).toBe(0)
  })
  test("rejects Infinity, returns default", () => {
    expect(num("Infinity", 5)).toBe(5)
  })
})

describe("rand62", () => {
  test("length matches n", () => {
    for (const n of [1, 8, 14, 26]) expect(rand62(n).length).toBe(n)
  })

  test("only uses B62 alphabet", () => {
    const s = rand62(200)
    for (const ch of s) expect(B62.includes(ch)).toBe(true)
  })

  test("distribution is not degenerate (100 samples all different)", () => {
    const set = new Set<string>()
    for (let i = 0; i < 100; i++) set.add(rand62(14))
    expect(set.size).toBeGreaterThan(95) // allow rare collisions
  })
})

describe("id", () => {
  test("prt_ prefix + correct length", () => {
    const v = id("prt")
    expect(v.startsWith("prt_")).toBe(true)
    expect(v.length).toBe(4 + ID_LEN)
  })

  test("tool_ prefix", () => {
    expect(id("tool").startsWith("tool_")).toBe(true)
  })

  test("hex prefix + b62 suffix structure", () => {
    const v = id("prt").slice(4)
    expect(v.slice(0, 12)).toMatch(/^[0-9a-f]{12}$/)
    expect(v.slice(12)).toMatch(/^[0-9A-Za-z]{14}$/)
  })

  test("monotonically ascending across rapid calls", () => {
    const a = id("prt")
    const b = id("prt")
    const c = id("prt")
    expect(b > a).toBe(true)
    expect(c > b).toBe(true)
  })

  test("1000 calls remain strictly ascending", () => {
    const ids = Array.from({ length: 1000 }, () => id("tool"))
    for (let i = 1; i < ids.length; i++) expect(ids[i] > ids[i - 1]).toBe(true)
  })
})

describe("fnv", () => {
  test("deterministic for same input", () => {
    expect(fnv("hello")).toBe(fnv("hello"))
  })

  test("distinct inputs → distinct hashes (high probability)", () => {
    expect(fnv("hello")).not.toBe(fnv("world"))
  })

  test("empty string", () => {
    expect(fnv("")).toBe("811c9dc5")
  })

  test("hex lowercase output", () => {
    expect(fnv("anything goes here 12345")).toMatch(/^[0-9a-f]+$/)
  })
})

describe("build", () => {
  test("valid minimal payload", () => {
    const s = build({ tool_name: "hacker", prompt: "think" }, 0)
    expect(s).not.toBeNull()
    expect(s!.tool).toBe("hacker")
    expect(s!.title).toBe("hacker") // defaults to tool_name
    expect(s!.agent).toBe("general") // default agent
    expect(s!.prompt).toBe("think")
    expect(s!.truncated).toBe(false)
    expect(s!.hash).toBe(fnv("think"))
  })

  test("uses custom title and agent when provided", () => {
    const s = build({ tool_name: "t", title: "My Title", agent: "hacker", prompt: "p" }, 0)
    expect(s!.title).toBe("My Title")
    expect(s!.agent).toBe("hacker")
  })

  test("rejects non-object", () => {
    expect(build(null, 0)).toBeNull()
    expect(build(42, 0)).toBeNull()
    expect(build("str", 0)).toBeNull()
  })

  test("rejects missing tool_name", () => {
    expect(build({ prompt: "p" }, 0)).toBeNull()
    expect(build({ tool_name: "", prompt: "p" }, 0)).toBeNull()
  })

  test("rejects missing prompt", () => {
    expect(build({ tool_name: "t" }, 0)).toBeNull()
    expect(build({ tool_name: "t", prompt: "" }, 0)).toBeNull()
  })

  test("truncates prompt over MAX_BYTES", () => {
    const big = "x".repeat(MAX_BYTES + 100)
    const s = build({ tool_name: "t", prompt: big }, 0)
    expect(s).not.toBeNull()
    expect(s!.truncated).toBe(true)
    expect(s!.prompt.length).toBeLessThan(big.length)
    expect(s!.prompt).toContain("[...truncated")
  })

  test("byte-safe truncation for CJK/emoji (blocker #2)", () => {
    // Each CJK char = 3 UTF-8 bytes. MAX_BYTES/3 chars = MAX_BYTES bytes exactly.
    // Adding one more char pushes over the limit.
    const cjk = "漢".repeat(Math.floor(MAX_BYTES / 3) + 100)
    expect(Buffer.byteLength(cjk, "utf8")).toBeGreaterThan(MAX_BYTES)
    const s = build({ tool_name: "t", prompt: cjk }, 0)
    expect(s).not.toBeNull()
    expect(s!.truncated).toBe(true)
    // The truncated prompt (minus the trailer) must fit within MAX_BYTES
    const body = s!.prompt.split("\n\n[...truncated")[0]
    expect(Buffer.byteLength(body, "utf8")).toBeLessThanOrEqual(MAX_BYTES)
  })

  test("does not truncate exactly at MAX_BYTES", () => {
    const exact = "x".repeat(MAX_BYTES)
    const s = build({ tool_name: "t", prompt: exact }, 0)
    expect(s!.truncated).toBe(false)
    expect(s!.prompt).toBe(exact)
  })
})

describe("parse", () => {
  const empty = { subs: [], responseShape: {} }

  test("empty / garbage returns empty", () => {
    expect(parse("")).toEqual(empty)
    expect(parse("x")).toEqual(empty)
    expect(parse("not json")).toEqual(empty)
    expect(parse("null")).toEqual(empty)
    expect(parse("42")).toEqual(empty)
  })

  test("_subagent single object → 1 Sub", () => {
    const raw = JSON.stringify({ _subagent: { tool_name: "t", prompt: "p" } })
    const out = parse(raw)
    expect(out.subs.length).toBe(1)
    expect(out.subs[0].tool).toBe("t")
    expect(out.responseShape).toEqual({})
  })

  test("_subagents array → N Subs", () => {
    const raw = JSON.stringify({
      _subagents: [
        { tool_name: "a", prompt: "pa" },
        { tool_name: "b", prompt: "pb" },
        { tool_name: "c", prompt: "pc" },
      ],
    })
    const out = parse(raw)
    expect(out.subs.length).toBe(3)
    expect(out.subs.map((s) => s.tool)).toEqual(["a", "b", "c"])
  })

  test("_subagents empty array rejected", () => {
    expect(parse(JSON.stringify({ _subagents: [] }))).toEqual(empty)
  })

  test("_subagents capped at MAX_FANOUT", () => {
    const many = Array.from({ length: MAX_FANOUT + 5 }, (_, i) => ({ tool_name: `t${i}`, prompt: `p${i}` }))
    const out = parse(JSON.stringify({ _subagents: many }))
    expect(out.subs.length).toBe(MAX_FANOUT)
  })

  test("invalid children skipped in array", () => {
    const raw = JSON.stringify({
      _subagents: [
        { tool_name: "good", prompt: "p" },
        { prompt: "no tool name" },
        { tool_name: "also-good", prompt: "p2" },
      ],
    })
    const out = parse(raw)
    expect(out.subs.length).toBe(2)
    expect(out.subs.map((s) => s.tool)).toEqual(["good", "also-good"])
  })

  test("neither _subagent nor _subagents → empty", () => {
    expect(parse(JSON.stringify({ other: "key" }))).toEqual(empty)
  })

  test("response_shape preserved alongside _subagent", () => {
    const raw = JSON.stringify({
      job_id: "job_123",
      session_id: "ses_456",
      status: "completed",
      _subagent: { tool_name: "exec", prompt: "do stuff" },
    })
    const out = parse(raw)
    expect(out.subs.length).toBe(1)
    expect(out.subs[0].tool).toBe("exec")
    expect(out.responseShape).toEqual({ job_id: "job_123", session_id: "ses_456", status: "completed" })
  })

  test("response_shape preserved alongside _subagents", () => {
    const raw = JSON.stringify({
      job_id: "job_789",
      _subagents: [{ tool_name: "a", prompt: "pa" }],
    })
    const out = parse(raw)
    expect(out.subs.length).toBe(1)
    expect(out.responseShape).toEqual({ job_id: "job_789" })
  })
})

describe("readText", () => {
  test("joins text parts from content[]", () => {
    expect(readText({ content: [{ type: "text", text: "A" }, { type: "text", text: "B" }] })).toBe("A\n\nB")
  })

  test("skips non-text parts", () => {
    expect(readText({ content: [{ type: "image" }, { type: "text", text: "only" }] as any })).toBe("only")
  })

  test("falls back to output string when content empty", () => {
    expect(readText({ output: "fallback" })).toBe("fallback")
  })

  test("returns empty when nothing", () => {
    expect(readText({})).toBe("")
  })

  test("prefers content over output", () => {
    expect(readText({ content: [{ type: "text", text: "C" }], output: "O" })).toBe("C")
  })
})

describe("stamp", () => {
  test("replaces existing content array in-place", () => {
    const r: any = { content: [{ type: "text", text: "old" }] }
    const ref = r.content
    stamp(r, "new")
    expect(ref).toBe(r.content) // same reference — in-place mutation
    expect(r.content).toEqual([{ type: "text", text: "new" }])
    expect(r.output).toBe("new")
  })

  test("creates content when missing", () => {
    const r: any = {}
    stamp(r, "hi")
    expect(r.content).toEqual([{ type: "text", text: "hi" }])
    expect(r.output).toBe("hi")
  })
})

describe("notify", () => {
  const sub = (title: string, truncated = false) => ({
    tool: "t", title, agent: "general", prompt: "p", truncated, hash: "h",
  })
  const okR = (title: string, childID = "ses_child", truncated = false) => ({
    sub: sub(title, truncated), childID,
  })

  test("ok-only — plural grammar + fire-and-forget phrasing", () => {
    const msg = notify([okR("a"), okR("b")], [], [])
    expect(msg).toContain("Dispatched 2 subagents")
    expect(msg).toContain("Task widgets will update as they complete")
    expect(msg).toContain("• a")
    expect(msg).toContain("• b")
  })

  test("ok-only singular grammar", () => {
    const msg = notify([okR("only")], [], [])
    expect(msg).toContain("Dispatched 1 subagent.")
    expect(msg).toContain("Task widget will update as it completes")
  })

  test("failed-only", () => {
    const msg = notify([], [sub("x"), sub("y")], [])
    expect(msg).toContain("Failed 2 subagents")
  })

  test("skipped-only mentions dedupe window", () => {
    const msg = notify([], [], [sub("dup")])
    expect(msg).toContain("Skipped 1 duplicate")
    expect(msg).toContain(`within ${Math.round(DEDUPE_MS / 1000)}s`)
  })

  test("mixed", () => {
    const msg = notify([okR("ok")], [sub("bad")], [sub("dup")])
    expect(msg).toContain("Dispatched 1")
    expect(msg).toContain("Failed 1")
    expect(msg).toContain("Skipped 1")
  })

  test("truncated note appears", () => {
    expect(notify([okR("big", "ses_big", true)], [], [])).toContain("truncated")
  })

  test("empty everything → fallback", () => {
    expect(notify([], [], [])).toContain("Nothing dispatched")
  })

  test("child session id appears in banner (for UI correlation)", () => {
    const msg = notify([okR("alpha", "ses_aaa"), okR("beta", "ses_bbb")], [], [])
    expect(msg).toContain("ses_aaa")
    expect(msg).toContain("ses_bbb")
  })

  test("no Results section in fire-and-forget model", () => {
    // Post-FF model: plugin doesn't await child output. Widget drives
    // completion. Banner must NOT promise results.
    const msg = notify([okR("x")], [], [])
    expect(msg).not.toContain("--- Results ---")
  })
})

describe("dupe", () => {
  beforeEach(() => _resetDedupe())

  test("first call returns false", () => {
    expect(dupe("p", "call_1")).toBe(false)
  })

  test("same pid+callID within window returns true (hook re-fire)", () => {
    dupe("p", "call_1")
    expect(dupe("p", "call_1")).toBe(true)
  })

  test("different callID — legit re-runs pass", () => {
    dupe("p", "call_1")
    expect(dupe("p", "call_2")).toBe(false)
  })

  test("different pid — cross-session isolation", () => {
    dupe("p1", "call_1")
    expect(dupe("p2", "call_1")).toBe(false)
  })

  test("eviction after MAX_SEEN entries", () => {
    for (let i = 0; i < MAX_SEEN + 5; i++) dupe("p", `call_${i}`)
    // earliest entries should be evicted → false again on insert
    expect(dupe("p", "call_0")).toBe(false)
  })
})

describe("base", () => {
  test("returns inner client when session._client.patch present", () => {
    const fake = { session: { _client: { patch: () => Promise.resolve({}) } } }
    expect(base(fake)).toBe(fake.session._client)
  })

  test("null when missing", () => {
    expect(base(null)).toBeNull()
    expect(base({})).toBeNull()
    expect(base({ session: {} })).toBeNull()
    expect(base({ session: { _client: {} } })).toBeNull()
    expect(base({ session: { _client: { patch: "not a function" } } })).toBeNull()
  })
})

describe("childOutput", () => {
  test("extracts last text part wrapped in task_result", () => {
    const data = { parts: [{ type: "text", text: "early" }, { type: "tool" }, { type: "text", text: "final" }] }
    const out = childOutput("ses_abc", data)
    expect(out).toContain("task_id: ses_abc")
    expect(out).toContain("<task_result>")
    expect(out).toContain("final")
    expect(out).toContain("</task_result>")
    expect(out).not.toContain("early")
  })

  test("empty when no text part", () => {
    const out = childOutput("ses_abc", { parts: [{ type: "tool" }] })
    expect(out).toContain("<task_result>\n\n</task_result>")
  })

  test("empty when parts missing", () => {
    const out = childOutput("ses_x", {})
    expect(out).toContain("task_id: ses_x")
    expect(out).toContain("<task_result>")
  })

  test("handles null/undefined data", () => {
    expect(childOutput("ses_y", null)).toContain("task_id: ses_y")
    expect(childOutput("ses_z", undefined)).toContain("task_id: ses_z")
  })
})

describe("buildEnvelope — dispatch schema", () => {
  const mkSub = (tool: string, title = tool) =>
    ({ tool, title, agent: "general", prompt: "p", truncated: false, hash: "h" }) as unknown as Parameters<
      typeof buildEnvelope
    >[0][0]["sub"]
  const okR = (tool: string, childID = "ses_" + tool) => ({ sub: mkSub(tool), childID })

  test("status=dispatched when any ok", () => {
    const env = buildEnvelope([okR("a")], [], [])
    expect(env.status).toBe("dispatched")
  })

  test("status=dispatch_failed when only failures", () => {
    const env = buildEnvelope([], [{ sub: mkSub("a"), reason: "boom" }], [])
    expect(env.status).toBe("dispatch_failed")
  })

  test("status=skipped when only skipped", () => {
    const env = buildEnvelope([], [], [mkSub("a")])
    expect(env.status).toBe("skipped")
  })

  test("status=nothing when empty", () => {
    const env = buildEnvelope([], [], [])
    expect(env.status).toBe("nothing")
  })

  test("status=dispatched wins over failed + skipped", () => {
    const env = buildEnvelope([okR("a")], [{ sub: mkSub("b") }], [mkSub("c")])
    expect(env.status).toBe("dispatched")
  })

  test("mode always plugin_subagent", () => {
    expect(buildEnvelope([], [], []).mode).toBe("plugin_subagent")
  })

  test("dispatched_at is ISO 8601 UTC", () => {
    const env = buildEnvelope([], [], [])
    expect(env.dispatched_at).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/)
  })

  test("children carry title + childID + agent + tool + truncated", () => {
    const env = buildEnvelope([okR("explore", "ses_42")], [], [])
    expect(env.children).toEqual([
      { title: "explore", childID: "ses_42", agent: "general", tool: "explore", truncated: false },
    ])
  })

  test("failed carries reason when provided", () => {
    const env = buildEnvelope([], [{ sub: mkSub("x"), reason: "create_failed" }], [])
    expect(env.failed[0]).toEqual({ title: "x", tool: "x", reason: "create_failed" })
  })

  test("skipped is shape {title,tool} only", () => {
    const env = buildEnvelope([], [], [mkSub("y", "Y-Title")])
    expect(env.skipped).toEqual([{ title: "Y-Title", tool: "y" }])
  })

  test("arrays default to [] when nothing of that kind", () => {
    const env = buildEnvelope([okR("a")], [], [])
    expect(env.failed).toEqual([])
    expect(env.skipped).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Runtime function tests (mocked client)
// ---------------------------------------------------------------------------

function mockCli(overrides: Partial<{
  create: (...a: unknown[]) => Promise<unknown>
  prompt: (...a: unknown[]) => Promise<unknown>
  abort: (...a: unknown[]) => Promise<unknown>
  messages: (...a: unknown[]) => Promise<unknown>
}> = {}) {
  return {
    session: {
      create: overrides.create ?? (async () => ({ data: { id: "child_abc" } })),
      prompt: overrides.prompt ?? (async () => ({ data: { parts: [{ type: "text", text: "done" }] } })),
      abort: overrides.abort ?? (async () => ({})),
      messages: overrides.messages ?? (async () => ({ data: [] })),
    },
  }
}

function mockBase(patchFn?: (...a: unknown[]) => Promise<unknown>) {
  return {
    patch: patchFn ?? (async () => ({})),
  }
}

describe("_resolveMid — message ID resolution with retry", () => {
  test("finds message matching callID", async () => {
    const cli = mockCli({
      messages: async () => ({
        data: [
          { info: { id: "m1", role: "user" }, parts: [{ type: "text" }] },
          { info: { id: "m2", role: "assistant" }, parts: [{ type: "tool", callID: "call_xyz" }] },
        ],
      }),
    })
    const mid = await _resolveMid(cli as never, "pid", "call_xyz")
    expect(mid).toBe("m2")
  })

  test("falls back to newest assistant when no callID match", async () => {
    const cli = mockCli({
      messages: async () => ({
        data: [
          { info: { id: "m1", role: "assistant" }, parts: [{ type: "text" }] },
          { info: { id: "m2", role: "user" }, parts: [{ type: "text" }] },
        ],
      }),
    })
    const mid = await _resolveMid(cli as never, "pid", "no_match")
    expect(mid).toBe("m1")
  })

  test("returns null after retries when no messages", async () => {
    const cli = mockCli({
      messages: async () => ({ data: [] }),
    })
    const mid = await _resolveMid(cli as never, "pid", "call_xyz")
    expect(mid).toBeNull()
  })

  test("returns null when messages call throws", async () => {
    const cli = mockCli({
      messages: async () => { throw new Error("network") },
    })
    const mid = await _resolveMid(cli as never, "pid", "call_xyz")
    expect(mid).toBeNull()
  })
})

describe("_patch — PATCH with retry", () => {
  test("succeeds on first attempt", async () => {
    const b = mockBase(async () => ({}))
    await expect(_patch(b as never, "pid", "mid", "part1", {}, "test")).resolves.toBeUndefined()
  })

  test("retries on error then succeeds", async () => {
    let calls = 0
    const b = mockBase(async () => {
      calls++
      if (calls < 3) return { error: "transient" }
      return {}
    })
    await expect(_patch(b as never, "pid", "mid", "part1", {}, "test")).resolves.toBeUndefined()
    expect(calls).toBe(3)
  })

  test("throws after max retries", async () => {
    const b = mockBase(async () => ({ error: "permanent" }))
    await expect(_patch(b as never, "pid", "mid", "part1", {}, "test")).rejects.toThrow(
      /PATCH failed after/,
    )
  })
})

describe("_dispatch — child session lifecycle", () => {
  test("success: creates child, patches running, fires prompt", async () => {
    const patchCalls: string[] = []
    const cli = mockCli({
      create: async () => ({ data: { id: "child_123" } }),
      prompt: async () => ({ data: { parts: [{ type: "text", text: "result" }] } }),
    })
    const b = mockBase(async (_a: unknown) => {
      patchCalls.push("patched")
      return {}
    })
    const sub = { tool: "ouroboros_qa", title: "QA", prompt: "check it", agent: "general" }
    const result = await _dispatch(cli as never, b as never, "pid", "mid", sub as never)
    expect(result.childID).toBe("child_123")
    // At least the PATCH-running call should have fired (awaited phase)
    expect(patchCalls.length).toBeGreaterThanOrEqual(1)
  })

  test("throws when child session create returns no id", async () => {
    const cli = mockCli({ create: async () => ({ data: {} }) })
    const b = mockBase()
    const sub = { tool: "ouroboros_qa", title: "QA", prompt: "check", agent: "general" }
    await expect(
      _dispatch(cli as never, b as never, "pid", "mid", sub as never),
    ).rejects.toThrow(/child session create returned no id/)
  })

  test("throws when PATCH-running fails", async () => {
    const cli = mockCli({ create: async () => ({ data: { id: "child_456" } }) })
    const b = mockBase(async () => ({ error: "server error" }))
    const sub = { tool: "ouroboros_qa", title: "QA", prompt: "check", agent: "general" }
    await expect(
      _dispatch(cli as never, b as never, "pid", "mid", sub as never),
    ).rejects.toThrow(/PATCH failed/)
  })
})
