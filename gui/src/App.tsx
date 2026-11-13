import * as Tooltip from "@radix-ui/react-tooltip";
import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { useEffect } from "react";

import { useT } from "./i18n";
import { adoptRunningJobs } from "./core/jobs";
import type { PageId } from "./core/router";
import { useRoute } from "./core/router";
import { watchServer } from "./core/server";
import { useStore } from "./core/store";
import { sidebarCollapsed } from "./core/ui";
import { ChatPage } from "./pages/Chat";
import { ConnectPage } from "./pages/Connect";
import { HealthPage } from "./pages/Health";
import { HomePage } from "./pages/Home";
import { McpPage } from "./pages/Mcp";
import { ModelsPage } from "./pages/Models";
import { ProvidersPage } from "./pages/Providers";
import { RunsPage } from "./pages/Runs";
import { JobSheet } from "./shell/JobSheet";
import { Palette } from "./shell/Palette";
import { Sidebar, useGlobalKeys } from "./shell/Sidebar";
import { ServerVeil } from "./shell/ServerVeil";
import { Toasts } from "./shell/Toasts";
import { TopBar } from "./shell/TopBar";

const VIEWS: Record<PageId, (props: { rest: string }) => React.ReactNode> = {
  home: HomePage,
  chat: ChatPage,
  runs: RunsPage,
  connect: ConnectPage,
  models: ModelsPage,
  providers: ProvidersPage,
  mcp: McpPage,
  health: HealthPage,
};

export function App() {
  const route = useRoute();
  const { t } = useT();
  const View = VIEWS[route.page];

  const collapsed = useStore(sidebarCollapsed);
  useGlobalKeys();
  useEffect(() => watchServer(), []);
  useEffect(() => {
    void adoptRunningJobs();
  }, []);
  useEffect(() => {
    document.title = `${t(`nav.${route.page}`)} · Ouroboros`;
  }, [route.page, t]);

  return (
    <MotionConfig reducedMotion="user">
      <Tooltip.Provider delayDuration={380} skipDelayDuration={200}>
        <a className="skip-link" href="#main">
          {t("shell.skip")}
        </a>
        <div className="app" data-collapsed={collapsed || undefined}>
          <Sidebar />
          <div className="frame">
            <TopBar />
            <main id="main" className="main" tabIndex={-1}>
              <AnimatePresence mode="wait" initial={false}>
                <motion.div
                  key={route.page}
                  className={`page page-${route.page}`}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0, transition: { duration: 0.24, ease: [0.22, 1, 0.36, 1] } }}
                  exit={{ opacity: 0, transition: { duration: 0.09, ease: "easeIn" } }}
                >
                  <View rest={route.rest} />
                </motion.div>
              </AnimatePresence>
            </main>
          </div>
        </div>
        <Palette />
        <JobSheet />
        <Toasts />
        <ServerVeil />
      </Tooltip.Provider>
    </MotionConfig>
  );
}
