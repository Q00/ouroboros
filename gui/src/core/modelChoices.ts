// How model pickers render a backend's model list. Pure, so the rules are tested
// on their own (modelChoices.test.ts).

/** The sentinel a self-selecting agent receives: its CLI's own configured model. */
export const AGENT_DEFAULT = "default";

/**
 * The model ids a picker lists next to its own "follow the agent" or empty
 * choice. The sentinel is left out: it means the same as that choice, so
 * listing it too would offer one setting twice.
 */
export function listedModels(models: readonly string[]): string[] {
  return models.filter((model) => model !== AGENT_DEFAULT);
}

/**
 * The option a stage's model picker shows. No model and the sentinel both
 * show the inherit option, so a saved `"default"` stays visible as what it
 * means instead of disappearing from the picker.
 */
export function stagePickerValue(model: string, custom: boolean, inherit: string, customValue: string): string {
  if (custom) return customValue;
  return model && model !== AGENT_DEFAULT ? model : inherit;
}

/** Whether a stage's model must be added to its picker because the list does not have it. */
export function needsOwnOption(model: string, custom: boolean, models: readonly string[]): boolean {
  return !custom && !!model && model !== AGENT_DEFAULT && !models.includes(model);
}

export type AskModelHint =
  | { key: "chat.new.model.litellm" }
  | { key: "chat.new.model.hint" }
  | { key: "chat.new.model.hintCurrent"; model: string };

/**
 * The hint under the Ask model field. An empty field means the default; when
 * the console knows what that runs on, the hint names it.
 */
export function askModelHint(backend: string, current: string | null | undefined): AskModelHint {
  if (backend === "litellm") return { key: "chat.new.model.litellm" };
  if (current && current !== AGENT_DEFAULT) return { key: "chat.new.model.hintCurrent", model: current };
  return { key: "chat.new.model.hint" };
}
