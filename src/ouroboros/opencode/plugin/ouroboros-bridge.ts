import type { Plugin } from "@opencode-ai/plugin"
import { appendFileSync, mkdirSync } from "fs"
import { join } from "path"
import { randomBytes } from "crypto"

// Platform-aware opencode config dir
export function cfg(): string {
  const home = process.env.HOME ?? process.env.USERPROFILE ?? "/tmp"
  if (process.env.OPENCODE_CONFIG_DIR)
    return process.env.OPENCODE_CONFIG_DIR
  if (process.platform === "win32")
    return join(process.env.APPDATA ?? join(home, "AppData", "Roaming"), "OpenCode")
  return join(process.env.XDG_CONFIG_HOME ?? join(home, ".config"), "opencode")
}

const DIR = join(cfg(), "plugins", "ouroboros-bridge")
const LOG = join(DIR, "bridge.log")
export const MAX_BYTES = 100_000
export const DEDUPE_MS = 5_000
export const MAX_FANOUT = 10
export const MAX_SEEN = 256
export const ID_LEN = 26
// A producer that appends to the visible question announces itself, so the
// server-side gatekeeper needs one grammar rather than a reverse-engineered
// catalogue of everyone's prose. These two literals are the Python constants in
// mcp/tools/advisory_dispatch.py; they cannot be imported across the language
// boundary, so a test pins them equal instead.
//
// Detecting notify()'s own text was the alternative and it does not hold:
// "[Ouroboros] " leads only the dispatched branch, while the failed- and
// skipped-only banners start with their own words.
export const OUROBOROS_DISPATCH_MARKER = "<!-- ouroboros-question-advisory-dispatch-v1 -->"
export const BRIDGE_NOTICE_OPENING = "> **Bridge dispatch — plugin_subagent:** "
export function num(v: string | undefined, d: number): number {
  const n = !v ? d : Number(v)
  return Number.isFinite(n) && n >= 0 ? n : d
}
export const CHILD_TIMEOUT_MS = num(process.env.OUROBOROS_CHILD_TIMEOUT_MS, 20 * 60 * 1000)
const AUTHORITY_TIMEOUT_MS = 5_000
const PATCH_RETRIES = 3
const RESOLVE_RETRIES = 5
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
  timeout?: ChildTimeout
}

interface Raw {
  tool_name: string
  title?: string
  agent?: string
  prompt: string
  timeout?: unknown
}

interface ChildTimeout {
  timeoutMs: number
  stopReason: "iteration_timeout" | "wall_clock_exhausted" | "child_timeout"
  source: string
  behavior?: string
  perIterationTimeoutSeconds?: number | null
  maxTotalSeconds?: number | null
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
    timeout: parseChildTimeout(r.timeout),
  }
}

function optionalNumber(v: unknown): number | null | undefined {
  if (v === null) return null
  if (v === undefined) return undefined
  return typeof v === "number" && Number.isFinite(v) ? v : undefined
}

export function parseChildTimeout(raw: unknown): ChildTimeout | undefined {
  if (!raw || typeof raw !== "object") return undefined
  const r = raw as Record<string, unknown>
  const timeoutMs = r.timeout_ms
  const stopReason = r.stop_reason
  if (typeof timeoutMs !== "number" || !Number.isFinite(timeoutMs) || timeoutMs <= 0) return undefined
  if (stopReason !== "iteration_timeout" && stopReason !== "wall_clock_exhausted") return undefined
  const perIterationTimeoutSeconds = optionalNumber(r.per_iteration_timeout_seconds)
  const maxTotalSeconds = optionalNumber(r.max_total_seconds)
  return {
    timeoutMs: Math.max(1, Math.floor(timeoutMs)),
    stopReason,
    source: typeof r.source === "string" && r.source ? r.source : "payload",
    behavior: typeof r.behavior === "string" && r.behavior ? r.behavior : undefined,
    perIterationTimeoutSeconds,
    maxTotalSeconds,
  }
}

