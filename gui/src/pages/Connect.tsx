import { ArrowLeft, BookOpen, Download, Plug, RefreshCw, Search, Terminal } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useMemo, useState } from "react";

import { useT } from "../i18n";
import { explain, useAction } from "../core/action";
import { get, post } from "../core/api";
import { backendName, shortPath } from "../core/format";
import { openJobs, trackJob } from "../core/jobs";
import { invalidate, useResource } from "../core/resource";
import { href, navigate } from "../core/router";
import { toast } from "../core/toast";
import type { ClientCard, ClientPlan, JobSnapshot } from "../core/types";
import { PageHead } from "../shell/PageHead";
import type { Tone } from "../ui/bits";
import { Chip, Command, Dot, Empty, Kpis, LoadError, Monogram, Notice, Skeleton } from "../ui/bits";
import { Button } from "../ui/Button";
import { Modal } from "../ui/Modal";
import { useDialog } from "../ui/useDialog";

type Busy = "install" | "setup";

const INTEGRATION_TONE: Record<ClientCard["integration"]["state"], Tone> = {
  connected: "ok",
  partial: "warn",
  missing: "muted",
  runtime: "info",
};

const PLUGIN_COMMANDS = ["claude plugin marketplace add Q00/ouroboros", "claude plugin install ouroboros@ouroboros"];

function DefaultChoice({ plan, tool, keep, onKeep }: { plan: ClientPlan; tool: string; keep: boolean; onKeep: (keep: boolean) => void }) {
  const { t } = useT();
  if (!plan.changes_default) return null;
  return (
    <fieldset className="choice">
      <legend>{t("connect.default.legend")}</legend>
      <label className="option">
        <input type="radio" name="default-agent" checked={keep} onChange={() => onKeep(true)} />
        <span className="option-title">{t("connect.default.keep", { agent: backendName(plan.current_default) })}</span>
      </label>
      <label className="option">
        <input type="radio" name="default-agent" checked={!keep} onChange={() => onKeep(false)} />
        <span className="option-title">{t("connect.default.switch", { agent: tool })}</span>
      </label>
    </fieldset>
  );
}

interface DialogProps {
  open: boolean;
  client: ClientCard;
  /** Ids of the agent tools whose CLI is installed. */
  installed: Set<string>;
  onClose: () => void;
  onSetup: (client: ClientCard, keepDefault: boolean) => Promise<boolean>;
  onInstall: (client: ClientCard, method: string, thenSetup: boolean, keepDefault: boolean) => Promise<boolean>;
}

function SetupDialog({ open, client, installed, onClose, onSetup }: DialogProps) {
  const { t } = useT();
  const plan = useResource<ClientPlan>(`/api/clients/${client.id}/plan`);
  // Until the user chooses: keep the current default whenever that agent can actually run.
  const [choice, setKeep] = useState<boolean | null>(null);
  const keep = choice ?? (!!plan.data && installed.has(plan.data.current_default));
  const [pending, setPending] = useState(false);
  return (
    <Modal
      open={open}
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={t("connect.setup.title", { tool: client.label })}
      description={t("connect.setup.lead")}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            pending={pending}
            disabled={!plan.data}
            icon={<Plug size={15} aria-hidden />}
            onClick={async () => {
              setPending(true);
              const started = await onSetup(client, keep && !!plan.data?.changes_default);
              setPending(false);
              if (started) onClose();
            }}
          >
            {t("connect.setup.run")}
          </Button>
        </>
      }
    >
      {plan.error ? <LoadError error={plan.error} onRetry={() => void plan.reload()} /> : null}
      {plan.data ? (
        <>
          <Command text={plan.data.command} wrap />
          <dl className="kv">
            <div>
              <dt>{t("connect.setup.writes")}</dt>
              <dd>
                <code>{plan.data.wiring}</code>
              </dd>
            </div>
            <div>
              <dt>{t("connect.setup.backup")}</dt>
              <dd>
                <code>{plan.data.backup}</code>
              </dd>
            </div>
          </dl>
          <DefaultChoice plan={plan.data} tool={client.label} keep={keep} onKeep={setKeep} />
        </>
      ) : plan.loading ? (
        <Skeleton lines={4} />
      ) : null}
    </Modal>
  );
}

