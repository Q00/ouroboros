import { describe, expect, test } from "bun:test";

import { clearNewChatDraft, newChatDraft, rememberNewChatDraft } from "./ui";

describe("New Chat draft", () => {
  test("retains the unsent goal and folder until a chat starts", () => {
    rememberNewChatDraft("Build a small dashboard", "/tmp/dashboard");
    expect(newChatDraft.get()).toEqual({ goal: "Build a small dashboard", cwd: "/tmp/dashboard" });

    // A page remount reads the same store, then an accepted POST clears it.
    const remountedDraft = newChatDraft.get();
    expect(remountedDraft).toEqual({ goal: "Build a small dashboard", cwd: "/tmp/dashboard" });
    clearNewChatDraft();
    expect(newChatDraft.get()).toEqual({ goal: "", cwd: "" });
  });
});