export function childTimeout(s: Sub): ChildTimeout {
  return s.timeout ?? {
    timeoutMs: CHILD_TIMEOUT_MS,
    stopReason: "child_timeout",
    source: "OUROBOROS_CHILD_TIMEOUT_MS",
  }
}

export function timeoutMessage(t: ChildTimeout): string {
  if (t.stopReason === "wall_clock_exhausted") {
    return `stop_reason=wall_clock_exhausted; child aborted after ${t.timeoutMs}ms wall-clock budget`
  }
  if (t.stopReason === "iteration_timeout") {
    return `stop_reason=iteration_timeout; child aborted after ${t.timeoutMs}ms per-iteration budget`
  }
  return `child timed out after ${t.timeoutMs}ms`
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

export function parseMetadata(meta: unknown): { subs: Sub[]; responseShape: Record<string, unknown>; preserveContent: boolean } {
  const empty = { subs: [], responseShape: {}, preserveContent: false }
  if (!meta || typeof meta !== "object") return empty
  const record = meta as Record<string, unknown>
  const raw = record.question_advisory_subagents
  if (!Array.isArray(raw) || raw.length === 0) return empty
  if (raw.length > MAX_FANOUT) log(`WARN metadata_fanout_capped requested=${raw.length} cap=${MAX_FANOUT}`)
  const subs = raw.slice(0, MAX_FANOUT).flatMap((p, i) => {
    const s = build(p, i)
    return s ? [s] : []
  })
  if (subs.length === 0) return empty
  const responseShape: Record<string, unknown> = {}
  for (const key of [
    "session_id",
    "ambiguity_score",
    "milestone",
    "seed_ready",
    "question_advisory_recommended",
    // Dispatch here is fire-and-forget: children run in the background and no
    // output exists when this hook returns, so this transport has no moment
    // where the parent holds every lane at once. The parent redeems the
    // fan-out itself once the Task widgets finish — which it can only do if
    // the identity survives into the response it can see. Without these two
    // keys the data lane's measurement never reaches re-entry, because nothing
    // downstream can name the fan-out it belongs to (#1754, #1825).
    "question_advisory_fanout_id",
    "question_advisory_result_correlation_key",
  ]) {
    if (key in record) responseShape[key] = record[key]
  }
  return {
    subs,
    responseShape,
    preserveContent: record.question_advisory_preserve_content === true,
  }
}

type ParsedDispatch = {
  subs: Sub[]
  responseShape: Record<string, unknown>
  preserveContent?: boolean
}

export function mergeDispatchSources(
  parsedText: ParsedDispatch,
  parsedMeta: ParsedDispatch,
): { subs: Sub[]; responseShape: Record<string, unknown>; preserveContent: boolean } {
  if (parsedText.subs.length === 0) {
    return {
      subs: parsedMeta.subs.slice(0, MAX_FANOUT),
      responseShape: parsedMeta.responseShape,
      preserveContent: parsedMeta.preserveContent === true,
    }
  }
  return {
    subs: [...parsedText.subs, ...parsedMeta.subs].slice(0, MAX_FANOUT),
    responseShape: { ...parsedText.responseShape, ...parsedMeta.responseShape },
    preserveContent: false,
  }
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

// Write bridge-authored text into a tool response, declaring it as the bridge's.
//
// The bridge is a second producer appending to a question the server rendered:
// on PLUGIN_PASSIVE the server stamps no directive of its own, and whatever we
// add here is text a host sees and may echo back as `last_question`. Undeclared,
// that echo is indistinguishable from the question and the server records the
// banner — fan-out id and all — as what it asked.
//
// The declaration is attached HERE rather than at each call site because there
// are three appends (dispatch, dedupe, pre-dispatch failure) and only the first
// had it. A rule that every call site must remember is a rule that gets a fourth
// call site. Passing `original` is what says "a question is in front of this".
export function stampBridge(r: Output, original: string | undefined, body: string): void {
  const declared = `${OUROBOROS_DISPATCH_MARKER}\n\n${BRIDGE_NOTICE_OPENING}\n${body}`
  stamp(r, original === undefined ? declared : `${original}\n\n${declared}`)
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

// A failure that drops the fan-out identity is worse than a failure: the parent
// cannot then declare the lanes `undispatched`, and a required lane pins the
// fan-out at `partial` for good. The identity lives in `_meta`, which the host
// model does not read — the response shape is the channel it does — so every
// pre-dispatch rejection carries it too.
function fail(r: Output, label: string, err: unknown, preservePrefix?: string, shape?: string): void {
  const msg = `[Ouroboros] Dispatch failed for '${label}': ${errMsg(err)}. See ${LOG}.`
  stampBridge(r, preservePrefix, msg + (shape ?? ""))
}

const seen = new Map<string, number>()
const ralphChildren = new Set<string>()

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
  ralphChildren.clear()
}

function isRalphTool(s: Sub): boolean {
  return s.tool === "ouroboros_ralph"
}

export function markRalphChild(childID: string): void {
  if (childID) ralphChildren.add(childID)
}

export function isNestedRalphDispatch(pid: string, subs: Sub[]): boolean {
  return ralphChildren.has(pid) && subs.some(isRalphTool)
}

export function isRalphOwnedSession(sessionID: string): boolean {
  return ralphChildren.has(sessionID)
}

// HeyAPI base client exposed via client.session._client (shared across namespaces).
type Base = {
  patch: (a: { url: string; path: Record<string, string>; body: unknown }) => Promise<{ data?: unknown; error?: unknown }>
}

export function base(client: unknown): Base | null {
  const b = (client as { session?: { _client?: Base } })?.session?._client
  return b && typeof b.patch === "function" ? b : null
}

export type PermissionAction = "allow" | "deny" | "ask"
export type PermissionRule = Readonly<{
  permission: string
  pattern: string
  action: PermissionAction
}>
export type PermissionRuleset = ReadonlyArray<PermissionRule>

type Cli = {
  session: {
    create: (a: {
      body: {
        parentID?: string
        title?: string
        permission?: ReadonlyArray<{
          permission: string
          pattern: string
          action: PermissionAction
        }>
      }
    }) => Promise<{ data?: { id: string } }>
    get?: (a: { path: { id: string } }) => Promise<{ data?: unknown; error?: unknown }>
    prompt: (a: { path: { id: string }; body: { agent?: string; parts: Array<{ type: string; text: string }> }; signal?: AbortSignal }) => Promise<{ data?: { info?: unknown; parts?: Array<{ type: string; text?: string }> } }>
    abort: (a: { path: { id: string } }) => Promise<{ data?: unknown }>
    messages: (a: { path: { id: string } }) => Promise<{ data?: Array<{ info: { id: string; role: string }; parts: Array<{ type: string; callID?: string }> }> }>
  }
  app?: {
    agents?: () => Promise<{ data?: unknown; error?: unknown }>
  }
}

function authorityError(reason: string): Error {
  // Never include response bodies or permission patterns in user-visible
  // dispatch failures. The reason is deliberately a closed vocabulary.
  return new Error(`authority snapshot unavailable: ${reason}`)
}

async function authorityDeadline<T>(lookup: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(authorityError("lookup timed out")), timeoutMs)
  })
  try {
    return await Promise.race([lookup, timeout])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

function record(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v)
}

