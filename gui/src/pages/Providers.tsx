import { ArrowLeft, CircleCheck, CircleX, ExternalLink, Eye, EyeOff, KeyRound, Search, Trash2, Zap } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useId, useMemo, useState } from "react";

import { useT } from "../i18n";
import { useAction } from "../core/action";
import { del, post, put } from "../core/api";
import { shortPath } from "../core/format";
import { invalidate, useResource } from "../core/resource";
import { href } from "../core/router";
import { toast } from "../core/toast";
import type { ProviderRow, ProviderTest, ProvidersView } from "../core/types";
import { PageHead } from "../shell/PageHead";
import { Chip, Command, CopyButton, Dot, Empty, Field, Kpis, LoadError, Monogram, Notice, Skeleton } from "../ui/bits";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { Segmented } from "../ui/Segmented";
import { useDialog } from "../ui/useDialog";

type Group = "all" | "configured" | "gateway" | "cloud" | "local";

interface EditorProps {
  row: ProviderRow;
  onSaved: (view: ProvidersView) => void;
  onRemove: (row: ProviderRow) => void;
}

function Editor({ row, onSaved, onRemove }: EditorProps) {
  const { t, tx } = useT();
  const ids = useId();
  const [key, setKey] = useState("");
  const [baseUrl, setBaseUrl] = useState(row.base_url ?? "");
  const [reveal, setReveal] = useState(false);
  const [result, setResult] = useState<ProviderTest | null>(null);

  const draft = () => ({ api_key: key.trim() || undefined, base_url: baseUrl.trim() });

  const [test, testing] = useAction(
    async () => {
      setResult(null);
      setResult(await post<ProviderTest>(`/api/providers/${row.id}/test`, draft()));
    },
    { tx, failTitle: t("providers.testFailed") },
  );

  const [save, saving] = useAction(
    async () => {
      const view = await put<ProvidersView>(`/api/providers/${row.id}`, draft());
      onSaved(view);
      setKey("");
      toast({ tone: "ok", title: t("providers.saved", { name: row.label }) });
    },
    { tx, failTitle: t("providers.saveFailed") },
  );

  const canSave = row.key_required ? key.trim().length > 0 || row.configured : true;

  return (
    <form
      className="provider-editor"
      onSubmit={(event) => {
        event.preventDefault();
        if (canSave) void save();
      }}
    >
      <div className="provider-fields">
        <Field
          label={row.key_required ? t("providers.key") : t("providers.keyOptional")}
          htmlFor={`${ids}-key`}
          aside={
            row.key_url ? (
              <a className="field-link" href={row.key_url} target="_blank" rel="noreferrer">
                {row.key_required ? t("providers.getKey") : t("providers.site")}
                <ExternalLink size={12} aria-hidden />
              </a>
            ) : null
          }
          hint={row.configured ? t("providers.keyKeep", { hint: row.key_hint ?? "" }) : t("providers.keyPrivacy")}
        >
          <div className="input-group">
            <input
              id={`${ids}-key`}
              className="input input-mono"
              type={reveal ? "text" : "password"}
              value={key}
              autoComplete="off"
              spellCheck={false}
              placeholder={row.configured ? "••••••••" : "sk-…"}
              onChange={(event) => setKey(event.target.value)}
            />
            <button type="button" className="icon-btn icon-btn-sm input-group-btn" aria-label={reveal ? t("providers.hideKey") : t("providers.showKey")} aria-pressed={reveal} onClick={() => setReveal((value) => !value)}>
              {reveal ? <EyeOff size={15} aria-hidden /> : <Eye size={15} aria-hidden />}
            </button>
          </div>
        </Field>
        <Field label={t("providers.baseUrl")} htmlFor={`${ids}-url`} hint={t("providers.baseUrl.hint")}>
          <input
            id={`${ids}-url`}
            className="input input-mono"
            type="url"
            inputMode="url"
            value={baseUrl}
            autoComplete="off"
            spellCheck={false}
            placeholder={row.default_base_url}
            onChange={(event) => setBaseUrl(event.target.value)}
          />
        </Field>
      </div>
      <div className="provider-model">
        <span>{t("providers.modelFormat")}</span>
        <code>{row.model_format}</code>
        <CopyButton text={row.model_format} />
      </div>
      <AnimatePresence initial={false}>
        {result ? (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22 }} style={{ overflow: "hidden" }}>
            {result.ok ? (
              <Notice tone="ok" icon={<CircleCheck size={16} aria-hidden />} title={t("providers.testOk", { count: result.count ?? 0 })}>
                {result.models && result.models.length > 0 ? result.models.slice(0, 8).join(", ") + (result.models.length > 8 ? " …" : "") : undefined}
              </Notice>
            ) : (
              <Notice tone="fail" icon={<CircleX size={16} aria-hidden />} title={t("providers.testBad")}>
                {result.message}
              </Notice>
            )}
          </motion.div>
        ) : null}
      </AnimatePresence>
      <div className="provider-actions">
        {row.configured ? (
          <Button variant="danger" size="sm" icon={<Trash2 size={14} aria-hidden />} onClick={() => onRemove(row)}>
            {t("providers.remove")}
          </Button>
        ) : (
          <span />
        )}
        <div className="provider-actions-main">
          <Button size="sm" icon={<Zap size={14} aria-hidden />} pending={testing} onClick={() => void test()}>
            {t("providers.test")}
          </Button>
          <Button size="sm" variant="primary" type="submit" pending={saving} disabled={!canSave}>
            {t("common.save")}
          </Button>
        </div>
      </div>
    </form>
  );
}

