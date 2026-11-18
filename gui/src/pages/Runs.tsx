import { Activity, ArrowLeft, CircleAlert, RefreshCw } from "lucide-react";
import { AnimatePresence, LayoutGroup, motion } from "motion/react";

import type { Key, Translator } from "../i18n";
import { useT } from "../i18n";
import { backendName, compactNumber, timeAgo } from "../core/format";
import { useResource } from "../core/resource";
import { href } from "../core/router";
import { useSnapshotStream } from "../core/stream";
import type { Board, BoardCard, ChatMessage, InterviewSummary, RunSummary } from "../core/types";
import { PageHead } from "../shell/PageHead";
import type { Tone } from "../ui/bits";
import { Chip, CopyButton, Empty, Kpis, LoadError, Notice, SectionTitle, Skeleton } from "../ui/bits";
import { Button } from "../ui/Button";
import { GLIDE, SETTLE } from "../core/motion";

export function runTone(status: string): Tone {
  if (status === "completed") return "ok";
  if (status === "failed") return "fail";
  if (status === "running" || status === "executing") return "info";
  if (status === "paused" || status === "interrupted") return "warn";
  return "muted";
}

export function RunStatus({ status }: { status: string }) {
  const { tx } = useT();
  return (
    <Chip tone={runTone(status)} pulse={status === "running"}>
      {tx(`run.status.${status}`, status)}
    </Chip>
  );
}

// Phase and activity values the runner reports (orchestrator/workflow_state.py,
// parallel_executor.py). Anything else is shown as the server sent it.
const PHASE_KEYS: Record<string, Key> = {
  Discover: "run.phase.discover",
  Define: "run.phase.define",
  Develop: "run.phase.develop",
  Deliver: "run.phase.deliver",
};

const ACTIVITY_KEYS: Record<string, Key> = {
  idle: "run.activity.idle",
  exploring: "run.activity.exploring",
  building: "run.activity.building",
  testing: "run.activity.testing",
  debugging: "run.activity.debugging",
  documenting: "run.activity.documenting",
  finalizing: "run.activity.finalizing",
  "Starting parallel execution": "run.activity.starting",
  Executing: "run.activity.executing",
  Monitoring: "run.activity.monitoring",
};

/** A run phase in the reader's language; an unknown one exactly as reported. */
export function phaseLabel(t: Translator["t"], phase: string): string {
  const key = PHASE_KEYS[phase];
  return key ? t(key) : phase;
}

/** A run activity in the reader's language; an unknown one exactly as reported. */
export function activityLabel(t: Translator["t"], activity: string): string {
  const level = /^Level (\d+) complete$/.exec(activity);
  if (level) return t("run.activity.levelComplete", { level: level[1] });
  const key = ACTIVITY_KEYS[activity];
  return key ? t(key) : activity;
}

export function Progress({ done, total, failed = 0, label }: { done: number; total: number; failed?: number; label: string }) {
  const safeTotal = Math.max(total, 1);
  return (
    <div className="progress" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
      <motion.span className="progress-done" animate={{ width: `${(done / safeTotal) * 100}%` }} transition={GLIDE} />
      {failed ? <motion.span className="progress-failed" animate={{ width: `${(failed / safeTotal) * 100}%` }} transition={GLIDE} /> : null}
    </div>
  );
}

/**
 * A run card's run is live but has no tasks yet: it is still planning them,
 * which can take minutes. A run that ended with none is not "preparing".
 */
export function runPreparing(message: Pick<ChatMessage, "progress" | "run_status"> | undefined): boolean {
  if (!message?.progress) return false;
  return message.progress.total === 0 && (message.run_status ?? "running") === "running";
}

/** One execution in a list; links to its board. */
export function RunRow({ run }: { run: RunSummary }) {
  const { t, locale } = useT();
  const total = run.total_count || run.node_count;
  return (
    <a className="run-row" href={href("runs", run.execution_id)}>
      <span className="run-row-main">
        <span className="run-row-goal">{run.goal || run.execution_id}</span>
        <span className="run-row-meta">
          {run.phase ? <span>{phaseLabel(t, run.phase)}</span> : null}
          {run.provider ? <span>{backendName(run.provider)}</span> : null}
          {run.start_time ? <span>{timeAgo(run.start_time, locale)}</span> : null}
        </span>
      </span>
      <span className="run-row-side">
        <RunStatus status={run.status} />
        <span className="run-row-count">
          {run.completed_count}/{total}
        </span>
      </span>
      <Progress done={run.completed_count} total={total} failed={run.failed_count} label={t("runs.progress")} />
    </a>
  );
}

