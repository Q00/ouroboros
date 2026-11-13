export function shortPath(path: string | null | undefined, home?: string): string {
  if (!path) return "";
  let shown = path;
  if (home && shown.startsWith(home)) shown = `~${shown.slice(home.length)}`;
  shown = shown.replace(/^\/(Users|home)\/[^/]+/, "~");
  if (shown.length <= 56) return shown;
  const parts = shown.split("/");
  if (parts.length <= 4) return shown;
  return `${parts.slice(0, 2).join("/")}/…/${parts.slice(-2).join("/")}`;
}

export function compactNumber(value: number, locale: string): string {
  try {
    return new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 }).format(value);
  } catch {
    return String(Math.round(value));
  }
}

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31_536_000],
  ["month", 2_592_000],
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];

/** "3 minutes ago" in the page language. Accepts epoch seconds or an ISO string. */
export function timeAgo(value: number | string | null | undefined, locale: string): string {
  if (value === null || value === undefined || value === "") return "";
  const seconds = typeof value === "number" ? value : Date.parse(value) / 1000;
  if (!Number.isFinite(seconds)) return "";
  const delta = seconds - Date.now() / 1000;
  const format = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  for (const [unit, size] of UNITS) {
    if (Math.abs(delta) >= size) return format.format(Math.round(delta / size), unit);
  }
  return format.format(Math.round(delta), "second");
}

export function clockTime(seconds: number, locale: string): string {
  return new Date(seconds * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
}

export function duration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return minutes ? `${minutes}m ${String(rest).padStart(2, "0")}s` : `${rest}s`;
}

/** "3h 12m" for an uptime in seconds. */
export function uptime(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "<1m";
  const hours = Math.floor(minutes / 60);
  if (hours < 1) return `${minutes}m`;
  const days = Math.floor(hours / 24);
  return days ? `${days}d ${hours % 24}h` : `${hours}h ${minutes % 60}m`;
}

/** Display names for agent and backend ids the API returns as bare ids. */
const BACKEND_NAMES: Record<string, string> = {
  claude: "Claude Code",
  claude_code: "Claude Code",
  claude_mcp: "Claude Code (MCP host)",
  codex: "Codex CLI",
  codex_mcp: "Codex (MCP host)",
  opencode: "OpenCode",
  hermes: "Hermes",
  kiro: "Kiro CLI",
  copilot: "GitHub Copilot CLI",
  gemini: "Gemini CLI",
  goose: "Goose",
  pi: "Pi",
  omp: "Oh My Pi",
  gjc: "GJC",
  zcode: "Zcode",
  antigravity: "Antigravity",
  grok: "Grok Build",
  megacode: "MegaCode",
  litellm: "LiteLLM",
  host: "Host session",
};

export function backendName(id: string | null | undefined): string {
  if (!id) return "";
  return BACKEND_NAMES[id] ?? id;
}

/** One or two capitals that stand in for a vendor mark. */
export function monogram(label: string): string {
  const words = label
    .replace(/\(.*?\)/g, " ")
    .split(/[\s/._-]+/)
    .filter((word) => /[A-Za-z0-9]/.test(word));
  // Words every tool shares say nothing about which tool this is.
  const telling = words.filter((word) => !GENERIC.has(word.toLowerCase()));
  const picked = telling.length > 0 ? telling : words;
  if (picked.length === 0) return "?";
  if (picked.length === 1) {
    const word = picked[0];
    return word.charAt(0).toUpperCase() + word.charAt(1).toLowerCase();
  }
  return (picked[0][0] + picked[1][0]).toUpperCase();
}

const GENERIC = new Set(["cli", "code", "ai", "build", "cloud", "api"]);
