import { useCallback, useEffect, useRef, useState } from "react";

import { ApiFailure, get } from "./api";

// Last good response per path, so returning to a page paints at once and
// refreshes underneath.
const cache = new Map<string, unknown>();
// When each path last answered, for the "updated" clock in the top bar.
const stamps = new Map<string, number>();

export interface Resource<T> {
  data: T | undefined;
  error: ApiFailure | null;
  /** First load, nothing to show yet. */
  loading: boolean;
  /** A reload is in flight while older data stays on screen. */
  refreshing: boolean;
  /** Epoch ms of the last successful reply, or 0. */
  updatedAt: number;
  reload: () => Promise<void>;
  /** Replace the data locally (optimistic updates, or a fresher server reply). */
  mutate: (next: T | ((prev: T | undefined) => T | undefined)) => void;
}

interface Options {
  /** Poll every N ms while the tab is visible. */
  interval?: number;
  enabled?: boolean;
}

export function useResource<T>(path: string, options: Options = {}): Resource<T> {
  const { interval, enabled = true } = options;
  const [data, setData] = useState<T | undefined>(() => cache.get(path) as T | undefined);
  const [error, setError] = useState<ApiFailure | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [updatedAt, setUpdatedAt] = useState(() => stamps.get(path) ?? 0);
  const pathRef = useRef(path);
  pathRef.current = path;

  const reload = useCallback(async () => {
    const requested = path;
    setRefreshing(true);
    try {
      const fresh = await get<T>(requested);
      cache.set(requested, fresh);
      stamps.set(requested, Date.now());
      if (pathRef.current === requested) {
        setData(fresh);
        setUpdatedAt(Date.now());
        setError(null);
      }
    } catch (failure) {
      if (pathRef.current === requested) {
        setError(failure instanceof ApiFailure ? failure : new ApiFailure(0, "error", String(failure)));
      }
    } finally {
      if (pathRef.current === requested) setRefreshing(false);
    }
  }, [path]);

  useEffect(() => {
    if (!enabled) return;
    setData(cache.get(path) as T | undefined);
    void reload();
    if (!interval) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, interval);
    return () => window.clearInterval(timer);
  }, [path, enabled, interval, reload]);

  const mutate = useCallback<Resource<T>["mutate"]>(
    (next) => {
      setData((prev) => {
        const resolved = typeof next === "function" ? (next as (p: T | undefined) => T | undefined)(prev) : next;
        if (resolved !== undefined) cache.set(path, resolved);
        return resolved;
      });
    },
    [path],
  );

  return { data, error, loading: data === undefined && !error, refreshing, updatedAt, reload, mutate };
}

/** Drop cached replies whose path starts with any of the prefixes. */
export function invalidate(...prefixes: string[]): void {
  for (const key of [...cache.keys()]) {
    if (prefixes.some((prefix) => key.startsWith(prefix))) cache.delete(key);
  }
}
