import type { PathContext } from "./format";
import { shortPath } from "./format";
import { createStore, useStore } from "./store";

// The home folder and symlinked roots the console reports in /api/overview,
// so every page shows a path the same way. Empty until the overview answers;
// shortPath then falls back to the /Users/<name> and /home/<name> forms.

export const pathContext = createStore<PathContext>({});

/** Take the overview's answer; an unchanged one keeps the same object, so nothing re-renders. */
export function learnPaths(home: string | undefined, aliases: [string, string][] | undefined): void {
  if (!home) return;
  const next: PathContext = { home, aliases: aliases ?? [] };
  pathContext.set((current) => (JSON.stringify(current) === JSON.stringify(next) ? current : next));
}

/** `shortPath` against what the console knows of this machine's paths. */
export function useShortPath(): (path: string | null | undefined) => string {
  const context = useStore(pathContext);
  return (path) => shortPath(path, context);
}
