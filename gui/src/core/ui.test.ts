import { describe, expect, test } from "bun:test";

import { newChatDraft } from "./ui";

describe("New Chat draft", () => {
  test("retains the unsent goal and folder until a chat starts", () => {
    newChatDraft.set({ goal: "Build a small dashboard", cwd: "/tmp/dashboard" });
    expect(newChatDraft.get()).toEqual({ goal: "Build a small dashboard", cwd: "/tmp/dashboard" });

    newChatDraft.set({ goal: "", cwd: "" });
  });
});
