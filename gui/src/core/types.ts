// Shapes returned by the console API. Field names follow the Python handlers.

export interface Integration {
  state: "connected" | "partial" | "missing" | "runtime";
  detail: string;
  path: string | null;
  code: string;
  vars: Record<string, string>;
}

export interface SignIn {
  state: "signed_in" | "signed_out" | "unknown";
  detail: string;
  fix: string | null;
}

export interface InstallMethod {
  id: string;
  label: string;
  command: string;
  requires: string;
  available: boolean;
}

export interface ClientCard {
  id: string;
  label: string;
  vendor: string;
  blurb: string;
  docs: string;
  wiring: string;
  role: "host" | "runtime";
  setup_note: string;
  installed: boolean;
  cli_path: string | null;
  integration: Integration;
  is_default: boolean;
  stages: string[];
  sign_in: SignIn | null;
  install: InstallMethod[];
  running_job: string | null;
}

export interface ClientPlan {
  client: string;
  command: string;
  wiring: string;
  current_default: string;
  changes_default: boolean;
  backup: string;
}

export type JobStatus = "running" | "succeeded" | "failed";

export interface JobSnapshot {
  id: string;
  kind: string;
  title: string;
  command: string;
  status: JobStatus;
  exit_code: number | null;
  started_at: number;
  finished_at: number | null;
  cursor: number;
  lines: string[];
  meta: Record<string, unknown>;
}

export interface RunSummary {
  execution_id: string;
  session_id: string;
  goal: string | null;
  status: string;
  node_count: number;
  completed_count: number;
  total_count: number;
  pending_count: number;
  executing_count: number;
  failed_count: number;
  phase: string | null;
  activity: string | null;
  provider: string | null;
  total_tokens: number;
  start_time: string | number | null;
  last_row?: number;
}

export interface InterviewSummary {
  family: string;
  interview_id: string;
  root_event_rowid: number;
}

export interface Overview {
  version: string;
  latest_version: string | null;
  update_available: boolean;
  python: string;
  platform: string;
  executable: string;
  config_path: string;
  default_runtime: string;
  llm_backend: string;
  clients: { total: number; installed: number; connected: number; attention: number };
  doctor: { pass: number; warn: number; fail: number };
  providers: { configured: number; total: number };
  runs: RunSummary[];
  running_jobs: JobSnapshot[];
}

export interface DoctorSurface {
  name: string;
  status: "pass" | "warn" | "fail";
  message: string;
  path: string | null;
  version: string | null;
  expected_version: string | null;
  launcher: string | null;
  remediation: string | null;
}

export interface Doctor {
  surfaces: DoctorSurface[];
  version: string;
  python: string;
  executable: string;
  commands: Record<string, string>;
}

export interface ComponentRow {
  extra: string;
  label: string;
  purpose: string;
  installed: string | null;
  wanted: string | null;
  available: boolean;
  packages: string[];
  command: string | null;
  conflict: string | null;
  running_job: string | null;
}

export interface ComponentsView {
  python: string;
  version: string | null;
  components: ComponentRow[];
  restart_needed_hint: string;
}

export interface RuntimeChoice {
  id: string;
  installed: boolean;
  cli_path: string | null;
  supports_llm: boolean;
}

export interface StageView {
  id: string;
  label: string;
  hint: string;
  runtime_saved: string | null;
  runtime_effective: string;
  model_key: string | null;
  model_label: string | null;
  model_saved: string | null;
  model_env: string[];
  completion_backend: string;
  models: string[];
  default_hint: string | null;
}

export interface RoutePreset {
  id: string;
  note: string;
  routes: Record<string, string>;
}

export interface SettingsView {
  config_path: string;
  runtimes: RuntimeChoice[];
  default_runtime: { saved: string; effective: string; env: string[] };
  llm_backend: { saved: string; effective: string; env: string[] };
  models_default: { saved: string; presets: string[]; env: string[] };
  models_pin: { saved: boolean; env: string[] };
  stages: StageView[];
  presets: RoutePreset[];
  undo_available: boolean;
}

export interface SettingsChange {
  key: string;
  old: unknown;
  new: unknown;
}

