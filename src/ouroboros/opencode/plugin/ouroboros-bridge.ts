import type { Plugin } from "@opencode-ai/plugin"
import { appendFileSync, mkdirSync } from "fs"
import { join } from "path"
import { randomBytes } from "crypto"

// Platform-aware opencode config dir
export function cfg(): string {
  const home = process.env.HOME ?? process.env.USERPROFILE ?? "/tmp"
  if (process.platform === "win32")
    return join(process.env.APPDATA ?? join(home, "AppData", "Roaming"), "OpenCode")
  if (process.platform === "darwin")
    return join(home, "Library", "Application Support", "OpenCode")
  return join(process.env.XDG_CONFIG_HOME ?? join(home, ".config"), "opencode")
}

const DIR = join(cfg(), "plugins", "ouroboros-bridge")
const LOG = join(DIR, "bridge.log")
export const MAX_BYTES = 100_000
export const DEDUPE_MS = 5_000
export const MAX_FANOUT = 10
export const MAX_SEEN = 256
export const ID_LEN = 26
export function num(v: string | undefined, d: number): number {
  const n = !v ? d : Number(v)
  return Number.isFinite(n) && n >= 0 ? n : d
}
export const CHILD_TIMEOUT_MS = num(process.env.OUROBOROS_CHILD_TIMEOUT_MS, 20 * 60 * 1000)
const PATCH_RETRIES = 3
const RESOLVE_RETRIES = 5
export const SUB_RETRIES = num(process.env.OUROBOROS_SUB_RETRIES, 2)
const BACKOFF_MS = 100

function log(msg: string): void {
  try {
    mkdirSync(DIR, { recursive: true })
    appendFileSync(LOG, `[${new Date().toISOString()}] ${msg}\n`)
  } catch {}
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

// Monotonic ID generator — matches opencode src/id/id.ts ascending format
let lastTs = 0
let ctr = 0
const B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
export { B62 }

export function rand62(n: number): string {
  const b = randomBytes(n)
  let s = ""
  for (let i = 0; i < n; i++) s += B62[b[i] % 62]
  return s
}

export function id(prefix: "prt" | "tool"): string {
  const now = Date.now()
  if (now !== lastTs) { lastTs = now; ctr = 0 }
  ctr++
  let v = BigInt(now) * BigInt(0x1000) + BigInt(ctr)
  const buf = Buffer.alloc(6)
  for (let i = 0; i < 6; i++) buf[i] = Number((v >> BigInt(40 - 8 * i)) & BigInt(0xff))
  return prefix + "_" + buf.toString("hex") + rand62(ID_LEN - 12)
}

export function fnv(s: string): string {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16)
}

interface Sub {
  tool: string
  title: string
  agent: string
  prompt: string
  truncated: boolean
  hash: string
}

interface Raw {
  tool_name: string
  title?: string
  agent?: string
  prompt: string
}

type Output = {
  content?: Array<{ type: string; text?: string; [k: string]: unknown }>
  output?: string
  metadata?: Record<string, unknown>
  [k: string]: unknown
}

export function build(p: unknown, idx: number): Sub | null {
  if (!p || typeof p !== "object") { log(`REJECT reason=payload_not_object idx=${idx}`); return null }
  const r = p as Partial<Raw>
  if (typeof r.tool_name !== "string" || !r.tool_name) { log(`REJECT reason=missing_tool_name idx=${idx}`); return null }
  if (typeof r.prompt !== "string" || !r.prompt) { log(`REJECT reason=missing_prompt idx=${idx} tool=${r.tool_name}`); return null }
  const truncated = Buffer.byteLength(r.prompt, "utf8") > MAX_BYTES
  const prompt = truncated
    ? r.prompt.slice(0, MAX_BYTES) + `\n\n[...truncated at ${Math.round(MAX_BYTES / 1024)}KB]`
    : r.prompt
  if (truncated) log(`WARN truncate idx=${idx} tool=${r.tool_name}`)
  return {
    tool: r.tool_name,
    title: typeof r.title === "string" && r.title ? r.title : r.tool_name,
    agent: typeof r.agent === "string" && r.agent ? r.agent : "general",
    prompt,
    truncated,
    hash: fnv(prompt),
  }
}

