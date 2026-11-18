// Run with `bun test` in gui/ (excluded from the page's type-check and bundle).
import { beforeAll, describe, expect, test } from "bun:test";

import en from "./en";
import es from "./es";
import ja from "./ja";
import ko from "./ko";
import { formatMessage, pluralCategory } from "./message";
import zh from "./zh";

// ./index reads the browser at import (saved language, <html lang>); give it just enough.
const scope = globalThis as unknown as Record<string, unknown>;
scope.window ??= { location: { search: "", hash: "" }, addEventListener() {}, setTimeout, clearTimeout };
scope.document ??= { documentElement: {}, querySelector: () => null };
Object.defineProperty(globalThis, "navigator", { value: { languages: ["en"], language: "en", platform: "" }, configurable: true });

type IndexModule = typeof import("./index");
let translator: IndexModule["translator"];

beforeAll(async () => {
  ({ translator } = await import("./index"));
});

const FILES = "{count, plural, one {# file} other {# files}}";

describe("formatMessage placeholders", () => {
  test("fills {name} and leaves a name it was not given in place", () => {
    expect(formatMessage("Install {name} into {folder}", { name: "codex" }, "en")).toBe("Install codex into {folder}");
  });

  test("without vars a template comes back exactly as written", () => {
    expect(formatMessage(`Changed: ${FILES}`, undefined, "en")).toBe(`Changed: ${FILES}`);
  });

  test("a placeholder with spaces is not one, as before", () => {
    expect(formatMessage("{ count } and {count}", { count: 3 }, "en")).toBe("{ count } and 3");
  });

  test("outside a plural block, # and } are plain text", () => {
    expect(formatMessage("#{n} } done", { n: 2 }, "en")).toBe("#2 } done");
  });
});

