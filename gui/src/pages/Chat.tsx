import {
  ArrowLeft,
  ArrowRight,
  Check,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleX,
  FileText,
  Folder,
  GitBranch,
  MessagesSquare,
  Play,
  Plus,
  Send,
  Sparkles,
  Sprout,
  Square,
  Trash2,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import { useT } from "../i18n";
import { useAction } from "../core/action";
import { del, post } from "../core/api";
import { backendName, shortPath, timeAgo } from "../core/format";
import { invalidate, useResource } from "../core/resource";
import { href, navigate } from "../core/router";
import { useSnapshotStream } from "../core/stream";
import type { BackendChoice, ChatDefaults, ChatMessage, ChatMode, ChatSession, ChatSessionRow, ChatSessionsView, ChatStatus, RunTile } from "../core/types";
import type { Tone } from "../ui/bits";
import { Chip, CopyButton, Empty, Field, LoadError, LogView, Notice, Skeleton } from "../ui/bits";
import { Button, IconButton } from "../ui/Button";
import { Spinner } from "../ui/Mark";
import { Modal } from "../ui/Modal";
import { Picker } from "../ui/Picker";
import { Segmented } from "../ui/Segmented";
import { Progress, RunStatus } from "./Runs";
import { GLIDE, SETTLE } from "../core/motion";

const FOLLOW = "__settings__";
const READY_AT = 0.2;

const STATUS_TONE: Record<ChatStatus, Tone> = {
  thinking: "muted",
  waiting_answer: "info",
  seed_ready_to_write: "info",
  seed_ready: "ok",
  running: "muted",
  done: "ok",
  failed: "fail",
  interrupted: "warn",
};

function StatusChip({ status }: { status: ChatStatus }) {
  const { tx } = useT();
  return (
    <Chip tone={STATUS_TONE[status] ?? "muted"} pulse={status === "thinking" || status === "running"}>
      {tx(`chat.status.${status}`, status)}
    </Chip>
  );
}

// ── New conversation ─────────────────────────────────────────────────

function NewChat({ defaults }: { defaults: ChatDefaults | undefined }) {
  const { t, tx } = useT();
  const ids = useId();
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState<ChatMode>("interview");
  const [cwd, setCwd] = useState("");
  const [backend, setBackend] = useState(FOLLOW);
  const [model, setModel] = useState("");
  const goalRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (defaults && !cwd) setCwd(defaults.cwd);
  }, [defaults, cwd]);
  // An agent that cannot serve the new mode falls back to the settings default.
  const pool: BackendChoice[] = (mode === "ask" ? defaults?.llm : defaults?.agents) ?? [];
  useEffect(() => {
    if (backend !== FOLLOW && !pool.some((item) => item.id === backend)) setBackend(FOLLOW);
  }, [backend, pool]);

  const [start, starting] = useAction(
    async () => {
      const session = await post<ChatSession>("/api/chat/sessions", {
        goal,
        mode,
        cwd,
        backend: backend === FOLLOW ? "" : backend,
        model: mode === "ask" ? model : "",
      });
      invalidate("/api/chat/sessions");
      navigate("chat", session.id);
    },
    { tx, failTitle: t("chat.new.failed") },
  );

  const chosen = pool.find((item) => item.id === backend);
  const suggestions = [
    ...(chosen?.models ?? []).filter((item) => item !== "default"),
    ...(backend === "litellm" ? (defaults?.providers ?? []).map((item) => item.model_format) : []),
  ];
  const followed = mode === "ask" ? undefined : defaults?.default_runtime;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (goal.trim()) void start();
  }

  return (
    <motion.form className="composer-hero" onSubmit={submit} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}>
      <h1 className="composer-hero-title">{t(`chat.new.title.${mode}`)}</h1>
      <p className="composer-hero-lead">{t(`chat.mode.${mode}.lead`)}</p>
      <Segmented<ChatMode>
        label={t("chat.mode")}
        value={mode}
        onChange={setMode}
        options={[
          { value: "interview", label: t("chat.mode.interview"), icon: <MessagesSquare size={14} aria-hidden /> },
          { value: "auto", label: t("chat.mode.auto"), icon: <Play size={14} aria-hidden /> },
          { value: "ask", label: t("chat.mode.ask"), icon: <Sparkles size={14} aria-hidden /> },
        ]}
      />
      <div className="composer-box">
        <label className="sr-only" htmlFor={`${ids}-goal`}>
          {t(`chat.new.title.${mode}`)}
        </label>
        <textarea
          id={`${ids}-goal`}
          ref={goalRef}
          className="composer-input"
          rows={4}
          value={goal}
          placeholder={t(`chat.new.placeholder.${mode}`)}
          onChange={(event) => setGoal(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && goal.trim()) {
              event.preventDefault();
              void start();
            }
          }}
        />
        <div className="composer-bar">
          <span className="composer-hint">{t("chat.new.shortcut")}</span>
          <Button type="submit" variant="primary" pending={starting} disabled={!goal.trim()} trailing={<ArrowRight size={15} aria-hidden />}>
            {t(`chat.new.start.${mode}`)}
          </Button>
        </div>
      </div>
      {mode !== "ask" ? (
        <div className="starters" aria-label={t("chat.new.starters")}>
          {(["a", "b", "c"] as const).map((key) => (
            <button
              key={key}
              type="button"
              className="starter"
              onClick={() => {
                setGoal(t(`chat.new.starter.${key}`));
                goalRef.current?.focus();
              }}
            >
              {t(`chat.new.starter.${key}`)}
            </button>
          ))}
        </div>
      ) : null}
      <div className="composer-options">
        {mode !== "ask" ? (
          <Field label={t("chat.new.folder")} htmlFor={`${ids}-cwd`} hint={t("chat.new.folder.hint")}>
            <div className="input-icon">
              <Folder size={15} aria-hidden />
              <input id={`${ids}-cwd`} className="input input-mono" value={cwd} spellCheck={false} autoComplete="off" onChange={(event) => setCwd(event.target.value)} />
            </div>
          </Field>
        ) : null}
        <Field label={mode === "ask" ? t("chat.new.backend") : t("chat.new.agent")} htmlFor={`${ids}-agent`} hint={mode === "ask" ? undefined : t("chat.new.agent.hint")}>
          <Picker
            id={`${ids}-agent`}
            value={backend}
            onChange={setBackend}
            options={[
              { value: FOLLOW, label: followed ? t("chat.new.follow", { agent: backendName(followed) }) : t("chat.new.followPlain") },
              ...pool.map((item) => ({ value: item.id, label: backendName(item.id), hint: item.installed ? undefined : t("models.notInstalled") })),
            ]}
          />
        </Field>
        {mode === "ask" ? (
          <Field label={t("models.model")} htmlFor={`${ids}-model`} hint={backend === "litellm" ? t("chat.new.model.litellm") : t("chat.new.model.hint")}>
            <input id={`${ids}-model`} className="input input-mono" list={`${ids}-models`} value={model} spellCheck={false} autoComplete="off" placeholder={t("chat.new.model.placeholder")} onChange={(event) => setModel(event.target.value)} />
            <datalist id={`${ids}-models`}>
              {suggestions.map((item) => (
                <option key={item} value={item} />
              ))}
            </datalist>
          </Field>
        ) : null}
      </div>
    </motion.form>
  );
}

