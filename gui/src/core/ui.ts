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
