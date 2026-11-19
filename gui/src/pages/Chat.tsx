import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleX,
  FileText,
  Folder,
  FolderGit2,
  FolderPlus,
  GitBranch,
  MessagesSquare,
  Pencil,
  Play,
  Plus,
  RotateCcw,
  Send,
  SlidersHorizontal,
  Sparkles,
  Sprout,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { FormEvent, KeyboardEvent, ReactNode } from "react";
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";

import type { Key, Translator } from "../i18n";
import { useT } from "../i18n";
import { useAction } from "../core/action";
import { ApiFailure, del, get, post } from "../core/api";
import { backendName, timeAgo } from "../core/format";
import { adoptRunningJobs } from "../core/jobs";
import { askModelHint, listedModels } from "../core/modelChoices";
import { useShortPath } from "../core/paths";
import { invalidate, useResource } from "../core/resource";
import { href, navigate } from "../core/router";
import { useSnapshotStream } from "../core/stream";
import type { AutoView, BackendChoice, ChatDefaults, ChatMessage, ChatMode, ChatProject, ChatSession, ChatSessionRow, ChatSessionsView, ChatStatus, FolderProbe, ModelRejection, Readiness, RunTile } from "../core/types";
import type { Tone } from "../ui/bits";
import { Chip, CopyButton, Empty, Field, LoadError, LogView, Notice, Skeleton } from "../ui/bits";
import { Button, IconButton } from "../ui/Button";
import { Spinner } from "../ui/Mark";
import { Modal } from "../ui/Modal";
import { Picker } from "../ui/Picker";
import { Segmented } from "../ui/Segmented";
import { AutoInterview, AutoLog, AutoStepper, autoActivity, autoEnding } from "./AutoView";
import { Progress, RunStatus, activityLabel, phaseLabel, runPreparing } from "./Runs";
import { EASE_OUT, GLIDE, SETTLE } from "../core/motion";

const FOLLOW = "__settings__";

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

/** A sentence the server sent with a stable key, in the reader's language; else its English text. */
function keyed(tx: Translator["tx"], prefix: string, message: ChatMessage): string {
  return message.key ? tx(`${prefix}.${message.key}`, message.text, message.vars) : message.text;
}

/**
 * The model a reply or chat used. The "default" sentinel means the agent CLI's
 * own configured model: name it when its config says which, else say so.
 */
function modelText(t: Translator["t"], model: string, hint?: { backend?: string | null; configured?: string | null }): string {
  if (model !== "default") return model;
  if (hint?.configured) return t("chat.model.cliDefault", { agent: backendName(hint.backend), model: hint.configured });
  return t("chat.model.agentDefault");
}

/**
 * The agent a chat runs on. An Auto chat names the one its job was launched
 * on (recorded from the job's `--runtime` and auto's stage routing), which
 * can differ from the agent picked; other chats name the agent picked.
 */
function chatAgent(session: ChatSession): string | null {
  return (session.mode === "auto" ? session.auto_model?.backend : null) || session.backend || null;
}

/** "Codex didn't accept the model gpt-6.2-astra: …", in the reader's language. */
function rejectionText(t: Translator["t"], tx: Translator["tx"], rejection: ModelRejection): string {
  const reason = rejection.reason_code ? tx(`chat.rejected.reason.${rejection.reason_code}`, rejection.reason) : rejection.reason;
  const agent = backendName(rejection.backend) || t("chat.rejected.agent");
  return rejection.model ? t("chat.rejected", { agent, model: rejection.model, reason }) : t("chat.rejected.noModel", { agent, reason });
}

/** Opens Agents & models, where a stage's model is chosen. */
function ModelsLink() {
  const { t } = useT();
  return (
    <a className="btn btn-secondary btn-sm" href={href("models")}>
      <SlidersHorizontal size={14} aria-hidden />
      <span className="btn-label">{t("chat.rejected.models")}</span>
    </a>
  );
}

/**
 * A turn the agent refused because of its model. One way on: send the turn
 * again on the agent's default model when the chat chose the model (an
 * explicit click; nothing changes the model on its own), or open Agents &
 * models when the model came from there. The provider's raw error stays
 * folded under Details.
 */
