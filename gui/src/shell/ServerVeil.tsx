import { Power, WifiOff } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";

import { useT } from "../i18n";
import { serverPhase } from "../core/server";
import { useStore } from "../core/store";
import { Command } from "../ui/bits";
import { Button } from "../ui/Button";
import { Spinner } from "../ui/Mark";
import { SETTLE } from "../core/motion";

/** Covers the page while the console process is away, and says why. */
export function ServerVeil() {
  const phase = useStore(serverPhase);
  const { t } = useT();
  return (
    <AnimatePresence>
      {phase !== "online" ? (
        <motion.div
          className="veil"
          role="alertdialog"
          aria-modal="true"
          aria-labelledby="veil-title"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
        >
          <motion.div
            className="veil-card"
            initial={{ y: 16, scale: 0.97 }}
            animate={{ y: 0, scale: 1 }}
            transition={SETTLE}
          >
            <div className="veil-icon">
              {phase === "restarting" ? <Spinner size={28} /> : phase === "stopped" ? <Power size={24} aria-hidden /> : <WifiOff size={24} aria-hidden />}
            </div>
            <h2 id="veil-title">{t(`veil.${phase}.title`)}</h2>
            <p>{t(`veil.${phase}.body`)}</p>
            {phase !== "restarting" ? <Command text="ouroboros gui" /> : null}
            {phase === "offline" ? (
              <Button variant="primary" onClick={() => window.location.reload()}>
                {t("veil.reload")}
              </Button>
            ) : null}
          </motion.div>
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}
