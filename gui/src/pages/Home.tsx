import { ArrowRight, Check, MessagesSquare, RefreshCw, Sprout } from "lucide-react";
import { motion } from "motion/react";
import { useState } from "react";

import { useT } from "../i18n";
import { backendName, shortPath } from "../core/format";
import { useResource } from "../core/resource";
import type { PageId } from "../core/router";
import { href } from "../core/router";
import type { Overview, SettingsView, StageView } from "../core/types";
import { PageHead } from "../shell/PageHead";
import { Chip, Command, Dot, Empty, Kpis, LoadError, SectionTitle, Skeleton } from "../ui/bits";
import { Button } from "../ui/Button";
import { Segmented } from "../ui/Segmented";
import { RunRow } from "./Runs";
import { SETTLE } from "../core/motion";

const STAGGER = {
  hidden: {},
  show: { transition: { staggerChildren: 0.05, delayChildren: 0.04 } },
};

const RISE = {
  hidden: { opacity: 0, y: 12 },
  show: { opacity: 1, y: 0, transition: { duration: 0.36, ease: [0.22, 1, 0.36, 1] as const } },
};

function ringPoint(angle: number, radius: number): [number, number] {
  const radians = ((angle - 90) * Math.PI) / 180;
  return [100 + radius * Math.cos(radians), 100 + radius * Math.sin(radians)];
}

function ringArc(from: number, to: number, radius: number): string {
  const [x0, y0] = ringPoint(from, radius);
  const [x1, y1] = ringPoint(to, radius);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)}A${radius} ${radius} 0 0 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

/** The pipeline as it runs: four stages on one ring, the head chasing the tail. */
function LoopRing({ stages, active, onActive }: { stages: StageView[]; active: number | null; onActive: (index: number | null) => void }) {
  const count = Math.max(stages.length, 1);
  const span = 360 / count;
  const gap = 9;
  return (
    <svg className="loop-ring" viewBox="0 0 200 200" aria-hidden>
      <circle cx="100" cy="100" r="78" className="loop-track" />
      {stages.map((stage, index) => (
        <motion.path
          key={stage.id}
          d={ringArc(index * span + gap, (index + 1) * span - gap, 78)}
          className="loop-arc"
          data-active={active === index || undefined}
          onPointerEnter={() => onActive(index)}
          onPointerLeave={() => onActive(null)}
          initial={{ pathLength: 0, opacity: 0 }}
          animate={{ pathLength: 1, opacity: 1 }}
          transition={{ duration: 0.7, delay: 0.15 + index * 0.16, ease: [0.22, 1, 0.36, 1] }}
        />
      ))}
      {/* The head runs over the arcs and passes under each numbered stage. */}
      <g className="loop-head">
        <circle cx="100" cy="22" r="4.5" />
      </g>
      {stages.map((stage, index) => {
        const [nx, ny] = ringPoint(index * span + span / 2, 78);
        return (
          <motion.g
            key={stage.id}
            className="loop-stage"
            data-active={active === index || undefined}
            onPointerEnter={() => onActive(index)}
            onPointerLeave={() => onActive(null)}
            initial={{ scale: 0, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ ...SETTLE, delay: 0.3 + index * 0.16 }}
            style={{ transformOrigin: `${nx}px ${ny}px` }}
          >
            <circle cx={nx} cy={ny} r="13" className="loop-node" />
            <text x={nx} y={ny} className="loop-node-text" textAnchor="middle" dominantBaseline="central">
              {index + 1}
            </text>
          </motion.g>
        );
      })}
    </svg>
  );
}

function LoopCard({ settings }: { settings: SettingsView | undefined }) {
  const { t, tx } = useT();
  const [active, setActive] = useState<number | null>(null);
  const stages = settings?.stages ?? [];
  return (
    <motion.section className="card loop-card" variants={RISE} aria-labelledby="loop-title">
      <div className="loop-figure">
        <LoopRing stages={stages} active={active} onActive={setActive} />
        <div className="loop-center">
          <span className="loop-center-label">{t("home.loop.default")}</span>
          <strong>{settings ? backendName(settings.default_runtime.effective) : "…"}</strong>
        </div>
      </div>
      <div className="loop-body">
        <h2 id="loop-title" className="loop-title">
          {t("home.loop.title")}
        </h2>
        <p className="loop-lead">{t("home.loop.lead")}</p>
        {settings ? (
          <ol className="loop-stages">
            {stages.map((stage, index) => (
              <li key={stage.id}>
                <a
                  className="loop-row"
                  href={href("models")}
                  data-active={active === index || undefined}
                  onPointerEnter={() => setActive(index)}
                  onPointerLeave={() => setActive(null)}
                  onFocus={() => setActive(index)}
                  onBlur={() => setActive(null)}
                >
                  <span className="loop-num" aria-hidden>
                    {index + 1}
                  </span>
                  <span className="loop-row-text">
                    <strong>{tx(`stage.${stage.id}.label`, stage.label)}</strong>
                    <span>{tx(`stage.${stage.id}.hint`, stage.hint)}</span>
                  </span>
                  <span className="loop-row-agent">
                    <Chip>{backendName(stage.runtime_effective)}</Chip>
                    <span className="loop-row-model">{stage.model_saved ?? t("models.followAgent")}</span>
                  </span>
                </a>
              </li>
            ))}
          </ol>
        ) : (
          <Skeleton lines={4} tall />
        )}
      </div>
    </motion.section>
  );
}

