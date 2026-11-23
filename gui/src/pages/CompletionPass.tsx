// The completion pass on an Auto run's card: after the criteria pass, Ouroboros
// uses the app in a browser like a first-time user, fixes what it found and uses
// it again (orchestrator/completion_pass.py). The card says only what the pass's
// stage events say (gui/completion_view.py) and shows the latest play's
// screenshots, served from the chat's folder.

import { CircleCheck, GitCompareArrows, MonitorPlay, MousePointerClick, ScanSearch, TriangleAlert, Wrench } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import type { ReactNode } from "react";
import { useState } from "react";

import type { Key, Translator } from "../i18n";
import { useT } from "../i18n";
import { streamUrl } from "../core/api";
import { EASE_OUT } from "../core/motion";
import type { CompletionPass as Pass, CompletionStep } from "../core/types";
import { Spinner } from "../ui/Mark";
import { Modal } from "../ui/Modal";

const PLAY = /^play(?:_(\d))?$/;
const LOOK = /^play_review(?:_(\d))?$/;
const PICK = /^pick_\d(?:_swapped)?$/;

const DONE: Record<NonNullable<Pass["status"]>, Key> = {
  fixed: "completion.done.fixed",
  clean: "completion.done.clean",
  fix_failed: "completion.done.fix_failed",
  review_failed: "completion.done.review_failed",
};

/** The pass's own line: what it is doing now, or how it ended. */
export function completionLine(t: Translator["t"], pass: Pass): string {
  if (pass.done) return t(pass.status ? DONE[pass.status] : "completion.done.other");
  const n = pass.round ?? 1;
  const count = pass.defects ?? 0;
  const stage = pass.stage;
  if (PLAY.test(stage)) return t("completion.stage.play", { n });
  if (LOOK.test(stage)) return t("completion.stage.play_review", { n });
  if (PICK.test(stage)) return t("completion.stage.pick");
  switch (stage) {
    case "review":
      return t("completion.stage.review");
    case "play_plan":
      return t("completion.stage.play_plan");
    case "fix":
      return pass.defects === null ? t("completion.stage.other") : t("completion.stage.fix", { count });
    case "repair":
      return pass.defects === null ? t("completion.stage.other") : t("completion.stage.repair", { count });
    case "recheck":
      return t("completion.stage.recheck");
    default:
      return t("completion.stage.other");
  }
}

/** The Verify step's short caption while the pass runs. */
export function completionCaption(t: Translator["t"], pass: Pass): string {
  const stage = pass.stage;
  if (PLAY.test(stage) || LOOK.test(stage)) return t("auto.caption.pass.play", { n: pass.round ?? 1 });
  if ((stage === "fix" || stage === "repair") && pass.defects !== null) return t("auto.caption.pass.fix", { count: pass.defects });
  if (PICK.test(stage)) return t("auto.caption.pass.pick");
  return t("auto.caption.pass.check");
}

function stepLabel(t: Translator["t"], step: CompletionStep): string {
  switch (step.kind) {
    case "review":
      return t("completion.step.review");
    case "play":
      return t("completion.step.play", { n: step.round });
    case "fix":
    case "repair":
      return step.defects === null ? t("completion.step.fixPlain") : t("completion.step.fix", { count: step.defects });
    case "pick":
      return t("completion.step.pick");
  }
}

const STEP_ICON: Record<CompletionStep["kind"], typeof Wrench> = {
  review: ScanSearch,
  play: MousePointerClick,
  fix: Wrench,
  repair: Wrench,
  pick: GitCompareArrows,
};

/** `03-after-dragging-to-rotate.png` → `after dragging to rotate`. */
export function shotName(file: string): string {
  return file.replace(/^\d{2}-/, "").replace(/\.png$/, "").replace(/[-_]+/g, " ").trim();
}

function shotUrl(chatId: string, round: number, file: string): string {
  return streamUrl(`/api/chat/sessions/${encodeURIComponent(chatId)}/playtest/round-${round}/${encodeURIComponent(file)}`);
}

function Steps({ pass, live }: { pass: Pass; live: boolean }) {
  const { t } = useT();
  return (
    <ol className="pass-steps" aria-label={t("completion.steps")}>
      {pass.steps.map((step, index) => {
        const Icon = STEP_ICON[step.kind];
        const current = live && !pass.done && index === pass.steps.length - 1;
        return (
          <motion.li
            key={`${index}-${step.kind}`}
            className="pass-step"
            data-kind={step.kind}
            data-current={current || undefined}
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.22, ease: EASE_OUT }}
          >
            <Icon size={12} aria-hidden />
            <span>{stepLabel(t, step)}</span>
          </motion.li>
        );
      })}
    </ol>
  );
}

function Shots({ chatId, shots }: { chatId: string; shots: NonNullable<Pass["shots"]> }) {
  const { t } = useT();
  const [open, setOpen] = useState<string | null>(null);
  return (
    <figure className="pass-shots">
      <ul className="pass-strip">
        <AnimatePresence initial={false}>
          {shots.names.map((file, index) => (
            <motion.li
              key={`${shots.round}/${file}`}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.28, ease: EASE_OUT, delay: Math.min(index, 7) * 0.04 }}
            >
              <button type="button" className="pass-shot" onClick={() => setOpen(file)} aria-label={t("completion.shot.open", { name: shotName(file) })}>
                <img src={shotUrl(chatId, shots.round, file)} alt="" loading="lazy" decoding="async" />
                <span className="pass-shot-name">{shotName(file)}</span>
              </button>
            </motion.li>
          ))}
        </AnimatePresence>
      </ul>
      <figcaption>{t("completion.shots", { count: shots.names.length, n: shots.round })}</figcaption>
      <Modal open={open !== null} onOpenChange={(next) => (next ? null : setOpen(null))} title={open ? shotName(open) : ""} wide>
        {open ? <img className="pass-shot-large" src={shotUrl(chatId, shots.round, open)} alt={shotName(open)} /> : null}
      </Modal>
    </figure>
  );
}

/** The pass under the run's tiles; `live` while the Auto chat still runs. */
export function CompletionPassView({ pass, chatId, live }: { pass: Pass; chatId: string | null; live: boolean }) {
  const { t } = useT();
  const failed = pass.done && (pass.status === "fix_failed" || pass.status === "review_failed");
  let mark: ReactNode = <MonitorPlay size={14} aria-hidden />;
  if (pass.done) mark = failed ? <TriangleAlert size={14} aria-hidden /> : <CircleCheck size={14} aria-hidden />;
  else if (live) mark = <Spinner size={13} />;
  const line = completionLine(t, pass);
  return (
    <section className="pass" data-state={pass.done ? (failed ? "warn" : "done") : live ? "live" : "stopped"}>
      <header className="pass-head">
        <strong>{t("completion.title")}</strong>
      </header>
      <p className="pass-now" role="status">
        <span className="pass-mark">{mark}</span>
        <AnimatePresence mode="wait" initial={false}>
          <motion.span key={line} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.18 }}>
            {line}
          </motion.span>
        </AnimatePresence>
      </p>
      {pass.steps.length > 0 ? <Steps pass={pass} live={live} /> : null}
      {pass.shots && pass.shots.names.length > 0 && chatId ? <Shots chatId={chatId} shots={pass.shots} /> : null}
    </section>
  );
}
