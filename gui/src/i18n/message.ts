// The message format the catalogs are written in. Pure (no DOM), so `bun test`
// can run it.
//
//   "{name}"                                          → vars.name
//   "{count, plural, one {# warning} other {# warnings}}"
//
// A plural block picks the branch for the CLDR plural category of the number
// in `vars[count]` in the page's locale (`Intl.PluralRules`), and uses `other`
// when the language has no branch for that category. `#` in a branch is that
// number, shown exactly as `{count}` would show it, and a branch may use other
// `{name}` placeholders. English and Spanish write `one` and `other`; Korean,
// Japanese and Chinese have one form and write a plain `{count}`.
//
// The format is a subset of ICU MessageFormat: no `=N` branches, no `offset`,
// no `select`, no apostrophe quoting. Anything that is not a well-formed
// placeholder or plural block is left as written, and so is a block whose
// number is missing or not a finite number: a broken string shows up on the
// page instead of being silently filled with the wrong word.

export type Vars = Record<string, string | number>;

const CATEGORIES = new Set<string>(["zero", "one", "two", "few", "many", "other"]);

type Part =
  | { kind: "text"; text: string }
  | { kind: "arg"; name: string }
  | { kind: "pound" }
  | { kind: "plural"; name: string; source: string; branches: Map<string, Part[]> };

const rules = new Map<string, Intl.PluralRules>();

/** The CLDR plural category of `value` in `locale` ("one", "other", …). */
export function pluralCategory(value: number, locale: string): Intl.LDMLPluralRule {
  let rule = rules.get(locale);
  if (!rule) {
    rule = new Intl.PluralRules(locale);
    rules.set(locale, rule);
  }
  return rule.select(value);
}

const isNameChar = (ch: string | undefined): boolean => ch !== undefined && /\w/.test(ch);
const isSpace = (ch: string | undefined): boolean => ch !== undefined && /\s/.test(ch);

function skipSpace(src: string, at: number): number {
  while (isSpace(src[at])) at += 1;
  return at;
}

function readName(src: string, at: number): [string, number] {
  let end = at;
  while (isNameChar(src[end])) end += 1;
  return [src.slice(at, end), end];
}

/**
 * The parts of `src` from `start`. Inside a plural branch, `#` is the number
 * and an unmatched `}` closes the branch; the result's `end` is that `}`. A
 * branch that never closes is malformed (null).
 */
function parseParts(src: string, start: number, inBranch: boolean): { parts: Part[]; end: number } | null {
  const parts: Part[] = [];
  let text = "";
  const flush = () => {
    if (text) parts.push({ kind: "text", text });
    text = "";
  };
  let at = start;
  while (at < src.length) {
    const ch = src[at];
    if (ch === "{") {
      const block = parseBlock(src, at);
      if (block) {
        flush();
        parts.push(block.part);
        at = block.end;
        continue;
      }
      if (opensArgument(src, at)) {
        // A `{name, …}` block this format refuses stays exactly as written,
        // placeholders inside it included.
        const end = closingBrace(src, at);
        text += src.slice(at, end);
        at = end;
        continue;
      }
    } else if (inBranch && ch === "}") {
      flush();
      return { parts, end: at };
    } else if (inBranch && ch === "#") {
      flush();
      parts.push({ kind: "pound" });
      at += 1;
      continue;
    }
    text += ch;
    at += 1;
  }
  if (inBranch) return null;
  flush();
  return { parts, end: at };
}

/** `{name,` at `start`: the opening of an ICU-style argument, well formed or not. */
function opensArgument(src: string, start: number): boolean {
  const [name, afterName] = readName(src, start + 1);
  return name !== "" && src[skipSpace(src, afterName)] === ",";
}

/** Just past the `}` that closes the `{` at `start`, or the end of `src` when none does. */
function closingBrace(src: string, start: number): number {
  let depth = 0;
  for (let at = start; at < src.length; at += 1) {
    if (src[at] === "{") depth += 1;
    else if (src[at] === "}" && --depth === 0) return at + 1;
  }
  return src.length;
}

/** `{name}` or `{name, plural, …}` at `start` (a `{`), or null when it is neither. */
function parseBlock(src: string, start: number): { part: Part; end: number } | null {
  const [name, afterName] = readName(src, start + 1);
  if (!name) return null;
  if (src[afterName] === "}") return { part: { kind: "arg", name }, end: afterName + 1 };
  const comma = skipSpace(src, afterName);
  if (src[comma] !== ",") return null;
  const [type, afterType] = readName(src, skipSpace(src, comma + 1));
  if (type !== "plural") return null;
  let at = skipSpace(src, afterType);
  if (src[at] !== ",") return null;
  const branches = new Map<string, Part[]>();
  at = skipSpace(src, at + 1);
  while (src[at] !== "}") {
    const [selector, afterSelector] = readName(src, at);
    if (!CATEGORIES.has(selector) || branches.has(selector)) return null;
    at = skipSpace(src, afterSelector);
    if (src[at] !== "{") return null;
    const branch = parseParts(src, at + 1, true);
    if (!branch) return null;
    branches.set(selector, branch.parts);
    at = skipSpace(src, branch.end + 1);
  }
  if (!branches.has("other")) return null;
  const end = at + 1;
  return { part: { kind: "plural", name, source: src.slice(start, end), branches }, end };
}

function render(parts: Part[], vars: Vars, locale: string, pound: string): string {
  let out = "";
  for (const part of parts) {
    if (part.kind === "text") {
      out += part.text;
    } else if (part.kind === "pound") {
      out += pound;
    } else if (part.kind === "arg") {
      out += Object.hasOwn(vars, part.name) ? String(vars[part.name]) : `{${part.name}}`;
    } else {
      const value = Object.hasOwn(vars, part.name) ? vars[part.name] : undefined;
      if (typeof value !== "number" || !Number.isFinite(value)) {
        out += part.source;
        continue;
      }
      const branch = part.branches.get(pluralCategory(value, locale)) ?? part.branches.get("other")!;
      out += render(branch, vars, locale, String(value));
    }
  }
  return out;
}

/** `template` with its placeholders and plural blocks filled for `locale`. */
export function formatMessage(template: string, vars: Vars | undefined, locale: string): string {
  if (!vars || !template.includes("{")) return template;
  // Only an unclosed plural branch fails to parse, and the top level is not a branch.
  const parsed = parseParts(template, 0, false)!;
  return render(parsed.parts, vars, locale, "#");
}
