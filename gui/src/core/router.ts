import { createStore, useStore } from "./store";

export const PAGES = [
  "home",
  "chat",
  "runs",
  "connect",
  "models",
  "providers",
  "mcp",
  "health",
] as const;

export type PageId = (typeof PAGES)[number];

export interface Route {
  page: PageId;
  /** Everything after the page name, e.g. a run or conversation id. */
  rest: string;
}

// Names the CLI and older links use for the same pages.
const ALIASES: Record<string, PageId> = {
  "": "home",
  overview: "home",
  integrations: "connect",
  clients: "connect",
  tools: "connect",
  settings: "models",
  agents: "models",
  doctor: "health",
  components: "health",
  daemon: "mcp",
  service: "mcp",
  "shared-mcp": "mcp",
};

function parse(hash: string): Route {
  const cleaned = hash.replace(/^#\/?/, "");
  const [head = "", ...tail] = cleaned.split("/");
  const name = head.toLowerCase();
  const page = (PAGES as readonly string[]).includes(name)
    ? (name as PageId)
    : (ALIASES[name] ?? "home");
  return { page, rest: tail.join("/") };
}

const route = createStore<Route>(parse(window.location.hash));

window.addEventListener("hashchange", () => {
  const next = parse(window.location.hash);
  const current = route.get();
  if (next.page !== current.page || next.rest !== current.rest) route.set(next);
});

export function useRoute(): Route {
  return useStore(route);
}

export function href(page: PageId, rest = ""): string {
  return rest ? `#${page}/${rest}` : `#${page}`;
}

export function navigate(page: PageId, rest = ""): void {
  window.location.hash = href(page, rest);
}
