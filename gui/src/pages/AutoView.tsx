// An Auto chat told as a live story: where the pipeline is (stepper), the
// interview it runs with itself, and what it is doing now. The data is
// `session.auto`, folded on the server from the progress events
// `ouroboros auto --progress-jsonl` writes (gui/auto_progress.py).

import { Check, ChevronDown, CircleCheck, CircleDashed, MessagesSquare, Minus, Pause, ScrollText, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { ReactNode } from "react";
import { useId, useState } from "react";

import type { Key, Translator } from "../i18n";
import { useT } from "../i18n";
import { EASE_OUT, SETTLE } from "../core/motion";
import type { AutoRound, AutoView, ChatMessage, ChatSession } from "../core/types";
import type { Tone } from "../ui/bits";
import { LogView } from "../ui/bits";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Mark";
import { runPreparing } from "./Runs";

// ── Steps ────────────────────────────────────────────────────────────

export type AutoStepId = "interview" | "spec" | "run" | "verify" | "result";
export type AutoStepState = "done" | "current" | "todo" | "failed" | "stopped" | "warn";

const STEP_IDS: AutoStepId[] = ["interview", "spec", "run", "verify", "result"];
const SPEC = STEP_IDS.indexOf("spec");
const RUN = STEP_IDS.indexOf("run");
const VERIFY = STEP_IDS.indexOf("verify");
const RESULT = STEP_IDS.indexOf("result");

// Pipeline phases (auto/state.py AutoPhase) on the five steps. Ralph, evaluation
// and the lateral recovery all serve verifying the result, so they share a step.
// Auto no longer reviews its spec (#3064); the review and repair phases of a
// chat from before that were part of getting the spec ready, so they stay on it.
const PHASE_STEP: Record<string, number> = {
  created: 0,
  interview: 0,
  seed_generation: SPEC,
  review: SPEC,
  repair: SPEC,
  run: RUN,
  ralph_handoff: VERIFY,
  evaluate: VERIFY,
  unstuck_lateral: VERIFY,
};

const RESULT_KEY: Record<NonNullable<AutoView["outcome"]>, Key> = {
  completed: "chat.result.outcome.completed",
  failed: "chat.result.outcome.failed",
  stopped: "chat.result.outcome.stopped",
  unverified: "chat.result.outcome.unverified",
};

export interface AutoStep {
  id: AutoStepId;
  state: AutoStepState;
  caption: string | null;
  /** More than the caption has room for (the round ceiling, a clipped caption). */
  title?: string;
}

function lastOf(messages: ChatMessage[], kind: ChatMessage["kind"]): ChatMessage | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].kind === kind) return messages[index];
  }
  return undefined;
}

function answeredCount(view: AutoView): number {
  return view.rounds_dropped + view.rounds.filter((round) => round.answer).length;
}

/** The step the pipeline has reached: from the phases it entered, then the run it started. */
function reachedStep(view: AutoView, run: ChatMessage | undefined): number {
  let reached = 0;
  for (const entry of view.phases) {
    const step = PHASE_STEP[entry.phase];
    if (step !== undefined) reached = Math.max(reached, step);
  }
  // `complete` is the pipeline handing the run off; the run itself goes on.
  if (view.phases.some((entry) => entry.phase === "complete") && (view.execution_id || run)) reached = Math.max(reached, RUN);
  if (run?.run_status === "completed") reached = Math.max(reached, VERIFY);
  return reached;
}

