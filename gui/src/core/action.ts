import { useCallback, useRef, useState } from "react";

import type { Translator } from "../i18n";
import { ApiFailure } from "./api";
import { toast } from "./toast";

/** The sentence to show for a failed call: a translated code, else the server's words. */
export function explain(failure: unknown, tx: Translator["tx"]): string {
  if (failure instanceof ApiFailure) return tx(`error.${failure.code}`, failure.message);
  return failure instanceof Error ? failure.message : String(failure);
}

/**
 * Wrap an async action so its button can show progress, a second click is
 * ignored while it runs, and a failure always reaches the user.
 */
export function useAction<A extends unknown[]>(
  fn: (...args: A) => Promise<unknown>,
  options: { tx: Translator["tx"]; failTitle: string; silent?: boolean },
): [(...args: A) => Promise<boolean>, boolean] {
  const [pending, setPending] = useState(false);
  const busy = useRef(false);
  const latest = useRef({ fn, options });
  latest.current = { fn, options };
  const run = useCallback(async (...args: A) => {
    if (busy.current) return false;
    busy.current = true;
    setPending(true);
    try {
      await latest.current.fn(...args);
      return true;
    } catch (failure) {
      const { tx, failTitle, silent } = latest.current.options;
      if (!silent) toast({ tone: "fail", title: failTitle, body: explain(failure, tx) });
      return false;
    } finally {
      busy.current = false;
      setPending(false);
    }
  }, []);
  return [run, pending];
}
