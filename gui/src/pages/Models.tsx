import { ArrowRight, Check, Save, Undo2 } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { useT } from "../i18n";
import { useAction } from "../core/action";
import { get, post } from "../core/api";
import { backendName, shortPath } from "../core/format";
import { invalidate, useResource } from "../core/resource";
import { toast } from "../core/toast";
import type { RoutePreset, SettingsResult, SettingsView, StageView } from "../core/types";
import { PageHead } from "../shell/PageHead";
import { Chip, Field, LoadError, Notice, SectionTitle, Skeleton } from "../ui/bits";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Mark";
import type { PickerOption } from "../ui/Picker";
import { Picker } from "../ui/Picker";
import { Segmented } from "../ui/Segmented";
import { Toggle } from "../ui/Toggle";
import { SETTLE } from "../core/motion";

// Values the save endpoint and the pickers agree on.
const INHERIT = "__inherit__";
const FOLLOW = "__follow__";
const CUSTOM = "__custom__";
const AGENT_DEFAULT = "default";

interface StageForm {
  runtime: string;
  model: string;
  /** The user chose this model here, so it is kept even if the agent changes. */
  touched: boolean;
  custom: boolean;
}

interface Form {
  defaultRuntime: string;
  modelsDefault: string;
  modelsPin: boolean;
  stages: Record<string, StageForm>;
}

function formOf(view: SettingsView): Form {
  const stages: Record<string, StageForm> = {};
  for (const stage of view.stages) {
    stages[stage.id] = { runtime: stage.runtime_saved ?? INHERIT, model: stage.model_saved ?? "", touched: false, custom: false };
  }
  return {
    defaultRuntime: view.default_runtime.saved,
    modelsDefault: view.models_default.saved,
    modelsPin: view.models_pin.saved,
    stages,
  };
}

/** Only what differs from the saved config goes to the server. */
function payloadOf(view: SettingsView, form: Form): Record<string, unknown> | null {
  const saved = formOf(view);
  const payload: Record<string, unknown> = {};
  if (form.defaultRuntime !== saved.defaultRuntime) payload.default_runtime = form.defaultRuntime;
  if (form.modelsDefault !== saved.modelsDefault) payload.models_default = form.modelsDefault;
  if (form.modelsPin !== saved.modelsPin) payload.models_pin = form.modelsPin;
  const stages: Record<string, unknown> = {};
  for (const [id, stage] of Object.entries(form.stages)) {
    const before = saved.stages[id];
    const modelChanged = stage.touched && stage.model !== before.model;
    if (stage.runtime !== before.runtime || modelChanged) {
      stages[id] = { runtime: stage.runtime, model: stage.model, model_explicit: stage.touched };
    }
  }
  if (Object.keys(stages).length > 0) payload.stages = stages;
  return Object.keys(payload).length > 0 ? payload : null;
}

