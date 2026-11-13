import * as Dialog from "@radix-ui/react-dialog";
import { CornerDownLeft, Globe, MessagesSquare, Monitor, Moon, Plus, RotateCw, Search, Sun, Terminal } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { LANGUAGES, setLang, useT } from "../i18n";
import { openJobs } from "../core/jobs";
import { useResource } from "../core/resource";
import { navigate } from "../core/router";
import { restartConsole } from "../core/server";
import { useStore } from "../core/store";
import { setThemeMode } from "../core/theme";
import type { ChatSessionsView } from "../core/types";
import { paletteOpen } from "../core/ui";
import { NAV } from "./Sidebar";

interface Entry {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon: LucideIcon;
  run: () => void;
}

/** ⌘K: jump to a page or conversation, or run a console action, from the keyboard. */
export function Palette() {
  const open = useStore(paletteOpen);
  const { t } = useT();
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const list = useRef<HTMLUListElement>(null);
  const chats = useResource<ChatSessionsView>("/api/chat/sessions", { enabled: open });

  const entries = useMemo<Entry[]>(() => {
    const pages: Entry[] = NAV.flatMap((group) =>
      group.items.map((item) => ({ id: `page-${item.page}`, group: t("shell.cmd.pages"), label: t(`nav.${item.page}`), icon: item.icon, run: () => navigate(item.page) })),
    );
    const actions: Entry[] = [
      { id: "new-chat", group: t("shell.cmd.actions"), label: t("chat.new"), icon: Plus, run: () => navigate("chat") },
      { id: "activity", group: t("shell.cmd.actions"), label: t("shell.activity"), icon: Terminal, run: () => openJobs() },
      { id: "theme-light", group: t("shell.cmd.actions"), label: `${t("shell.theme")}: ${t("shell.theme.light")}`, icon: Sun, run: () => setThemeMode("light") },
      { id: "theme-dark", group: t("shell.cmd.actions"), label: `${t("shell.theme")}: ${t("shell.theme.dark")}`, icon: Moon, run: () => setThemeMode("dark") },
      { id: "theme-system", group: t("shell.cmd.actions"), label: `${t("shell.theme")}: ${t("shell.theme.system")}`, icon: Monitor, run: () => setThemeMode("system") },
      ...LANGUAGES.map((item) => ({ id: `lang-${item.id}`, group: t("shell.cmd.actions"), label: `${t("shell.language")}: ${item.name}`, icon: Globe, run: () => setLang(item.id) })),
      { id: "restart", group: t("shell.cmd.actions"), label: t("shell.restart"), icon: RotateCw, run: () => void restartConsole() },
    ];
    const talks: Entry[] = (chats.data?.sessions ?? []).slice(0, 12).map((row) => ({
      id: `chat-${row.id}`,
      group: t("chat.list"),
      label: row.title,
      hint: t(`chat.mode.${row.mode}`),
      icon: MessagesSquare,
      run: () => navigate("chat", row.id),
    }));
    return [...pages, ...talks, ...actions];
  }, [t, chats.data]);

  const needle = query.trim().toLowerCase();
  const shown = needle ? entries.filter((entry) => entry.label.toLowerCase().includes(needle)) : entries;
  const active = shown[Math.min(cursor, shown.length - 1)];

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
    }
  }, [open]);
  useEffect(() => {
    list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [cursor, needle]);

  function choose(entry: Entry | undefined) {
    if (!entry) return;
    paletteOpen.set(false);
    entry.run();
  }

  let lastGroup = "";
  return (
    <Dialog.Root open={open} onOpenChange={(next) => paletteOpen.set(next)}>
      <Dialog.Portal>
        <Dialog.Overlay className="scrim" />
        <Dialog.Content className="palette" aria-describedby={undefined}>
          <Dialog.Title className="sr-only">{t("shell.search")}</Dialog.Title>
          <div className="palette-input">
            <Search size={16} aria-hidden />
            <input
              role="combobox"
              aria-expanded
              aria-controls="palette-list"
              aria-activedescendant={active ? `palette-${active.id}` : undefined}
              aria-label={t("shell.search")}
              placeholder={t("shell.searchPlaceholder")}
              value={query}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => {
                setQuery(event.target.value);
                setCursor(0);
              }}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing) return;
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setCursor((value) => (shown.length ? (Math.min(value, shown.length - 1) + 1) % shown.length : 0));
                } else if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setCursor((value) => (shown.length ? (Math.min(value, shown.length - 1) - 1 + shown.length) % shown.length : 0));
                } else if (event.key === "Enter") {
                  event.preventDefault();
                  choose(active);
                }
              }}
            />
            <kbd>Esc</kbd>
          </div>
          <ul id="palette-list" className="palette-list" role="listbox" aria-label={t("shell.search")} ref={list}>
            {shown.length === 0 ? <li className="palette-empty">{t("shell.cmd.empty")}</li> : null}
            {shown.map((entry, index) => {
              const heading = entry.group !== lastGroup ? entry.group : null;
              lastGroup = entry.group;
              const Icon = entry.icon;
              const selected = entry === active;
              return (
                <li key={entry.id} role="presentation">
                  {heading ? <p className="palette-group">{heading}</p> : null}
                  <div id={`palette-${entry.id}`} role="option" aria-selected={selected} className="palette-item" onPointerMove={() => setCursor(index)} onClick={() => choose(entry)}>
                    <Icon size={15} aria-hidden />
                    <span className="palette-label">{entry.label}</span>
                    {entry.hint ? <span className="palette-hint">{entry.hint}</span> : null}
                    {selected ? <CornerDownLeft size={13} aria-hidden className="palette-enter" /> : null}
                  </div>
                </li>
              );
            })}
          </ul>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