const COLUMNS = ["pending", "executing", "completed", "failed"] as const;

function CardTile({ card }: { card: BoardCard }) {
  const { t, locale } = useT();
  return (
    <motion.li
      layout
      layoutId={`card-${card.id}`}
      className={`ac-card ac-${card.status}`}
      style={{ marginInlineStart: Math.min(Number(card.depth) || 0, 3) * 10 }}
      initial={{ opacity: 0, scale: 0.94 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.94 }}
      transition={SETTLE}
    >
      <p className="ac-title">
        {card.ac_index ? <span className="ac-index">AC {card.ac_index}</span> : null}
        {card.title || card.id}
      </p>
      <p className="ac-meta">
        {card.provider ? <span>{backendName(card.provider)}</span> : null}
        {card.model ? <span>{card.model}</span> : null}
        {card.tool ? <span>{t("runs.tool", { tool: card.tool })}</span> : null}
        {card.tokens ? <span>{t("runs.tokens", { count: card.tokens, tokens: compactNumber(card.tokens, locale) })}</span> : null}
      </p>
    </motion.li>
  );
}

function BoardView({ runId }: { runId: string }) {
  const { t, tx, locale } = useT();
  const path = `/api/runs/${encodeURIComponent(runId)}`;
  const first = useResource<Board>(`${path}/board`);
  const stream = useSnapshotStream<Board>(`${path}/stream`);
  const board = stream.latest ?? first.data;
  const columns = board?.columns ?? {};
  const counts = COLUMNS.map((name) => columns[name]?.length ?? 0);
  const total = board?.meta.total || counts.reduce((sum, value) => sum + value, 0);
  const status = counts[1] > 0 ? "running" : counts[3] > 0 ? "failed" : total > 0 && counts[2] >= total ? "completed" : "running";

  return (
    <>
      <PageHead
        title={board?.meta.goal || t("runs.board")}
        lead={
          <span className="run-id">
            <a className="back-link" href={href("runs")}>
              <ArrowLeft size={14} aria-hidden />
              {t("runs.back")}
            </a>
            <code>{runId}</code>
            <CopyButton text={runId} />
          </span>
        }
        actions={
          <Chip tone={stream.live ? "ok" : "muted"} pulse={stream.live}>
            {stream.live ? t("runs.live") : t("runs.connecting")}
          </Chip>
        }
      />
      {first.error && !board ? <LoadError error={first.error} onRetry={() => void first.reload()} /> : null}
      {board ? (
        <>
          <section className="card card-pad board-summary">
            <div className="board-summary-top">
              <RunStatus status={status} />
              <span className="board-count">{t("runs.count", { done: counts[2], total })}</span>
              {board.meta.phase ? <Chip>{phaseLabel(t, board.meta.phase)}</Chip> : null}
              {board.meta.provider ? <Chip tone="info">{backendName(board.meta.provider)}</Chip> : null}
              {board.meta.total_tokens ? <span className="board-tokens">{t("runs.tokens", { count: board.meta.total_tokens, tokens: compactNumber(board.meta.total_tokens, locale) })}</span> : null}
            </div>
            <Progress done={counts[2]} total={total} failed={counts[3]} label={t("runs.progress")} />
            <AnimatePresence mode="wait">
              {board.meta.activity ? (
                <motion.p key={board.meta.activity} className="board-activity" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.18 }}>
                  {activityLabel(t, board.meta.activity)}
                </motion.p>
              ) : null}
            </AnimatePresence>
          </section>
          <LayoutGroup>
            <div className="board">
              {COLUMNS.map((name, index) => (
                <section key={name} className={`board-col board-col-${name}`} aria-labelledby={`col-${name}`}>
                  <header className="board-col-head">
                    <h2 id={`col-${name}`}>{tx(`run.column.${name}`, name)}</h2>
                    <span className="board-col-count">{counts[index]}</span>
                  </header>
                  <ul className="board-cards">
                    <AnimatePresence initial={false} mode="popLayout">
                      {(columns[name] ?? []).map((card) => (
                        <CardTile key={card.id} card={{ ...card, status: name }} />
                      ))}
                    </AnimatePresence>
                  </ul>
                  {counts[index] === 0 ? <p className="board-col-empty">{t("runs.columnEmpty")}</p> : null}
                </section>
              ))}
            </div>
          </LayoutGroup>
        </>
      ) : first.loading ? (
        <div className="card card-pad">
          <Skeleton lines={5} />
        </div>
      ) : null}
    </>
  );
}

