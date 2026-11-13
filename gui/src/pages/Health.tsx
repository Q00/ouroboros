import { CircleAlert, CircleCheck, Download, PackageCheck, RotateCw, TriangleAlert } from "lucide-react";
import { motion } from "motion/react";
import { useState } from "react";

import { useT } from "../i18n";
import { explain, useAction } from "../core/action";
import { ApiFailure, post } from "../core/api";
import { shortPath } from "../core/format";
import { openJobs, trackJob } from "../core/jobs";
import { invalidate, useResource } from "../core/resource";
import { restartConsole } from "../core/server";
import { toast } from "../core/toast";
import type { ComponentRow, ComponentsView, Doctor, DoctorSurface, JobSnapshot } from "../core/types";
import { PageHead } from "../shell/PageHead";
import { Chip, Command, Kpis, LoadError, Notice, SectionTitle, Skeleton } from "../ui/bits";
import { Segmented } from "../ui/Segmented";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { useDialog } from "../ui/useDialog";

const SURFACE_ICON = { pass: CircleCheck, warn: TriangleAlert, fail: CircleAlert };
const SURFACE_TONE = { pass: "ok", warn: "warn", fail: "fail" } as const;

function humanize(name: string): string {
  const spaced = name.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function Surface({ surface }: { surface: DoctorSurface }) {
  const { tx } = useT();
  const Icon = SURFACE_ICON[surface.status];
  return (
    <motion.li className="surface" variants={{ hidden: { opacity: 0, x: -8 }, show: { opacity: 1, x: 0 } }}>
      <Icon size={17} aria-hidden className={`tone-${SURFACE_TONE[surface.status]}`} />
      <div className="surface-text">
        <strong>{tx(`doctor.${surface.name}`, humanize(surface.name))}</strong>
        <span>{surface.message}</span>
        {surface.remediation ? <span className="surface-fix">{surface.remediation}</span> : null}
        {surface.path ? (
          <span className="path" title={surface.path}>
            {shortPath(surface.path)}
          </span>
        ) : null}
      </div>
      <Chip tone={SURFACE_TONE[surface.status]}>{tx(`doctor.status.${surface.status}`, surface.status)}</Chip>
    </motion.li>
  );
}

export function HealthPage(_props: { rest: string }) {
  const { t, tx } = useT();
  const doctor = useResource<Doctor>("/api/doctor", { interval: 20_000 });
  const components = useResource<ComponentsView>("/api/components", { interval: 8000 });
  const dialog = useDialog<ComponentRow>();
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [restartHint, setRestartHint] = useState(false);
  const [starting, setStarting] = useState(false);
  const [tab, setTab] = useState<"components" | "checks" | "commands">("components");

  const [restart, restarting] = useAction(() => restartConsole(), { tx, failTitle: t("shell.powerFailed") });

  async function install(row: ComponentRow, acceptConflict: boolean) {
    setStarting(true);
    try {
      const { job } = await post<{ job: JobSnapshot }>(`/api/components/${row.extra}/install`, { accept_conflict: acceptConflict });
      dialog.hide();
      setBusy((current) => ({ ...current, [row.extra]: true }));
      void trackJob(job, { reveal: true }).then(async (done) => {
        setBusy((current) => ({ ...current, [row.extra]: false }));
        invalidate("/api/providers", "/api/overview");
        await components.reload();
        if (done.status === "succeeded") {
          setRestartHint(true);
          toast({ tone: "ok", title: t("health.install.ok", { name: row.label }), body: t("health.restartHint"), action: { label: t("shell.restart"), run: () => void restart() } });
        } else {
          toast({ tone: "fail", title: t("health.install.fail", { name: row.label }), action: { label: t("jobs.view"), run: () => openJobs(done.id) } });
        }
      });
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.code === "conflict" && !acceptConflict) {
        // The server found a conflict the list did not show yet: ask, with its words.
        dialog.show({ ...row, conflict: failure.message });
      } else {
        toast({ tone: "fail", title: t("health.install.fail", { name: row.label }), body: explain(failure, tx) });
      }
    } finally {
      setStarting(false);
    }
  }

  const surfaces = doctor.data?.surfaces ?? [];
  const tally = { pass: 0, warn: 0, fail: 0 };
  for (const surface of surfaces) tally[surface.status] += 1;
  const subject = dialog.subject;

  return (
    <>
      <PageHead
        tabs={
          <Segmented
            label={t("health.title")}
            value={tab}
            onChange={setTab}
            options={[
              { value: "components", label: t("health.components") },
              { value: "checks", label: t("health.checks") },
              { value: "commands", label: t("health.commands") },
            ]}
          />
        }
        title={t("health.title")}
        lead={t("health.lead")}
        actions={
          <Button icon={<RotateCw size={15} aria-hidden />} pending={restarting} onClick={() => void restart()}>
            {t("shell.restart")}
          </Button>
        }
      />
      <div className="stack">
        {restartHint ? (
          <Notice
            tone="info"
            title={t("health.restartHint")}
            action={
              <Button size="sm" variant="primary" pending={restarting} onClick={() => void restart()}>
                {t("shell.restart")}
              </Button>
            }
          />
        ) : null}

        <Kpis
          items={[
            { label: t("health.kpi.pass"), dot: "ok", value: tally.pass },
            { label: t("health.kpi.warn"), dot: tally.warn ? "warn" : "muted", value: tally.warn },
            { label: t("health.kpi.fail"), dot: tally.fail ? "fail" : "muted", value: tally.fail },
            { label: t("health.components"), dot: "muted", value: components.data ? `${components.data.components.filter((row) => row.installed !== null).length} / ${components.data.components.length}` : "–" },
          ]}
        />
        <section className="card card-pad" aria-labelledby="health-components" hidden={tab !== "components"}>
          <SectionTitle>
            <span id="health-components">{t("health.components")}</span>
          </SectionTitle>
          <p className="section-note">{t("health.componentsNote")}</p>
          {components.error && !components.data ? <LoadError error={components.error} onRetry={() => void components.reload()} /> : null}
          {components.loading ? <Skeleton lines={4} tall /> : null}
          {components.data ? (
            <ul className="components">
              {components.data.components.map((row) => {
                const working = busy[row.extra] || !!row.running_job;
                const current = row.installed !== null && (!row.wanted || row.installed === row.wanted);
                return (
                  <li key={row.extra} className="component">
                    <div className="component-icon" data-on={row.installed !== null || undefined} aria-hidden>
                      <PackageCheck size={17} />
                    </div>
                    <div className="component-text">
                      <strong>
                        {row.label} <code>[{row.extra}]</code>
                      </strong>
                      <span>{tx(`component.${row.extra}.purpose`, row.purpose)}</span>
                      {row.conflict ? (
                        <span className="component-conflict">
                          <TriangleAlert size={13} aria-hidden /> {t("health.conflictShort")}
                        </span>
                      ) : null}
                    </div>
                    <div className="component-state">
                      {row.installed ? <Chip tone={current ? "ok" : "warn"}>{current ? t("health.installedAt", { version: row.installed }) : t("health.outdated", { have: row.installed, want: row.wanted ?? "" })}</Chip> : <Chip>{t("health.notInstalled")}</Chip>}
                      {working ? (
                        <Button size="sm" pending onClick={() => openJobs(row.running_job ?? undefined)}>
                          {t("connect.installing")}
                        </Button>
                      ) : !current && row.available ? (
                        <Button size="sm" variant={row.installed ? "secondary" : "primary"} icon={<Download size={14} aria-hidden />} onClick={() => dialog.show(row)}>
                          {row.installed ? t("health.update") : t("connect.install")}
                        </Button>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : null}
          {components.data ? (
            <p className="section-note">
              {t("health.interpreter")} <span className="path">{shortPath(components.data.python)}</span>
            </p>
          ) : null}
        </section>

        <section className="card card-pad" aria-labelledby="health-checks" hidden={tab !== "checks"}>
          <SectionTitle
            aside={
              doctor.data ? (
                <span className="tally">
                  <Chip tone="ok">{t("health.tally.pass", { count: tally.pass })}</Chip>
                  {tally.warn ? <Chip tone="warn">{t("health.tally.warn", { count: tally.warn })}</Chip> : null}
                  {tally.fail ? <Chip tone="fail">{t("health.tally.fail", { count: tally.fail })}</Chip> : null}
                </span>
              ) : null
            }
          >
            <span id="health-checks">{t("health.checks")}</span>
          </SectionTitle>
          {doctor.error && !doctor.data ? <LoadError error={doctor.error} onRetry={() => void doctor.reload()} /> : null}
          {doctor.loading ? <Skeleton lines={6} /> : null}
          {doctor.data ? (
            <motion.ul className="surfaces" initial="hidden" animate="show" variants={{ show: { transition: { staggerChildren: 0.03 } } }}>
              {surfaces.map((surface) => (
                <Surface key={surface.name} surface={surface} />
              ))}
            </motion.ul>
          ) : null}
        </section>

        {doctor.data ? (
          <section className="card card-pad" aria-labelledby="health-commands" hidden={tab !== "commands"}>
            <SectionTitle>
              <span id="health-commands">{t("health.commands")}</span>
            </SectionTitle>
            <ul className="commands">
              {Object.entries(doctor.data.commands).map(([name, command]) => (
                <li key={name}>
                  <span>{tx(`health.command.${name}`, humanize(name))}</span>
                  <Command text={command} />
                </li>
              ))}
            </ul>
            <a className="more-link" href="./THIRD_PARTY_NOTICES.txt" target="_blank" rel="noreferrer">
              {t("health.notices")}
            </a>
          </section>
        ) : null}
      </div>

      {subject ? (
        <Modal
          open={dialog.open}
          onOpenChange={(next) => (next ? undefined : dialog.hide())}
          title={t("health.install.title", { name: subject.label })}
          description={t("health.install.lead")}
          footer={
            <>
              <Button variant="ghost" onClick={dialog.hide}>
                {t("common.cancel")}
              </Button>
              <Button variant={subject.conflict ? "danger" : "primary"} pending={starting} icon={<Download size={15} aria-hidden />} onClick={() => void install(subject, !!subject.conflict)}>
                {subject.conflict ? t("health.install.anyway") : t("connect.install.run")}
              </Button>
            </>
          }
        >
          {subject.conflict ? (
            <Notice tone="warn" icon={<TriangleAlert size={16} aria-hidden />} title={t("health.conflict.title")}>
              {tx(`component.${subject.extra}.conflict`, subject.conflict)}
            </Notice>
          ) : null}
          {subject.command ? <Command text={subject.command} wrap /> : null}
          <ul className="pkg-list">
            {subject.packages.map((name) => (
              <li key={name}>
                <code>{name}</code>
              </li>
            ))}
          </ul>
        </Modal>
      ) : null}
    </>
  );
}