describe("formatMessage plural blocks", () => {
  test("English takes one at 1 and other at 0 and 2", () => {
    expect([0, 1, 2].map((count) => formatMessage(FILES, { count }, "en"))).toEqual(["0 files", "1 file", "2 files"]);
  });

  test("Spanish takes one at 1 and other at 0 and 2", () => {
    const template = "{count, plural, one {# archivo} other {# archivos}}";
    expect([0, 1, 2].map((count) => formatMessage(template, { count }, "es"))).toEqual(["0 archivos", "1 archivo", "2 archivos"]);
  });

  test("Korean, Japanese and Chinese have one form: every count is other", () => {
    for (const locale of ["ko", "ja", "zh-CN"]) {
      expect([0, 1, 2].map((count) => pluralCategory(count, locale))).toEqual(["other", "other", "other"]);
      expect(formatMessage(FILES, { count: 1 }, locale)).toBe("1 files");
    }
  });

  test("a category the block has no branch for falls back to other", () => {
    expect(pluralCategory(1, "en")).toBe("one");
    expect(formatMessage("{count, plural, other {# item(s)}}", { count: 1 }, "en")).toBe("1 item(s)");
    // Spanish has a `many` category for large round numbers; catalogs only write one/other.
    expect(formatMessage("{count, plural, one {# token} other {# tokens}}", { count: 1_000_000 }, "es")).toBe("1000000 tokens");
  });

  test("# is the number as given, and a branch fills other placeholders", () => {
    const template = "{count, plural, one {{shown} token} other {{shown} tokens}} in {where}";
    expect(formatMessage(template, { count: 1, shown: "1", where: "run" }, "en")).toBe("1 token in run");
    expect(formatMessage(template, { count: 1500, shown: "1.5K", where: "run" }, "en")).toBe("1.5K tokens in run");
    expect(formatMessage("{n, plural, one {# x} other {# xs}}", { n: 1.5 }, "en")).toBe("1.5 xs");
  });

  test("two blocks in one string each follow their own number", () => {
    const template = "{a, plural, one {# file} other {# files}}, {b, plural, one {# line} other {# lines}}";
    expect(formatMessage(template, { a: 1, b: 2 }, "en")).toBe("1 file, 2 lines");
    expect(formatMessage(template, { a: 2, b: 1 }, "en")).toBe("2 files, 1 line");
  });

  test("in a nested block # is the inner number", () => {
    const template = "{a, plural, one {# run with {b, plural, one {# step} other {# steps}}} other {# runs}}";
    expect(formatMessage(template, { a: 1, b: 3 }, "en")).toBe("1 run with 3 steps");
    expect(formatMessage(template, { a: 4, b: 1 }, "en")).toBe("4 runs");
  });

  test("whitespace inside the block syntax is allowed, and none is required", () => {
    expect(formatMessage("{count,plural,one{# a}other{# b}}", { count: 1 }, "en")).toBe("1 a");
    expect(formatMessage("{count ,  plural ,\n one {# a}\n other {# b} }", { count: 2 }, "en")).toBe("2 b");
  });

  test("a missing, text, NaN or infinite number leaves the block as written", () => {
    for (const vars of [{}, { count: "1" }, { count: Number.NaN }, { count: Number.POSITIVE_INFINITY }]) {
      expect(formatMessage(`Changed: ${FILES}`, vars, "en")).toBe(`Changed: ${FILES}`);
    }
  });

  test("a malformed block is left as written instead of guessed at", () => {
    const malformed = [
      "{count, plural, one {# file}}", // no other
      "{count, plural, one {# file} lots {# files} other {# files}}", // not a CLDR category
      "{count, plural, =0 {none} other {# files}}", // exact branches are not supported
      "{count, plural, one {# file} one {# file} other {# files}}", // duplicate branch
      "{count, select, one {# file} other {# files}}", // not a plural block
      "{count, plural one {# file} other {# files}}", // missing comma
      "{count, plural, one # file other {# files}}", // branch without braces
      "{count, plural, one {# file} other {# files}", // unclosed block
      "{count, plural, one {# file} other {# files", // unclosed branch
      "{count, plural, one {{name} file}}", // no other, with a placeholder the caller gives
      "{count, select, one {{name}} other {{name}s}}", // not a plural block, with placeholders
      "{count, plural, one {{name} file} other {{name} files}", // unclosed, with placeholders
    ];
    for (const template of malformed) expect(formatMessage(template, { count: 1, name: "X" }, "en")).toBe(template);
  });

  test("only the refused block stays as written; the rest of the string is filled", () => {
    expect(formatMessage("{count, plural, one {{name}}} for {name}", { count: 1, name: "X" }, "en")).toBe("{count, plural, one {{name}}} for X");
    const nested = "{a, plural, one {# run, {b, plural, one {{name}}}} other {# runs}}";
    expect(formatMessage(nested, { a: 1, b: 1, name: "X" }, "en")).toBe("1 run, {b, plural, one {{name}}}");
    expect(formatMessage(nested, { a: 2, b: 1, name: "X" }, "en")).toBe("2 runs");
  });
});

const CATALOGS = { en, es, ko, ja, zh } as Record<string, Record<string, string>>;
const LOCALES: Record<string, string> = { en: "en", es: "es", ko: "ko", ja: "ja", zh: "zh-CN" };
const names = (template: string) => new Set([...template.matchAll(/\{(\w+)[,}]/g)].map((match) => match[1]));

