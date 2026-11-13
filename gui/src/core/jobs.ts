import { ApiFailure, get } from "./api";
import { createStore } from "./store";
import type { JobSnapshot } from "./types";

// Background CLI jobs (installs, `ouroboros setup`) the page is following.
// The server keeps each job's output; this polls only the new lines.

export interface TrackedJob extends JobSnapshot {
  /** The server restarted or forgot the job before it reported an exit. */
  lost?: boolean;
}

interface JobsState {
  jobs: Record<string, TrackedJob>;
  order: string[];
  sheetOpen: boolean;
  focus: string | null;
}

export const jobs = createStore<JobsState>({ jobs: {}, order: [], sheetOpen: false, focus: null });

const waiters = new Map<string, ((job: TrackedJob) => void)[]>();
const MAX_LINES = 2000;
let timer: number | null = null;

function merge(snapshot: JobSnapshot): TrackedJob {
  const previous = jobs.get().jobs[snapshot.id];
  const lines = previous ? [...previous.lines, ...snapshot.lines] : [...snapshot.lines];
  return { ...snapshot, lines: lines.slice(-MAX_LINES) };
}

function put(job: TrackedJob): void {
  jobs.set((state) => ({
    ...state,
    jobs: { ...state.jobs, [job.id]: job },
    order: state.order.includes(job.id) ? state.order : [job.id, ...state.order],
  }));
  if (job.status !== "running") {
    const pending = waiters.get(job.id) ?? [];
    waiters.delete(job.id);
    pending.forEach((resolve) => resolve(job));
  }
}

async function poll(): Promise<void> {
  const running = Object.values(jobs.get().jobs).filter((job) => job.status === "running" && !job.lost);
  if (running.length === 0) {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
    return;
  }
  await Promise.all(
    running.map(async (job) => {
      try {
        put(merge(await get<JobSnapshot>(`/api/jobs/${job.id}?since=${job.cursor}`)));
      } catch (failure) {
        if (failure instanceof ApiFailure && failure.status === 404) {
          put({ ...job, status: "failed", lost: true });
        }
      }
    }),
  );
}

function ensurePolling(): void {
  if (timer === null) timer = window.setInterval(() => void poll(), 650);
}

/** Start following a job. Resolves when it exits. */
export function trackJob(snapshot: JobSnapshot, options: { reveal?: boolean } = {}): Promise<TrackedJob> {
  const existing = jobs.get().jobs[snapshot.id];
  if (!existing) put({ ...snapshot, lines: [...snapshot.lines] });
  if (options.reveal) openJobs(snapshot.id);
  return new Promise((resolve) => {
    const current = jobs.get().jobs[snapshot.id];
    if (current && current.status !== "running") {
      resolve(current);
      return;
    }
    waiters.set(snapshot.id, [...(waiters.get(snapshot.id) ?? []), resolve]);
    ensurePolling();
  });
}

/** Pick up jobs that were already running when the page loaded. */
export async function adoptRunningJobs(): Promise<void> {
  try {
    const { jobs: listed } = await get<{ jobs: JobSnapshot[] }>("/api/jobs");
    for (const job of listed) {
      if (!jobs.get().jobs[job.id]) {
        // The list omits output; a running job is read from its first line.
        put({ ...job, cursor: job.status === "running" ? 0 : job.cursor, lines: [] });
      }
    }
    ensurePolling();
  } catch {
    // The activity list simply starts empty.
  }
}

/** Fetch the full output of a finished job that was adopted without it. */
export async function hydrateJob(id: string): Promise<void> {
  const job = jobs.get().jobs[id];
  if (!job || job.lines.length > 0 || job.status === "running" || job.lost) return;
  try {
    const full = await get<JobSnapshot>(`/api/jobs/${id}?since=0`);
    jobs.set((state) => ({ ...state, jobs: { ...state.jobs, [id]: { ...full } } }));
  } catch {
    // Leave the row without output.
  }
}

export function openJobs(focus?: string): void {
  jobs.set((state) => ({ ...state, sheetOpen: true, focus: focus ?? state.focus ?? state.order[0] ?? null }));
}

export function closeJobs(): void {
  jobs.set((state) => ({ ...state, sheetOpen: false }));
}

export function focusJob(id: string): void {
  jobs.set((state) => ({ ...state, focus: id }));
}
