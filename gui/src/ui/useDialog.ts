import { useCallback, useState } from "react";

/**
 * State for a dialog that is about one thing (a tool, a provider). The
 * subject stays in place after closing so the dialog can animate out with
 * its content intact.
 */
export function useDialog<T>(): { subject: T | null; open: boolean; show: (subject: T) => void; hide: () => void } {
  const [subject, setSubject] = useState<T | null>(null);
  const [open, setOpen] = useState(false);
  const show = useCallback((next: T) => {
    setSubject(next);
    setOpen(true);
  }, []);
  const hide = useCallback(() => setOpen(false), []);
  return { subject, open, show, hide };
}