describe("the catalogs", () => {
  test("all five have the same keys", () => {
    const keys = Object.keys(en).sort();
    for (const [lang, catalog] of Object.entries(CATALOGS)) expect([lang, Object.keys(catalog).sort()]).toEqual([lang, keys]);
  });

  test("a translation only uses placeholders the English string is given", () => {
    const stray: string[] = [];
    for (const [lang, catalog] of Object.entries(CATALOGS)) {
      for (const [key, template] of Object.entries(catalog)) {
        const allowed = names((en as Record<string, string>)[key]);
        for (const name of names(template)) if (!allowed.has(name)) stray.push(`${lang} ${key} {${name}}`);
      }
    }
    expect(stray).toEqual([]);
  });

  test("every string renders at 0, 1 and 2 with no plural syntax left over", () => {
    const leftover: string[] = [];
    for (const [lang, catalog] of Object.entries(CATALOGS)) {
      for (const [key, template] of Object.entries(catalog)) {
        for (const count of [0, 1, 2]) {
          const vars = Object.fromEntries([...names(template)].map((name) => [name, count]));
          const out = formatMessage(template, vars, LOCALES[lang]);
          if (/[{}#]/.test(out)) leftover.push(`${lang} ${key} @${count}: ${out}`);
        }
      }
    }
    expect(leftover).toEqual([]);
  });
});

describe("real catalog strings at 0, 1 and 2", () => {
  const at = (lang: "en" | "es" | "ko" | "ja" | "zh", key: Parameters<ReturnType<IndexModule["translator"]>["t"]>[0], extra: Record<string, number> = {}) =>
    [0, 1, 2].map((count) => translator(lang).t(key, { count, ...extra }));

  test("the top bar's warning count", () => {
    expect(at("en", "home.stat.health.warn")).toEqual(["0 warnings", "1 warning", "2 warnings"]);
    expect(at("en", "health.tally.warn")).toEqual(["0 warnings", "1 warning", "2 warnings"]);
    expect(at("ko", "home.stat.health.warn")).toEqual(["경고 0건", "경고 1건", "경고 2건"]);
  });

  test("the Auto stepper's criteria caption", () => {
    expect(at("en", "auto.caption.criteria")).toEqual(["0 criteria", "1 criterion", "2 criteria"]);
    expect(at("es", "auto.caption.criteria")).toEqual(["0 criterios", "1 criterio", "2 criterios"]);
    expect(at("ja", "auto.caption.criteria")).toEqual(["基準 0 件", "基準 1 件", "基準 2 件"]);
    expect(at("zh", "auto.caption.criteria")).toEqual(["0 条标准", "1 条标准", "2 条标准"]);
    const done = [1, 2].map((total) => translator("en").t("auto.caption.criteriaDone", { done: 1, total }));
    expect(done).toEqual(["1/1 criterion", "1/2 criteria"]);
  });

  test("questions, files and log lines", () => {
    expect(at("en", "auto.caption.questions")).toEqual(["0 questions", "1 question", "2 questions"]);
    expect(at("en", "auto.caption.files")).toEqual(["0 files changed", "1 file changed", "2 files changed"]);
    expect(at("es", "auto.caption.files")).toEqual(["0 archivos cambiados", "1 archivo cambiado", "2 archivos cambiados"]);
    expect(at("en", "auto.caption.filesInternal", { internal: 1 })).toEqual(["0 files (+1 cache)", "1 file (+1 cache)", "2 files (+1 cache)"]);
    expect(at("en", "auto.log.lines")).toEqual(["0 lines", "1 line", "2 lines"]);
  });

  test("a verb that agrees with the count", () => {
    expect(at("en", "nav.badge.warn")).toEqual(["0 need attention", "1 needs attention", "2 need attention"]);
    expect(at("en", "auto.interview.dropped")).toEqual([
      "The first 0 rounds aren't kept in this chat.",
      "The first round isn't kept in this chat.",
      "The first 2 rounds aren't kept in this chat.",
    ]);
  });

  test("a noun that follows the total, not the first number", () => {
    const tools = [1, 2].map((total) => translator("en").t("home.stat.tools.detail", { installed: 1, total }));
    expect(tools).toEqual(["1 of 1 agent tool installed", "1 of 2 agent tools installed"]);
    const es1 = [1, 2].map((total) => translator("es").t("home.stat.tools.detail", { installed: 1, total }));
    expect(es1).toEqual(["1 de 1 herramienta de agente instalada", "1 de 2 herramientas de agente instaladas"]);
  });

  test("a token count picks its word from the number and shows the compact form", () => {
    const en1 = translator("en");
    expect(en1.t("runs.tokens", { count: 1, tokens: "1" })).toBe("1 token");
    expect(en1.t("runs.tokens", { count: 1500, tokens: "1.5K" })).toBe("1.5K tokens");
    expect(translator("ko").t("runs.tokens", { count: 1500, tokens: "1.5천" })).toBe("1.5천 토큰");
  });

  test("text from outside the catalog takes English plural rules", () => {
    const fallback = "{count, plural, one {# thing} other {# things}}";
    expect(translator("ko").tx("server.only.key", fallback, { count: 1 })).toBe("1 thing");
    expect(translator("ko").tx("home.stat.health.warn", fallback, { count: 1 })).toBe("경고 1건");
  });
});