function Checklist({ overview }: { overview: Overview }) {
  const { t } = useT();
  const steps = stepsOf(overview);
  const done = steps.filter((step) => step.done).length;
  return (
    <motion.section className="card card-pad" variants={RISE} aria-labelledby="checklist-title">
      <SectionTitle aside={<span className="count-note">{t("home.setup.progress", { done, total: steps.length })}</span>}>
        <span id="checklist-title">{t("home.setup.title")}</span>
      </SectionTitle>
      <div className="meter" role="progressbar" aria-valuemin={0} aria-valuemax={steps.length} aria-valuenow={done} aria-labelledby="checklist-title">
        <motion.span className="meter-fill" initial={{ width: 0 }} animate={{ width: `${(done / steps.length) * 100}%` }} transition={{ duration: 0.7, delay: 0.3, ease: [0.22, 1, 0.36, 1] }} />
      </div>
      <ul className="steps">
        {steps.map((step) => (
          <li key={step.id}>
            <a className="step" href={href(step.page)} data-done={step.done || undefined}>
              <span className="step-mark" aria-hidden>
                {step.done ? <Check size={13} /> : null}
              </span>
              <span className="step-text">
                <strong>{t(`home.setup.${step.id}`)}</strong>
                <span>{t(`home.setup.${step.id}.hint`)}</span>
              </span>
              <span className="sr-only">{step.done ? t("common.done") : t("common.todo")}</span>
              <ArrowRight size={14} aria-hidden className="step-arrow" />
            </a>
          </li>
        ))}
      </ul>
    </motion.section>
  );
}

function stepsOf(overview: Overview): { id: "install" | "connect" | "health" | "run"; done: boolean; page: PageId }[] {
  return [
    { id: "install", done: overview.clients.installed > 0, page: "connect" },
    { id: "connect", done: overview.clients.connected > 0, page: "connect" },
    { id: "health", done: overview.doctor.fail === 0 && overview.doctor.warn === 0, page: "health" },
    { id: "run", done: overview.runs.length > 0, page: "chat" },
  ];
}

/** Setup progress in one line, with the next thing to do. */
function SetupBrief({ overview }: { overview: Overview }) {
  const { t } = useT();
  const steps = stepsOf(overview);
  const done = steps.filter((step) => step.done).length;
  const next = steps.find((step) => !step.done);
  return (
    <motion.section className="card card-pad" variants={RISE} aria-labelledby="brief-title">
      <SectionTitle aside={<span className="count-note">{t("home.setup.progress", { done, total: steps.length })}</span>}>
        <span id="brief-title">{t("home.setup.title")}</span>
      </SectionTitle>
      <div className="meter" role="progressbar" aria-valuemin={0} aria-valuemax={steps.length} aria-valuenow={done} aria-labelledby="brief-title">
        <motion.span className="meter-fill" initial={{ width: 0 }} animate={{ width: `${(done / steps.length) * 100}%` }} transition={{ duration: 0.7, delay: 0.25, ease: [0.22, 1, 0.36, 1] }} />
      </div>
      {next ? (
        <a className="step" href={href(next.page)}>
          <span className="step-mark" aria-hidden />
          <span className="step-text">
            <strong>{t(`home.setup.${next.id}`)}</strong>
            <span>{t(`home.setup.${next.id}.hint`)}</span>
          </span>
          <ArrowRight size={14} aria-hidden className="step-arrow" />
        </a>
      ) : (
        <p className="section-note">{t("home.setup.done")}</p>
      )}
    </motion.section>
  );
}

function Readiness({ overview }: { overview: Overview }) {
  const { t } = useT();
  const rows: { page: PageId; label: string; value: string; tone: "ok" | "warn" | "fail" | "muted" }[] = [
    { page: "connect", label: t("nav.connect"), value: t("home.stat.tools.detail", { installed: overview.clients.installed, total: overview.clients.total }), tone: overview.clients.connected ? "ok" : overview.clients.installed ? "warn" : "muted" },
    { page: "models", label: t("nav.models"), value: t("home.stat.agent.detail", { backend: backendName(overview.llm_backend) }), tone: "ok" },
    { page: "providers", label: t("nav.providers"), value: `${overview.providers.configured} / ${overview.providers.total}`, tone: overview.providers.configured ? "ok" : "muted" },
    { page: "health", label: t("nav.health"), value: t("home.stat.health.detail", { pass: overview.doctor.pass }), tone: overview.doctor.fail ? "fail" : overview.doctor.warn ? "warn" : "ok" },
  ];
  return (
    <motion.section className="card card-pad" variants={RISE} aria-labelledby="ready-title">
      <SectionTitle>
        <span id="ready-title">{t("home.ready.title")}</span>
      </SectionTitle>
      <ul className="ready">
        {rows.map((row) => (
          <li key={row.page}>
            <a className="ready-row" href={href(row.page)}>
              <Dot tone={row.tone} />
              <strong>{row.label}</strong>
              <span>{row.value}</span>
              <ArrowRight size={13} aria-hidden className="step-arrow" />
            </a>
          </li>
        ))}
      </ul>
    </motion.section>
  );
}

