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

// Ensure log dir exists once at module load, not per-call.
try { mkdirSync(DIR, { recursive: true }) } catch {}

export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function log(msg: string): void {
  try {
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

// Truncate string to at most maxBytes of UTF-8. Walks backward past
// continuation bytes (10xxxxxx) to a valid character boundary.
export function truncateUtf8(s: string, maxBytes: number): string {
  const buf = Buffer.from(s, "utf8")
  if (buf.length <= maxBytes) return s
  let end = maxBytes
  // Skip past any UTF-8 continuation bytes (0x80..0xBF)
  while (end > 0 && (buf[end] & 0xC0) === 0x80) end--
  return buf.subarray(0, end).toString("utf8")
}

export function build(p: unknown, idx: number): Sub | null {
  if (!p || typeof p !== "object") { log(`REJECT reason=payload_not_object idx=${idx}`); return null }
  const r = p as Partial<Raw>
  if (typeof r.tool_name !== "string" || !r.tool_name) { log(`REJECT reason=missing_tool_name idx=${idx}`); return null }
  if (typeof r.prompt !== "string" || !r.prompt) { log(`REJECT reason=missing_prompt idx=${idx} tool=${r.tool_name}`); return null }
  const truncated = Buffer.byteLength(r.prompt, "utf8") > MAX_BYTES
  const prompt = truncated
    ? truncateUtf8(r.prompt, MAX_BYTES) + `\n\n[...truncated at ${Math.round(MAX_BYTES / 1024)}KB]`
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
// Also extracts non-subagent top-level keys as response_shape (blocker #1).
export function parse(raw: string): { subs: Sub[]; responseShape: Record<string, unknown> } {
  const empty = { subs: [], responseShape: {} }
  if (!raw || raw.length < 2) return empty
  let obj: unknown
  try { obj = JSON.parse(raw) } catch { return empty }
  if (!obj || typeof obj !== "object") return empty
  const record = obj as Record<string, unknown>

  // Extract response_shape: all top-level keys EXCEPT _subagent/_subagents
  const responseShape: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(record)) {
    if (k !== "_subagent" && k !== "_subagents") responseShape[k] = v
  }

  const multi = record._subagents
  if (Array.isArray(multi)) {
    if (multi.length === 0) { log("REJECT reason=empty_subagents_array"); return empty }
    if (multi.length > MAX_FANOUT) log(`WARN fanout_capped requested=${multi.length} cap=${MAX_FANOUT}`)
    const subs = multi.slice(0, MAX_FANOUT).flatMap((p, i) => {
      const s = build(p, i)
      return s ? [s] : []
    })
    return { subs, responseShape }
  }
  const single = record._subagent
  if (single && typeof single === "object") {
    const s = build(single, 0)
    return s ? { subs: [s], responseShape } : empty
  }
  return empty
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
}

// Build human-readable dispatch banner.
// Fire-and-forget model: children run in background; Task widgets drive
// completion. No child output is available at hook return time — the
// widget state (running → completed/error) is the source of truth.
// A structured envelope is attached separately in out.metadata.ouroboros_dispatch.
export function notify(
  ok: OkResult[],
  failed: Sub[],
  skipped: Sub[],
): string {
  const sec = Math.round(DEDUPE_MS / 1000)
  const lines: string[] = []
  if (ok.length > 0) {
    const s = ok.length === 1 ? "" : "s"
    lines.push(`[Ouroboros] Dispatched ${ok.length} subagent${s}. Task widget${s} will update as ${ok.length === 1 ? "it completes" : "they complete"}.`)
    for (const r of ok) {
      const note = r.sub.truncated ? ` (truncated to ${Math.round(MAX_BYTES / 1024)}KB)` : ""
      lines.push(`  • ${r.sub.title} → agent='${r.sub.agent}'${note} [child=${r.childID}]`)
    }
  }
  if (failed.length > 0) {
    lines.push(`Failed ${failed.length} subagent${failed.length === 1 ? "" : "s"} before dispatch:`)
    for (const s of failed) lines.push(`  • ${s.title}`)
  }
  if (skipped.length > 0) {
    lines.push(`Skipped ${skipped.length} duplicate${skipped.length === 1 ? "" : "s"} (within ${sec}s window):`)
    for (const s of skipped) lines.push(`  • ${s.title}`)
  }
  return lines.length > 0 ? lines.join("\n") : "[Ouroboros] Nothing dispatched."
}

// Standardized dispatch envelope for MCP caller / downstream tooling.
// Attached to out.metadata.ouroboros_dispatch — structured counterpart of notify().
export interface DispatchEnvelope {
  status: "dispatched" | "dispatch_failed" | "skipped" | "nothing"
  mode: "plugin_subagent"
  dispatched_at: string
  children: Array<{ title: string; childID: string; agent: string; tool: string; truncated: boolean }>
  failed: Array<{ title: string; tool: string; reason?: string }>
  skipped: Array<{ title: string; tool: string }>
}

export function buildEnvelope(
  ok: OkResult[],
  failed: Array<{ sub: Sub; reason?: string }>,
  skipped: Sub[],
): DispatchEnvelope {
  let status: DispatchEnvelope["status"] = "nothing"
  if (ok.length > 0) status = "dispatched"
  else if (failed.length > 0) status = "dispatch_failed"
  else if (skipped.length > 0) status = "skipped"
  return {
    status,
    mode: "plugin_subagent",
    dispatched_at: new Date().toISOString(),
    children: ok.map((r) => ({
      title: r.sub.title, childID: r.childID, agent: r.sub.agent, tool: r.sub.tool, truncated: r.sub.truncated,
    })),
    failed: failed.map((f) => ({ title: f.sub.title, tool: f.sub.tool, reason: f.reason })),
    skipped: skipped.map((s) => ({ title: s.title, tool: s.tool })),
  }
}

function fail(r: Output, label: string, err: unknown): void {
  stamp(r, `[Ouroboros] Dispatch failed for '${label}': ${errMsg(err)}. See ${LOG}.`)
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
    log(`PATCH_RETRY tag=${tag} attempt=${i + 1} err=${errMsg(last)}`)
    await sleep(BACKOFF_MS * (i + 1))
  }
  throw new Error(`PATCH failed after ${PATCH_RETRIES} attempts: ${errMsg(last)}`)
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

// Single subagent dispatch: create child session + PATCH running — both awaited
// (fast: ~10-100ms each). Then fires session.prompt WITHOUT await (fire-and-forget).
// Background completion handler attaches .then/.catch to PATCH the widget to
// completed/error state when the child finishes.
//
// Why fire-and-forget: opencode's MCP hook must return fast. Awaiting
// session.prompt blocks the main LLM for the full child execution
// (potentially minutes). The Task widget created by patch-running is the
// source of truth — opencode natively tracks widget state transitions and
// injects child output back into parent context on completion. The plugin
// does NOT need to await the child to preserve the contract.
//
// Trade-off: we lose in-plugin retry on prompt failure. Retries still
// cover the awaited create+patch-running failures (pre-dispatch).
// Post-dispatch failures get PATCHed to error state — widget reflects it,
// no silent loss. If the user wants retry-on-prompt-failure, that would
// need a new dispatch call (same shape as a fresh invocation).
async function dispatch(cli: Cli, b: Base, pid: string, mid: string, s: Sub): Promise<{ childID: string }> {
  const partID = id("prt")
  const callID = id("tool")
  const start = Date.now()
  const input = { description: s.title, prompt: s.prompt, subagent_type: s.agent }

  // --- Awaited phase (fast) ---
  const created = await cli.session.create({ body: { parentID: pid, title: s.title } })
  const childID = created?.data?.id
  if (!childID) throw new Error("child session create returned no id")
  log(`CHILD_CREATED pid=${pid} child=${childID} title=${s.title}`)

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
      metadata: { sessionId: childID },
      time: { start },
    },
  }, `running:${partID}`)
  log(`PATCH_RUNNING part=${partID} child=${childID}`)

  // --- Fire-and-forget phase ---
  // Hook returns to opencode before the child finishes. Completion is
  // handled by the promise chain below, which PATCHes the widget when
  // the child resolves/rejects/times out.
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), CHILD_TIMEOUT_MS)

  cli.session.prompt({
    path: { id: childID },
    body: { agent: s.agent, parts: [{ type: "text", text: s.prompt }] },
    signal: ctrl.signal,
  }).then(async (res) => {
    clearTimeout(timer)
    const data = (res as { data?: unknown })?.data
    const out = childOutput(childID, data)
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
        metadata: { sessionId: childID },
        time: { start, end: Date.now() },
      },
    }, `done:${partID}`).catch((e) => log(`PATCH_DONE_FAIL part=${partID} err=${errMsg(e)}`))
    log(`PROMPT_DONE part=${partID} child=${childID} bytes=${out.length}`)
  }).catch(async (e: unknown) => {
    clearTimeout(timer)
    const err = e instanceof Error ? e : new Error(String(e))
    const msg = ctrl.signal.aborted ? `child timed out after ${CHILD_TIMEOUT_MS}ms` : err.message
    await cli.session.abort({ path: { id: childID } }).catch((ae) => log(`ABORT_FAIL child=${childID} err=${errMsg(ae)}`))
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
        error: `${msg} (child=${childID})`,
        metadata: { sessionId: childID },
        time: { start, end: Date.now() },
      },
    }, `error:${partID}`).catch((pe) => log(`PATCH_ERR_FAIL part=${partID} err=${errMsg(pe)}`))
    log(`PROMPT_ERR part=${partID} child=${childID} err=${msg}`)
  })

  return { childID }
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
        const { subs, responseShape } = parse(readText(out))
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
          const meta = (out.metadata ?? {}) as Record<string, unknown>
          meta.ouroboros_dispatch = buildEnvelope([], [], subs)
          if (Object.keys(responseShape).length > 0) meta.ouroboros_response_shape = responseShape
          out.metadata = meta
          return
        }

        const mid = await resolveMid(cli, pid, callID)
        if (!mid) {
          log(`REJECT reason=no_message_found pid=${pid} callID=${callID}`)
          fail(out, subs[0].tool, new Error("could not resolve messageID"))
          return
        }

        log(`DISPATCH_START pid=${pid} mid=${mid} tool=${subs[0].tool} count=${subs.length}`)

        // dispatch() awaits create+patch_running (fast) then fires prompt
        // fire-and-forget. Promise.allSettled here resolves when each child
        // is registered (widget running), NOT when each child finishes.
        // Hook returns to opencode in ~100ms regardless of child runtime.
        const results = await Promise.allSettled(subs.map((s) => dispatch(cli, b, pid, mid, s)))
        const ok: OkResult[] = results.flatMap((r, i) => r.status === "fulfilled"
          ? [{ sub: subs[i], childID: r.value.childID }]
          : [])
        const failed: Array<{ sub: Sub; reason?: string }> = results.flatMap((r, i) => {
          if (r.status !== "rejected") return []
          const reason = errMsg(r.reason)
          log(`DISPATCH_REJECT idx=${i} title=${subs[i].title} reason=${reason}`)
          return [{ sub: subs[i], reason }]
        })

        log(`DISPATCH_DONE pid=${pid} ok=${ok.length} failed=${failed.length}`)
        stamp(out, notify(ok, failed.map((f) => f.sub), []))

        const envelope = buildEnvelope(ok, failed, [])
        const meta = (out.metadata ?? {}) as Record<string, unknown>
        meta.ouroboros_dispatch = envelope
        meta.ouroboros_subagents = subs.map((s) => ({ tool: s.tool, agent: s.agent, title: s.title, hash: s.hash, truncated: s.truncated }))
        meta.ouroboros_children = ok.map((r) => ({ title: r.sub.title, childID: r.childID }))
        if (failed.length > 0) meta.ouroboros_dispatch_failed = failed.map((f) => ({ title: f.sub.title, reason: f.reason }))
        if (Object.keys(responseShape).length > 0) meta.ouroboros_response_shape = responseShape
        out.metadata = meta
      } catch (e) {
        log(`HOOK_CRASH err=${e instanceof Error ? e.stack ?? e.message : errMsg(e)}`)
      }
    },
  }
}

// V1 default export: opencode plugin loader's legacy path iterates
// Object.values(mod) and throws on non-function exports (MAX_BYTES etc).
// V1 path uses mod.default {id, server} and skips the scan.
export default {
  id: "ouroboros-bridge",
  server: OuroborosBridge,
}

// Test-only exports for mocked-client coverage.
export { resolveMid as _resolveMid, dispatch as _dispatch, patch as _patch, sleep as _sleep, PATCH_RETRIES as _PATCH_RETRIES, RESOLVE_RETRIES as _RESOLVE_RETRIES }