function RunList() {
  const { t, tx } = useT();
  const runs = useResource<{ runs: RunSummary[]; runs_error?: string | null; interviews: InterviewSummary[] }>("/api/runs", { interval: 6000 });
  const data = runs.data;
  return (
    <>
      <PageHead
        title={t("runs.title")}
        lead={t("runs.lead")}
        actions={
          <Button icon={<RefreshCw size={14} aria-hidden />} pending={runs.refreshing && !!data} onClick={() => void runs.reload()}>
            {t("common.refresh")}
          </Button>
        }
      />
      {runs.error && !data ? <LoadError error={runs.error} onRetry={() => void runs.reload()} /> : null}
      {runs.loading ? (
        <div className="card card-pad">
          <Skeleton lines={6} />
        </div>
      ) : null}
      {data ? (
        <div className="stack">
          <Kpis
            items={[
              { label: tx("run.status.running", "running"), dot: data.runs.some((run) => run.status === "running") ? "info" : "muted", value: data.runs.filter((run) => run.status === "running").length },
              { label: tx("run.status.completed", "completed"), dot: "ok", value: data.runs.filter((run) => run.status === "completed").length },
              { label: tx("run.status.failed", "failed"), dot: data.runs.some((run) => run.status === "failed") ? "fail" : "muted", value: data.runs.filter((run) => run.status === "failed").length },
              { label: t("runs.interviews"), dot: "muted", value: data.interviews.length },
            ]}
          />
          <section className="card card-pad" aria-labelledby="runs-list">
            <SectionTitle aside={<span className="count-note">{data.runs.length}</span>}>
              <span id="runs-list">{t("runs.executions")}</span>
            </SectionTitle>
            {data.runs_error ? (
              // The run index refused to answer: say so, rather than "no runs yet".
              <Notice tone="warn" icon={<CircleAlert size={16} aria-hidden />} title={t("runs.unreadable")}>
                {t("runs.unreadableBody", { error: data.runs_error })}
              </Notice>
            ) : data.runs.length === 0 ? (
              <Empty
                icon={<Activity size={20} aria-hidden />}
                title={t("runs.empty")}
                body={t("runs.emptyBody")}
                action={
                  <a className="btn btn-primary" href={href("chat")}>
                    <span className="btn-label">{t("home.loop.start")}</span>
                  </a>
                }
              />
            ) : (
              <motion.ul className="run-list" initial="hidden" animate="show" variants={{ show: { transition: { staggerChildren: 0.03 } } }}>
                {data.runs.map((run) => (
                  <motion.li key={run.execution_id} variants={{ hidden: { opacity: 0, y: 8 }, show: { opacity: 1, y: 0 } }}>
                    <RunRow run={run} />
                  </motion.li>
                ))}
              </motion.ul>
            )}
          </section>
          {data.interviews.length > 0 ? (
            <section className="card card-pad" aria-labelledby="runs-interviews">
              <SectionTitle aside={<span className="count-note">{data.interviews.length}</span>}>
                <span id="runs-interviews">{t("runs.interviews")}</span>
              </SectionTitle>
              <p className="section-note">{t("runs.interviewsNote")}</p>
              <ul className="id-list">
                {data.interviews.map((item) => (
                  <li key={item.interview_id}>
                    <code>{item.interview_id}</code>
                    <CopyButton text={item.interview_id} />
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </div>
      ) : null}
    </>
  );
}

export function RunsPage({ rest }: { rest: string }) {
  return rest ? <BoardView key={rest} runId={decodeURIComponent(rest)} /> : <RunList />;
}