export interface SettingsResult {
  changes: SettingsChange[];
  reconnect: boolean;
  saved: boolean;
}

export interface ProviderRow {
  id: string;
  label: string;
  group: "cloud" | "gateway" | "local";
  featured: boolean;
  default_base_url: string;
  base_url: string | null;
  key_url: string;
  key_required: boolean;
  configured: boolean;
  key_hint: string | null;
  model_format: string;
}

export interface ProvidersView {
  path: string;
  providers: ProviderRow[];
  other: string[];
  llm_backend: string;
  litellm_installed: boolean;
  opencodex: { url: string; version: string | null } | null;
}

export interface ProviderTest {
  ok: boolean;
  status?: number;
  message?: string;
  count?: number;
  models?: string[];
}

export interface McpStatus {
  enabled: boolean;
  running: boolean;
  listening: boolean;
  pid: number | null;
  port: number;
  url: string;
  command: string;
  started_at: number | null;
  restarts: number;
  last_exit: number | null;
  error: string | null;
  log: string[];
}

export interface ServiceStatus {
  supported: boolean;
  manager: string | null;
  installed: boolean;
  loaded: boolean;
  pid: number | null;
  unit_path: string | null;
  log_path: string;
  this_process_is_service: boolean;
}

export interface McpHost {
  id: string;
  label: string;
  path: string;
  exists: boolean;
  state: "shared" | "other_url" | "stdio" | "absent";
  error: string | null;
}

export interface DaemonView {
  mcp: McpStatus;
  service: ServiceStatus;
  stdio_servers: number;
  clients: McpHost[];
}

export interface BoardCard {
  id: string;
  title?: string;
  status: string;
  depth?: number | null;
  parent_id?: string | null;
  ac_index?: number | null;
  provider?: string | null;
  tool?: string | null;
  model?: string | null;
  model_tier?: string | null;
  tokens?: number | null;
}

export interface Board {
  meta: {
    execution_id: string | null;
    session_id: string | null;
    goal: string | null;
    phase: string | null;
    activity: string | null;
    completed: number;
    total: number;
    provider: string | null;
    total_tokens: number;
  };
  columns: Record<string, BoardCard[]>;
  providers?: unknown;
}

export type ChatMode = "interview" | "auto" | "ask";

export type ChatStatus =
  | "thinking"
  | "waiting_answer"
  | "seed_ready_to_write"
  | "seed_ready"
  | "running"
  | "done"
  | "failed"
  | "interrupted";

export interface RunTile {
  id: string;
  title: string;
  status: "pending" | "executing" | "completed" | "failed";
  depth: number;
}

export interface RunProgress {
  tiles: RunTile[];
  status: string;
  counts: { pending: number; executing: number; completed: number; failed: number };
  total: number;
  done: number;
  phase: string | null;
  activity: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  kind: "text" | "question" | "note" | "error" | "seed" | "run" | "result" | "log";
  text: string;
  ts: number;
  /** A fixed sentence the page translates (`chat.note.<key>`); `text` is its English. */
  key?: string;
  vars?: Record<string, string | number>;
  ambiguity?: number | null;
  /** How a user answer was given, when Ouroboros drafted it from the repository or a default. */
  via?: "auto_confirmed" | "repo_confirmed" | "suggested";
  /** For an auto-confirmed answer: the manifest or config file it was read from. For a result: "auto". */
  source?: string;
  evidence?: string[];
  /** The question an auto-confirmed or confirmed answer answered. */
  question?: string;
  corrected?: boolean;
  corrects?: string;
  model?: string | null;
  /** For a reply whose model is the "default" sentinel: which agent's default, and its configured model. */
  model_backend?: string | null;
  model_default?: string | null;
  acceptance_criteria?: string[];
  /** An Auto Seed: how many criteria it has, when the chat keeps only the first ones. */
  acceptance_total?: number;
  constraints?: string[];
  /** `null` when the Seed was too large to keep in the chat; `seed_path` then names its file. */
  seed_yaml?: string | null;
  seed_path?: string | null;
  execution_id?: string | null;
  job_id?: string | null;
  run_status?: string;
  progress?: RunProgress | null;
  lines?: string[];
  worktree?: string;
  branch?: string | null;
  base?: string;
  files?: string[];
  file_count?: number;
  uncommitted?: number;
  /** An Auto result: how it ended, the run status it reported, and the folder it worked in. */
  outcome?: "completed" | "failed" | "stopped" | "unverified";
  handoff_status?: string | null;
  folder?: string;
  in_repo?: boolean;
}