function permissionRuleset(value: unknown, scope: "parent" | "agent"): PermissionRuleset {
  if (!Array.isArray(value)) throw authorityError(`invalid ${scope} ruleset`)
  return Object.freeze(value.map((raw) => {
    if (!record(raw)) throw authorityError(`invalid ${scope} rule`)
    const permission = raw.permission
    const pattern = raw.pattern
    const action = raw.action
    if (typeof permission !== "string" || permission.length === 0)
      throw authorityError(`invalid ${scope} permission`)
    if (typeof pattern !== "string" || pattern.length === 0)
      throw authorityError(`invalid ${scope} pattern`)
    if (action !== "allow" && action !== "deny" && action !== "ask")
      throw authorityError(`invalid ${scope} action`)
    return Object.freeze({ permission, pattern, action })
  }))
}

// Mirrors OpenCode's deriveSubagentSessionPermission(). Agent rules are only
// inspected for exact recursive-tool declarations; they remain agent-owned and
// are not copied into the child session ruleset.
export function deriveSubagentSessionPermission(
  parentPermission: PermissionRuleset,
  agentPermission: PermissionRuleset,
): PermissionRuleset {
  const canTodo = agentPermission.some((rule) => rule.permission === "todowrite")
  const canTask = agentPermission.some((rule) => rule.permission === "task")
  return Object.freeze([
    ...parentPermission.filter(
      (rule) => rule.permission === "external_directory" || rule.action === "deny",
    ),
    ...(canTodo ? [] : [Object.freeze({ permission: "todowrite", pattern: "*", action: "deny" as const })]),
    ...(canTask ? [] : [Object.freeze({ permission: "task", pattern: "*", action: "deny" as const })]),
  ])
}

