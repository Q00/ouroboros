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
  ambiguity?: number | null;
  ready?: boolean;
  model?: string | null;
  acceptance_criteria?: string[];
  constraints?: string[];
  seed_yaml?: string;
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
}

export interface Suggestion {
  pending: boolean;
  question?: string;
  text?: string;
  source?: "repo" | "model";
  evidence?: string[];
  error?: string;
}

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
  ambiguity?: number | null;
  seed_ready?: boolean;
  last_question?: string | null;
  interview_session_id?: string;
  seed_yaml?: string;
  execution_id?: string | null;
  auto_job_id?: string;
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