/** One round of the interview Auto runs with itself, paired by round number. */
export interface AutoRound {
  round: number;
  question: string | null;
  answer: string | null;
  /** auto/answerer.py AutoAnswerSource: user_goal, repo_fact, assumption, … */
  answer_source: string | null;
  /** The ambiguity the interview reported with this question, when it did. */
  ambiguity?: number | null;
}

/** An Auto chat's structured view, folded from `ouroboros auto --progress-jsonl` (gui/auto_progress.py). */
export interface AutoView {
  v: number;
  auto_session_id: string | null;
  /** The auto pipeline phase (auto/state.py AutoPhase). */
  phase: string | null;
  message: string | null;
  phases: { phase: string; at: string | null }[];
  rounds: AutoRound[];
  rounds_dropped: number;
  /** One point per round that reported an ambiguity, in round order. */
  ambiguity_trend?: { round: number; value: number }[];
  max_rounds: number | null;
  max_repair_rounds: number | null;
  grade: string | null;
  grades: { grade: string; repair_round: number; at: string | null }[];
  review_message: string | null;
  findings: string[];
  repair_round: number;
  execution_id: string | null;
  seed_loaded: boolean;
  outcome: "completed" | "failed" | "stopped" | "unverified" | null;
  skipped: number;
  read_error: string | null;
}

export type GitKind = "ready" | "no_commits" | "not_repo" | "unreadable" | "no_git";

/** GET /api/chat/folder: whether a folder exists and is a git repository with a commit. */
export interface FolderProbe {
  path: string;
  exists: boolean;
  git: GitKind | null;
  detail?: string;
}

export interface Suggestion {
  pending: boolean;
  question?: string;
  text?: string;
  source?: "repo" | "model";
  evidence?: string[];
  error?: string;
  /** Started by Ouroboros for a question the server marked as answerable from code. */
  auto?: boolean;
  repo_checked?: boolean;
}

/** One per-dimension floor of the server's completion gate. */
export interface ReadinessDimension {
  key: string;
  label: string;
  floor: number;
  clarity: number | null;
  met: boolean;
}

/** The interview's completion gate as the server evaluates it. The page holds no thresholds. */
export interface Readiness {
  score: number;
  threshold: number;
  score_ok: boolean | null;
  dimensions: ReadinessDimension[];
  qualified: boolean | null;
  streak: number;
  streak_required: number;
  complete: boolean;
  brownfield: boolean;
}

export type ChatProject = "new" | "existing";

export interface ChatSessionRow {
  id: string;
  title: string;
  mode: ChatMode;
  backend: string | null;
  model: string | null;
  status: ChatStatus;
  cwd: string;
  updated_at: number;
  created_at: number;
}

export interface ChatSession extends ChatSessionRow {
  goal: string;
  version: number;
  messages: ChatMessage[];
  project?: ChatProject | null;
  ambiguity?: number | null;
  readiness?: Readiness | null;
  seed_ready?: boolean;
  last_question?: string | null;
  interview_session_id?: string;
  seed_yaml?: string;
  execution_id?: string | null;
  auto_job_id?: string;
  /** Auto chats started since the structured view exists; older ones have only their log. */
  auto?: AutoView | null;
  suggestion?: Suggestion;
  activity?: string[];
}

export interface BackendChoice {
  id: string;
  installed: boolean;
  models: string[];
}

export interface ChatDefaults {
  cwd: string;
  home: string;
  default_runtime: string;
  execute_agent: string;
  agents: BackendChoice[];
  llm: BackendChoice[];
  providers: { id: string; label: string; model_format: string }[];
}

export interface ChatSessionsView {
  sessions: ChatSessionRow[];
  defaults: ChatDefaults;
}