export function autoSteps(session: ChatSession, view: AutoView, t: Translator["t"]): AutoStep[] {
  const run = lastOf(session.messages, "run");
  const seed = lastOf(session.messages, "seed");
  const result = lastOf(session.messages, "result");
  const live = session.status === "running";
  const reached = reachedStep(view, run);
  const outcome = view.outcome;

  const states: AutoStepState[] = STEP_IDS.map((_, index) => {
    if (outcome === "completed") return "done";
    if (outcome === "unverified") return index === VERIFY ? "warn" : index === RESULT || index <= reached ? "done" : "todo";
    if (index < reached) return "done";
    // A failed or stopped run ends on the result step too, in the same state.
    if (index === RESULT && (outcome === "failed" || outcome === "stopped")) return outcome === "failed" ? "failed" : "stopped";
    if (index > reached) return "todo";
    if (live) return "current";
    if (outcome === "stopped") return "stopped";
    if (outcome === "failed" || session.status === "failed") return "failed";
    return "stopped"; // the console restarted under it
  });

  const captions: (string | null)[] = STEP_IDS.map((id, index) => {
    const state = states[index];
    if (state === "todo") return null;
    switch (id) {
      case "interview": {
        // The limit is a ceiling, not a plan: the count shows alone.
        const last = view.rounds[view.rounds.length - 1];
        if (state === "current" && last) return t("auto.caption.roundOnly", { n: last.round });
        const count = answeredCount(view);
        return count ? t("auto.caption.questions", { count }) : null;
      }
      case "spec": {
        const count = seed ? (seed.acceptance_total ?? seed.acceptance_criteria?.length ?? 0) : 0;
        return count ? t("auto.caption.criteria", { count }) : null;
      }
      case "run":
        if (run?.progress && run.progress.total) return t("auto.caption.criteriaDone", { done: run.progress.done, total: run.progress.total });
        // The run exists but is still planning its tasks, often for minutes.
        if (state === "current" && runPreparing(run)) return t("auto.caption.preparing");
        return state === "current" ? t("auto.caption.starting") : null;
      case "verify":
        if (state === "warn") return t("auto.caption.unverified");
        if (state === "current") return view.phase === "unstuck_lateral" ? t("auto.caption.unstuck") : t("auto.caption.verifying");
        return state === "done" ? t("auto.caption.verified") : null;
      case "result": {
        // Not verified is said once, on the verify step.
        if (!outcome || outcome === "unverified") return null;
        if (outcome !== "completed") return t(RESULT_KEY[outcome]);
        const { files, internal } = changedCounts(result);
        if (files > 0 && internal > 0) return t("auto.caption.filesInternal", { count: files, internal });
        return files > 0 ? t("auto.caption.files", { count: files }) : t(RESULT_KEY[outcome]);
      }
    }
  });
  const changed = changedCounts(result);
  const resultTitle = outcome === "completed" && changed.files > 0 && changed.internal > 0 ? t("auto.caption.filesInternal.title", { count: changed.files, internal: changed.internal }) : undefined;

  const live0 = states[0] === "current" && view.max_rounds;
  return STEP_IDS.map((id, index) => ({
    id,
    state: states[index],
    caption: captions[index],
    title: index === 0 && live0 ? t("auto.interview.ceiling", { max: view.max_rounds ?? 0 }) : index === RESULT && resultTitle ? resultTitle : (captions[index] ?? undefined),
  }));
}

/** Project files and Ouroboros state/bytecode an Auto result counted; none outside a repository. */
function changedCounts(result: ChatMessage | undefined): { files: number; internal: number } {
  if (!result?.in_repo) return { files: 0, internal: 0 };
  return { files: result.file_count ?? 0, internal: result.internal_count ?? 0 };
}

/** The step a failed or stopped Auto chat ended on, for the footer that explains it. */
export function autoEndStep(steps: AutoStep[]): AutoStep | undefined {
  return steps.slice(0, RESULT).find((step) => step.state === "failed" || step.state === "stopped");
}

const STEP_LABEL: Record<AutoStepId, Key> = {
  interview: "auto.step.interview",
  spec: "auto.step.spec",
  run: "auto.step.run",
  verify: "auto.step.verify",
  result: "auto.step.result",
};

const STATE_LABEL: Record<AutoStepState, Key> = {
  done: "auto.state.done",
  current: "auto.state.current",
  todo: "auto.state.todo",
  failed: "auto.state.failed",
  stopped: "auto.state.stopped",
  warn: "auto.state.warn",
};

function StepMark({ state, index }: { state: AutoStepState; index: number }) {
  let inner: ReactNode = <span className="auto-step-num">{index + 1}</span>;
  if (state === "done") inner = <Check size={12} strokeWidth={3} />;
  else if (state === "current") inner = <span className="auto-step-live" />;
  else if (state === "failed") inner = <X size={12} strokeWidth={3} />;
  else if (state === "stopped") inner = <Pause size={10} strokeWidth={3} />;
  else if (state === "warn") inner = <Minus size={12} strokeWidth={3} />;
  return (
    <span className="auto-step-mark" aria-hidden>
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span key={state} className="auto-step-glyph" initial={{ scale: 0.4, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.4, opacity: 0 }} transition={{ duration: 0.18 }}>
          {inner}
        </motion.span>
      </AnimatePresence>
    </span>
  );
}

