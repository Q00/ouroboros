// Run with `bun test` in gui/ (excluded from the page's type-check and bundle).
import { describe, expect, test } from "bun:test";

import { AGENT_DEFAULT, askModelHint, listedModels, needsOwnOption, stagePickerValue } from "./modelChoices";

const INHERIT = "__follow__";
const CUSTOM = "__custom__";
const CODEX = ["default", "gpt-6.2-sol", "gpt-6.2-astra", "gpt-6-sol"];

describe("listedModels", () => {
  test("drops the sentinel, which the inherit option already offers", () => {
    expect(listedModels(CODEX)).toEqual(["gpt-6.2-sol", "gpt-6.2-astra", "gpt-6-sol"]);
  });

  test("keeps every other id in order", () => {
    expect(listedModels(["opus", "sonnet", "claude-haiku-4-5"])).toEqual(["opus", "sonnet", "claude-haiku-4-5"]);
    expect(listedModels([])).toEqual([]);
    expect(listedModels([AGENT_DEFAULT])).toEqual([]);
  });
});

describe("stagePickerValue", () => {
  test("a saved sentinel shows as the inherit option, not as nothing", () => {
    expect(stagePickerValue(AGENT_DEFAULT, false, INHERIT, CUSTOM)).toBe(INHERIT);
  });

  test("no model shows as the inherit option", () => {
    expect(stagePickerValue("", false, INHERIT, CUSTOM)).toBe(INHERIT);
  });

  test("a chosen model shows as itself", () => {
    expect(stagePickerValue("gpt-6-sol", false, INHERIT, CUSTOM)).toBe("gpt-6-sol");
  });

  test("a custom entry shows as custom, whatever it holds", () => {
    expect(stagePickerValue(AGENT_DEFAULT, true, INHERIT, CUSTOM)).toBe(CUSTOM);
    expect(stagePickerValue("x", true, INHERIT, CUSTOM)).toBe(CUSTOM);
  });

  test("the value always names an option the picker renders", () => {
    for (const model of ["", AGENT_DEFAULT, "gpt-6-sol", "gpt-legacy"]) {
      const options = [INHERIT, ...listedModels(CODEX), ...(needsOwnOption(model, false, CODEX) ? [model] : []), CUSTOM];
      expect(options).toContain(stagePickerValue(model, false, INHERIT, CUSTOM));
    }
  });
});

describe("needsOwnOption", () => {
  test("a saved id the list lacks gets its own option", () => {
    expect(needsOwnOption("gpt-legacy", false, CODEX)).toBe(true);
  });

  test("the sentinel, a listed id, no model or a custom entry do not", () => {
    expect(needsOwnOption(AGENT_DEFAULT, false, CODEX)).toBe(false);
    expect(needsOwnOption(AGENT_DEFAULT, false, ["gpt-6-sol"])).toBe(false);
    expect(needsOwnOption("gpt-6-sol", false, CODEX)).toBe(false);
    expect(needsOwnOption("", false, CODEX)).toBe(false);
    expect(needsOwnOption("gpt-legacy", true, CODEX)).toBe(false);
  });
});

describe("askModelHint", () => {
  test("names what an empty field runs on when it is known", () => {
    expect(askModelHint("codex", "gpt-6-sol")).toEqual({ key: "chat.new.model.hintCurrent", model: "gpt-6-sol" });
  });

  test("keeps the plain hint when it is not", () => {
    expect(askModelHint("codex", null)).toEqual({ key: "chat.new.model.hint" });
    expect(askModelHint("codex", undefined)).toEqual({ key: "chat.new.model.hint" });
    expect(askModelHint("codex", "")).toEqual({ key: "chat.new.model.hint" });
    expect(askModelHint("codex", AGENT_DEFAULT)).toEqual({ key: "chat.new.model.hint" });
  });

  test("LiteLLM keeps its provider/model hint", () => {
    expect(askModelHint("litellm", "openrouter/x")).toEqual({ key: "chat.new.model.litellm" });
  });
});
