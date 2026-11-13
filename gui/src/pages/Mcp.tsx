import { CircleAlert, LogIn, RotateCw } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";

import { useT } from "../i18n";
import { explain, useAction } from "../core/action";
import { post } from "../core/api";
import { shortPath, timeAgo } from "../core/format";
import { invalidate, useResource } from "../core/resource";
import { expectHandoff } from "../core/server";
import { toast } from "../core/toast";
import type { DaemonView, McpHost, ServiceStatus } from "../core/types";
import { PageHead } from "../shell/PageHead";
import type { Tone } from "../ui/bits";
import { Chip, Command, CopyButton, Dot, Kpis, LoadError, LogView, Monogram, Notice, SectionTitle, Skeleton } from "../ui/bits";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { Toggle } from "../ui/Toggle";

const HOST_TONE: Record<McpHost["state"], Tone> = {
  shared: "ok",
  stdio: "muted",
  other_url: "warn",
  absent: "muted",
};

type Phase = "off" | "starting" | "listening" | "error";

function phaseOf(view: DaemonView, wanted: boolean | null): Phase {
  const { mcp } = view;
  if (mcp.listening) return wanted === false ? "off" : "listening";
  if (mcp.error && (mcp.enabled || wanted)) return "error";
  if (mcp.running || mcp.enabled || wanted) return "starting";
  return "off";
}

