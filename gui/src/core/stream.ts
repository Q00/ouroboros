import { useEffect, useRef, useState } from "react";

import { streamUrl } from "./api";

/**
 * Follow a server-sent event stream of whole-object snapshots.
 * Returns the latest snapshot and whether the connection is live.
 */
export function useSnapshotStream<T>(path: string | null): { latest: T | null; live: boolean } {
  const [latest, setLatest] = useState<T | null>(null);
  const [live, setLive] = useState(false);
  const pathRef = useRef(path);
  pathRef.current = path;

  useEffect(() => {
    setLatest(null);
    setLive(false);
    if (!path) return;
    const source = new EventSource(streamUrl(path));
    source.onopen = () => setLive(true);
    source.onmessage = (event) => {
      try {
        setLatest(JSON.parse(event.data) as T);
      } catch {
        // A partial frame; the next snapshot replaces it.
      }
    };
    // The browser reconnects on its own; only the indicator changes.
    source.onerror = () => setLive(false);
    return () => source.close();
  }, [path]);

  return { latest, live };
}