/** The five steps of an Auto chat, across the top of its thread. */
export function AutoStepper({ session, view }: { session: ChatSession; view: AutoView }) {
  const { t } = useT();
  const steps = autoSteps(session, view, t);
  // The narrow line names the step where it ended, not the result step after it.
  const ended = autoEndStep(steps);
  // The step to name on a narrow window: where it is, else the last one it got to.
  let nowIndex = ended ? steps.indexOf(ended) : steps.findIndex((step) => step.state === "current");
  if (nowIndex < 0) nowIndex = Math.max(steps.map((step) => step.state !== "todo").lastIndexOf(true), 0);
  const now = steps[nowIndex];
  return (
    <section className="auto-steps" aria-label={t("auto.steps")}>
      <ol className="auto-steps-list">
        {steps.map((step, index) => (
          <li key={step.id} className="auto-step" data-state={step.state} aria-current={step.state === "current" ? "step" : undefined}>
            <span className="auto-step-rail" aria-hidden>
              <StepMark state={step.state} index={index} />
              {index < steps.length - 1 ? <span className="auto-step-line" /> : null}
            </span>
            <span className="auto-step-text" title={step.title}>
              <strong>{t(STEP_LABEL[step.id])}</strong>
              <AnimatePresence mode="wait" initial={false}>
                {step.caption ? (
                  <motion.span key={step.caption} className="auto-step-caption" initial={{ opacity: 0, y: 3 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -3 }} transition={{ duration: 0.16 }}>
                    {step.caption}
                  </motion.span>
                ) : null}
              </AnimatePresence>
              <span className="sr-only">{t(STATE_LABEL[step.state])}</span>
            </span>
          </li>
        ))}
      </ol>
      {/* Narrow windows show one line for where it is, instead of five labels. */}
      <p className="auto-steps-now" aria-hidden>
        <span className="auto-steps-count">{t("auto.steps.position", { n: nowIndex + 1, total: steps.length })}</span>
        <strong>{t(STEP_LABEL[now.id])}</strong>
        {now.caption ? <span>{now.caption}</span> : null}
      </p>
    </section>
  );
}

// ── Interview ────────────────────────────────────────────────────────

// Where an answer came from (auto/answerer.py AutoAnswerSource). Grounded
// answers read as settled; guesses are marked so they can be reviewed.
const SOURCE: Record<string, { key: Key; tone: Tone }> = {
  user_goal: { key: "auto.source.user_goal", tone: "ok" },
  repo_fact: { key: "auto.source.repo_fact", tone: "ok" },
  existing_convention: { key: "auto.source.existing_convention", tone: "ok" },
  user_preference: { key: "auto.source.user_preference", tone: "ok" },
  requester: { key: "auto.source.requester", tone: "ok" },
  conservative_default: { key: "auto.source.conservative_default", tone: "warn" },
  assumption: { key: "auto.source.assumption", tone: "warn" },
  non_goal: { key: "auto.source.non_goal", tone: "muted" },
  blocker: { key: "auto.source.blocker", tone: "fail" },
};

function SourceChip({ source, count }: { source: string | null; count?: number }) {
  const { t } = useT();
  if (!source) return null;
  const known = SOURCE[source];
  return (
    <span className="auto-source" data-tone={known?.tone ?? "muted"}>
      <span className="auto-source-dot" aria-hidden />
      {known ? t(known.key) : source}
      {count !== undefined ? <span className="auto-source-n">{count}</span> : null}
    </span>
  );
}

const score = (value: number) => value.toFixed(2);