// Parse { _subagent: {...} } OR { _subagents: [...] } from tool output text.
// Single function, no hardcoding — returns 1..N Sub objects uniformly.
export function parse(raw: string): Sub[] {
  if (!raw || raw.length < 2) return []
  let obj: unknown
  try { obj = JSON.parse(raw) } catch { return [] }
  if (!obj || typeof obj !== "object") return []
  const multi = (obj as { _subagents?: unknown })._subagents
  if (Array.isArray(multi)) {
    if (multi.length === 0) { log("REJECT reason=empty_subagents_array"); return [] }
    if (multi.length > MAX_FANOUT) log(`WARN fanout_capped requested=${multi.length} cap=${MAX_FANOUT}`)
    return multi.slice(0, MAX_FANOUT).flatMap((p, i) => {
      const s = build(p, i)
      return s ? [s] : []
    })
  }
  const single = (obj as { _subagent?: unknown })._subagent
  if (single && typeof single === "object") {
    const s = build(single, 0)
    return s ? [s] : []
  }
  return []
}

export function readText(r: Output): string {
  if (Array.isArray(r.content)) {
    const texts = r.content
      .filter((c): c is { type: "text"; text: string } => c?.type === "text" && typeof c.text === "string")
      .map((c) => c.text)
    if (texts.length) return texts.join("\n\n")
  }
  return typeof r.output === "string" ? r.output : ""
}

export function stamp(r: Output, msg: string): void {
  if (Array.isArray(r.content)) {
    try { r.content.length = 0; r.content.push({ type: "text", text: msg }) }
    catch { r.content = [{ type: "text", text: msg }] }
  } else {
    r.content = [{ type: "text", text: msg }]
  }
  try { r.output = msg } catch {}
}

export interface OkResult {
  sub: Sub
  childID: string
  output: string
}

export function notify(
  ok: OkResult[],
  failed: Sub[],
  skipped: Sub[],
): string {
  const sec = Math.round(DEDUPE_MS / 1000)
  const lines: string[] = []
  if (ok.length > 0) {
    lines.push(`[Ouroboros] Dispatched ${ok.length} subagent${ok.length === 1 ? "" : "s"} in parallel.`)
    for (const r of ok) {
      const note = r.sub.truncated ? ` (truncated to ${Math.round(MAX_BYTES / 1024)}KB)` : ""
      lines.push(`  • ${r.sub.title} → agent='${r.sub.agent}'${note}`)
    }
  }
  if (failed.length > 0) {
    lines.push(`Failed ${failed.length} subagent${failed.length === 1 ? "" : "s"}:`)
    for (const s of failed) lines.push(`  • ${s.title}`)
  }
  if (skipped.length > 0) {
    lines.push(`Skipped ${skipped.length} duplicate${skipped.length === 1 ? "" : "s"} (within ${sec}s window):`)
    for (const s of skipped) lines.push(`  • ${s.title}`)
  }
  if (ok.length > 0) {
    // Contract-preserving result propagation: parent LLM needs the actual
    // child output to continue reasoning. Banner alone is not enough.
    lines.push("")
    lines.push("--- Results ---")
    for (const r of ok) {
      lines.push(`### ${r.sub.title} (${r.childID})`)
      lines.push(r.output)
      lines.push("")
    }
  }
  return lines.length > 0 ? lines.join("\n") : "[Ouroboros] Nothing dispatched."
}

function fail(r: Output, label: string, err: unknown): void {
  stamp(r, `[Ouroboros] Dispatch failed for '${label}': ${err instanceof Error ? err.message : String(err)}. See ${LOG}.`)
}

const seen = new Map<string, number>()

