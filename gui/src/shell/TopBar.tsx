import { Menu, RefreshCw, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";

import { useT } from "../i18n";
import { backendName } from "../core/format";
import { jobs, openJobs } from "../core/jobs";
import { invalidate, useResource } from "../core/resource";
import { href, useRoute } from "../core/router";
import { useStore } from "../core/store";
import type { Overview } from "../core/types";
import { IconButton } from "../ui/Button";
import { Spinner } from "../ui/Mark";
import { Brand, Drawer } from "./Sidebar";

/** The thin bar above every page: what the console is doing, and how fresh the data is. */
export function TopBar() {
  const { t, locale } = useT();
  const route = useRoute();
  const overview = useResource<Overview>("/api/overview", { interval: 12_000 });
  const jobState = useStore(jobs);
  const [drawer, setDrawer] = useState(false);
  useEffect(() => setDrawer(false), [route.page, route.rest]);
  const data = overview.data;
  const running = Object.values(jobState.jobs).filter((job) => job.status === "running").length;
  const issues = data ? data.doctor.fail || data.doctor.warn : 0;
  const clock = overview.updatedAt ? new Date(overview.updatedAt).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" }) : "";

  return (
    <header className="topbar">
      <button type="button" className="icon-btn icon-btn-sm topbar-menu" aria-label={t("shell.menu")} onClick={() => setDrawer(true)}>
        <Menu size={17} aria-hidden />
      </button>
      <span className="topbar-brand">
        <Brand />
      </span>
      <div className="topbar-chips">
        {data ? (
          <a className="status-chip" href={href("models")}>
            <span className="dot dot-ok" aria-hidden />
            <span className="status-chip-label">{t("home.stat.agent")}</span>
            <strong>{backendName(data.default_runtime)}</strong>
          </a>
        ) : null}
        {data && issues ? (
          <a className={`status-chip status-chip-${data.doctor.fail ? "fail" : "warn"}`} href={href("health")}>
            <TriangleAlert size={12} aria-hidden />
            <strong>{data.doctor.fail ? t("home.stat.health.fail", { count: data.doctor.fail }) : t("home.stat.health.warn", { count: data.doctor.warn })}</strong>
          </a>
        ) : null}
        {running ? (
          <button type="button" className="status-chip status-chip-info" onClick={() => openJobs()}>
            <Spinner size={12} />
            <strong>{t("shell.activityRunning", { count: running })}</strong>
          </button>
        ) : null}
      </div>
      <span className="topbar-clock" aria-live="off">
        {clock ? t("shell.updated", { time: clock }) : ""}
      </span>
      <IconButton
        label={t("common.refresh")}
        className="icon-btn-sm"
        onClick={() => {
          invalidate("/api/");
          void overview.reload();
        }}
      >
        <RefreshCw size={14} aria-hidden className={overview.refreshing ? "spin-once" : undefined} />
      </IconButton>
      <Drawer open={drawer} onOpenChange={setDrawer} />
    </header>
  );
}
