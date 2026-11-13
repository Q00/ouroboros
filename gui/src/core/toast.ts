import { createStore } from "./store";

export type Tone = "ok" | "fail" | "warn" | "info";

export interface Toast {
  id: number;
  tone: Tone;
  title: string;
  body?: string;
  action?: { label: string; run: () => void };
}

export const toasts = createStore<Toast[]>([]);

let nextId = 1;

export function dismissToast(id: number): void {
  toasts.set((list) => list.filter((item) => item.id !== id));
}

export function toast(input: Omit<Toast, "id"> & { duration?: number }): number {
  const { duration, ...rest } = input;
  const id = nextId++;
  toasts.set((list) => [...list.slice(-3), { id, ...rest }]);
  const life = duration ?? (rest.tone === "fail" ? 9000 : rest.action ? 8000 : 4200);
  window.setTimeout(() => dismissToast(id), life);
  return id;
}