function RejectedModel({ message, onRetry, retrying }: { message: ChatMessage; onRetry?: () => void; retrying?: boolean }) {
  const { t, tx } = useT();
  const rejection = message.rejection as ModelRejection;
  return (
    <div className="rejected" role="alert">
      <div className="rejected-head">
        <CircleAlert size={16} aria-hidden />
        <p>{rejectionText(t, tx, rejection)}</p>
      </div>
      <div className="rejected-actions">
        {onRetry ? (
          <Button size="sm" variant="primary" icon={<RotateCcw size={14} aria-hidden />} pending={retrying} onClick={onRetry}>
            {t("chat.rejected.retry")}
          </Button>
        ) : rejection.source === "settings" ? (
          <ModelsLink />
        ) : null}
        {message.detail ? (
          <details className="rejected-detail">
            <summary>{t("chat.rejected.details")}</summary>
            <pre>{message.detail}</pre>
          </details>
        ) : null}
      </div>
    </div>
  );
}

// ── New conversation ─────────────────────────────────────────────────

/** Whether the folder typed into the new-chat form exists and is a git repository with a commit. */
function useFolderProbe(path: string): FolderProbe | null {
  const [probe, setProbe] = useState<FolderProbe | null>(null);
  useEffect(() => {
    const target = path.trim();
    setProbe(null);
    if (!target) return;
    let live = true;
    const timer = window.setTimeout(() => {
      get<FolderProbe>(`/api/chat/folder?path=${encodeURIComponent(target)}`)
        .then((result) => {
          if (live) setProbe(result);
        })
        .catch(() => {
          if (live) setProbe(null);
        });
    }, 350);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [path]);
  return probe;
}

/** One line under the folder field: what a hand-off (or Auto) will find there. */
function FolderStatus({ probe, mode }: { probe: FolderProbe | null; mode: ChatMode }) {
  const { t } = useT();
  if (!probe) return null;
  let tone: "ok" | "warn" | "fail" | "mute";
  let text: string;
  const repo = probe.git === "ready" || probe.git === "no_commits";
  if (!probe.exists) {
    tone = "fail";
    text = t("chat.folder.missing");
  } else if (probe.git === "unreadable") {
    tone = "warn";
    text = t("chat.folder.unreadable", { detail: probe.detail ?? "" });
  } else if (mode === "auto") {
    // Auto works in the folder itself; git only decides whether its changes can be listed.
    tone = repo ? "mute" : "warn";
    text = repo ? t("chat.folder.autoRepo") : t("chat.folder.autoPlain");
  } else if (probe.git === "ready") {
    tone = "ok";
    text = t("chat.folder.ready");
  } else if (probe.git === "no_commits") {
    tone = "warn";
    text = t("chat.folder.noCommits");
  } else if (probe.git === "not_repo") {
    tone = "warn";
    text = t("chat.folder.notRepo");
  } else {
    tone = "warn";
    text = t("chat.folder.noGit");
  }
  const Icon = tone === "ok" ? CircleCheck : tone === "mute" ? FolderGit2 : CircleAlert;
  return (
    <p className={`folder-status tone-${tone}`} role="status">
      <Icon size={13} aria-hidden />
      <span>{text}</span>
    </p>
  );
}

function NewChat({ defaults }: { defaults: ChatDefaults | undefined }) {
  const { t, tx } = useT();
  const shortPath = useShortPath();
  const ids = useId();
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState<ChatMode>("interview");
  // A new project by default: the console's launch folder says nothing about
  // what the user is building, so reading it as the project must be a choice.
  const [project, setProject] = useState<ChatProject>("new");
  const [cwd, setCwd] = useState("");
  const [backend, setBackend] = useState(FOLLOW);
  const [model, setModel] = useState("");
  const goalRef = useRef<HTMLTextAreaElement>(null);
  const probe = useFolderProbe(mode === "ask" ? "" : cwd);

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
        ...(mode === "interview" ? { project } : {}),
        backend: backend === FOLLOW ? "" : backend,
        model: mode === "ask" ? model : "",
      });
      invalidate("/api/chat/sessions");
      // Auto runs as a console job: list it in Activity with the others.
      if (mode === "auto") void adoptRunningJobs();
      navigate("chat", session.id);
    },
    { tx, failTitle: t("chat.new.failed") },
  );

  const chosen = pool.find((item) => item.id === backend);
  const labels = chosen?.model_labels ?? {};
  // An empty field is the default, so the "default" sentinel is not suggested.
  const suggestions = [
    ...listedModels(chosen?.models ?? []),
    ...(backend === "litellm" ? (defaults?.providers ?? []).map((item) => item.model_format) : []),
  ];
  const modelHint = askModelHint(backend, chosen?.default_model);
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
      {mode === "interview" ? (
        <div className="project-choice">
          <Segmented<ChatProject>
            label={t("chat.new.project")}
            value={project}
            onChange={setProject}
            options={[
              { value: "new", label: t("chat.new.project.new"), icon: <FolderPlus size={14} aria-hidden /> },
              { value: "existing", label: t("chat.new.project.existing"), icon: <FolderGit2 size={14} aria-hidden /> },
            ]}
          />
          <p className="project-choice-lead">{project === "existing" ? t("chat.new.project.existingLead", { folder: shortPath(cwd) || "…" }) : t("chat.new.project.newLead")}</p>
        </div>
      ) : null}
      <div className="composer-options">
        {mode !== "ask" ? (
          <Field
            label={mode === "interview" && project === "new" ? t("chat.new.runFolder") : t("chat.new.folder")}
            htmlFor={`${ids}-cwd`}
            hint={mode === "auto" ? t("chat.new.folder.autoHint") : mode === "interview" && project === "new" ? t("chat.new.runFolder.hint") : t("chat.new.folder.hint")}
          >
            <div className="input-icon">
              <Folder size={15} aria-hidden />
              <input id={`${ids}-cwd`} className="input input-mono" value={cwd} spellCheck={false} autoComplete="off" onChange={(event) => setCwd(event.target.value)} />
            </div>
            <FolderStatus probe={probe} mode={mode} />
          </Field>
        ) : null}
        <Field label={mode === "ask" ? t("chat.new.backend") : t("chat.new.agent")} htmlFor={`${ids}-agent`} hint={mode === "ask" ? undefined : t("chat.new.agent.hint")}>
          <Picker
            id={`${ids}-agent`}
            value={backend}
            onChange={setBackend}
            options={[
              { value: FOLLOW, label: followed ? t("chat.new.follow", { agent: backendName(followed) }) : t("chat.new.followPlain") },
              // An agent that is not installed cannot be picked; the server refuses it too.
              ...pool.map((item) => ({ value: item.id, label: backendName(item.id), hint: item.installed ? undefined : t("models.notInstalled"), disabled: !item.installed })),
            ]}
          />
        </Field>
        {mode === "ask" ? (
          <Field
            label={t("models.model")}
            htmlFor={`${ids}-model`}
            hint={
              <>
                {modelHint.key === "chat.new.model.hintCurrent" ? t(modelHint.key, { model: modelHint.model }) : t(modelHint.key)}
                {suggestions.length > 0 ? ` ${t("models.availability")}` : null}
              </>
            }
          >
            <input id={`${ids}-model`} className="input input-mono" list={`${ids}-models`} value={model} spellCheck={false} autoComplete="off" placeholder={t("chat.new.model.placeholder")} onChange={(event) => setModel(event.target.value)} />
            <datalist id={`${ids}-models`}>
              {suggestions.map((item) => (
                <option key={item} value={item} label={labels[item]} />
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
  const preparing = runPreparing(message);
  return (
    <div className="bubble-card run-card">
      <div className="run-card-head">
        <Play size={16} aria-hidden />
        <strong>{t("chat.run.title")}</strong>
        <RunStatus status={status} />
        {/* What it is doing only while it does it; a finished bar says the rest. */}
        {status === "running" && progress?.phase ? <Chip>{phaseLabel(t, progress.phase)}</Chip> : null}
        {message.execution_id ? (
          <a className="more-link" href={href("runs", message.execution_id)}>
            {t("chat.run.board")}
            <ArrowRight size={13} aria-hidden />
          </a>
        ) : null}
      </div>
      {preparing ? (
        <p className="run-card-activity" role="status">
          <Spinner size={13} /> {t("chat.run.preparing")}
        </p>
      ) : progress ? (
        <>
          <div className="run-card-progress">
            <Progress done={progress.done} total={progress.total} failed={progress.counts.failed} label={t("runs.progress")} />
            <span>{t("runs.count", { done: progress.done, total: progress.total })}</span>
          </div>
          {status === "running" && progress.activity ? <p className="run-card-activity">{activityLabel(t, progress.activity)}</p> : null}
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
  const more = (message.acceptance_total ?? criteria.length) - criteria.length;
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
          {more > 0 ? <p className="section-note">{t("chat.result.more", { count: more })}</p> : null}
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
      ) : message.seed_path ? (
        <dl className="kv">
          <div>
            <dt>{t("auto.seed.file")}</dt>
            <dd>
              <code>{message.seed_path}</code>
              <CopyButton text={message.seed_path} />
            </dd>
          </div>
        </dl>
      ) : null}
    </div>
  );
}

const OUTCOME: Record<NonNullable<ChatMessage["outcome"]>, { key: Key; tone: Tone }> = {
  completed: { key: "chat.result.outcome.completed", tone: "ok" },
  failed: { key: "chat.result.outcome.failed", tone: "fail" },
  stopped: { key: "chat.result.outcome.stopped", tone: "warn" },
  unverified: { key: "chat.result.outcome.unverified", tone: "warn" },
};

/**
 * Ouroboros' own state and Python bytecode an Auto result lists apart: folded
 * under one muted line with their count, never dropped.
 */
function InternalFiles({ files, count }: { files: string[]; count: number }) {
  const { t } = useT();
  const ids = useId();
  const [open, setOpen] = useState(false);
  return (
    <div className="file-group">
      <button type="button" className="file-group-toggle" aria-expanded={open} aria-controls={`${ids}-files`} onClick={() => setOpen((value) => !value)}>
        <ChevronDown className="auto-chevron" size={14} aria-hidden data-open={open || undefined} />
        <span>{t("chat.result.internal")}</span>
        <span className="file-group-count">{count}</span>
      </button>
      <AnimatePresence initial={false}>
        {open ? (
          <motion.div id={`${ids}-files`} className="auto-collapse" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22, ease: EASE_OUT }}>
            <p className="section-note">{t("chat.result.internalNote")}</p>
            <ul className="file-list file-list-muted">
              {files.map((file) => (
                <li key={file}>
                  <code>{file}</code>
                </li>
              ))}
            </ul>
            {count > files.length ? <p className="section-note">{t("chat.result.more", { count: count - files.length })}</p> : null}
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

/** What an Auto chat left behind. Auto works in the project folder itself, not a worktree. */
function AutoResultCard({ message }: { message: ChatMessage }) {
  const { t } = useT();
  const shortPath = useShortPath();
  const files = message.files ?? [];
  const outcome = OUTCOME[message.outcome ?? "unverified"] ?? OUTCOME.unverified;
  const count = message.file_count ?? files.length;
  const internal = message.internal_files ?? [];
  const internalCount = message.internal_count ?? internal.length;
  return (
    <div className="bubble-card result-card">
      <div className="seed-head">
        <Folder size={16} aria-hidden />
        <strong>{t("chat.result.title")}</strong>
        <Chip tone={outcome.tone}>{t(outcome.key)}</Chip>
      </div>
      {message.outcome === "unverified" ? <p className="section-note">{t("chat.result.unverifiedBody", { status: message.handoff_status ?? "–" })}</p> : null}
      {message.folder ? (
        <dl className="kv">
          <div>
            <dt>{t("chat.result.folder")}</dt>
            <dd>
              {/* The short form reads; the full path is in the tooltip and the copy. */}
              <code title={message.folder}>{shortPath(message.folder)}</code>
              <CopyButton text={message.folder} />
            </dd>
          </div>
        </dl>
      ) : null}
      {message.in_repo ? (
        count > 0 || internalCount > 0 ? (
          <>
            {count > 0 ? (
              <>
                <h4>{internalCount > 0 ? t("chat.result.projectFiles", { count }) : t("chat.result.folderFiles", { count })}</h4>
                <ul className="file-list">
                  {files.map((file) => (
                    <li key={file}>
                      <code>{file}</code>
                    </li>
                  ))}
                </ul>
                {count > files.length ? <p className="section-note">{t("chat.result.more", { count: count - files.length })}</p> : null}
              </>
            ) : (
              <p className="section-note">{t("chat.result.noProjectFiles")}</p>
            )}
            {internalCount > 0 ? <InternalFiles files={internal} count={internalCount} /> : null}
            <p className="section-note">{t("chat.result.folderNote")}</p>
          </>
        ) : (
          <p className="section-note">{t("chat.result.noFiles")}</p>
        )
      ) : (
        <p className="section-note">{t("chat.result.notRepo")}</p>
      )}
    </div>
  );
}

function ResultCard({ message }: { message: ChatMessage }) {
  const { t } = useT();
  const files = message.files ?? [];
  if (message.source === "auto") return <AutoResultCard message={message} />;
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

/** How Ouroboros drafted a user answer from the repository, and how to correct an auto-confirmed one. */
function AnswerProvenance({ message, onCorrect }: { message: ChatMessage; onCorrect?: (message: ChatMessage) => void }) {
  const { t } = useT();
  if (message.corrects) return <span className="bubble-via">{t("chat.answer.correction")}</span>;
  if (message.via !== "auto_confirmed" && message.via !== "repo_confirmed") return null;
  const auto = message.via === "auto_confirmed";
  return (
    <div className="bubble-via">
      <span>{auto ? t("chat.answer.autoConfirmed", { source: message.source ?? "" }) : t("chat.answer.repoConfirmed")}</span>
      {auto && message.corrected ? <span>· {t("chat.answer.corrected")}</span> : null}
      {auto && !message.corrected && onCorrect ? (
        <button type="button" className="bubble-via-action" onClick={() => onCorrect(message)}>
          <Pencil size={12} aria-hidden />
          {t("chat.answer.correct")}
        </button>
      ) : null}
    </div>
  );
}

function MessageView({ message, running, questionNumber, onCorrect, auto, onRetry, retrying }: { message: ChatMessage; running: boolean; questionNumber: number; onCorrect?: (message: ChatMessage) => void; auto?: AutoView | null; onRetry?: () => void; retrying?: boolean }) {
  const { t, tx } = useT();
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
      body = <p className="bubble-note">{keyed(tx, "chat.note", message)}</p>;
      break;
    case "error":
      body =
        message.code === "model_rejected" && message.rejection ? (
          <RejectedModel message={message} onRetry={onRetry} retrying={retrying} />
        ) : (
          <Notice tone="fail" icon={<CircleAlert size={16} aria-hidden />}>
            {keyed(tx, "chat.note", message)}
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
      // An Auto chat with a structured view tells its story here; its raw log
      // is folded away at the end of the thread.
      if (auto) {
        body = <AutoInterview view={auto} live={running} />;
        break;
      }
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
        <div className={`bubble ${message.role === "user" ? "bubble-user" : "bubble-assistant"}`} data-local={message.id.startsWith("local-") || undefined} data-via={message.via}>
          <p>{message.text}</p>
          {message.model ? <span className="bubble-model">{modelText(t, message.model, { backend: message.model_backend, configured: message.model_default })}</span> : null}
          {message.role === "user" ? <AnswerProvenance message={message} onCorrect={onCorrect} /> : null}
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

/**
 * The interview's completion gate as the server reports it: the ambiguity score
 * against the server's gate, each area against its floor, and the streak of
 * qualifying answers the server waits for before it completes on its own.
 */
function ReadinessMeter({ readiness }: { readiness: Readiness }) {
  const { t, tx } = useT();
  const clamp = (value: number) => Math.min(Math.max(value, 0), 1);
  const score = clamp(readiness.score);
  const threshold = readiness.threshold.toFixed(2);
  let goal: string;
  if (readiness.complete) goal = t("chat.readiness.complete");
  else if (readiness.qualified) goal = t("chat.readiness.confirming", { streak: readiness.streak, required: readiness.streak_required });
  else if (readiness.score_ok) goal = t("chat.readiness.floors", { threshold });
  else goal = t("chat.readiness.open", { threshold });
  const ready = readiness.complete || readiness.qualified === true;
  return (
    <div className="readiness">
      <div className="ambiguity" data-ready={ready || undefined}>
        <span className="ambiguity-label">{t("chat.ambiguity")}</span>
        <div className="ambiguity-track" role="meter" aria-label={t("chat.ambiguity")} aria-valuemin={0} aria-valuemax={1} aria-valuenow={Number(score.toFixed(2))} aria-valuetext={t("chat.ambiguity.text", { value: score.toFixed(2) })}>
          <motion.span className="ambiguity-fill" animate={{ width: `${score * 100}%` }} transition={GLIDE} />
          <span className="ambiguity-gate" style={{ left: `${clamp(readiness.threshold) * 100}%` }} aria-hidden />
        </div>
        <span className="ambiguity-value">{score.toFixed(2)}</span>
        <span className="ambiguity-goal">{goal}</span>
      </div>
      {readiness.dimensions.length > 0 ? (
        <ul className="readiness-dims" aria-label={t("chat.readiness.dims")}>
          {readiness.dimensions.map((dimension) => (
            <li key={dimension.key} data-met={dimension.met || undefined}>
              {dimension.met ? <Check size={11} aria-hidden /> : <CircleDashed size={11} aria-hidden />}
              <span>{tx(`chat.readiness.dim.${dimension.key}`, dimension.label)}</span>
              <span className="readiness-dim-value">
                {t("chat.readiness.dim.value", { value: dimension.clarity === null ? "–" : dimension.clarity.toFixed(2), floor: dimension.floor.toFixed(2) })}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
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
  const shortPath = useShortPath();
  const seed = [...session.messages].reverse().find((message) => message.kind === "seed");
  const run = [...session.messages].reverse().find((message) => message.kind === "run");
  const reply = [...session.messages].reverse().find((message) => message.kind === "text" && message.role === "assistant" && message.model);
  // An Auto chat names the agent and model its run uses, as configured when it started.
  const autoModel = session.mode === "auto" ? session.auto_model : null;
  const model = session.model || reply?.model || autoModel?.model;
  const hint = autoModel && !session.model ? { backend: autoModel.backend, configured: autoModel.configured } : { backend: reply?.model_backend, configured: reply?.model_default };
  const agent = chatAgent(session);
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
          {session.project ? (
            <div>
              <dt>{t("chat.new.project")}</dt>
              <dd>{t(`chat.new.project.${session.project}`)}</dd>
            </div>
          ) : null}
          <div>
            <dt>{t("models.agent")}</dt>
            <dd>{agent ? backendName(agent) : t("chat.new.followPlain")}</dd>
          </div>
          {model ? (
            <div>
              <dt>{t("models.model")}</dt>
              <dd>
                <code>{modelText(t, model, hint)}</code>
              </dd>
            </div>
          ) : null}
          {session.auto?.auto_session_id ? (
            <div>
              <dt>{t("auto.pane.session")}</dt>
              <dd>
                <code>{session.auto.auto_session_id}</code>
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
          {runPreparing(run) ? (
            <p className="pane-note">{t("chat.run.preparing")}</p>
          ) : (
            <>
              <Progress done={run.progress.done} total={run.progress.total} failed={run.progress.counts.failed} label={t("runs.progress")} />
              <p className="pane-note">{t("runs.count", { done: run.progress.done, total: run.progress.total })}</p>
            </>
          )}
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
  const shortPath = useShortPath();
  const path = `/api/chat/sessions/${encodeURIComponent(id)}`;
  const loaded = useResource<ChatSession>(path);
  const stream = useSnapshotStream<ChatSession>(`${path}/stream`);
  const [session, setSession] = useState<ChatSession | undefined>(loaded.data);
  const [draft, setDraft] = useState("");
  // The auto-confirmed answer the next message corrects, if the user asked to correct one.
  const [correcting, setCorrecting] = useState<ChatMessage | null>(null);
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
  // An Auto chat grows inside its cards (rounds, answers, phases), not only at the tail.
  const autoRounds = session?.auto?.rounds;
  const autoSize = autoRounds ? autoRounds.length * 2 + (autoRounds[autoRounds.length - 1]?.answer ? 1 : 0) + (session?.auto?.phases.length ?? 0) : 0;
  useLayoutEffect(() => {
    const element = scroller.current;
    if (element && pinned.current) element.scrollTo({ top: element.scrollHeight, behavior: count > 1 ? "smooth" : "auto" });
  }, [count, tailSize, autoSize, session?.status, session?.suggestion?.pending, session?.suggestion?.text]);

  const [send, sending] = useAction(
    async (text: string, corrects?: string) => {
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
      setCorrecting(null);
      pinned.current = true;
      try {
        const next = await post<ChatSession>(`${path}/messages`, corrects ? { text, corrects } : { text });
        setSession(next);
      } catch (failure) {
        setSession(before);
        setDraft(text);
        throw failure;
      }
    },
    { tx, failTitle: t("chat.sendFailed") },
  );

  // Set when a hand-off was refused because the folder has no git repository or no commit.
  const [gitOffer, setGitOffer] = useState<"not_repo" | "no_commits" | null>(null);
  type Action = "seed" | "run" | "suggest" | "accept" | "dismiss" | "stop" | "retry_default";
  const [act, acting] = useAction(
    async (action: Action, extra: Record<string, unknown> = {}) => {
      try {
        const next = await post<ChatSession>(`${path}/actions`, { action, ...extra });
        setSession(next);
      } catch (failure) {
        if (action === "run" && failure instanceof ApiFailure && failure.code === "needs_git") {
          // Explained on the hand-off card, with a button that prepares the folder.
          setGitOffer(failure.extra.reason === "no_commits" ? "no_commits" : "not_repo");
          return;
        }
        throw failure;
      }
      if (action === "run") {
        setGitOffer(null);
        invalidate("/api/runs", "/api/overview");
      }
    },
    { tx, failTitle: t("chat.actionFailed") },
  );
  const [lastAction, setLastAction] = useState<string>("");
  const run = (action: Action, extra?: Record<string, unknown>) => {
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
  const auto = mode === "auto" ? (session.auto ?? null) : null;
  const autoLog = auto ? session.messages.find((message) => message.kind === "log") : undefined;
  const autoNow = auto ? autoActivity(session, auto) : null;
  const autoEnd = auto ? autoEnding(session, auto, t) : null;
  // Sending again on the agent's default model, for an Ask turn refused for the model the chat named.
  const lastMessage = session.messages[session.messages.length - 1];
  const retryable = ask && status === "failed" && !!session.model && lastMessage?.code === "model_rejected" && lastMessage.rejection?.source === "chat";

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const text = draft.trim();
    if (text && canType && !sending) void send(text, correcting?.id);
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
          {/* The goal's whole first line, cut by the header's width with an ellipsis; all of it on hover. */}
          <h1 title={session.goal || session.title}>{goalLine(session.goal) || session.title}</h1>
          <p>
            <Chip>{t(`chat.mode.${mode}`)}</Chip>
            {session.project ? <Chip>{t(`chat.new.project.${session.project}`)}</Chip> : null}
            {chatAgent(session) ? <Chip tone="info">{backendName(chatAgent(session))}</Chip> : null}
            {session.model ? <Chip>{modelText(t, session.model)}</Chip> : null}
            {!ask ? (
              <span className="path" title={session.cwd}>
                {shortPath(session.cwd)}
              </span>
            ) : null}
          </p>
        </div>
        <AnimatePresence mode="wait" initial={false}>
          <motion.span key={status} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.16 }}>
            <StatusChip status={auto?.outcome === "stopped" ? "interrupted" : status} />
          </motion.span>
        </AnimatePresence>
        <IconButton label={t("chat.delete")} onClick={() => setConfirmDelete(true)}>
          <Trash2 size={16} aria-hidden />
        </IconButton>
      </header>

      {mode === "interview" && session.readiness ? <ReadinessMeter readiness={session.readiness} /> : null}
      <div
        className="thread-scroll"
        ref={scroller}
        onScroll={(event) => {
          const element = event.currentTarget;
          pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
        }}
      >
        {/* Inside the scroll box, so it lines up with the cards under any scrollbar. */}
        {auto ? <AutoStepper session={session} view={auto} /> : null}
        {/* An Auto thread announces from its own live parts (the interview rounds), not twice. */}
        <ol className="thread" aria-live={auto ? undefined : "polite"} aria-label={t("chat.thread")}>
          {session.messages.map((message) => {
            if (message.kind === "question") questionNumber += 1;
            // The stepper, the result card and the footer say how Auto ended.
            if (auto && ((message.kind === "note" && message.key === "auto_finished") || (message.kind === "error" && message.key === "auto_exited"))) return null;
            return (
              <MessageView
                key={message.id}
                message={message}
                running={status === "running"}
                questionNumber={questionNumber}
                auto={auto}
                onRetry={retryable && message === lastMessage ? () => void run("retry_default") : undefined}
                retrying={busyAction("retry_default")}
                onCorrect={canType ? (target) => {
                  setCorrecting(target);
                  inputRef.current?.focus();
                } : undefined}
              />
            );
          })}
          <AnimatePresence>
            {autoNow ? (
              <motion.li key={autoNow} className="msg msg-left" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, transition: { duration: 0.1 } }}>
                <div className="thinking" role="status">
                  <Spinner size={16} />
                  <span>{t(autoNow)}</span>
                </div>
              </motion.li>
            ) : null}
          </AnimatePresence>
          {auto && autoLog ? (
            <li className="msg">
              <AutoLog message={autoLog} live={status === "running"} view={auto} />
            </li>
          ) : null}
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
                  <Spinner size={14} /> {suggestion.auto ? t("chat.suggest.autoPending") : t("chat.suggest.pending")}
                </p>
              ) : suggestion.rejection ? (
                <Notice tone="warn" title={t("chat.suggest.failed")} action={suggestion.rejection.source === "settings" ? <ModelsLink /> : undefined}>
                  {rejectionText(t, tx, suggestion.rejection)}
                </Notice>
              ) : suggestion.error ? (
                <Notice tone="warn" title={t("chat.suggest.failed")}>
                  {suggestion.error}
                </Notice>
              ) : (
                <>
                  <div className="suggestion-head">
                    <Sparkles size={14} aria-hidden />
                    <strong>{suggestion.source === "repo" ? t("chat.suggest.repoTitle") : t("chat.suggest.title")}</strong>
                    <Chip tone={suggestion.source === "repo" ? "ok" : "muted"}>{suggestion.source === "repo" ? t("chat.suggest.fromRepo") : t("chat.suggest.fromModel")}</Chip>
                  </div>
                  <p className="suggestion-text">{suggestion.text}</p>
                  {suggestion.language_mismatch ? <p className="suggestion-note">{t("chat.suggest.otherLanguage")}</p> : null}
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
                    <Button size="sm" variant="ghost" icon={<X size={14} aria-hidden />} pending={busyAction("dismiss")} onClick={() => void run("dismiss")}>
                      {t("chat.suggest.reject")}
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => {
                        setDraft(suggestion.text ?? "");
                        inputRef.current?.focus();
                      }}
                    >
                      {t("chat.suggest.edit")}
                    </Button>
                    <Button size="sm" variant="primary" pending={busyAction("accept")} icon={<Check size={14} aria-hidden />} onClick={() => void run("accept")}>
                      {suggestion.source === "repo" ? t("chat.suggest.confirm") : t("chat.suggest.send")}
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
          {canRun && gitOffer ? (
            <motion.div key="run-git" className="next-step" role="alert" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <div>
                <strong>{gitOffer === "no_commits" ? t("chat.git.noCommits.title") : t("chat.git.notRepo.title")}</strong>
                <span>{gitOffer === "no_commits" ? t("chat.git.noCommits.body") : t("chat.git.notRepo.body")}</span>
              </div>
              <Button variant="primary" icon={<FolderGit2 size={15} aria-hidden />} pending={busyAction("run")} onClick={() => void run("run", { prepare_git: true })}>
                {gitOffer === "no_commits" ? t("chat.git.prepare.noCommits") : t("chat.git.prepare.notRepo")}
              </Button>
            </motion.div>
          ) : canRun ? (
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
            <motion.div key="stop" className="next-step next-step-auto" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <div>
                <strong>{t("chat.next.auto.title")}</strong>
                <span>{t("chat.next.auto.body")}</span>
              </div>
              <Button size="sm" variant="danger" icon={<Square size={12} aria-hidden />} pending={busyAction("stop")} onClick={() => void run("stop")}>
                {t("chat.stop")}
              </Button>
            </motion.div>
          ) : null}
          {auto && autoEnd ? (
            <motion.div key="auto-end" className="next-step next-step-auto next-step-end" role="status" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <div>
                <strong>{t(autoEnd.title)}</strong>
                <span>{autoEnd.body}</span>
              </div>
              <a className="btn btn-secondary btn-sm" href={href("chat")}>
                <Plus size={14} aria-hidden />
                <span className="btn-label">{t("chat.new")}</span>
              </a>
            </motion.div>
          ) : null}
          {status === "done" && !ask && !autoEnd ? (
            <motion.div key="done" className={`next-step next-step-done${mode === "auto" ? " next-step-auto" : ""}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
              <div>
                <strong>{t("chat.next.done.title")}</strong>
                <span>{mode === "auto" ? t("chat.next.done.autoBody") : t("chat.next.done.body")}</span>
              </div>
              <a className={`btn btn-secondary${mode === "auto" ? " btn-sm" : ""}`} href={href("chat")}>
                <Plus size={mode === "auto" ? 14 : 15} aria-hidden />
                <span className="btn-label">{t("chat.new")}</span>
              </a>
            </motion.div>
          ) : null}
        </AnimatePresence>

        {mode !== "auto" && !(mode === "interview" && (session.seed_yaml || status === "seed_ready_to_write")) ? (
          <form className="composer-box composer-inline" onSubmit={submit} data-disabled={!canType || undefined}>
            {correcting ? (
              <div className="composer-correcting">
                <Pencil size={13} aria-hidden />
                <span>{t("chat.correcting", { question: correcting.question ?? "", answer: correcting.text })}</span>
                <IconButton label={t("chat.correcting.cancel")} onClick={() => setCorrecting(null)}>
                  <X size={14} aria-hidden />
                </IconButton>
              </div>
            ) : null}
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

/** The first line of a goal with text, for the thread header. */
function goalLine(goal: string | undefined): string {
  return (goal ?? "").split("\n").map((line) => line.trim()).find(Boolean) ?? "";
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

