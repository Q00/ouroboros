import { createStore, readLocal, useStore, writeLocal } from "./store";

export type ThemeMode = "system" | "light" | "dark";

const KEY = "ouroboros.theme";
const media = window.matchMedia("(prefers-color-scheme: dark)");

function initial(): ThemeMode {
  const saved = readLocal(KEY);
  return saved === "light" || saved === "dark" ? saved : "system";
}

const mode = createStore<ThemeMode>(initial());

function apply(): void {
  const current = mode.get();
  const dark = current === "dark" || (current === "system" && media.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

media.addEventListener("change", apply);
mode.subscribe(apply);
apply();

export function useThemeMode(): ThemeMode {
  return useStore(mode);
}

export function setThemeMode(next: ThemeMode): void {
  writeLocal(KEY, next);
  // Cross-fade the palette instead of snapping.
  const root = document.documentElement;
  root.classList.add("theme-shift");
  window.setTimeout(() => root.classList.remove("theme-shift"), 320);
  mode.set(next);
}
