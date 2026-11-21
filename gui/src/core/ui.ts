import { createStore, readLocal, writeLocal } from "./store";

// Small pieces of interface state several components share.

const SIDEBAR_KEY = "ouroboros.sidebar";

export const sidebarCollapsed = createStore<boolean>(readLocal(SIDEBAR_KEY) === "collapsed");

export function toggleSidebar(): void {
  sidebarCollapsed.set((value) => {
    writeLocal(SIDEBAR_KEY, value ? "open" : "collapsed");
    return !value;
  });
}

export const paletteOpen = createStore<boolean>(false);

export interface NewChatDraft {
  goal: string;
  cwd: string;
}

/** Keeps an unsent New Chat draft while the console page changes. */
export const newChatDraft = createStore<NewChatDraft>({ goal: "", cwd: "" });

export function rememberNewChatDraft(goal: string, cwd: string): void {
  newChatDraft.set({ goal, cwd });
}

export function clearNewChatDraft(): void {
  newChatDraft.set({ goal: "", cwd: "" });
}