export function ProvidersPage({ rest }: { rest: string }) {
  const { t, tx } = useT();
  const providers = useResource<ProvidersView>("/api/providers");
  const view = providers.data;
  const [group, setGroup] = useState<Group>("all");
  const [query, setQuery] = useState("");
  const removal = useDialog<ProviderRow>();

  const accept = (next: ProvidersView) => {
    providers.mutate(next);
    invalidate("/api/overview", "/api/chat");
  };

  const gatewayUrl = view?.opencodex ? `${view.opencodex.url}/v1` : "";
  const proxy = view?.providers.find((row) => row.id === "litellm_proxy");
  const gatewayLinked = !!proxy?.configured && (proxy.base_url ?? proxy.default_base_url) === gatewayUrl;

  const [useGateway, linking] = useAction(
    async () => {
      // Shown as linked at once; the server's reply confirms or reverts it.
      const before = providers.data;
      providers.mutate((current) =>
        current ? { ...current, providers: current.providers.map((row) => (row.id === "litellm_proxy" ? { ...row, configured: true, base_url: gatewayUrl, key_hint: "set" } : row)) } : current,
      );
      try {
        accept(await put<ProvidersView>("/api/providers/litellm_proxy", { base_url: gatewayUrl }));
        toast({ tone: "ok", title: t("providers.gateway.linked") });
      } catch (failure) {
        if (before) providers.mutate(before);
        throw failure;
      }
    },
    { tx, failTitle: t("providers.saveFailed") },
  );

  const [remove, removing] = useAction(
    async (row: ProviderRow) => {
      accept(await del<ProvidersView>(`/api/providers/${row.id}`));
      removal.hide();
      toast({ tone: "ok", title: t("providers.removed", { name: row.label }) });
    },
    { tx, failTitle: t("providers.removeFailed") },
  );

  const rows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return (view?.providers ?? [])
      .filter((row) => (group === "all" ? true : group === "configured" ? row.configured : row.group === group))
      .filter((row) => !needle || row.label.toLowerCase().includes(needle) || row.id.includes(needle))
      .sort((a, b) => Number(b.configured) - Number(a.configured) || Number(b.featured) - Number(a.featured));
  }, [view, group, query]);
  const selected = view?.providers.find((row) => row.id === rest);
  const withKey = view?.providers.filter((row) => row.configured).length ?? 0;

  return (
    <>
      <PageHead
        title={t("providers.title")}
        lead={
          <>
            {t("providers.lead")} {view ? <span className="path">{shortPath(view.path)}</span> : null}
          </>
        }
      />
      {providers.error && !view ? <LoadError error={providers.error} onRetry={() => void providers.reload()} /> : null}
      {providers.loading ? (
        <div className="card card-pad">
          <Skeleton lines={8} />
        </div>
      ) : null}
      {view ? (
        <div className="master" data-detail={rest ? "" : undefined}>
          <aside className="master-list card" aria-label={t("providers.title")}>
            <label className="search">
              <Search size={14} aria-hidden />
              <span className="sr-only">{t("providers.search")}</span>
              <input className="input" type="search" value={query} placeholder={t("providers.search")} onChange={(event) => setQuery(event.target.value)} />
            </label>
            <Segmented<Group>
              size="sm"
              label={t("providers.filter")}
              value={group}
              onChange={setGroup}
              options={[
                { value: "all", label: t("providers.group.all") },
                { value: "configured", label: t("providers.group.configured") },
                { value: "gateway", label: t("providers.group.gateway") },
                { value: "cloud", label: t("providers.group.cloud") },
                { value: "local", label: t("providers.group.local") },
              ]}
            />
            {rows.length === 0 ? <p className="master-empty">{t("providers.empty")}</p> : null}
            <ul>
              {rows.map((row) => (
                <li key={row.id}>
                  <a className="master-item" href={href("providers", row.id)} aria-current={selected?.id === row.id ? "true" : undefined}>
                    <Monogram label={row.label} size={28} dim={!row.configured} />
                    <span className="master-item-text">
                      <strong>{row.label}</strong>
                      <span>{row.configured ? (row.key_hint && row.key_hint !== "set" ? row.key_hint : t("providers.configured")) : row.key_required ? t("providers.noKey") : t("providers.keyless")}</span>
                    </span>
                    <Dot tone={row.configured ? "ok" : "muted"} />
                  </a>
                </li>
              ))}
            </ul>
            {view.other.length > 0 ? <p className="master-empty">{t("providers.other", { names: view.other.join(", ") })}</p> : null}
          </aside>
          <div className="master-detail">
            <a className="back-link detail-back" href={href("providers")}>
              <ArrowLeft size={14} aria-hidden />
              {t("providers.back")}
            </a>
            <Kpis
              joined
              items={[
                { label: t("providers.group.configured"), dot: withKey ? "ok" : "muted", value: withKey },
                { label: t("providers.kpi.total"), dot: "muted", value: view.providers.length },
                { label: t("providers.kpi.backend"), dot: view.llm_backend === "litellm" ? "ok" : "muted", value: view.llm_backend },
              ]}
            />
            {view.opencodex ? (
              <section className="card gateway" aria-labelledby="gateway-title">
                <div className="gateway-pulse" aria-hidden>
                  <Zap size={16} />
                </div>
                <div className="gateway-text">
                  <h2 id="gateway-title">{t("providers.gateway.title")}</h2>
                  <p>
                    {t("providers.gateway.body", { version: view.opencodex.version ?? "?" })} <code>{gatewayUrl}</code>
                  </p>
                </div>
                {gatewayLinked ? (
                  <Chip tone="ok" icon={<CircleCheck size={12} aria-hidden />}>
                    {t("providers.gateway.active")}
                  </Chip>
                ) : (
                  <Button variant="primary" pending={linking} onClick={() => void useGateway()}>
                    {t("providers.gateway.use")}
                  </Button>
                )}
              </section>
            ) : null}
            {selected ? (
              <motion.section key={selected.id} className="card provider-card" aria-labelledby="provider-title" initial={{ opacity: 0, x: 10 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}>
                <header className="detail-head">
                  <Monogram label={selected.label} size={36} dim={!selected.configured} />
                  <div className="detail-name">
                    <h2 id="provider-title">
                      {selected.label}
                      {selected.configured ? (
                        <Chip tone="ok" icon={<KeyRound size={11} aria-hidden />}>
                          {selected.key_hint && selected.key_hint !== "set" ? selected.key_hint : t("providers.configured")}
                        </Chip>
                      ) : null}
                    </h2>
                    <span>
                      <code>{selected.id}</code> · {t(`providers.group.${selected.group}`)}
                    </span>
                  </div>
                </header>
                <Editor key={selected.id} row={selected} onSaved={accept} onRemove={removal.show} />
              </motion.section>
            ) : (
              <div className="card">
                <Empty icon={<KeyRound size={18} aria-hidden />} title={t("providers.pick")} />
              </div>
            )}
            {!view.litellm_installed ? (
              <Notice
                tone="warn"
                title={t("providers.litellm.title")}
                action={
                  <a className="btn btn-sm btn-secondary" href={href("health")}>
                    <span className="btn-label">{t("providers.litellm.open")}</span>
                  </a>
                }
              >
                {t("providers.litellm.body")}
              </Notice>
            ) : null}
            {view.llm_backend !== "litellm" ? (
              <div className="hint-row">
                <p>{t("providers.backend", { backend: view.llm_backend })}</p>
                <Command text="ouroboros config set llm.backend litellm" />
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      {removal.subject ? (
        <Modal
          open={removal.open}
          onOpenChange={(next) => (next ? undefined : removal.hide())}
          title={t("providers.remove.title", { name: removal.subject.label })}
          description={t("providers.remove.body")}
          footer={
            <>
              <Button variant="ghost" onClick={removal.hide}>
                {t("common.cancel")}
              </Button>
              <Button variant="danger" pending={removing} onClick={() => removal.subject && void remove(removal.subject)}>
                {t("providers.remove")}
              </Button>
            </>
          }
        />
      ) : null}
    </>
  );
}