function showValue(value: unknown, none: string): string {
  if (value === null || value === undefined || value === "") return none;
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

interface StageCardProps {
  view: SettingsView;
  stage: StageView;
  index: number;
  form: Form;
  onChange: (next: StageForm) => void;
}

function StageCard({ view, stage, index, form, onChange }: StageCardProps) {
  const { t, tx } = useT();
  const ids = useId();
  const state = form.stages[stage.id];
  const routingChanged = state.runtime !== (stage.runtime_saved ?? INHERIT) || form.defaultRuntime !== view.default_runtime.saved;
  const runtime = state.runtime === INHERIT ? form.defaultRuntime : state.runtime;
  const supportsLlm = view.runtimes.find((item) => item.id === runtime)?.supports_llm;
  // With unchanged routing the server's own answer stands; otherwise mirror its rule.
  const backend = !routingChanged ? stage.completion_backend : supportsLlm || stage.id === "execute" ? runtime : view.llm_backend.effective;
  const remote = useResource<{ models: string[] }>(`/api/settings/models?backend=${encodeURIComponent(backend)}`, {
    enabled: backend !== stage.completion_backend,
  });
  const models = backend === stage.completion_backend ? stage.models : (remote.data?.models ?? []);
  const loadingModels = backend !== stage.completion_backend && remote.loading;

  const runtimeOptions: PickerOption[] = [
    { value: INHERIT, label: t("models.inherit", { agent: backendName(form.defaultRuntime) }) },
    ...view.runtimes.map((item) => ({
      value: item.id,
      label: backendName(item.id),
      hint: item.installed ? undefined : t("models.notInstalled"),
    })),
  ];

  const known = new Set(models);
  const modelOptions: PickerOption[] = [
    { value: FOLLOW, label: t("models.followAgent"), hint: stage.default_hint ? t("models.followHint", { model: stage.default_hint }) : undefined },
    ...models.map((model) => ({ value: model, label: model === AGENT_DEFAULT ? t("models.agentDefault") : model })),
  ];
  if (state.model && !known.has(state.model) && !state.custom) modelOptions.push({ value: state.model, label: state.model });
  modelOptions.push({ value: CUSTOM, label: t("models.custom") });
  const pickerValue = state.custom ? CUSTOM : state.model || FOLLOW;

  return (
    <motion.section
      className="stage"
      aria-labelledby={`${ids}-title`}
      variants={{ hidden: { opacity: 0, y: 12 }, show: { opacity: 1, y: 0, transition: { duration: 0.32, ease: [0.22, 1, 0.36, 1] } } }}
    >
      <header className="stage-head">
        <span className="loop-num" aria-hidden>
          {index + 1}
        </span>
        <div>
          <h3 id={`${ids}-title`}>{tx(`stage.${stage.id}.label`, stage.label)}</h3>
          <p>{tx(`stage.${stage.id}.hint`, stage.hint)}</p>
        </div>
      </header>
      <div className="stage-fields">
        <Field label={t("models.agent")} htmlFor={`${ids}-agent`}>
          <Picker id={`${ids}-agent`} value={state.runtime} options={runtimeOptions} onChange={(value) => onChange({ ...state, runtime: value })} />
        </Field>
        <Field
          label={t("models.model")}
          htmlFor={`${ids}-model`}
          aside={loadingModels ? <Spinner size={13} label={t("models.loadingModels")} /> : backend !== runtime ? <span className="field-aside">{t("models.via", { backend: backendName(backend) })}</span> : null}
          hint={stage.model_env.length > 0 ? t("models.envOverride", { name: stage.model_env.join(", ") }) : undefined}
        >
          <Picker
            id={`${ids}-model`}
            value={pickerValue}
            options={modelOptions}
            onChange={(value) => {
              if (value === CUSTOM) onChange({ ...state, custom: true, touched: true, model: state.model });
              else onChange({ ...state, custom: false, touched: true, model: value === FOLLOW ? "" : value });
            }}
          />
        </Field>
        <AnimatePresence initial={false}>
          {state.custom ? (
            <motion.div className="stage-custom" initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }}>
              <Field label={t("models.customLabel")} htmlFor={`${ids}-custom`} hint={stage.model_key ? <code>{stage.model_key}</code> : undefined}>
                <input
                  id={`${ids}-custom`}
                  className="input input-mono"
                  value={state.model}
                  placeholder={models.find((model) => model !== AGENT_DEFAULT) ?? "model-id"}
                  spellCheck={false}
                  autoComplete="off"
                  onChange={(event) => onChange({ ...state, model: event.target.value, touched: true })}
                />
              </Field>
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
    </motion.section>
  );
}

function PresetButton({ preset, view, active, onApply }: { preset: RoutePreset; view: SettingsView; active: boolean; onApply: () => void }) {
  const { t, tx } = useT();
  const missing = [...new Set(Object.values(preset.routes))].filter((id) => !view.runtimes.find((item) => item.id === id)?.installed);
  return (
    <button type="button" className="preset" aria-pressed={active} onClick={onApply}>
      <span className="preset-name">
        {tx(`preset.${preset.id}.name`, preset.id)}
        {active ? <Check size={14} aria-hidden /> : null}
      </span>
      <span className="preset-note">{tx(`preset.${preset.id}.note`, preset.note)}</span>
      <span className="preset-route">
        {view.stages.map((stage, index) => (
          <span key={stage.id} className="preset-hop">
            {index > 0 ? <ArrowRight size={11} aria-hidden /> : null}
            {backendName(preset.routes[stage.id] ?? "")}
          </span>
        ))}
      </span>
      {missing.length > 0 ? <span className="preset-missing">{t("models.presetMissing", { agents: missing.map(backendName).join(", ") })}</span> : null}
    </button>
  );
}

export function ModelsPage(_props: { rest: string }) {
  const { t, tx } = useT();
  const settings = useResource<SettingsView>("/api/settings");
  const view = settings.data;
  const [form, setForm] = useState<Form | null>(null);
  const [preview, setPreview] = useState<SettingsResult | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const stamp = useRef("");

  // A fresh server view replaces the form unless the user has edits in flight.
  useEffect(() => {
    if (!view) return;
    const signature = JSON.stringify(formOf(view));
    if (stamp.current !== signature) {
      stamp.current = signature;
      setForm(formOf(view));
      setPreview(null);
    }
  }, [view]);

  const payload = useMemo(() => (view && form ? payloadOf(view, form) : null), [view, form]);
  const payloadKey = payload ? JSON.stringify(payload) : "";

  useEffect(() => {
    if (!payloadKey) {
      setPreview(null);
      setPreviewing(false);
      return;
    }
    setPreviewing(true);
    let stale = false;
    const timer = window.setTimeout(async () => {
      try {
        const result = await post<SettingsResult>("/api/settings/preview", JSON.parse(payloadKey));
        if (!stale) setPreview(result);
      } catch {
        if (!stale) setPreview(null);
      } finally {
        if (!stale) setPreviewing(false);
      }
    }, 220);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [payloadKey]);

  const [undo, undoing] = useAction(
    async () => {
      await post("/api/settings/undo");
      invalidate("/api/overview", "/api/clients", "/api/chat");
      await settings.reload();
      toast({ tone: "ok", title: t("models.undone") });
    },
    { tx, failTitle: t("models.undoFailed") },
  );

  const [save, saving] = useAction(
    async () => {
      if (!payload) return;
      const result = await post<SettingsResult>("/api/settings/save", payload);
      invalidate("/api/overview", "/api/clients", "/api/chat");
      const fresh = await get<SettingsView>("/api/settings");
      settings.mutate(fresh);
      if (result.saved) {
        toast({
          tone: "ok",
          title: t("models.saved", { count: result.changes.length }),
          body: result.reconnect ? t("models.reconnect") : undefined,
          // The very first save has no earlier file to go back to.
          action: fresh.undo_available ? { label: t("models.undo"), run: () => void undo() } : undefined,
        });
      } else {
        toast({ tone: "info", title: t("models.nothing") });
      }
    },
    { tx, failTitle: t("models.saveFailed") },
  );

  if (settings.error && !view) {
    return (
      <>
        <PageHead title={t("models.title")} lead={t("models.lead")} />
        <LoadError error={settings.error} onRetry={() => void settings.reload()} />
      </>
    );
  }
  if (!view || !form) {
    return (
      <>
        <PageHead title={t("models.title")} lead={t("models.lead")} />
        <div className="card card-pad">
          <Skeleton lines={6} />
        </div>
      </>
    );
  }

  const dirty = payload !== null;
  const changes = preview?.changes ?? [];
  const presetActive = (preset: RoutePreset) =>
    view.stages.every((stage) => {
      const state = form.stages[stage.id];
      const runtime = state.runtime === INHERIT ? form.defaultRuntime : state.runtime;
      return runtime === preset.routes[stage.id];
    });
  const tierOptions = [...new Set([...view.models_default.presets, form.modelsDefault])].filter(Boolean);

  return (
    <>
      <PageHead
        title={t("models.title")}
        lead={
          <>
            {t("models.lead")} <span className="path">{shortPath(view.config_path)}</span>
          </>
        }
        actions={
          <Button icon={<Undo2 size={15} aria-hidden />} pending={undoing} disabled={!view.undo_available || dirty} onClick={() => void undo()} title={view.undo_available ? undefined : t("models.undoNone")}>
            {t("models.undoLast")}
          </Button>
        }
      />
      <motion.div className="stack has-savebar" initial="hidden" animate="show" variants={{ show: { transition: { staggerChildren: 0.05 } } }}>
        <motion.section className="card card-pad" variants={{ hidden: { opacity: 0, y: 12 }, show: { opacity: 1, y: 0 } }} aria-labelledby="models-defaults">
          <SectionTitle>
            <span id="models-defaults">{t("models.defaults")}</span>
          </SectionTitle>
          <div className="defaults">
            <Field
              label={t("models.defaultAgent")}
              htmlFor="default-agent"
              hint={view.default_runtime.env.length > 0 ? t("models.envOverride", { name: view.default_runtime.env.join(", ") }) : t("models.defaultAgent.hint")}
            >
              <Picker
                id="default-agent"
                value={form.defaultRuntime}
                onChange={(value) => setForm({ ...form, defaultRuntime: value })}
                options={view.runtimes.map((item) => ({
                  value: item.id,
                  label: backendName(item.id),
                  hint: item.installed ? undefined : t("models.notInstalled"),
                }))}
              />
            </Field>
            <Field
              label={t("models.tier")}
              hint={view.models_default.env.length > 0 ? t("models.envOverride", { name: view.models_default.env.join(", ") }) : tx(`tier.${form.modelsDefault}.hint`, t("models.tier.hint"))}
            >
              <Segmented
                stretch
                label={t("models.tier")}
                value={form.modelsDefault}
                onChange={(value) => setForm({ ...form, modelsDefault: value })}
                options={tierOptions.map((id) => ({ value: id, label: tx(`tier.${id}`, id) }))}
              />
            </Field>
            <div className="switch-row">
              <div>
                <label htmlFor="models-pin" className="field-label">
                  {t("models.pin")}
                </label>
                <p className="field-hint">{view.models_pin.env.length > 0 ? t("models.envOverride", { name: view.models_pin.env.join(", ") }) : t("models.pin.hint")}</p>
              </div>
              <Toggle id="models-pin" label={t("models.pin")} checked={form.modelsPin} onChange={(checked) => setForm({ ...form, modelsPin: checked })} />
            </div>
          </div>
          {view.llm_backend.effective !== view.default_runtime.effective ? (
            <p className="section-note">{t("models.llmBackend", { backend: backendName(view.llm_backend.effective) })}</p>
          ) : null}
        </motion.section>

        <motion.section variants={{ hidden: { opacity: 0, y: 12 }, show: { opacity: 1, y: 0 } }} aria-labelledby="models-presets">
          <SectionTitle>
            <span id="models-presets">{t("models.presets")}</span>
          </SectionTitle>
          <div className="presets">
            {view.presets.map((preset) => (
              <PresetButton
                key={preset.id}
                preset={preset}
                view={view}
                active={presetActive(preset)}
                onApply={() => {
                  const stages = { ...form.stages };
                  for (const stage of view.stages) {
                    const target = preset.routes[stage.id];
                    if (target) stages[stage.id] = { ...stages[stage.id], runtime: target === form.defaultRuntime && !stage.runtime_saved ? INHERIT : target };
                  }
                  setForm({ ...form, stages });
                }}
              />
            ))}
          </div>
        </motion.section>

        <section aria-labelledby="models-stages">
          <SectionTitle>
            <span id="models-stages">{t("models.stages")}</span>
          </SectionTitle>
          <div className="card stages">
            {view.stages.map((stage, index) => (
              <StageCard key={stage.id} view={view} stage={stage} index={index} form={form} onChange={(next) => setForm({ ...form, stages: { ...form.stages, [stage.id]: next } })} />
            ))}
          </div>
        </section>
      </motion.div>

      <AnimatePresence>
        {dirty ? (
          <motion.div
            className="savebar"
            role="region"
            aria-label={t("models.pending")}
            initial={{ y: 80, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: 80, opacity: 0 }}
            transition={SETTLE}
          >
            <div className="savebar-changes" aria-live="polite">
              <strong>
                {previewing && !preview ? <Spinner size={13} /> : null}
                {preview ? (changes.length > 0 ? t("models.changes", { count: changes.length }) : t("models.noEffect")) : t("models.checking")}
              </strong>
              {changes.length > 0 ? (
                <ul className="diff">
                  {changes.map((change) => (
                    <li key={change.key}>
                      <code>{change.key}</code>
                      <span className="diff-old">{showValue(change.old, t("models.unset"))}</span>
                      <ArrowRight size={12} aria-hidden />
                      <span className="diff-new">{showValue(change.new, t("models.unset"))}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
              {preview?.reconnect ? <Chip tone="warn">{t("models.reconnect")}</Chip> : null}
            </div>
            <div className="savebar-actions">
              <Button variant="ghost" onClick={() => setForm(formOf(view))}>
                {t("models.discard")}
              </Button>
              <Button variant="primary" icon={<Save size={15} aria-hidden />} pending={saving} disabled={!!preview && changes.length === 0} onClick={() => void save()}>
                {t("common.save")}
              </Button>
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
      {view.default_runtime.env.length > 0 ? <Notice tone="warn">{t("models.envNote")}</Notice> : null}
    </>
  );
}