export function dupe(pid: string, callID: string): boolean {
  // Identity = parent session + MCP callID. One MCP call = one dispatch.
  // If the tool.execute.after hook fires twice for the same callID
  // (opencode edge case), the second fire dedupes. Distinct MCP
  // invocations have distinct callIDs and never dedupe.
  const key = `${pid}::${callID}`
  const now = Date.now()
  const prev = seen.get(key)
  if (prev !== undefined && now - prev < DEDUPE_MS) return true
  seen.set(key, now)
  if (seen.size > MAX_SEEN) {
    let i = 0
    for (const k of seen.keys()) {
      if (i++ >= Math.floor(MAX_SEEN / 2)) break
      seen.delete(k)
    }
  }
  return false
}

export function _resetDedupe(): void {
  seen.clear()
}

// HeyAPI base client exposed via client.session._client (shared across namespaces).
type Base = {
  patch: (a: { url: string; path: Record<string, string>; body: unknown }) => Promise<{ data?: unknown; error?: unknown }>
}

export function base(client: unknown): Base | null {
  const b = (client as { session?: { _client?: Base } })?.session?._client
  return b && typeof b.patch === "function" ? b : null
}

type Cli = {
  session: {
    create: (a: { body: { parentID?: string; title?: string } }) => Promise<{ data?: { id: string } }>
    prompt: (a: { path: { id: string }; body: { agent?: string; parts: Array<{ type: string; text: string }> }; signal?: AbortSignal }) => Promise<{ data?: { info?: unknown; parts?: Array<{ type: string; text?: string }> } }>
    abort: (a: { path: { id: string } }) => Promise<{ data?: unknown }>
    messages: (a: { path: { id: string } }) => Promise<{ data?: Array<{ info: { id: string; role: string }; parts: Array<{ type: string; callID?: string }> }> }>
  }
}

// Walk parts for the last text entry — mirrors opencode src/tool/task.ts:158.
export function childOutput(childID: string, data: unknown): string {
  const parts = (data as { parts?: Array<{ type: string; text?: string }> })?.parts
  const text = Array.isArray(parts)
    ? [...parts].reverse().find((p) => p?.type === "text" && typeof p?.text === "string")?.text ?? ""
    : ""
  return [
    `task_id: ${childID}`,
    "",
    "<task_result>",
    text,
    "</task_result>",
  ].join("\n")
}

// PATCH with retry on network/server blips.
async function patch(b: Base, pid: string, mid: string, partID: string, body: unknown, tag: string): Promise<void> {
  let last: unknown
  for (let i = 0; i < PATCH_RETRIES; i++) {
    const r = await b.patch({
      url: "/session/{sessionID}/message/{messageID}/part/{partID}",
      path: { sessionID: pid, messageID: mid, partID },
      body,
    }).catch((e) => ({ error: e }))
    if (!r.error) return
    last = r.error
    log(`PATCH_RETRY tag=${tag} attempt=${i + 1} err=${last instanceof Error ? last.message : String(last)}`)
    await sleep(BACKOFF_MS * (i + 1))
  }
  throw new Error(`PATCH failed after ${PATCH_RETRIES} attempts: ${last instanceof Error ? last.message : String(last)}`)
}

// Resolve assistant messageID hosting this callID — with retry for race conditions.
async function resolveMid(cli: Cli, pid: string, callID: string): Promise<string | null> {
  for (let i = 0; i < RESOLVE_RETRIES; i++) {
    const res = await cli.session.messages({ path: { id: pid } }).catch(() => null)
    const msgs = res?.data
    if (Array.isArray(msgs)) {
      for (let j = msgs.length - 1; j >= 0; j--) {
        const m = msgs[j]
        if (m.info.role !== "assistant") continue
        if (m.parts.some((p) => p.type === "tool" && p.callID === callID)) return m.info.id
      }
      // fallback: newest assistant — valid for MCP tool results attached post-factum
      for (let j = msgs.length - 1; j >= 0; j--) {
        if (msgs[j].info.role === "assistant") return msgs[j].info.id
      }
    }
    if (i < RESOLVE_RETRIES - 1) await sleep(BACKOFF_MS)
  }
  return null
}

