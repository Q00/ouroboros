import { useMemo } from "react";

import { createStore, readLocal, useStore, writeLocal } from "../core/store";
import en from "./en";
import es from "./es";
import ja from "./ja";
import ko from "./ko";
import zh from "./zh";

export type Key = keyof typeof en;
export type Catalog = Record<Key, string>;
export type Lang = "en" | "ko" | "zh" | "ja" | "es";
export type Vars = Record<string, string | number>;

const CATALOGS: Record<Lang, Catalog> = { en, ko, zh, ja, es };

export const LANGUAGES: { id: Lang; name: string; locale: string }[] = [
  { id: "en", name: "English", locale: "en" },
  { id: "ko", name: "한국어", locale: "ko" },
  { id: "zh", name: "简体中文", locale: "zh-CN" },
  { id: "ja", name: "日本語", locale: "ja" },
  { id: "es", name: "Español", locale: "es" },
];

const KEY = "ouroboros.lang";

function detect(): Lang {
  const forced = new URLSearchParams(window.location.search).get("lang");
  const saved = forced || readLocal(KEY);
  if (saved && saved in CATALOGS) return saved as Lang;
  for (const tag of navigator.languages ?? [navigator.language]) {
    const base = tag.toLowerCase().split("-")[0];
    if (base in CATALOGS) return base as Lang;
  }
  return "en";
}

const lang = createStore<Lang>(detect());

function applyLang(): void {
  const entry = LANGUAGES.find((item) => item.id === lang.get());
  document.documentElement.lang = entry?.locale ?? "en";
}

lang.subscribe(applyLang);
applyLang();

export function setLang(next: Lang): void {
  writeLocal(KEY, next);
  lang.set(next);
}

function fill(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name: string) =>
    name in vars ? String(vars[name]) : whole,
  );
}

export interface Translator {
  lang: Lang;
  locale: string;
  /** A catalog string. */
  t: (key: Key, vars?: Vars) => string;
  /**
   * A string for an id the server sent (a status code, a tool id). Falls back
   * to the server's own English text when the catalog has no entry for it.
   */
  tx: (key: string, fallback: string, vars?: Vars) => string;
}

export function useT(): Translator {
  const current = useStore(lang);
  return useMemo(() => {
    const catalog = CATALOGS[current];
    const locale = LANGUAGES.find((item) => item.id === current)?.locale ?? "en";
    return {
      lang: current,
      locale,
      t: (key, vars) => fill(catalog[key] ?? en[key] ?? key, vars),
      tx: (key, fallback, vars) => {
        const found = (catalog as Record<string, string>)[key] ?? (en as Record<string, string>)[key];
        return fill(found ?? fallback, vars);
      },
    };
  }, [current]);
}
