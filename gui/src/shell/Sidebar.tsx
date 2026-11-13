import * as Dialog from "@radix-ui/react-dialog";
import {
  Activity,
  Globe,
  HeartPulse,
  House,
  KeyRound,
  MessagesSquare,
  Monitor,
  Moon,
  Network,
  PanelLeft,
  Plug,
  Power,
  RotateCw,
  Search,
  SlidersHorizontal,
  Sun,
  Terminal,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { motion } from "motion/react";
import { useEffect, useState } from "react";

import type { Lang } from "../i18n";
import { LANGUAGES, setLang, useT } from "../i18n";
import { explain } from "../core/action";
import { uptime } from "../core/format";
import { jobs, openJobs } from "../core/jobs";
import { useResource } from "../core/resource";
import type { PageId } from "../core/router";
import { href, navigate, useRoute } from "../core/router";
import { restartConsole, serverUptime, stopConsole } from "../core/server";
import { useStore } from "../core/store";
import type { ThemeMode } from "../core/theme";
import { setThemeMode, useThemeMode } from "../core/theme";
import { toast } from "../core/toast";
import type { Overview } from "../core/types";
import { paletteOpen, sidebarCollapsed, toggleSidebar } from "../core/ui";
import { Button, IconButton } from "../ui/Button";
import { Mark, Spinner } from "../ui/Mark";
import { Modal } from "../ui/Modal";
import { Picker } from "../ui/Picker";
import { SNAP } from "../core/motion";

export const NAV: { id: "work" | "setup"; items: { page: PageId; icon: LucideIcon }[] }[] = [
  {
    id: "work",
    items: [
      { page: "home", icon: House },
      { page: "chat", icon: MessagesSquare },
      { page: "runs", icon: Activity },
    ],
  },
  {
    id: "setup",
    items: [
      { page: "connect", icon: Plug },
      { page: "models", icon: SlidersHorizontal },
      { page: "providers", icon: KeyRound },
      { page: "mcp", icon: Network },
      { page: "health", icon: HeartPulse },
    ],
  },
];

const ORDER: PageId[] = NAV.flatMap((group) => group.items.map((item) => item.page));
const MAC = /Mac|iPhone|iPad/.test(navigator.platform);
const ALT = MAC ? "⌥" : "Alt+";
export const MOD = MAC ? "⌘" : "Ctrl+";

/** ⌘K opens search; ⌥1 … ⌥8 jump between pages. */
export function useGlobalKeys(): void {
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        paletteOpen.set((open) => !open);
        return;
      }
      if (event.altKey && !event.metaKey && !event.ctrlKey && /^Digit[1-8]$/.test(event.code)) {
        event.preventDefault();
        navigate(ORDER[Number(event.code.slice(5)) - 1]);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

function useBadges(): Partial<Record<PageId, { count: number; tone: "info" | "warn" | "fail" }>> {
  const { data } = useResource<Overview>("/api/overview", { interval: 12_000 });
  if (!data) return {};
  const badges: Partial<Record<PageId, { count: number; tone: "info" | "warn" | "fail" }>> = {};
  const active = data.runs.filter((run) => run.status === "running").length;
  if (active) badges.runs = { count: active, tone: "info" };
  if (data.clients.attention) badges.connect = { count: data.clients.attention, tone: "warn" };
  if (data.doctor.fail) badges.health = { count: data.doctor.fail, tone: "fail" };
  return badges;
}

function Nav({ layoutKey, onNavigate }: { layoutKey: string; onNavigate?: () => void }) {
  const route = useRoute();
  const { t } = useT();
  const badges = useBadges();
  return (
    <nav className="nav" aria-label={t("shell.nav")}>
      {NAV.map((group, groupIndex) => (
        <div key={group.id} className="nav-group">
          <div className="nav-heading">
            <span id={`nav-${layoutKey}-${group.id}`}>{t(`nav.group.${group.id}`)}</span>
            {groupIndex === 0 ? (
              <button type="button" className="side-find" onClick={() => paletteOpen.set(true)} aria-label={t("shell.search")}>
                <Search size={13} aria-hidden />
                <kbd>{MOD}K</kbd>
              </button>
            ) : null}
          </div>
          <ul aria-labelledby={`nav-${layoutKey}-${group.id}`}>
            {group.items.map(({ page, icon: Icon }) => {
              const active = route.page === page;
              const badge = badges[page];
              return (
                <li key={page}>
                  <a className="nav-link" href={href(page)} aria-current={active ? "page" : undefined} title={t(`nav.${page}`)} onClick={onNavigate}>
                    {active ? <motion.span layoutId={`nav-pill-${layoutKey}`} className="nav-pill" transition={SNAP} /> : null}
                    <Icon size={16} aria-hidden className="nav-icon" />
                    <span className="nav-label">{t(`nav.${page}`)}</span>
                    {badge ? (
                      <span className={`nav-badge nav-badge-${badge.tone}`} aria-label={t(`nav.badge.${badge.tone}`, { count: badge.count })}>
                        {badge.count}
                      </span>
                    ) : (
                      <kbd className="nav-key" aria-hidden>
                        {ALT}
                        {ORDER.indexOf(page) + 1}
                      </kbd>
                    )}
                  </a>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

const THEME_NEXT: Record<ThemeMode, ThemeMode> = { system: "light", light: "dark", dark: "system" };
const THEME_ICON: Record<ThemeMode, LucideIcon> = { system: Monitor, light: Sun, dark: Moon };

function Footer() {
  const { t, tx, lang } = useT();
  const theme = useThemeMode();
  const up = useStore(serverUptime);
  const jobState = useStore(jobs);
  const { data } = useResource<Overview>("/api/overview");
  const [confirm, setConfirm] = useState<"restart" | "stop" | null>(null);
  const [pending, setPending] = useState(false);
  const running = Object.values(jobState.jobs).filter((job) => job.status === "running").length;
  const ThemeIcon = THEME_ICON[theme];

  async function go() {
    const action = confirm;
    if (!action) return;
    setPending(true);
    try {
      if (action === "restart") {
        setConfirm(null);
        await restartConsole();
      } else {
        await stopConsole();
        setConfirm(null);
      }
    } catch (failure) {
      toast({ tone: "fail", title: t("shell.powerFailed"), body: explain(failure, tx) });
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="side-foot">
      <div className="side-row">
        <span className="dot dot-ok dot-pulse" aria-hidden />
        <span className="side-row-label">
          <strong>{t("shell.console")}</strong>
          <span>{up ? uptime(up) : ""}</span>
        </span>
        <IconButton label={t("shell.restart")} className="icon-btn-sm" onClick={() => setConfirm("restart")}>
          <RotateCw size={14} aria-hidden />
        </IconButton>
        <IconButton label={t("shell.stop")} className="icon-btn-sm" onClick={() => setConfirm("stop")}>
          <Power size={14} aria-hidden />
        </IconButton>
      </div>
      <div className="side-row">
        <Globe size={15} aria-hidden className="side-row-icon" />
        <span className="side-row-label side-lang">
          <Picker size="sm" label={t("shell.language")} value={lang} onChange={(value) => setLang(value as Lang)} options={LANGUAGES.map((item) => ({ value: item.id, label: item.name }))} />
        </span>
        <IconButton label={`${t("shell.theme")}: ${t(`shell.theme.${theme}`)}`} className="icon-btn-sm" onClick={() => setThemeMode(THEME_NEXT[theme])}>
          <ThemeIcon size={14} aria-hidden />
        </IconButton>
      </div>
      <div className="side-row">
        <button type="button" className="side-activity" onClick={() => openJobs()} title={t("shell.activity")}>
          {running ? <Spinner size={14} /> : <Terminal size={15} aria-hidden />}
          <span className="side-row-label">{running ? t("shell.activityRunning", { count: running }) : t("shell.activity")}</span>
        </button>
        <span className="side-version" title={data?.executable}>
          {data ? `v${data.version}` : ""}
        </span>
      </div>
      <Modal
        open={confirm !== null}
        onOpenChange={(open) => (open ? undefined : setConfirm(null))}
        title={confirm === "stop" ? t("shell.stop.title") : t("shell.restart.title")}
        description={confirm === "stop" ? t("shell.stop.body") : t("shell.restart.body")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirm(null)}>
              {t("common.cancel")}
            </Button>
            <Button variant={confirm === "stop" ? "danger" : "primary"} pending={pending} onClick={() => void go()}>
              {confirm === "stop" ? t("shell.stop") : t("shell.restart")}
            </Button>
          </>
        }
      />
    </div>
  );
}

export function Brand() {
  return (
    <a className="brand" href={href("home")} aria-label="Ouroboros">
      <Mark size={22} className="brand-mark" />
      <span className="brand-word" aria-hidden>
        ouroboros
      </span>
    </a>
  );
}

/** The side rail on wide windows: full labels, or icons only when collapsed. */
export function Sidebar() {
  const { t } = useT();
  const collapsed = useStore(sidebarCollapsed);
  return (
    <aside className="side" data-collapsed={collapsed || undefined}>
      <div className="side-head">
        <Brand />
        <IconButton label={collapsed ? t("shell.expand") : t("shell.collapse")} className="icon-btn-sm side-toggle" aria-expanded={!collapsed} onClick={toggleSidebar}>
          <PanelLeft size={16} aria-hidden />
        </IconButton>
      </div>
      <Nav layoutKey="side" />
      <Footer />
    </aside>
  );
}

/** The same rail as a slide-in drawer on narrow windows. */
export function Drawer({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t } = useT();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="scrim" />
        <Dialog.Content className="side drawer">
          <Dialog.Title className="sr-only">{t("shell.nav")}</Dialog.Title>
          <Dialog.Description className="sr-only">{t("shell.nav")}</Dialog.Description>
          <div className="side-head">
            <Brand />
          </div>
          <Nav layoutKey="drawer" onNavigate={() => onOpenChange(false)} />
          <Footer />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
