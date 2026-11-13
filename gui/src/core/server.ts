import { healthz, post } from "./api";
import { createStore } from "./store";

// Whether the console process behind this page is reachable, and the two
// deliberate ways it goes away: a restart (same port, new process, new
// token) and a stop.

export type ServerPhase = "online" | "offline" | "restarting" | "stopped";

export const serverPhase = createStore<ServerPhase>("online");

/** Seconds the console process has been up, as last reported. */
export const serverUptime = createStore<number>(0);

let knownPid: number | null = null;

async function probe(): Promise<void> {
  const phase = serverPhase.get();
  if (phase === "restarting" || phase === "stopped") return;
  const health = await healthz();
  if (health) {
    if (knownPid !== null && health.pid !== knownPid) {
      // Another process answers now: its token differs, so start over.
      window.location.reload();
      return;
    }
    knownPid = health.pid;
    serverUptime.set(health.uptime);
    serverPhase.set("online");
  } else {
    serverPhase.set("offline");
  }
}

export function watchServer(): () => void {
  void probe();
  const timer = window.setInterval(() => void probe(), 5000);
  return () => window.clearInterval(timer);
}

/** Wait until a different console process answers, then reload into it. */
export async function awaitNewProcess(previousPid: number | null, timeoutMs = 90_000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  let sawDown = false;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    const health = await healthz();
    if (!health) {
      sawDown = true;
      continue;
    }
    if (health.pid !== previousPid || (sawDown && previousPid === null)) {
      window.location.reload();
      return true;
    }
  }
  return false;
}

export async function restartConsole(): Promise<void> {
  const before = (await healthz())?.pid ?? knownPid;
  serverPhase.set("restarting");
  try {
    await post("/api/server/restart");
  } catch (failure) {
    serverPhase.set("online");
    throw failure;
  }
  const back = await awaitNewProcess(before);
  if (!back) serverPhase.set("offline");
}

/** The console is handing over to another process (the login service). */
export async function expectHandoff(): Promise<void> {
  const before = knownPid;
  serverPhase.set("restarting");
  const back = await awaitNewProcess(before);
  if (!back) serverPhase.set("offline");
}

export async function stopConsole(): Promise<void> {
  await post("/api/server/stop");
  serverPhase.set("stopped");
}
