import { useMemo } from "react";

import { createStore, readLocal, useStore, writeLocal } from "../core/store";
import en from "./en";
import es from "./es";
import ja from "./ja";
import ko from "./ko";
import { formatMessage, type Vars } from "./message";
import zh from "./zh";

export type { Vars } from "./message";
export type Key = keyof typeof en;
export type Catalog = Record<Key, string>;
export type Lang = "en" | "ko" | "zh" | "ja" | "es";

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

export interface Translator {
  lang: Lang;
  locale: string;
  /**
   * A catalog string. Plural blocks pick their word from a number in `vars`
   * by the language's plural rules (see ./message.ts).
   */
  t: (key: Key, vars?: Vars) => string;
  /**
   * A string for an id the server sent (a status code, a tool id). Falls back
   * to the server's own English text when the catalog has no entry for it.
   */
  tx: (key: string, fallback: string, vars?: Vars) => string;
}

/** The translator for `current`, outside React (the page uses `useT`). */
export function translator(current: Lang): Translator {
  const catalog = CATALOGS[current] as Record<string, string>;
  const locale = LANGUAGES.find((item) => item.id === current)?.locale ?? "en";
  const english = en as Record<string, string>;
  // Text from outside this language's catalog is English, so it takes English plural rules.
  const lookup = (key: string, fallback: string, vars?: Vars): string =>
    Object.hasOwn(catalog, key)
      ? formatMessage(catalog[key], vars, locale)
      : formatMessage(Object.hasOwn(english, key) ? english[key] : fallback, vars, "en");
  return {
    lang: current,
    locale,
    t: (key, vars) => lookup(key, key, vars),
    tx: lookup,
  };
}

export function useT(): Translator {
  const current = useStore(lang);
  return useMemo(() => translator(current), [current]);
}