// ── Message kinds ────────────────────────────────────────────────────

const TILE_ICON = {
  pending: CircleDashed,
  executing: Spinner,
  completed: CircleCheck,
  failed: CircleX,
};

function Tile({ tile }: { tile: RunTile }) {
  const { tx } = useT();
  const Icon = TILE_ICON[tile.status] ?? CircleDashed;
  return (
    <motion.li layout className={`tile tile-${tile.status}`} initial={{ opacity: 0, scale: 0.92 }} animate={{ opacity: 1, scale: 1 }} transition={SETTLE}>
      <span className="tile-icon">
        <AnimatePresence mode="wait" initial={false}>
          <motion.span key={tile.status} initial={{ scale: 0.4, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.4, opacity: 0 }} transition={{ duration: 0.16 }}>
            <Icon size={15} aria-hidden />
          </motion.span>
        </AnimatePresence>
      </span>
      <span className="tile-title">{tile.title}</span>
      <span className="sr-only">{tx(`run.column.${tile.status}`, tile.status)}</span>
    </motion.li>
  );
}

function RunCard({ message }: { message: ChatMessage }) {
  const { t } = useT();
  const progress = message.progress;
  const status = message.run_status ?? "running";
  return (
    <div className="bubble-card run-card">
      <div className="run-card-head">
        <strong>{t("chat.run.title")}</strong>
        <RunStatus status={status} />
        {progress?.phase ? <Chip>{progress.phase}</Chip> : null}
        {message.execution_id ? (
          <a className="more-link" href={href("runs", message.execution_id)}>
            {t("chat.run.board")}
            <ArrowRight size={13} aria-hidden />
          </a>
        ) : null}
      </div>
      {progress ? (
        <>
          <div className="run-card-progress">
            <Progress done={progress.done} total={progress.total} failed={progress.counts.failed} label={t("runs.progress")} />
            <span>{t("runs.count", { done: progress.done, total: progress.total })}</span>
          </div>
          {progress.activity ? <p className="run-card-activity">{progress.activity}</p> : null}
          {progress.tiles.length > 0 ? (
            <ul className="tiles">
              {progress.tiles.map((tile) => (
                <Tile key={tile.id} tile={tile} />
              ))}
            </ul>
          ) : null}
        </>
      ) : (
        <p className="run-card-activity">
          <Spinner size={13} /> {t("chat.run.starting")}
        </p>
      )}
    </div>
  );
}

