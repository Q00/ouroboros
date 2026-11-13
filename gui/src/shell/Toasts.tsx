import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";

import { useT } from "../i18n";
import { useStore } from "../core/store";
import { dismissToast, toasts } from "../core/toast";
import { SETTLE } from "../core/motion";

const ICONS = {
  ok: CircleCheck,
  fail: CircleAlert,
  warn: TriangleAlert,
  info: Info,
};

export function Toasts() {
  const list = useStore(toasts);
  const { t } = useT();
  return (
    <div className="toasts" role="region" aria-label={t("shell.notifications")}>
      <AnimatePresence initial={false}>
        {list.map((item) => {
          const Icon = ICONS[item.tone];
          return (
            <motion.div
              key={item.id}
              layout
              className={`toast toast-${item.tone}`}
              role={item.tone === "fail" ? "alert" : "status"}
              initial={{ opacity: 0, y: -18, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, x: 32, transition: { duration: 0.16 } }}
              transition={SETTLE}
            >
              <Icon size={17} aria-hidden className="toast-icon" />
              <div className="toast-text">
                <strong>{item.title}</strong>
                {item.body ? <span>{item.body}</span> : null}
              </div>
              {item.action ? (
                <button
                  type="button"
                  className="toast-action"
                  onClick={() => {
                    item.action?.run();
                    dismissToast(item.id);
                  }}
                >
                  {item.action.label}
                </button>
              ) : null}
              <button type="button" className="icon-btn icon-btn-sm" aria-label={t("common.dismiss")} onClick={() => dismissToast(item.id)}>
                <X size={14} aria-hidden />
              </button>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