/** A few points as a line on a fixed scale from 0, so a small change draws small. */
function Sparkline({ values }: { values: number[] }) {
  const width = 48;
  const height = 16;
  const pad = 2;
  const high = Math.max(0.5, ...values);
  const points = values.map((value, index) => [
    pad + (index * (width - pad * 2)) / (values.length - 1),
    pad + ((high - Math.max(value, 0)) / high) * (height - pad * 2),
  ]);
  const [lastX, lastY] = points[points.length - 1];
  return (
    <svg className="auto-spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden>
      <polyline points={points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")} />
      <circle cx={lastX} cy={lastY} r={2} />
    </svg>
  );
}

/** The ambiguity the interview reported: the latest value, and its trend once there are two. */
function AmbiguityTrend({ view }: { view: AutoView }) {
  const { t } = useT();
  const values = (view.ambiguity_trend ?? []).map((point) => point.value);
  if (values.length === 0) return null;
  const latest = values[values.length - 1];
  const many = values.length > 1;
  return (
    <span
      className="auto-trend"
      role="img"
      aria-label={many ? t("auto.ambiguity.trend", { from: score(values[0]), to: score(latest), count: values.length }) : t("auto.ambiguity", { value: score(latest) })}
      title={many ? `${score(values[0])} → ${score(latest)}` : undefined}
    >
      <span className="auto-trend-label">{t("chat.ambiguity")}</span>
      {many ? <Sparkline values={values} /> : null}
      <span className="auto-trend-value">{score(latest)}</span>
    </span>
  );
}

// The interview's own prefix, for a view saved before the fold split it off.
const AMBIGUITY_PREFIX = /^\(\s*ambiguity\s*:\s*[\d.]+\s*\)\s*/;

function Round({ round, pending }: { round: AutoRound; pending: boolean }) {
  const { t } = useT();
  const ambiguity = typeof round.ambiguity === "number" ? t("auto.ambiguity", { value: score(round.ambiguity) }) : undefined;
  const question = round.question?.replace(AMBIGUITY_PREFIX, "") || round.question;
  return (
    <motion.li className="auto-round" layout="position" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={SETTLE}>
      <div className="auto-q">
        <span className="auto-q-num" aria-hidden title={ambiguity}>
          Q{round.round}
        </span>
        <p>
          <span className="sr-only">
            {t("chat.question", { n: round.round })}
            {ambiguity ? ` (${ambiguity})` : ""}:{" "}
          </span>
          {question ?? "…"}
        </p>
      </div>
      <AnimatePresence mode="wait" initial={false}>
        {round.answer ? (
          <motion.div key="answer" className="auto-a" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.22, ease: EASE_OUT }}>
            <div className="auto-a-head">
              <span className="sr-only">{t("auto.interview.answer")}</span>
              <SourceChip source={round.answer_source} />
            </div>
            <p>{round.answer}</p>
          </motion.div>
        ) : pending ? (
          <motion.p key="pending" className="auto-a auto-pending" role="status" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <Spinner size={13} /> {t("auto.interview.answering")}
          </motion.p>
        ) : (
          <motion.p key="none" className="auto-a auto-unanswered" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
            {t("auto.interview.unanswered")}
          </motion.p>
        )}
      </AnimatePresence>
    </motion.li>
  );
}

const SHOWN_WHILE_LIVE = 4;