function SeedCard({ message }: { message: ChatMessage }) {
  const { t } = useT();
  const [yaml, setYaml] = useState(false);
  const criteria = message.acceptance_criteria ?? [];
  const constraints = message.constraints ?? [];
  return (
    <div className="bubble-card seed-card">
      <div className="seed-head">
        <Sprout size={16} aria-hidden />
        <strong>{t("chat.seed.title")}</strong>
      </div>
      <p className="seed-goal">{message.text}</p>
      {criteria.length > 0 ? (
        <>
          <h4>{t("chat.seed.criteria", { count: criteria.length })}</h4>
          <ol className="seed-list">
            {criteria.map((item, index) => (
              <li key={index}>
                <span className="seed-num" aria-hidden>
                  {index + 1}
                </span>
                {item}
              </li>
            ))}
          </ol>
        </>
      ) : null}
      {constraints.length > 0 ? (
        <>
          <h4>{t("chat.seed.constraints")}</h4>
          <ul className="seed-bullets">
            {constraints.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </>
      ) : null}
      {message.seed_yaml ? (
        <>
          <div className="seed-actions">
            <Button size="sm" variant="ghost" icon={<FileText size={14} aria-hidden />} aria-expanded={yaml} onClick={() => setYaml((value) => !value)}>
              {yaml ? t("chat.seed.hideYaml") : t("chat.seed.showYaml")}
            </Button>
            <CopyButton text={message.seed_yaml} label={t("chat.seed.copyYaml")} />
          </div>
          <AnimatePresence initial={false}>
            {yaml ? (
              <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.24 }} style={{ overflow: "hidden" }}>
                <pre className="log seed-yaml" tabIndex={0}>
                  {message.seed_yaml}
                </pre>
              </motion.div>
            ) : null}
          </AnimatePresence>
        </>
      ) : null}
    </div>
  );
}

function ResultCard({ message }: { message: ChatMessage }) {
  const { t } = useT();
  const files = message.files ?? [];
  return (
    <div className="bubble-card result-card">
      <div className="seed-head">
        <GitBranch size={16} aria-hidden />
        <strong>{t("chat.result.title")}</strong>
      </div>
      <dl className="kv">
        {message.worktree ? (
          <div>
            <dt>{t("chat.result.worktree")}</dt>
            <dd>
              <code>{message.worktree}</code>
              <CopyButton text={message.worktree} />
            </dd>
          </div>
        ) : null}
        {message.branch ? (
          <div>
            <dt>{t("chat.result.branch")}</dt>
            <dd>
              <code>{message.branch}</code>
              <CopyButton text={message.branch} />
            </dd>
          </div>
        ) : null}
        {message.base ? (
          <div>
            <dt>{t("chat.result.base")}</dt>
            <dd>
              <code>{message.base}</code>
            </dd>
          </div>
        ) : null}
      </dl>
      <h4>
        {t("chat.result.files", { count: message.file_count ?? files.length })}
        {message.uncommitted ? <Chip tone="warn">{t("chat.result.uncommitted", { count: message.uncommitted })}</Chip> : null}
      </h4>
      {files.length > 0 ? (
        <ul className="file-list">
          {files.map((file) => (
            <li key={file}>
              <code>{file}</code>
            </li>
          ))}
        </ul>
      ) : (
        <p className="section-note">{t("chat.result.noFiles")}</p>
      )}
      {(message.file_count ?? 0) > files.length ? <p className="section-note">{t("chat.result.more", { count: (message.file_count ?? 0) - files.length })}</p> : null}
    </div>
  );
}