type Tab = "overview" | "setup";

export function HomePage(_props: { rest: string }) {
  const { t } = useT();
  const overview = useResource<Overview>("/api/overview", { interval: 12_000 });
  const settings = useResource<SettingsView>("/api/settings");
  const [tab, setTab] = useState<Tab>("overview");
  const data = overview.data;
  const state = !data ? "ok" : data.doctor.fail ? "fail" : data.doctor.warn || data.clients.attention || data.clients.connected === 0 ? "warn" : "ok";

  return (
    <>
      <PageHead
        tabs={
          <Segmented<Tab>
            label={t("home.title")}
            value={tab}
            onChange={setTab}
            options={[
              { value: "overview", label: t("home.tab.overview") },
              { value: "setup", label: t("home.setup.title") },
            ]}
          />
        }
        dot={state}
        dotPulse={state === "ok"}
        title={t(`home.head.${state}`)}
        actions={
          <>
            <Button icon={<RefreshCw size={14} aria-hidden />} pending={overview.refreshing && !!data} onClick={() => void overview.reload()}>
              {t("common.refresh")}
            </Button>
            <a className="btn btn-primary" href={href("chat")}>
              <MessagesSquare size={14} aria-hidden />
              <span className="btn-label">{t("home.loop.start")}</span>
            </a>
          </>
        }
      />
      {overview.error && !data ? <LoadError error={overview.error} onRetry={() => void overview.reload()} /> : null}
      {!data && overview.loading ? (
        <div className="card card-pad">
          <Skeleton lines={5} />
        </div>
      ) : null}
      {data ? (
        <motion.div key={tab} className="stack" variants={STAGGER} initial="hidden" animate="show">
          <motion.div variants={RISE}>
            <Kpis
              items={[
                { label: t("home.fact.version"), value: `v${data.version}`, href: href("health") },
                { label: t("home.stat.agent"), value: backendName(data.default_runtime), href: href("models") },
                { label: t("home.stat.tools"), value: `${data.clients.connected} / ${data.clients.installed}`, href: href("connect") },
                {
                  label: t("home.stat.health"),
                  value: data.doctor.fail ? t("home.stat.health.fail", { count: data.doctor.fail }) : data.doctor.warn ? t("home.stat.health.warn", { count: data.doctor.warn }) : t("home.stat.health.ok"),
                  tone: data.doctor.fail ? "fail" : data.doctor.warn ? "warn" : undefined,
                  href: href("health"),
                },
              ]}
            />
          </motion.div>
          {tab === "overview" ? (
            <>
              <div className="home-grid">
                <LoopCard settings={settings.data} />
                <motion.section className="card card-pad" variants={RISE} aria-labelledby="recent-title">
                  <SectionTitle aside={<a className="more-link" href={href("runs")}>{t("home.runs.all")}</a>}>
                    <span id="recent-title">{t("home.runs.title")}</span>
                  </SectionTitle>
                  {data.runs.length === 0 ? (
                    <Empty icon={<Sprout size={18} aria-hidden />} title={t("home.runs.empty")} body={t("home.runs.emptyBody")} />
                  ) : (
                    <ul className="run-list">
                      {data.runs.map((run) => (
                        <li key={run.execution_id}>
                          <RunRow run={run} />
                        </li>
                      ))}
                    </ul>
                  )}
                </motion.section>
                <SetupBrief overview={data} />
                <Readiness overview={data} />
              </div>
              {data.update_available ? (
                <motion.section className="card card-pad update" variants={RISE}>
                  <div>
                    <strong>{t("home.update.title", { version: data.latest_version ?? "" })}</strong>
                    <p>{t("home.update.body", { version: data.version })}</p>
                  </div>
                  <Command text="ouroboros update" />
                </motion.section>
              ) : null}
            </>
          ) : (
            <>
              <Checklist overview={data} />
              <motion.dl className="facts card card-pad" variants={RISE}>
                <div>
                  <dt>Python</dt>
                  <dd>{data.python}</dd>
                </div>
                <div>
                  <dt>{t("home.fact.platform")}</dt>
                  <dd>{data.platform}</dd>
                </div>
                <div>
                  <dt>{t("home.stat.providers")}</dt>
                  <dd>
                    {data.providers.configured} / {data.providers.total}
                  </dd>
                </div>
                <div>
                  <dt>{t("home.fact.config")}</dt>
                  <dd className="path">{shortPath(data.config_path)}</dd>
                </div>
              </motion.dl>
            </>
          )}
        </motion.div>
      ) : null}
    </>
  );
}
