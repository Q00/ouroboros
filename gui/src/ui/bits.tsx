import { Check, CircleAlert, Copy, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";

import { useT } from "../i18n";
import type { ApiFailure } from "../core/api";
import { monogram } from "../core/format";
import { Button } from "./Button";
import { Tip } from "./Tip";

export type Tone = "ok" | "warn" | "fail" | "info" | "muted";

export function Chip({ tone = "muted", children, icon, pulse }: { tone?: Tone; children: ReactNode; icon?: ReactNode; pulse?: boolean }) {
  return (
    <span className={`chip chip-${tone}`}>
      {pulse ? <span className="dot dot-pulse" aria-hidden /> : icon}
      {children}
    </span>
  );
}

export function Dot({ tone = "muted", pulse }: { tone?: Tone; pulse?: boolean }) {
  return <span className={`dot dot-${tone} ${pulse ? "dot-pulse" : ""}`} aria-hidden />;
}

/** Letters standing in for a vendor mark; the console never ships vendor logos. */
export function Monogram({ label, size = 32, dim }: { id?: string; label: string; size?: number; dim?: boolean }) {
  return (
    <span className="monogram" data-dim={dim || undefined} style={{ width: size, height: size, fontSize: size * 0.38 }} aria-hidden>
      {monogram(label)}
    </span>
  );
}

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

export function CopyButton({ text, label }: { text: string; label?: string }) {
  const { t } = useT();
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const name = copied ? t("common.copied") : (label ?? t("common.copy"));
  return (
    <Tip text={name}>
      <button
        type="button"
        className="icon-btn icon-btn-sm"
        aria-label={name}
        data-done={copied || undefined}
        onClick={async () => {
          if (await copyText(text)) {
            setCopied(true);
            window.clearTimeout(timer.current);
            timer.current = window.setTimeout(() => setCopied(false), 1400);
          }
        }}
      >
        <span className="swap" data-on={copied || undefined}>
          <Copy size={14} aria-hidden />
          <Check size={14} aria-hidden />
        </span>
      </button>
    </Tip>
  );
}

/** A shell command the user may want to copy. */
export function Command({ text, wrap }: { text: string; wrap?: boolean }) {
  return (
    <div className={`command ${wrap ? "command-wrap" : ""}`}>
      <span className="command-prompt" aria-hidden>
        $
      </span>
      <code>{text}</code>
      <CopyButton text={text} />
    </div>
  );
}

export function Skeleton({ lines = 3, tall }: { lines?: number; tall?: boolean }) {
  return (
    <div className="skeleton" aria-hidden>
      {Array.from({ length: lines }, (_, index) => (
        <span key={index} className={`skeleton-line ${tall ? "skeleton-tall" : ""}`} style={{ width: `${92 - ((index * 17) % 40)}%` }} />
      ))}
    </div>
  );
}

export function Empty({ icon, title, body, action }: { icon?: ReactNode; title: string; body?: string; action?: ReactNode }) {
  return (
    <div className="empty">
      {icon ? <div className="empty-icon">{icon}</div> : null}
      <p className="empty-title">{title}</p>
      {body ? <p className="empty-body">{body}</p> : null}
      {action}
    </div>
  );
}

/** A load failure with the server's own words and a way to try again. */
export function LoadError({ error, onRetry }: { error: ApiFailure; onRetry?: () => void }) {
  const { t, tx } = useT();
  return (
    <div className="notice notice-fail" role="alert">
      <CircleAlert size={16} aria-hidden />
      <div className="notice-text">
        <strong>{t("common.loadFailed")}</strong>
        <span>{tx(`error.${error.code}`, error.message)}</span>
      </div>
      {onRetry ? (
        <Button size="sm" icon={<RefreshCw size={13} aria-hidden />} onClick={onRetry}>
          {t("common.retry")}
        </Button>
      ) : null}
    </div>
  );
}

export function Notice({ tone = "info", icon, title, children, action }: { tone?: "info" | "warn" | "fail" | "ok"; icon?: ReactNode; title?: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className={`notice notice-${tone}`}>
      {icon}
      <div className="notice-text">
        {title ? <strong>{title}</strong> : null}
        {children ? <span>{children}</span> : null}
      </div>
      {action}
    </div>
  );
}

export function Field({ label, htmlFor, hint, children, aside }: { label: string; htmlFor?: string; hint?: ReactNode; children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="field">
      <div className="field-head">
        <label className="field-label" htmlFor={htmlFor}>
          {label}
        </label>
        {aside}
      </div>
      {children}
      {hint ? <p className="field-hint">{hint}</p> : null}
    </div>
  );
}

/** Process output: follows the newest line unless the reader scrolled up. */
export function LogView({ lines, live, emptyText, maxHeight = 320 }: { lines: string[]; live?: boolean; emptyText?: string; maxHeight?: number }) {
  const box = useRef<HTMLPreElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const element = box.current;
    if (element && pinned.current) element.scrollTop = element.scrollHeight;
  }, [lines.length]);
  return (
    <pre
      ref={box}
      className="log"
      style={{ maxHeight }}
      tabIndex={0}
      aria-live={live ? "polite" : undefined}
      onScroll={(event) => {
        const element = event.currentTarget;
        pinned.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
      }}
    >
      {lines.length === 0 ? <span className="log-empty">{emptyText ?? ""}</span> : null}
      {lines.map((line, index) => (
        <span key={index} className={line.startsWith("$ ") ? "log-line log-cmd" : "log-line"}>
          {line || " "}
          {"\n"}
        </span>
      ))}
      {live ? <span className="log-caret" aria-hidden /> : null}
    </pre>
  );
}

export function SectionTitle({ children, aside }: { children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="section-title">
      <h2>{children}</h2>
      {aside}
    </div>
  );
}

export interface KpiItem {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: Tone;
  /** A status dot before the label. */
  dot?: Tone;
  href?: string;
}

/** A row of headline numbers. `joined` draws them as one strip with dividers. */
export function Kpis({ items, joined }: { items: KpiItem[]; joined?: boolean }) {
  return (
    <dl className={`kpis ${joined ? "kpis-joined" : ""}`} style={{ ["--kpi-count" as string]: items.length }}>
      {items.map((item) => {
        const body = (
          <>
            <dt>
              {item.dot ? <Dot tone={item.dot} /> : null}
              {item.label}
            </dt>
            <dd className={item.tone ? `tone-${item.tone}` : undefined}>{item.value}</dd>
            {item.detail ? <dd className="kpi-detail">{item.detail}</dd> : null}
          </>
        );
        return item.href ? (
          <a key={item.label} className="kpi kpi-link" href={item.href}>
            {body}
          </a>
        ) : (
          <div key={item.label} className="kpi">
            {body}
          </div>
        );
      })}
    </dl>
  );
}