export function McpPage(_props: { rest: string }) {
  const { t, tx, locale } = useT();
  const daemon = useResource<DaemonView>("/api/daemon", { interval: 3000 });
  const view = daemon.data;
  // What the user just asked for, shown until the server agrees.
  const [wanted, setWanted] = useState<boolean | null>(null);
  const [hostPending, setHostPending] = useState<Record<string, boolean>>({});
  const [showLog, setShowLog] = useState(false);
  const [serviceDialog, setServiceDialog] = useState<"install" | "uninstall" | null>(null);

  const [switchServer] = useAction(
    async (on: boolean) => {
      setWanted(on);
      try {
        daemon.mutate(await post<DaemonView>("/api/daemon/mcp", { action: on ? "start" : "stop" }));
        toast({ tone: "ok", title: on ? t("mcp.started") : t("mcp.stopped") });
      } finally {
        setWanted(null);
      }
    },
    { tx, failTitle: t("mcp.switchFailed") },
  );

  const [restart, restarting] = useAction(
    async () => {
      daemon.mutate(await post<DaemonView>("/api/daemon/mcp", { action: "restart" }));
      toast({ tone: "ok", title: t("mcp.restarted") });
    },
    { tx, failTitle: t("mcp.switchFailed") },
  );

  async function setHost(host: McpHost, useShared: boolean, quiet = false) {
    setHostPending((current) => ({ ...current, [host.id]: useShared }));
    try {
      daemon.mutate(await post<DaemonView>(`/api/daemon/clients/${host.id}`, { use_shared: useShared }));
      invalidate("/api/clients", "/api/overview");
      if (!quiet) {
        toast({
          tone: "ok",
          title: useShared ? t("mcp.host.on", { tool: host.label }) : t("mcp.host.off", { tool: host.label }),
          body: t("mcp.host.restartTool"),
          action: { label: t("models.undo"), run: () => void setHost(host, !useShared, true) },
        });
      }
    } catch (failure) {
      toast({ tone: "fail", title: t("mcp.host.failed", { tool: host.label }), body: explain(failure, tx) });
    } finally {
      setHostPending((current) => {
        const next = { ...current };
        delete next[host.id];
        return next;
      });
    }
  }

  const [changeService, servicePending] = useAction(
    async (action: "install" | "uninstall") => {
      const result = await post<{ service: ServiceStatus; handoff: boolean }>("/api/daemon/service", { action });
      setServiceDialog(null);
      if (result.handoff) {
        await expectHandoff();
        return;
      }
      await daemon.reload();
      toast({ tone: "ok", title: action === "install" ? t("mcp.service.installed") : t("mcp.service.removed") });
    },
    { tx, failTitle: t("mcp.service.failed") },
  );

  if (daemon.error && !view) {
    return (
      <>
        <PageHead title={t("mcp.title")} lead={t("mcp.lead")} />
        <LoadError error={daemon.error} onRetry={() => void daemon.reload()} />
      </>
    );
  }
  if (!view) {
    return (
      <>
        <PageHead title={t("mcp.title")} lead={t("mcp.lead")} />
        <div className="card card-pad">
          <Skeleton lines={6} />
        </div>
      </>
    );
  }

  const { mcp, service } = view;
  const phase = phaseOf(view, wanted);
  const on = wanted ?? mcp.enabled;
  const phaseTone: Tone = phase === "listening" ? "ok" : phase === "error" ? "fail" : phase === "starting" ? "info" : "muted";

  return (
    <>
      <PageHead
        title={t("mcp.title")}
        lead={t("mcp.lead")}
        dot={phaseTone}
        dotPulse={phase === "starting" || phase === "listening"}
        actions={
          <>
            <Button icon={<RotateCw size={14} aria-hidden />} pending={restarting} disabled={!mcp.enabled} onClick={() => void restart()}>
              {t("mcp.restart")}
            </Button>
            <label className="switch-label">
              <span>{t("mcp.toggle")}</span>
              <Toggle label={t("mcp.toggle")} checked={on} pending={wanted !== null} onChange={(checked) => void switchServer(checked)} />
            </label>
          </>
        }
      />
      <div className="stack">
        <Kpis
          items={[
            {
              label: t("mcp.server"),
              value: (
                <AnimatePresence mode="wait" initial={false}>
                  <motion.span key={phase} className="kpi-swap" initial={{ opacity: 0, y: 5 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -5 }} transition={{ duration: 0.16 }}>
                    {t(`mcp.phase.${phase}`)}
                  </motion.span>
                </AnimatePresence>
              ),
              tone: phase === "listening" ? "ok" : phase === "error" ? "fail" : undefined,
            },
            { label: t("mcp.fact.started"), value: mcp.started_at ? timeAgo(mcp.started_at, locale) : "–", detail: mcp.pid ? `${t("mcp.fact.pid")} ${mcp.pid}` : undefined },
            { label: t("mcp.fact.restarts"), value: mcp.restarts },
            { label: t("mcp.fact.stdio"), value: view.stdio_servers },
          ]}
        />
        <section className="card card-pad" aria-label={t("mcp.server")}>
          <div className="mcp-url">
            <code>{mcp.url}</code>
            <CopyButton text={mcp.url} label={t("mcp.copyUrl")} />
          </div>
          {mcp.error && on ? (
            <Notice tone="fail" icon={<CircleAlert size={16} aria-hidden />}>
              {tx(mcp.error.startsWith("No MCP 2 launcher") ? "mcp.error.noLauncher" : mcp.error.startsWith("Port ") ? "mcp.error.port" : "mcp.error.other", mcp.error, { port: mcp.port })}
            </Notice>
          ) : null}
          <p className="section-note">{t("mcp.stdioNote", { count: view.stdio_servers })}</p>
          {mcp.command ? <Command text={mcp.command} /> : null}
          {mcp.log.length > 0 ? (
            <>
              <div>
                <Button variant="ghost" size="sm" aria-expanded={showLog} onClick={() => setShowLog((value) => !value)}>
                  {showLog ? t("mcp.hideLog") : t("mcp.showLog")}
                </Button>
              </div>
              <AnimatePresence initial={false}>
                {showLog ? (
                  <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.24 }} style={{ overflow: "hidden" }}>
                    <LogView lines={mcp.log} live={mcp.running} maxHeight={240} />
                  </motion.div>
                ) : null}
              </AnimatePresence>
            </>
          ) : null}
        </section>

        <section aria-labelledby="mcp-hosts-title">
          <SectionTitle>
            <span id="mcp-hosts-title">{t("mcp.hosts")}</span>
          </SectionTitle>
          <p className="section-note">{t("mcp.hostsNote")}</p>
          {!mcp.enabled && wanted !== true ? <Notice tone="info">{t("mcp.hostsNeedServer")}</Notice> : null}
          <ul className="host-grid">
            {view.clients.map((host) => {
              const pending = host.id in hostPending;
              const shared = pending ? hostPending[host.id] : host.state === "shared";
              return (
                <li key={host.id} className="card host-card">
                  <div className="host-card-head">
                    <Monogram label={host.label} size={26} dim={host.state === "absent"} />
                    <strong>{host.label}</strong>
                    <Chip tone={HOST_TONE[host.state]} icon={<Dot tone={HOST_TONE[host.state]} />}>
                      {t(`mcp.host.state.${host.state}`)}
                    </Chip>
                  </div>
                  <span className="path" title={host.path}>
                    {shortPath(host.path)}
                  </span>
                  {host.error ? <span className="tone-fail">{host.error}</span> : null}
                  <div className="host-card-foot">
                    <Toggle
                      label={t("mcp.host.toggle", { tool: host.label })}
                      checked={shared}
                      pending={pending}
                      disabled={pending || (!shared && !mcp.enabled) || !!host.error}
                      onChange={(checked) => void setHost(host, checked)}
                    />
                  </div>
                </li>
              );
            })}
          </ul>
          <p className="section-note">{t("mcp.claudeNote")}</p>
        </section>

        <section className="card card-pad" aria-labelledby="mcp-service-title">
          <SectionTitle
            aside={
              service.supported ? (
                <Chip tone={service.loaded ? "ok" : service.installed ? "warn" : "muted"}>
                  {service.loaded ? t("mcp.service.on") : service.installed ? t("mcp.service.installedIdle") : t("mcp.service.off")}
                </Chip>
              ) : null
            }
          >
            <span id="mcp-service-title">{t("mcp.service.title")}</span>
          </SectionTitle>
          <p className="section-note">{service.supported ? t("mcp.service.body", { manager: service.manager ?? "" }) : t("mcp.service.unsupported")}</p>
          {service.supported ? (
            <div className="service-row">
              <span className="path" title={service.unit_path ?? undefined}>
                {shortPath(service.unit_path)}
              </span>
              {service.installed ? (
                <Button variant="danger" size="sm" onClick={() => setServiceDialog("uninstall")}>
                  {t("mcp.service.remove")}
                </Button>
              ) : (
                <Button size="sm" icon={<LogIn size={14} aria-hidden />} onClick={() => setServiceDialog("install")}>
                  {t("mcp.service.install")}
                </Button>
              )}
            </div>
          ) : null}
        </section>
      </div>

      <Modal
        open={serviceDialog !== null}
        onOpenChange={(next) => (next ? undefined : setServiceDialog(null))}
        title={serviceDialog === "uninstall" ? t("mcp.service.remove.title") : t("mcp.service.install.title")}
        description={serviceDialog === "uninstall" ? t("mcp.service.remove.body") : t("mcp.service.install.body")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setServiceDialog(null)}>
              {t("common.cancel")}
            </Button>
            <Button variant={serviceDialog === "uninstall" ? "danger" : "primary"} pending={servicePending} onClick={() => serviceDialog && void changeService(serviceDialog)}>
              {serviceDialog === "uninstall" ? t("mcp.service.remove") : t("mcp.service.install")}
            </Button>
          </>
        }
      >
        <Command text={serviceDialog === "uninstall" ? "ouroboros service uninstall" : "ouroboros service install"} />
        {service.unit_path ? <p className="path">{service.unit_path}</p> : null}
      </Modal>
    </>
  );
}