// Load one immutable authority snapshot for the complete fan-out. SDK v1 does
// not type the permission fields but its runtime client forwards and returns
// them; v2 types the same wire values. We therefore shape-check the wire data
// instead of guessing from SDK types or local configuration.
async function authoritySnapshot(
  cli: Cli,
  parentID: string,
  targetAgents: ReadonlyArray<string>,
  timeoutMs = AUTHORITY_TIMEOUT_MS,
): Promise<ReadonlyMap<string, PermissionRuleset>> {
  if (typeof cli?.session?.get !== "function" || typeof cli?.app?.agents !== "function")
    throw authorityError("client API missing")

  const lookup = Promise.all([
    Promise.resolve().then(() => cli.session.get!({ path: { id: parentID } })).catch(() => null),
    Promise.resolve().then(() => cli.app!.agents!()).catch(() => null),
  ])
  const [parentResult, agentsResult] = await authorityDeadline(lookup, timeoutMs)
  if (!parentResult || parentResult.error || !record(parentResult.data))
    throw authorityError("parent lookup failed")
  if (parentResult.data.id !== parentID)
    throw authorityError("parent mismatch")

  // Native OpenCode treats an absent optional session permission as []. This
  // is upstream's explicit derivation rule, not inferred authority.
  const parentPermission = parentResult.data.permission === undefined
    ? Object.freeze([]) as PermissionRuleset
    : permissionRuleset(parentResult.data.permission, "parent")

  if (!agentsResult || agentsResult.error || !Array.isArray(agentsResult.data))
    throw authorityError("agent catalog lookup failed")
  const catalog = new Map<string, PermissionRuleset>()
  for (const rawAgent of agentsResult.data) {
    if (!record(rawAgent) || typeof rawAgent.name !== "string" || rawAgent.name.length === 0)
      throw authorityError("invalid agent catalog")
    if (catalog.has(rawAgent.name)) throw authorityError("duplicate agent")
    catalog.set(rawAgent.name, permissionRuleset(rawAgent.permission, "agent"))
  }

  const snapshot = new Map<string, PermissionRuleset>()
  for (const name of new Set(targetAgents)) {
    const agentPermission = catalog.get(name)
    if (!agentPermission) throw authorityError("target agent missing")
    snapshot.set(name, deriveSubagentSessionPermission(parentPermission, agentPermission))
  }
  return snapshot
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
// Fails closed: returns null if exact callID match not found after all retries.
// Never falls back to arbitrary messages — prevents cross-talk in busy sessions.
async function resolveMid(cli: Cli, pid: string, callID: string): Promise<string | null> {
  for (let i = 0; i < RESOLVE_RETRIES; i++) {
    const res = await cli.session.messages({ path: { id: pid } }).catch(() => null)
    const msgs = res?.data
    if (Array.isArray(msgs)) {
      for (let j = msgs.length - 1; j >= 0; j--) {
        // Shape-checked rather than trusted: a stale or malformed entry used to
        // throw out of the hook entirely, and the hook's only handler logs. The
        // response was then left untouched — no failure notice and no fan-out
        // identity — which strands a registered required lane with no way to be
        // declared undispatched.
        const m = msgs[j] as { info?: { role?: unknown; id?: unknown }; parts?: unknown }
        if (!m || typeof m !== "object") continue
        if (m.info?.role !== "assistant" || typeof m.info?.id !== "string") continue
        if (!Array.isArray(m.parts)) continue
        const hit = m.parts.some(
          (p) => p && typeof p === "object"
            && (p as { type?: unknown }).type === "tool"
            && (p as { callID?: unknown }).callID === callID,
        )
        if (hit) return m.info.id
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
async function dispatch(
  cli: Cli,
  b: Base,
  pid: string,
  mid: string,
  s: Sub,
  permission: PermissionRuleset,
): Promise<{ childID: string }> {
  const partID = id("prt")
  const callID = id("tool")
  const start = Date.now()
  const input = { description: s.title, prompt: s.prompt, subagent_type: s.agent }
  const timeout = childTimeout(s)

  // --- Awaited phase (fast) ---
  const created = await cli.session.create({
    body: {
      parentID: pid,
      title: s.title,
      permission,
    },
  })
  const childID = created?.data?.id
  if (!childID) throw new Error("child session create returned no id")
  if (isRalphTool(s) || isRalphOwnedSession(pid)) markRalphChild(childID)
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
      metadata: {
        sessionId: childID,
        timeout_ms: timeout.timeoutMs,
        timeout_source: timeout.source,
        stop_reason_on_timeout: timeout.stopReason,
      },
      time: { start },
    },
  }, `running:${partID}`)
  log(`PATCH_RUNNING part=${partID} child=${childID}`)

  // --- Fire-and-forget phase ---
  // Hook returns to opencode before the child finishes. Completion is
  // handled by the promise chain below, which PATCHes the widget when
  // the child resolves/rejects/times out.
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), timeout.timeoutMs)

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
    const msg = ctrl.signal.aborted ? timeoutMessage(timeout) : err.message
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
        metadata: {
          sessionId: childID,
          ...(ctrl.signal.aborted ? {
            stop_reason: timeout.stopReason,
            timeout_ms: timeout.timeoutMs,
            timeout_source: timeout.source,
          } : {}),
        },
        time: { start, end: Date.now() },
      },
    }, `error:${partID}`).catch((pe) => log(`PATCH_ERR_FAIL part=${partID} err=${errMsg(pe)}`))
    log(`PROMPT_ERR part=${partID} child=${childID} err=${msg}`)
  })

  return { childID }
}