// Single subagent attempt: child session → PATCH running → prompt (timeout) → PATCH completed.
// Returns {childID, output} on success; throws on failure (caller decides retry).
async function attempt(cli: Cli, b: Base, pid: string, mid: string, partID: string, callID: string, start: number, s: Sub, input: Record<string, unknown>, n: number): Promise<{ childID: string; output: string }> {
  const created = await cli.session.create({ body: { parentID: pid, title: s.title } })
  const childID = created?.data?.id
  if (!childID) throw new Error("child session create returned no id")
  log(`CHILD_CREATED pid=${pid} child=${childID} title=${s.title} attempt=${n}`)

  await patch(b, pid, mid, partID, {
    id: partID,
    messageID: mid,
    sessionID: pid,
    type: "tool",
    tool: "task",
    callID,
    state: {
      status: "running",
      input,
      title: s.title,
      metadata: { sessionId: childID, attempt: n },
      time: { start },
    },
  }, `running:${partID}:a${n}`)
  log(`PATCH_RUNNING part=${partID} child=${childID} attempt=${n}`)

  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), CHILD_TIMEOUT_MS)

  const res = await cli.session.prompt({
    path: { id: childID },
    body: { agent: s.agent, parts: [{ type: "text", text: s.prompt }] },
    signal: ctrl.signal,
  }).catch((e) => ({ error: e instanceof Error ? e : new Error(String(e)) } as const))
  clearTimeout(timer)

  if ("error" in res) {
    const msg = ctrl.signal.aborted ? `child timed out after ${CHILD_TIMEOUT_MS}ms` : res.error.message
    await cli.session.abort({ path: { id: childID } }).catch((e) => log(`ABORT_FAIL child=${childID} err=${e instanceof Error ? e.message : String(e)}`))
    throw new Error(`${msg} (child=${childID})`)
  }

  const out = childOutput(childID, res.data)
  await patch(b, pid, mid, partID, {
    id: partID,
    messageID: mid,
    sessionID: pid,
    type: "tool",
    tool: "task",
    callID,
    state: {
      status: "completed",
      input,
      output: out,
      title: s.title,
      metadata: { sessionId: childID, attempt: n },
      time: { start, end: Date.now() },
    },
  }, `done:${partID}`)
  log(`PATCH_DONE part=${partID} child=${childID} bytes=${out.length} attempt=${n}`)
  return { childID, output: out }
}

// Subagent lifecycle with respawn: retries up to SUB_RETRIES times with NEW child session each time.
// Same function spawns 1 or N — caller fans out via Promise.allSettled.
// Returns {childID, output} on success so caller can propagate child result
// back to the parent MCP caller (contract preservation).
async function run(cli: Cli, b: Base, pid: string, mid: string, s: Sub): Promise<{ childID: string; output: string }> {
  const partID = id("prt")
  const callID = id("tool")
  const start = Date.now()
  const input = { description: s.title, prompt: s.prompt, subagent_type: s.agent }
  let lastErr: Error | undefined

  for (let n = 1; n <= SUB_RETRIES + 1; n++) {
    const r = await attempt(cli, b, pid, mid, partID, callID, start, s, input, n)
      .then((v) => ({ ok: true as const, v }), (e) => ({ ok: false as const, e: e instanceof Error ? e : new Error(String(e)) }))
    if (r.ok) return r.v
    lastErr = r.e
    log(`SUB_RETRY part=${partID} title=${s.title} attempt=${n}/${SUB_RETRIES + 1} err=${r.e.message}`)
    if (n <= SUB_RETRIES) await sleep(BACKOFF_MS * n)
  }

  const finalErr = lastErr ?? new Error("unknown failure")
  await patch(b, pid, mid, partID, {
    id: partID,
    messageID: mid,
    sessionID: pid,
    type: "tool",
    tool: "task",
    callID,
    state: {
      status: "error",
      input,
      error: `${finalErr.message} (exhausted ${SUB_RETRIES + 1} attempts)`,
      metadata: { attempts: SUB_RETRIES + 1 },
      time: { start, end: Date.now() },
    },
  }, `error:${partID}`).catch((e) => log(`PATCH_ERR_FAIL part=${partID} err=${e instanceof Error ? e.message : String(e)}`))
  log(`PATCH_ERR part=${partID} title=${s.title} err=${finalErr.message}`)
  throw finalErr
}