function MessageView({ message, running, questionNumber }: { message: ChatMessage; running: boolean; questionNumber: number }) {
  const { t } = useT();
  let body: ReactNode;
  let shape = "left";
  switch (message.kind) {
    case "question":
      body = (
        <div className="bubble bubble-question">
          <span className="bubble-tag">{t("chat.question", { n: questionNumber })}</span>
          <p>{message.text}</p>
        </div>
      );
      break;
    case "note":
      shape = "center";
      body = <p className="bubble-note">{message.text}</p>;
      break;
    case "error":
      body = (
        <Notice tone="fail" icon={<CircleAlert size={16} aria-hidden />}>
          {message.text}
        </Notice>
      );
      break;
    case "seed":
      body = <SeedCard message={message} />;
      break;
    case "run":
      body = <RunCard message={message} />;
      break;
    case "result":
      body = <ResultCard message={message} />;
      break;
    case "log":
      body = (
        <div className="bubble-card">
          <div className="seed-head">
            <code>{message.text}</code>
          </div>
          <LogView lines={message.lines ?? []} live={running} emptyText={t("jobs.waiting")} maxHeight={360} />
        </div>
      );
      break;
    default:
      shape = message.role === "user" ? "right" : "left";
      body = (
        <div className={`bubble ${message.role === "user" ? "bubble-user" : "bubble-assistant"}`} data-local={message.id.startsWith("local-") || undefined}>
          <p>{message.text}</p>
          {message.model ? <span className="bubble-model">{message.model}</span> : null}
        </div>
      );
  }
  return (
    <motion.li
      layout="position"
      className={`msg msg-${shape}`}
      initial={{ opacity: 0, y: 14, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={SETTLE}
    >
      {body}
    </motion.li>
  );
}

/** How far the interview is from "clear enough": the gate sits at 0.2. */
function AmbiguityMeter({ value }: { value: number }) {
  const { t } = useT();
  const clamped = Math.min(Math.max(value, 0), 1);
  const ready = clamped <= READY_AT;
  return (
    <div className="ambiguity" data-ready={ready || undefined}>
      <span className="ambiguity-label">{t("chat.ambiguity")}</span>
      <div className="ambiguity-track" role="meter" aria-label={t("chat.ambiguity")} aria-valuemin={0} aria-valuemax={1} aria-valuenow={Number(clamped.toFixed(2))} aria-valuetext={t("chat.ambiguity.text", { value: clamped.toFixed(2) })}>
        <motion.span className="ambiguity-fill" animate={{ width: `${clamped * 100}%` }} transition={GLIDE} />
        <span className="ambiguity-gate" style={{ left: `${READY_AT * 100}%` }} aria-hidden />
      </div>
      <span className="ambiguity-value">{clamped.toFixed(2)}</span>
      <span className="ambiguity-goal">{ready ? t("chat.ambiguity.ready") : t("chat.ambiguity.goal")}</span>
    </div>
  );
}

// ── Context pane ─────────────────────────────────────────────────────

type StepState = "done" | "current" | "todo" | "failed";

function stepsOf(session: ChatSession): { id: "interview" | "seed" | "run" | "result"; state: StepState }[] {
  const hasSeed = !!session.seed_yaml;
  const hasRun = session.messages.some((message) => message.kind === "run");
  const hasResult = session.messages.some((message) => message.kind === "result");
  const failed = session.status === "failed";
  return [
    { id: "interview", state: hasSeed || session.status === "seed_ready_to_write" ? "done" : failed ? "failed" : "current" },
    { id: "seed", state: hasSeed ? "done" : session.seed_ready || session.status === "seed_ready_to_write" ? "current" : "todo" },
    { id: "run", state: session.status === "done" ? "done" : hasRun ? (failed ? "failed" : "current") : hasSeed ? "current" : "todo" },
    { id: "result", state: hasResult ? "done" : "todo" },
  ];
}

const STEP_LABEL = { interview: "chat.mode.interview", seed: "chat.seed.title", run: "chat.run.title", result: "chat.result.title" } as const;

/** The right-hand pane: where this conversation is in the pipeline, and its facts. */
function ContextPane({ session }: { session: ChatSession }) {
  const { t, locale } = useT();
  const seed = [...session.messages].reverse().find((message) => message.kind === "seed");
  const run = [...session.messages].reverse().find((message) => message.kind === "run");
  return (
    <aside className="chat-pane-side" aria-label={t("chat.pane.title")}>
      {session.mode === "interview" ? (
        <section>
          <h2 className="pane-title">{t("chat.pane.steps")}</h2>
          <ol className="steps-rail">
            {stepsOf(session).map((step, index) => (
              <li key={step.id} data-state={step.state}>
                <span className="steps-rail-mark" aria-hidden>
                  {step.state === "done" ? <Check size={11} /> : index + 1}
                </span>
                <span>{t(STEP_LABEL[step.id])}</span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}
      <section>
        <h2 className="pane-title">{t("chat.pane.title")}</h2>
        <dl className="rows rows-tight">
          <div>
            <dt>{t("chat.mode")}</dt>
            <dd>{t(`chat.mode.${session.mode}`)}</dd>
          </div>
          <div>
            <dt>{t("models.agent")}</dt>
            <dd>{session.backend ? backendName(session.backend) : t("chat.new.followPlain")}</dd>
          </div>
          {session.model ? (
            <div>
              <dt>{t("models.model")}</dt>
              <dd>
                <code>{session.model}</code>
              </dd>
            </div>
          ) : null}
          {session.mode !== "ask" ? (
            <div>
              <dt>{t("chat.new.folder")}</dt>
              <dd>
                <code title={session.cwd}>{shortPath(session.cwd)}</code>
              </dd>
            </div>
          ) : null}
          <div>
            <dt>{t("shell.updated", { time: "" }).trim()}</dt>
            <dd>{timeAgo(session.updated_at, locale)}</dd>
          </div>
        </dl>
      </section>
      {seed ? (
        <section>
          <h2 className="pane-title">{t("chat.seed.title")}</h2>
          <p className="pane-text">{seed.text}</p>
          <p className="pane-note">{t("chat.seed.criteria", { count: seed.acceptance_criteria?.length ?? 0 })}</p>
        </section>
      ) : null}
      {run?.progress ? (
        <section>
          <h2 className="pane-title">{t("chat.run.title")}</h2>
          <Progress done={run.progress.done} total={run.progress.total} failed={run.progress.counts.failed} label={t("runs.progress")} />
          <p className="pane-note">{t("runs.count", { done: run.progress.done, total: run.progress.total })}</p>
          {run.execution_id ? (
            <a className="more-link" href={href("runs", run.execution_id)}>
              {t("chat.run.board")}
              <ArrowRight size={13} aria-hidden />
            </a>
          ) : null}
        </section>
      ) : null}
    </aside>
  );
}

function ModesPane() {
  const { t } = useT();
  return (
    <aside className="chat-pane-side" aria-label={t("chat.mode")}>
      <section>
        <h2 className="pane-title">{t("chat.mode")}</h2>
        <dl className="modes">
          {(["interview", "auto", "ask"] as const).map((mode) => (
            <div key={mode}>
              <dt>{t(`chat.mode.${mode}`)}</dt>
              <dd>{t(`chat.mode.${mode}.lead`)}</dd>
            </div>
          ))}
        </dl>
      </section>
    </aside>
  );
}

// ── One conversation ─────────────────────────────────────────────────

function Thread({ id }: { id: string }) {
  const { t, tx } = useT();
  const path = `/api/chat/sessions/${encodeURIComponent(id)}`;
  const loaded = useResource<ChatSession>(path);
  const stream = useSnapshotStream<ChatSession>(`${path}/stream`);
  const [session, setSession] = useState<ChatSession | undefined>(loaded.data);
  const [draft, setDraft] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Whichever source is newer wins; the stream usually is.
  const accept = (next: ChatSession | null | undefined) => {
    if (!next) return;
    setSession((current) => (!current || next.version >= current.version || current.messages.some((m) => m.id.startsWith("local-")) ? next : current));
  };
  useEffect(() => accept(loaded.data), [loaded.data]);
  useEffect(() => accept(stream.latest), [stream.latest]);

  const count = session?.messages.length ?? 0;
  const tail = session?.messages[count - 1];
  const tailSize = (tail?.lines?.length ?? 0) + (tail?.progress?.tiles.length ?? 0);
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element && pinned.current) element.scrollTo({ top: element.scrollHeight, behavior: count > 1 ? "smooth" : "auto" });
  }, [count, tailSize, session?.status, session?.suggestion?.pending, session?.suggestion?.text]);

  const [send, sending] = useAction(
    async (text: string) => {
      const before = session;
      // Show the message at once; the server's snapshot replaces it.
      setSession((current) =>
        current
          ? {
              ...current,
              status: "thinking",
              suggestion: undefined,
              messages: [...current.messages, { id: `local-${Date.now()}`, role: "user", kind: "text", text, ts: Date.now() / 1000 }],
            }
          : current,
      );
      setDraft("");
      pinned.current = true;
      try {
        const next = await post<ChatSession>(`${path}/messages`, { text });
        setSession(next);
      } catch (failure) {
        setSession(before);
        setDraft(text);
        throw failure;
      }
    },
    { tx, failTitle: t("chat.sendFailed") },
  );

  const [act, acting] = useAction(
    async (action: "seed" | "run" | "suggest" | "stop", extra: Record<string, unknown> = {}) => {
      const next = await post<ChatSession>(`${path}/actions`, { action, ...extra });
      setSession(next);
      if (action === "run") invalidate("/api/runs", "/api/overview");
    },
    { tx, failTitle: t("chat.actionFailed") },
  );
  const [lastAction, setLastAction] = useState<string>("");
  const run = (action: "seed" | "run" | "suggest" | "stop", extra?: Record<string, unknown>) => {
    setLastAction(action);
    return act(action, extra);
  };

  const [remove, removing] = useAction(
    async () => {
      await del(path);
      invalidate("/api/chat/sessions");
      setConfirmDelete(false);
      navigate("chat");
    },
    { tx, failTitle: t("chat.deleteFailed") },
  );

  if (loaded.error && !session) {
    return (
      <div className="chat-thread">
        {loaded.error.status === 404 ? (
          <Empty
            icon={<MessagesSquare size={20} aria-hidden />}
            title={t("chat.missing")}
            action={
              <a className="btn btn-primary" href={href("chat")}>
                <span className="btn-label">{t("chat.new")}</span>
              </a>
            }
          />
        ) : (
          <LoadError error={loaded.error} onRetry={() => void loaded.reload()} />
        )}
      </div>
    );
  }
  if (!session) {
    return (
      <div className="chat-thread chat-thread-pad">
        <Skeleton lines={6} />
      </div>
    );
  }

  const { status, mode } = session;
  const ask = mode === "ask";
  const canType = ask ? ["waiting_answer", "done", "failed", "interrupted"].includes(status) : status === "waiting_answer";
  const suggestion = session.suggestion;
  const canWriteSeed = mode === "interview" && !!session.interview_session_id && ["waiting_answer", "seed_ready_to_write", "interrupted", "failed"].includes(status) && !session.seed_yaml;
  const seedReady = !!session.seed_ready || status === "seed_ready_to_write";
  const canRun = mode === "interview" && !!session.seed_yaml && status !== "running" && status !== "thinking" && status !== "done";
  const busyAction = (name: string) => acting && lastAction === name;
  let questionNumber = 0;

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const text = draft.trim();
    if (text && canType && !sending) void send(text);
  }

  function onKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="thread-layout">
    <div className="chat-thread">
      <header className="thread-head">
        <a className="icon-btn thread-back" href={href("chat")} aria-label={t("chat.back")}>
          <ArrowLeft size={16} aria-hidden />
        </a>
        <div className="thread-title">
          <h1>{session.title}</h1>
          <p>
            <Chip>{t(`chat.mode.${mode}`)}</Chip>
            {session.backend ? <Chip tone="info">{backendName(session.backend)}</Chip> : null}
            {session.model ? <Chip>{session.model}</Chip> : null}
            {!ask ? (
              <span className="path" title={session.cwd}>
                {shortPath(session.cwd)}
              </span>
            ) : null}
          </p>
        </div>
        <AnimatePresence mode="wait" initial={false}>
          <motion.span key={status} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.16 }}>
            <StatusChip status={status} />
          </motion.span>
        </AnimatePresence>
        <IconButton label={t("chat.delete")} onClick={() => setConfirmDelete(true)}>
          <Trash2 size={16} aria-hidden />
        </IconButton>
      </header>

      {mode === "interview" && typeof session.ambiguity === "number" ? <AmbiguityMeter value={session.ambiguity} /> : null}

      <div
        className="thread-scroll"
        ref={scroller}
        onScroll={(event) => {
          const element = event.currentTarget;
          pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
        }}
      >
        <ol className="thread" aria-live="polite" aria-label={t("chat.thread")}>
          {session.messages.map((message) => {
            if (message.kind === "question") questionNumber += 1;
            return <MessageView key={message.id} message={message} running={status === "running"} questionNumber={questionNumber} />;
          })}
          <AnimatePresence>
            {status === "thinking" ? (
              <motion.li key="thinking" className="msg msg-left" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, transition: { duration: 0.1 } }}>
                <div className="thinking" role="status">
                  <Spinner size={16} />
                  <span>{t("chat.thinking")}</span>
                  {session.activity && session.activity.length > 0 ? <code>{session.activity[session.activity.length - 1]}</code> : null}
                </div>
              </motion.li>
            ) : null}
          </AnimatePresence>
        </ol>
      </div>

      <footer className="thread-foot">
        <AnimatePresence initial={false}>
          {suggestion && (suggestion.pending || suggestion.text || suggestion.error) && status === "waiting_answer" ? (
            <motion.div key="suggestion" className="suggestion" initial={{ opacity: 0, y: 12, height: 0 }} animate={{ opacity: 1, y: 0, height: "auto" }} exit={{ opacity: 0, height: 0 }} transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}>
              {suggestion.pending ? (
                <p className="suggestion-pending" role="status">
                  <Spinner size={14} /> {t("chat.suggest.pending")}
                </p>
              ) : suggestion.error ? (
                <Notice tone="warn" title={t("chat.suggest.failed")}>
                  {suggestion.error}
                </Notice>
              ) : (
                <>
                  <div className="suggestion-head">
                    <Sparkles size={14} aria-hidden />
                    <strong>{t("chat.suggest.title")}</strong>
                    <Chip tone={suggestion.source === "repo" ? "ok" : "muted"}>{suggestion.source === "repo" ? t("chat.suggest.fromRepo") : t("chat.suggest.fromModel")}</Chip>
                  </div>
                  <p className="suggestion-text">{suggestion.text}</p>
                  {suggestion.evidence && suggestion.evidence.length > 0 ? (
                    <ul className="suggestion-evidence">
                      {suggestion.evidence.map((item, index) => (
                        <li key={index}>
                          <code>{item}</code>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  <div className="suggestion-actions">
                    <Button
                      size="sm"
                      onClick={() => {
                        setDraft(suggestion.text ?? "");
                        inputRef.current?.focus();
                      }}
                    >
                      {t("chat.suggest.edit")}
                    </Button>
                    <Button size="sm" variant="primary" pending={sending} icon={<Check size={14} aria-hidden />} onClick={() => suggestion.text && void send(suggestion.text)}>
                      {t("chat.suggest.send")}
                    </Button>
                  </div>
                </>
              )}
            </motion.div>
          ) : null}
        </AnimatePresence>

        <AnimatePresence initial={false} mode="popLayout">
          {canWriteSeed && seedReady ? (
            <motion.div key="seed" className="next-step" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <div>
                <strong>{t("chat.next.seed.title")}</strong>
                <span>{t("chat.next.seed.body")}</span>
              </div>
              <Button variant="primary" icon={<Sprout size={15} aria-hidden />} pending={busyAction("seed")} onClick={() => void run("seed")}>
                {t("chat.next.seed")}
              </Button>
            </motion.div>
          ) : null}
          {canRun ? (
            <motion.div key="run" className="next-step" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <div>
                <strong>{t("chat.next.run.title")}</strong>
                <span>{t("chat.next.run.body")}</span>
              </div>
              <Button variant="primary" icon={<Play size={15} aria-hidden />} pending={busyAction("run")} onClick={() => void run("run")}>
                {status === "failed" || status === "interrupted" ? t("chat.next.runAgain") : t("chat.next.run")}
              </Button>
            </motion.div>
          ) : null}
          {mode === "auto" && status === "running" ? (
            <motion.div key="stop" className="next-step" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <div>
                <strong>{t("chat.next.auto.title")}</strong>
                <span>{t("chat.next.auto.body")}</span>
              </div>
              <Button variant="danger" icon={<Square size={13} aria-hidden />} pending={busyAction("stop")} onClick={() => void run("stop")}>
                {t("chat.stop")}
              </Button>
            </motion.div>
          ) : null}
          {status === "done" && !ask ? (
            <motion.div key="done" className="next-step next-step-done" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <div>
                <strong>{t("chat.next.done.title")}</strong>
                <span>{t("chat.next.done.body")}</span>
              </div>
              <a className="btn btn-secondary" href={href("chat")}>
                <Plus size={15} aria-hidden />
                <span className="btn-label">{t("chat.new")}</span>
              </a>
            </motion.div>
          ) : null}
        </AnimatePresence>

        {mode !== "auto" && !(mode === "interview" && (session.seed_yaml || status === "seed_ready_to_write")) ? (
          <form className="composer-box composer-inline" onSubmit={submit} data-disabled={!canType || undefined}>
            <label className="sr-only" htmlFor="thread-input">
              {ask ? t("chat.input.ask") : t("chat.input.answer")}
            </label>
            <textarea
              id="thread-input"
              ref={inputRef}
              className="composer-input"
              rows={2}
              value={draft}
              disabled={!canType}
              placeholder={canType ? (ask ? t("chat.input.ask") : t("chat.input.answer")) : t("chat.input.wait")}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKey}
            />
            <div className="composer-bar">
              <div className="composer-tools">
                {mode === "interview" ? (
                  <Button size="sm" variant="ghost" icon={<Sparkles size={14} aria-hidden />} pending={busyAction("suggest") || !!suggestion?.pending} disabled={status !== "waiting_answer" || !session.last_question} onClick={() => void run("suggest")}>
                    {t("chat.suggest")}
                  </Button>
                ) : null}
                {canWriteSeed && !seedReady ? (
                  <Button size="sm" variant="ghost" icon={<Sprout size={14} aria-hidden />} pending={busyAction("seed")} onClick={() => void run("seed", { force: true })} title={t("chat.seed.early.hint")}>
                    {t("chat.seed.early")}
                  </Button>
                ) : null}
              </div>
              <Button type="submit" variant="primary" size="sm" pending={sending} disabled={!canType || !draft.trim()} icon={<Send size={14} aria-hidden />}>
                {t("chat.send")}
              </Button>
            </div>
          </form>
        ) : null}
      </footer>

      <Modal
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("chat.delete.title")}
        description={t("chat.delete.body")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
              {t("common.cancel")}
            </Button>
            <Button variant="danger" pending={removing} onClick={() => void remove()}>
              {t("chat.delete")}
            </Button>
          </>
        }
      />
    </div>
    <ContextPane session={session} />
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────

function SessionList({ rows, active, loading }: { rows: ChatSessionRow[] | undefined; active: string; loading: boolean }) {
  const { t, locale } = useT();
  return (
    <aside className="chat-list" aria-label={t("chat.list")}>
      <a className="btn btn-secondary chat-new" href={href("chat")} aria-current={active ? undefined : "page"}>
        <Plus size={15} aria-hidden />
        <span className="btn-label">{t("chat.new")}</span>
      </a>
      {loading ? <Skeleton lines={4} /> : null}
      {rows && rows.length === 0 ? <p className="chat-list-empty">{t("chat.list.empty")}</p> : null}
      <ul>
        <AnimatePresence initial={false}>
          {(rows ?? []).map((row) => (
            <motion.li key={row.id} layout initial={{ opacity: 0, x: -10 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, height: 0 }} transition={SETTLE}>
              <a className="chat-item" href={href("chat", row.id)} aria-current={row.id === active ? "page" : undefined}>
                <span className="chat-item-title">{row.title}</span>
                <span className="chat-item-meta">
                  <span className={`dot dot-${STATUS_TONE[row.status] ?? "muted"} ${row.status === "thinking" || row.status === "running" ? "dot-pulse" : ""}`} aria-hidden />
                  {t(`chat.mode.${row.mode}`)} · {timeAgo(row.updated_at, locale)}
                </span>
              </a>
            </motion.li>
          ))}
        </AnimatePresence>
      </ul>
    </aside>
  );
}

export function ChatPage({ rest }: { rest: string }) {
  const sessions = useResource<ChatSessionsView>("/api/chat/sessions", { interval: 5000 });
  const active = rest ? decodeURIComponent(rest) : "";
  return (
    <div className="chat" data-thread={active ? "" : undefined}>
      <SessionList rows={sessions.data?.sessions} active={active} loading={sessions.loading} />
      <section className="chat-main">
        {sessions.error && !sessions.data && !active ? <LoadError error={sessions.error} onRetry={() => void sessions.reload()} /> : null}
        <AnimatePresence mode="wait" initial={false}>
          {active ? (
            <motion.div key={active} className="chat-pane" initial={{ opacity: 0, x: 12 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, transition: { duration: 0.1 } }} transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}>
              <Thread id={active} />
            </motion.div>
          ) : (
            <motion.div key="new" className="chat-pane" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { duration: 0.1 } }}>
              <div className="thread-layout">
                <div className="chat-pane-new">
                  <NewChat defaults={sessions.data?.defaults} />
                </div>
                <ModesPane />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </section>
    </div>
  );
}