function sourceCounts(rounds: AutoRound[]): [string, number][] {
  const counts = new Map<string, number>();
  for (const round of rounds) {
    if (round.answer && round.answer_source) counts.set(round.answer_source, (counts.get(round.answer_source) ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]);
}

/** The interview Auto runs with itself: each question, the answer it chose, and where that came from. */
export function AutoInterview({ view, live }: { view: AutoView; live: boolean }) {
  const { t } = useT();
  const ids = useId();
  const interviewing = live && (view.phase === null || view.phase === "created" || view.phase === "interview");
  const [open, setOpen] = useState(false);
  const [earlier, setEarlier] = useState(false);
  const rounds = view.rounds;
  const last = rounds[rounds.length - 1];
  const answered = answeredCount(view);

  if (!interviewing) {
    // An interview that never asked anything and never handed over to the
    // spec did not finish: say it stopped, without the finished check.
    const passed = view.phases.some((entry) => entry.phase !== "created" && entry.phase !== "interview");
    if (rounds.length === 0 && !passed) {
      return (
        <div className="bubble-card auto-card auto-interview" data-collapsed="">
          <p className="auto-summary auto-summary-static">
            <span className="auto-summary-icon" data-tone="muted" aria-hidden>
              <CircleDashed size={16} />
            </span>
            <span className="auto-summary-text">
              <strong>{t("auto.interview.notStarted")}</strong>
            </span>
          </p>
        </div>
      );
    }
    const counts = sourceCounts(rounds);
    return (
      <div className="bubble-card auto-card auto-interview" data-collapsed={!open || undefined}>
        <button type="button" className="auto-summary" aria-expanded={open} aria-controls={`${ids}-rounds`} onClick={() => setOpen((value) => !value)} disabled={rounds.length === 0}>
          <span className="auto-summary-icon" aria-hidden>
            <CircleCheck size={16} />
          </span>
          <span className="auto-summary-text">
            <strong>{answered ? t("auto.interview.summary", { count: answered }) : t("auto.interview.none")}</strong>
            {counts.length > 0 ? (
              <span className="auto-summary-sources">
                {counts.slice(0, 4).map(([source, count]) => (
                  <SourceChip key={source} source={source} count={count} />
                ))}
              </span>
            ) : null}
          </span>
          <AmbiguityTrend view={view} />
          {rounds.length > 0 ? <ChevronDown className="auto-chevron" size={16} aria-hidden /> : null}
        </button>
        <AnimatePresence initial={false}>
          {open ? (
            <motion.div id={`${ids}-rounds`} className="auto-collapse" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.26, ease: EASE_OUT }}>
              {view.rounds_dropped ? <p className="auto-note">{t("auto.interview.dropped", { count: view.rounds_dropped })}</p> : null}
              <ol className="auto-rounds">
                {rounds.map((round) => (
                  <Round key={round.round} round={round} pending={false} />
                ))}
              </ol>
              <button type="button" className="auto-earlier auto-fold" onClick={() => setOpen(false)}>
                <ChevronDown className="auto-chevron" size={14} aria-hidden data-open="" />
                {t("auto.interview.fold")}
              </button>
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
    );
  }

  const hidden = earlier ? 0 : Math.max(rounds.length - SHOWN_WHILE_LIVE, 0);
  const shown = rounds.slice(hidden);
  const waitingAnswer = !!last && !last.answer;
  return (
    <div className="bubble-card auto-card auto-interview" data-live="">
      <div className="auto-card-head">
        <MessagesSquare size={16} aria-hidden />
        <strong>{t("auto.interview.title")}</strong>
        <span className="auto-card-side">
          <AmbiguityTrend view={view} />
        </span>
      </div>
      <p className="auto-lead">{t("auto.interview.lead")}</p>
      {hidden > 0 || (earlier && rounds.length > SHOWN_WHILE_LIVE) ? (
        <button type="button" className="auto-earlier" aria-expanded={earlier} onClick={() => setEarlier((value) => !value)}>
          <ChevronDown className="auto-chevron" size={14} aria-hidden data-open={earlier || undefined} />
          {earlier ? t("auto.interview.hideEarlier") : t("auto.interview.earlier", { count: hidden + view.rounds_dropped })}
        </button>
      ) : null}
      <ol className="auto-rounds" aria-live="polite" aria-relevant="additions text" aria-label={t("auto.interview.title")}>
        {shown.map((round) => (
          <Round key={round.round} round={round} pending={round === last && waitingAnswer} />
        ))}
        <AnimatePresence initial={false}>
          {!waitingAnswer ? (
            <motion.li key={`next-${last?.round ?? 0}`} className="auto-next" role="status" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, transition: { duration: 0.1 } }}>
              <Spinner size={14} />
              <span>{last ? t("auto.interview.thinking") : t("auto.interview.first")}</span>
            </motion.li>
          ) : null}
        </AnimatePresence>
      </ol>
    </div>
  );
}

// ── What it is doing now ─────────────────────────────────────────────

/** The line under the thread saying what Auto is doing between the cards, or `null`. */
export function autoActivity(session: ChatSession, view: AutoView): Key | null {
  if (session.status !== "running") return null;
  const run = lastOf(session.messages, "run");
  switch (view.phase) {
    // An engine from before #3064 reviews the spec after writing it: still the spec step.
    case "seed_generation":
    case "review":
    case "repair":
      return "auto.activity.seed";
    case "run":
      return run ? null : "auto.activity.run";
    case "complete":
      if (!run) return "auto.activity.run";
      return run.run_status === "completed" || run.run_status === "failed" ? "auto.activity.finishing" : null;
    case "ralph_handoff":
    case "evaluate":
      return "auto.activity.verify";
    case "unstuck_lateral":
      return "auto.activity.unstuck";
    default:
      return null; // the interview card shows its own progress
  }
}