export const OuroborosBridge: Plugin = async (ctx) => {
  log(`INIT dir=${ctx.directory ?? "?"} timeout=${CHILD_TIMEOUT_MS}ms retries=${SUB_RETRIES}`)
  return {
    "tool.execute.after": async (input, output) => {
      try {
        if (!input || typeof input !== "object") return
        if (typeof input.tool !== "string" || !input.tool.startsWith("ouroboros_")) return
        if (!output || typeof output !== "object") return

        const out = output as Output
        const subs = parse(readText(out))
        if (subs.length === 0) return

        const pid = typeof input.sessionID === "string" ? input.sessionID : ""
        const callID = typeof input.callID === "string" ? input.callID : ""
        if (!pid) { log(`REJECT reason=empty_sessionID tool=${subs[0].tool}`); fail(out, subs[0].tool, new Error("empty sessionID")); return }
        if (!callID) { log(`REJECT reason=empty_callID tool=${subs[0].tool}`); fail(out, subs[0].tool, new Error("empty callID")); return }

        const cli = ctx.client as unknown as Cli
        const b = base(ctx.client)
        if (!cli?.session?.create || !cli.session.prompt || !cli.session.abort || !cli.session.messages || !b) {
          log(`REJECT reason=client_not_ready tool=${subs[0].tool}`)
          fail(out, subs[0].tool, new Error("client not ready"))
          return
        }

        if (dupe(pid, callID)) {
          log(`DEDUPE pid=${pid} callID=${callID} tool=${subs[0].tool} count=${subs.length}`)
          stamp(out, notify([], [], subs))
          return
        }

        const mid = await resolveMid(cli, pid, callID)
        if (!mid) {
          log(`REJECT reason=no_message_found pid=${pid} callID=${callID}`)
          fail(out, subs[0].tool, new Error("could not resolve messageID"))
          return
        }

        log(`DISPATCH_START pid=${pid} mid=${mid} tool=${subs[0].tool} count=${subs.length}`)

        const results = await Promise.allSettled(subs.map((s) => run(cli, b, pid, mid, s)))
        const ok: OkResult[] = results.flatMap((r, i) => r.status === "fulfilled"
          ? [{ sub: subs[i], childID: r.value.childID, output: r.value.output }]
          : [])
        const failed = results.flatMap((r, i) => {
          if (r.status !== "rejected") return []
          log(`DISPATCH_REJECT idx=${i} title=${subs[i].title} reason=${r.reason instanceof Error ? r.reason.message : String(r.reason)}`)
          return [subs[i]]
        })

        log(`DISPATCH_DONE pid=${pid} ok=${ok.length} failed=${failed.length}`)
        stamp(out, notify(ok, failed, []))

        const meta = (out.metadata ?? {}) as Record<string, unknown>
        meta.ouroboros_subagents = subs.map((s) => ({ tool: s.tool, agent: s.agent, title: s.title, hash: s.hash, truncated: s.truncated }))
        meta.ouroboros_children = ok.map((r) => ({ title: r.sub.title, childID: r.childID }))
        if (failed.length > 0) meta.ouroboros_dispatch_failed = failed.map((s) => s.title)
        out.metadata = meta
      } catch (e) {
        log(`HOOK_CRASH err=${e instanceof Error ? e.stack ?? e.message : String(e)}`)
      }
    },
  }
}