function InstallDialog({ open, client, installed, onClose, onInstall }: DialogProps) {
  const { t } = useT();
  const isClaude = client.id === "claude";
  const plan = useResource<ClientPlan>(`/api/clients/${client.id}/plan`, { enabled: !isClaude });
  const firstAvailable = client.install.find((method) => method.available)?.id ?? "";
  const [method, setMethod] = useState(firstAvailable);
  const [thenSetup, setThenSetup] = useState(!isClaude);
  // Until the user chooses: keep the current default whenever that agent can actually run.
  const [choice, setKeep] = useState<boolean | null>(null);
  const keep = choice ?? (!!plan.data && installed.has(plan.data.current_default));
  const [pending, setPending] = useState(false);
  return (
    <Modal
      open={open}
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={t("connect.install.title", { tool: client.label })}
      description={t("connect.install.lead", { vendor: client.vendor })}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            pending={pending}
            disabled={!method}
            icon={<Download size={15} aria-hidden />}
            onClick={async () => {
              setPending(true);
              const started = await onInstall(client, method, thenSetup && !isClaude, keep && !!plan.data?.changes_default);
              setPending(false);
              if (started) onClose();
            }}
          >
            {t("connect.install.run")}
          </Button>
        </>
      }
    >
      <fieldset className="choice">
        <legend>{t("connect.install.method")}</legend>
        {client.install.map((item) => (
          <label key={item.id} className="option">
            <input type="radio" name="install-method" value={item.id} checked={method === item.id} disabled={!item.available} onChange={() => setMethod(item.id)} />
            <span className="option-title">
              {t(item.id === "script" ? "connect.install.script" : "connect.install.via", { name: item.label })}
              {item.available ? null : <span className="option-need"> · {t("connect.install.needs", { tool: item.requires })}</span>}
            </span>
            <code>{item.command}</code>
          </label>
        ))}
      </fieldset>
      {firstAvailable ? null : <Notice tone="warn">{t("connect.install.none")}</Notice>}
      {isClaude ? (
        <Notice tone="info" title={t("connect.plugin.after")}>
          {t("connect.plugin.lead")}
        </Notice>
      ) : (
        <>
          <label className="check">
            <input type="checkbox" checked={thenSetup} onChange={(event) => setThenSetup(event.target.checked)} />
            <span>
              {t("connect.install.then")}
              <small>
                <code>ouroboros setup --runtime {client.id}</code>
              </small>
            </span>
          </label>
          <AnimatePresence initial={false}>
            {thenSetup && plan.data?.changes_default ? (
              <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }} style={{ overflow: "hidden" }}>
                <DefaultChoice plan={plan.data} tool={client.label} keep={keep} onKeep={setKeep} />
              </motion.div>
            ) : null}
          </AnimatePresence>
        </>
      )}
    </Modal>
  );
}

function PluginDialog({ open, client, onClose }: { open: boolean; client: ClientCard; onClose: () => void }) {
  const { t } = useT();
  return (
    <Modal
      open={open}
      onOpenChange={(next) => (next ? undefined : onClose())}
      title={t("connect.plugin.title")}
      description={t("connect.plugin.lead")}
      footer={
        <>
          <a className="btn btn-ghost" href={client.docs} target="_blank" rel="noreferrer">
            <BookOpen size={15} aria-hidden />
            <span className="btn-label">{t("connect.docs")}</span>
          </a>
          <Button variant="primary" onClick={onClose}>
            {t("common.done")}
          </Button>
        </>
      }
    >
      <ol className="numbered">
        {PLUGIN_COMMANDS.map((command) => (
          <li key={command}>
            <Command text={command} wrap />
          </li>
        ))}
      </ol>
      <p className="section-note">{t("connect.plugin.note")}</p>
    </Modal>
  );
}

type Bucket = "connected" | "installed" | "missing";

function bucketOf(client: ClientCard): Bucket {
  if (!client.installed) return "missing";
  return client.integration.state === "connected" || client.integration.state === "partial" || client.is_default ? "connected" : "installed";
}

function dotOf(client: ClientCard): Tone {
  if (!client.installed) return "muted";
  return INTEGRATION_TONE[client.integration.state] === "muted" ? "info" : INTEGRATION_TONE[client.integration.state];
}

interface DetailProps {
  client: ClientCard;
  busy: Busy | undefined;
  onOpen: (kind: "install" | "setup" | "plugin", client: ClientCard) => void;
}