// ── How it ended ─────────────────────────────────────────────────────

/**
 * The footer of an Auto chat that did not finish verified: what happened and
 * where to look, or `null` while it runs or when it completed. A failed job's
 * exit code is said here rather than as a notice in the thread.
 */
export function autoEnding(session: ChatSession, view: AutoView, t: Translator["t"]): { title: Key; body: string } | null {
  if (session.status === "running" || view.outcome === "completed") return null;
  if (view.outcome === "unverified") return { title: "auto.end.unverified.title", body: t("auto.end.unverified.body") };
  if (session.status !== "failed" && session.status !== "interrupted") return null;
  const ended = autoEndStep(autoSteps(session, view, t));
  const step = ended ? t(STEP_LABEL[ended.id]) : null;
  if (view.outcome === "stopped") return { title: "auto.end.stopped.title", body: step ? t("auto.end.stoppedAt", { step }) : "" };
  if (!view.outcome && session.status === "interrupted") return { title: "auto.end.failed.title", body: t("auto.end.restarted") };
  let body = ended?.id === "run" ? t("auto.end.run") : t("auto.end.other");
  const exited = [...session.messages].reverse().find((message) => message.kind === "error" && message.key === "auto_exited");
  const code = exited?.vars?.code;
  if (code !== undefined && code !== null) body = `${body} ${t("auto.end.code", { code })}`;
  return { title: "auto.end.failed.title", body };
}

// ── Raw output ───────────────────────────────────────────────────────

// structlog's console lines: `2026-11-16T23:54:22.448527Z [info     ] event …`.
const INTERNAL = /^\d{4}-\d{2}-\d{2}T\S+\s+\[\s*(debug|info|warning|warn|error|critical|exception)\s*\]/i;

/** The job's own output, folded away; internal log lines stay hidden until asked for. */
export function AutoLog({ message, live, view }: { message: ChatMessage; live: boolean; view: AutoView }) {
  const { t } = useT();
  const ids = useId();
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  const lines = message.lines ?? [];
  const internal = lines.filter((line) => INTERNAL.test(line)).length;
  const shown = all ? lines : lines.filter((line) => !INTERNAL.test(line));
  return (
    <div className="auto-log">
      <Button
        size="sm"
        variant="ghost"
        className="auto-log-toggle"
        icon={<ScrollText size={14} aria-hidden />}
        trailing={<ChevronDown className="auto-chevron" size={14} aria-hidden data-open={open || undefined} />}
        aria-expanded={open}
        aria-controls={`${ids}-log`}
        onClick={() => setOpen((value) => !value)}
      >
        {open ? t("auto.log.hide") : t("auto.log.show")}
        <span className="auto-log-count">{t("auto.log.lines", { count: shown.length })}</span>
      </Button>
      <AnimatePresence initial={false}>
        {open ? (
          <motion.div
            id={`${ids}-log`}
            className="auto-collapse"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.24, ease: EASE_OUT }}
            onAnimationComplete={(definition) => {
              // Bring the opened log into view; closing leaves the scroll alone.
              if (typeof definition === "object" && definition !== null && "height" in definition && definition.height === "auto") {
                document.getElementById(`${ids}-log`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
              }
            }}
          >
            <div className="auto-log-body">
              <div className="auto-log-tools">
                <code>{message.text}</code>
                {internal > 0 ? (
                  <label className="auto-log-all">
                    <input type="checkbox" checked={all} onChange={(event) => setAll(event.target.checked)} />
                    {t("auto.log.all", { count: internal })}
                  </label>
                ) : null}
              </div>
              <LogView lines={shown} live={live} emptyText={t("jobs.waiting")} maxHeight={360} />
              {view.skipped > 0 ? <p className="auto-note">{t("auto.log.skipped", { count: view.skipped })}</p> : null}
              {view.read_error ? <p className="auto-note">{t("auto.log.readError", { error: view.read_error })}</p> : null}
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}