export const OuroborosBridge: Plugin = async (ctx) => {
  log(`INIT dir=${ctx.directory ?? "?"} timeout=${CHILD_TIMEOUT_MS}ms`)
  return {
    "tool.execute.after": async (input, output) => {
      // Enough to render a failure from the catch below. An exception after the
      // fan-out was registered is the same harm as an explicit rejection, so it
      // must reach the host as one rather than as silence.
      let rescue: { out: Output; prefix?: string; shape: string; label: string } | null = null
      try {
        if (!input || typeof input !== "object") return
        if (typeof input.tool !== "string" || !input.tool.startsWith("ouroboros_")) return
        if (!output || typeof output !== "object") return

        const out = output as Output
        const originalText = readText(out)
        const parsedText = parse(originalText)
        const parsedMeta = parseMetadata(out.metadata)
        const merged = mergeDispatchSources(parsedText, parsedMeta)
        const subs = merged.subs
        const responseShape = merged.responseShape
        const preserveContent = merged.preserveContent
        if (subs.length === 0) return

        const pid = typeof input.sessionID === "string" ? input.sessionID : ""
        const callID = typeof input.callID === "string" ? input.callID : ""
        const failurePrefix = preserveContent ? originalText : undefined
        // Preserve response_shape in text so the LLM can read the contract
        // fields build_subagent_result() provides — session_id, status, and the
        // fan-out identity. Computed before the rejections below so a failure
        // carries it too, and shared by every path that stamps.
        const shapeSuffix = Object.keys(responseShape).length > 0
          ? "\n\n```json\n" + JSON.stringify(responseShape, null, 2) + "\n```"
          : ""
        rescue = { out, prefix: failurePrefix, shape: shapeSuffix, label: subs[0].tool }
        if (!pid) { log(`REJECT reason=empty_sessionID tool=${subs[0].tool}`); fail(out, subs[0].tool, new Error("empty sessionID"), failurePrefix, shapeSuffix); return }
        if (!callID) { log(`REJECT reason=empty_callID tool=${subs[0].tool}`); fail(out, subs[0].tool, new Error("empty callID"), failurePrefix, shapeSuffix); return }
        if (isNestedRalphDispatch(pid, subs)) {
          log(`REJECT reason=nested_ralph pid=${pid} tool=${subs[0].tool}`)
          fail(out, "ouroboros_ralph", new Error("nested ouroboros_ralph delegation is not allowed"), failurePrefix, shapeSuffix)
          return
        }

        const cli = ctx.client as unknown as Cli
        const b = base(ctx.client)
        if (!cli?.session?.create || !cli.session.prompt || !cli.session.abort || !cli.session.messages || !b) {
          log(`REJECT reason=client_not_ready tool=${subs[0].tool}`)
          fail(out, subs[0].tool, new Error("client not ready"), failurePrefix, shapeSuffix)
          return
        }

        if (dupe(pid, callID)) {
          log(`DEDUPE pid=${pid} callID=${callID} tool=${subs[0].tool} count=${subs.length}`)
          const dedupeBanner = notify([], [], subs) + shapeSuffix
          stampBridge(out, preserveContent ? originalText : undefined, dedupeBanner)
          const meta = (out.metadata ?? {}) as Record<string, unknown>
          meta.ouroboros_dispatch = buildEnvelope([], [], subs)
          if (Object.keys(responseShape).length > 0) meta.ouroboros_response_shape = responseShape
          out.metadata = meta
          return
        }

        const mid = await resolveMid(cli, pid, callID)
        if (!mid) {
          log(`REJECT reason=no_message_found pid=${pid} callID=${callID}`)
          fail(out, subs[0].tool, new Error("could not resolve messageID"), failurePrefix, shapeSuffix)
          return
        }

        log(`DISPATCH_START pid=${pid} mid=${mid} tool=${subs[0].tool} count=${subs.length}`)

        // dispatch() awaits create+patch_running (fast) then fires prompt
        // fire-and-forget. Promise.allSettled here resolves when each child
        // is registered (widget running), NOT when each child finishes.
        // Hook returns to opencode in ~100ms regardless of child runtime.
        let results: Array<PromiseSettledResult<{ childID: string }>>
        try {
          const authority = await authoritySnapshot(cli, pid, subs.map((s) => s.agent))
          results = await Promise.allSettled(subs.map((s) => {
            const permission = authority.get(s.agent)
            // The snapshot loader proves every target exists. Keep this guard
            // at the use site so a future refactor cannot silently omit policy.
            if (!permission) return Promise.reject(authorityError("target authority missing"))
            return dispatch(cli, b, pid, mid, s, permission)
          }))
        } catch (e) {
          results = subs.map(() => ({ status: "rejected", reason: e }))
        }
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
        const banner = notify(ok, failed.map((f) => f.sub), [])
        stampBridge(out, preserveContent ? originalText : undefined, banner + shapeSuffix)

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
        if (rescue) {
          try { fail(rescue.out, rescue.label, e, rescue.prefix, rescue.shape) } catch {}
        }
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
export {
  resolveMid as _resolveMid,
  dispatch as _dispatch,
  authoritySnapshot as _authoritySnapshot,
  patch as _patch,
  sleep as _sleep,
  PATCH_RETRIES as _PATCH_RETRIES,
  RESOLVE_RETRIES as _RESOLVE_RETRIES,
}
