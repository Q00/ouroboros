import { CircleCheck, CircleX, Terminal } from "lucide-react";
import { useEffect } from "react";

import { useT } from "../i18n";
import { clockTime, duration } from "../core/format";
import type { TrackedJob } from "../core/jobs";
import { closeJobs, focusJob, hydrateJob, jobs } from "../core/jobs";
import { useStore } from "../core/store";
import { Command, Empty, LogView } from "../ui/bits";
import { Spinner } from "../ui/Mark";
import { Modal } from "../ui/Modal";

function JobIcon({ job }: { job: TrackedJob }) {
  if (job.status === "running") return <Spinner size={15} />;
  if (job.status === "succeeded") return <CircleCheck size={15} aria-hidden className="tone-ok" />;
  return <CircleX size={15} aria-hidden className="tone-fail" />;
}

/** Everything the console is running in the background, with live output. */
export function JobSheet() {
  const state = useStore(jobs);
  const { t, tx, locale } = useT();
  const list = state.order.map((id) => state.jobs[id]).filter(Boolean);
  const focused = (state.focus && state.jobs[state.focus]) || list[0];

  useEffect(() => {
    if (state.sheetOpen && focused) void hydrateJob(focused.id);
  }, [state.sheetOpen, focused]);

  return (
    <Modal
      side
      open={state.sheetOpen}
      onOpenChange={(open) => (open ? undefined : closeJobs())}
      title={t("jobs.title")}
      description={t("jobs.lead")}
    >
      {list.length === 0 ? (
        <Empty icon={<Terminal size={20} aria-hidden />} title={t("jobs.empty")} body={t("jobs.emptyBody")} />
      ) : (
        <>
          <ul className="job-list">
            {list.map((job) => (
              <li key={job.id}>
                <button
                  type="button"
                  className="job-row"
                  aria-current={focused?.id === job.id || undefined}
                  onClick={() => focusJob(job.id)}
                >
                  <JobIcon job={job} />
                  <span className="job-row-title">{tx(`job.kind.${job.kind}`, job.title, { name: job.title.replace(/^(Install|Set up)\s+/, "") })}</span>
                  <span className="job-row-time">
                    {job.finished_at ? duration(job.finished_at - job.started_at) : clockTime(job.started_at, locale)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {focused ? (
            <section className="job-detail" aria-label={focused.title}>
              <Command text={focused.command} wrap />
              <LogView lines={focused.lines} live={focused.status === "running"} emptyText={t("jobs.waiting")} maxHeight={460} />
              <p className={`job-outcome tone-${focused.status === "failed" ? "fail" : focused.status === "succeeded" ? "ok" : "mute"}`} role="status">
                {focused.status === "running"
                  ? t("jobs.running")
                  : focused.lost
                    ? t("jobs.lost")
                    : focused.status === "succeeded"
                      ? t("jobs.succeeded")
                      : t("jobs.failed", { code: focused.exit_code ?? "?" })}
              </p>
            </section>
          ) : null}
        </>
      )}
    </Modal>
  );
}