/** Everything about one tool: where it stands and the one next step. */
function Detail({ client, busy, onOpen }: DetailProps) {
  const { t, tx } = useT();
  const { integration, sign_in: signIn } = client;
  const working = busy ?? (client.running_job ? "setup" : undefined);
  const needsWiring = integration.state === "missing" || integration.state === "partial";

  let action: React.ReactNode;
  if (working) {
    action = (
      <Button pending onClick={() => openJobs(client.running_job ?? undefined)}>
        {working === "install" ? t("connect.installing") : t("connect.settingUp")}
      </Button>
    );
  } else if (!client.installed) {
    action =
      client.install.length > 0 ? (
        <Button variant="primary" icon={<Download size={14} aria-hidden />} onClick={() => onOpen("install", client)}>
          {t("connect.install")}
        </Button>
      ) : null;
  } else if (client.id === "claude") {
    action = needsWiring ? (
      <Button variant="primary" icon={<Plug size={14} aria-hidden />} onClick={() => onOpen("plugin", client)}>
        {t("connect.plugin.open")}
      </Button>
    ) : (
      <Button icon={<Terminal size={14} aria-hidden />} onClick={() => onOpen("plugin", client)}>
        {t("connect.plugin.commands")}
      </Button>
    );
  } else if (needsWiring) {
    action = (
      <Button variant="primary" icon={<Plug size={14} aria-hidden />} onClick={() => onOpen("setup", client)}>
        {t("connect.connect")}
      </Button>
    );
  } else if (integration.state === "runtime" && !client.is_default) {
    action = (
      <Button variant="primary" icon={<Plug size={14} aria-hidden />} onClick={() => onOpen("setup", client)}>
        {t("connect.useRuntime")}
      </Button>
    );
  } else {
    action = (
      <Button icon={<RefreshCw size={14} aria-hidden />} onClick={() => onOpen("setup", client)}>
        {t("connect.again")}
      </Button>
    );
  }

  return (
    <motion.article
      key={client.id}
      className="detail"
      aria-labelledby={`tool-${client.id}`}
      initial={{ opacity: 0, x: 10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
    >
      <header className="detail-head">
        <a className="icon-btn icon-btn-sm detail-back" href={href("connect")} aria-label={t("connect.back")}>
          <ArrowLeft size={16} aria-hidden />
        </a>
        <Monogram label={client.label} size={36} dim={!client.installed} />
        <div className="detail-name">
          <h2 id={`tool-${client.id}`}>
            {client.label}
            {client.is_default ? <Chip tone="ok">{t("connect.isDefault")}</Chip> : null}
            {client.role === "runtime" ? <Chip>{t("connect.runtimeOnly")}</Chip> : null}
          </h2>
          <span>{client.vendor}</span>
        </div>
        <div className="detail-actions">
          <a className="btn btn-secondary" href={client.docs} target="_blank" rel="noreferrer">
            <BookOpen size={14} aria-hidden />
            <span className="btn-label">{client.installed || client.install.length > 0 ? t("connect.docs") : t("connect.installGuide")}</span>
          </a>
          {action}
        </div>
      </header>
      <Kpis
        joined
        items={[
          { label: t("connect.detail.cli"), dot: client.installed ? "ok" : "muted", value: client.installed ? t("connect.group.installed") : t("connect.group.missing") },
          { label: "Ouroboros", dot: INTEGRATION_TONE[integration.state], value: t(`connect.state.${integration.state}`) },
          signIn
            ? { label: t("connect.detail.signin"), dot: signIn.state === "signed_in" ? "ok" : signIn.state === "signed_out" ? "warn" : "muted", value: t(`connect.signin.${signIn.state}`) }
            : { label: t("connect.detail.signin"), dot: "muted", value: "–" },
        ]}
      />
      <section className="card card-pad">
        <p className="detail-blurb">{tx(`client.${client.id}.blurb`, client.blurb)}</p>
        <dl className="rows">
          <div>
            <dt>Ouroboros</dt>
            <dd>{tx(`integration.${integration.code}`, integration.detail, integration.vars)}</dd>
          </div>
          {client.cli_path ? (
            <div>
              <dt>{t("connect.detail.cli")}</dt>
              <dd>
                <code title={client.cli_path}>{shortPath(client.cli_path)}</code>
              </dd>
            </div>
          ) : null}
          <div>
            <dt>{t("connect.setup.writes")}</dt>
            <dd>
              <code>{client.wiring}</code>
            </dd>
          </div>
          {integration.path ? (
            <div>
              <dt>{t("connect.detail.path")}</dt>
              <dd>
                <code title={integration.path}>{shortPath(integration.path)}</code>
              </dd>
            </div>
          ) : null}
          {signIn && signIn.state !== "unknown" ? (
            <div>
              <dt>{t("connect.detail.signin")}</dt>
              <dd>{signIn.state === "signed_out" && signIn.fix ? <code>{signIn.fix}</code> : signIn.detail || t(`connect.signin.${signIn.state}`)}</dd>
            </div>
          ) : null}
          {client.stages.length > 0 ? (
            <div>
              <dt>{t("connect.stages").replace(/[:：]\s*$/, "")}</dt>
              <dd className="chips">
                {client.stages.map((stage) => (
                  <Chip key={stage}>{tx(`stage.${stage}.label`, stage)}</Chip>
                ))}
              </dd>
            </div>
          ) : null}
        </dl>
      </section>
      {!client.installed && client.install.length > 0 ? (
        <section className="card card-pad" aria-label={t("connect.install.method")}>
          <h3 className="card-title">{t("connect.install.method")}</h3>
          {client.install.map((method) => (
            <div key={method.id} className="install-line">
              <span>
                {t(method.id === "script" ? "connect.install.script" : "connect.install.via", { name: method.label })}
                {method.available ? null : <span className="option-need"> · {t("connect.install.needs", { tool: method.requires })}</span>}
              </span>
              <Command text={method.command} />
            </div>
          ))}
        </section>
      ) : null}
      {client.id === "claude" && client.setup_note ? <Notice tone="info">{t("connect.plugin.lead")}</Notice> : null}
    </motion.article>
  );
}

export function ConnectPage({ rest }: { rest: string }) {
  const { t, tx } = useT();
  const clients = useResource<{ clients: ClientCard[] }>("/api/clients", { interval: 15_000 });
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState<Record<string, Busy>>({});
  const dialog = useDialog<{ kind: "install" | "setup" | "plugin"; client: ClientCard }>();
  const list = clients.data?.clients;

  const [refresh, refreshing] = useAction(
    async () => {
      const fresh = await get<{ clients: ClientCard[] }>("/api/clients?fresh=1");
      clients.mutate(fresh);
      invalidate("/api/overview", "/api/settings", "/api/chat");
    },
    { tx, failTitle: t("common.loadFailed") },
  );

  // Follow jobs that were already running when the page opened.
  useEffect(() => {
    for (const client of list ?? []) {
      if (client.running_job && !busy[client.id]) {
        void get<JobSnapshot>(`/api/jobs/${client.running_job}`).then((job) => trackJob(job).then(() => refresh()));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list?.map((client) => client.running_job).join(",")]);

  const setBusyFor = (id: string, value: Busy | undefined) =>
    setBusy((current) => {
      const next = { ...current };
      if (value) next[id] = value;
      else delete next[id];
      return next;
    });

  async function runSetup(client: ClientCard, keepDefault: boolean): Promise<boolean> {
    setBusyFor(client.id, "setup");
    try {
      const { job } = await post<{ job: JobSnapshot }>(`/api/clients/${client.id}/setup`, { keep_default: keepDefault });
      void trackJob(job, { reveal: true }).then(async (done) => {
        setBusyFor(client.id, undefined);
        await refresh();
        toast(
          done.status === "succeeded"
            ? { tone: "ok", title: t("connect.setup.ok", { tool: client.label }) }
            : { tone: "fail", title: t("connect.setup.fail", { tool: client.label }), action: { label: t("jobs.view"), run: () => openJobs(done.id) } },
        );
      });
      return true;
    } catch (failure) {
      setBusyFor(client.id, undefined);
      toast({ tone: "fail", title: t("connect.setup.fail", { tool: client.label }), body: explain(failure, tx) });
      return false;
    }
  }

  async function runInstall(client: ClientCard, method: string, thenSetup: boolean, keepDefault: boolean): Promise<boolean> {
    setBusyFor(client.id, "install");
    try {
      const { job } = await post<{ job: JobSnapshot }>(`/api/clients/${client.id}/install`, { method });
      void trackJob(job, { reveal: true }).then(async (done) => {
        setBusyFor(client.id, undefined);
        await refresh();
        if (done.status !== "succeeded") {
          toast({ tone: "fail", title: t("connect.install.fail", { tool: client.label }), action: { label: t("jobs.view"), run: () => openJobs(done.id) } });
          return;
        }
        toast({ tone: "ok", title: t("connect.install.ok", { tool: client.label }) });
        if (thenSetup) void runSetup(client, keepDefault);
      });
      return true;
    } catch (failure) {
      setBusyFor(client.id, undefined);
      toast({ tone: "fail", title: t("connect.install.fail", { tool: client.label }), body: explain(failure, tx) });
      return false;
    }
  }

  const installedIds = useMemo(() => new Set((list ?? []).filter((client) => client.installed).map((client) => client.id)), [list]);
  const groups = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const shown = (list ?? []).filter((client) => !needle || client.label.toLowerCase().includes(needle) || client.vendor.toLowerCase().includes(needle) || client.id.includes(needle));
    return (["connected", "installed", "missing"] as Bucket[]).map((bucket) => ({ bucket, items: shown.filter((client) => bucketOf(client) === bucket) })).filter((group) => group.items.length > 0);
  }, [list, query]);
  const selected = list?.find((client) => client.id === rest) ?? (rest ? undefined : list?.[0]);
  const count = (bucket: Bucket) => list?.filter((client) => bucketOf(client) === bucket).length ?? 0;

  return (
    <>
      <PageHead
        title={t("connect.title")}
        lead={t("connect.lead")}
        actions={
          <Button icon={<RefreshCw size={14} aria-hidden />} pending={refreshing} onClick={() => void refresh()}>
            {t("connect.rescan")}
          </Button>
        }
      />
      {clients.error && !list ? <LoadError error={clients.error} onRetry={() => void clients.reload()} /> : null}
      {clients.loading ? (
        <div className="card card-pad">
          <Skeleton lines={8} />
        </div>
      ) : null}
      {list ? (
        <div className="master" data-detail={rest ? "" : undefined}>
          <aside className="master-list card" aria-label={t("connect.title")}>
            <label className="search">
              <Search size={14} aria-hidden />
              <span className="sr-only">{t("connect.search")}</span>
              <input className="input" type="search" value={query} placeholder={t("connect.search")} onChange={(event) => setQuery(event.target.value)} />
            </label>
            {groups.length === 0 ? <p className="master-empty">{t("connect.empty")}</p> : null}
            {groups.map((group) => (
              <section key={group.bucket} aria-labelledby={`bucket-${group.bucket}`}>
                <h2 className="master-heading" id={`bucket-${group.bucket}`}>
                  {t(group.bucket === "connected" ? "connect.state.connected" : `connect.group.${group.bucket}`)}
                  <span>{group.items.length}</span>
                </h2>
                <ul>
                  {group.items.map((client) => (
                    <li key={client.id}>
                      <a className="master-item" href={href("connect", client.id)} aria-current={selected?.id === client.id ? "true" : undefined}>
                        <Monogram label={client.label} size={28} dim={!client.installed} />
                        <span className="master-item-text">
                          <strong>{client.label}</strong>
                          <span>{busy[client.id] || client.running_job ? t("connect.settingUp") : client.vendor}</span>
                        </span>
                        <Dot tone={dotOf(client)} pulse={!!busy[client.id] || !!client.running_job} />
                      </a>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </aside>
          <div className="master-detail">
            <Kpis
              joined
              items={[
                { label: t("connect.state.connected"), dot: "ok", value: count("connected") },
                { label: t("connect.group.installed"), dot: "info", value: count("installed") },
                { label: t("connect.group.missing"), dot: "muted", value: count("missing") },
              ]}
            />
            {selected ? (
              <Detail key={selected.id} client={selected} busy={busy[selected.id]} onOpen={(kind, target) => dialog.show({ kind, client: target })} />
            ) : (
              <Empty
                icon={<Plug size={18} aria-hidden />}
                title={t("connect.empty")}
                action={
                  <Button onClick={() => navigate("connect")}>{t("connect.back")}</Button>
                }
              />
            )}
          </div>
        </div>
      ) : null}
      {dialog.subject?.kind === "setup" ? (
        <SetupDialog key={dialog.subject.client.id} open={dialog.open} client={dialog.subject.client} installed={installedIds} onClose={dialog.hide} onSetup={runSetup} onInstall={runInstall} />
      ) : null}
      {dialog.subject?.kind === "install" ? (
        <InstallDialog key={dialog.subject.client.id} open={dialog.open} client={dialog.subject.client} installed={installedIds} onClose={dialog.hide} onSetup={runSetup} onInstall={runInstall} />
      ) : null}
      {dialog.subject?.kind === "plugin" ? <PluginDialog open={dialog.open} client={dialog.subject.client} onClose={dialog.hide} /> : null}
    </>
  );
}
