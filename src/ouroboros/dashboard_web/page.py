"""The single-file dashboard page (HTML + CSS + JS, no external assets).

Served at ``/``; it opens an SSE connection to ``/events`` and re-renders a
Kanban whenever the server pushes a new board snapshot. Kept as one string so the
dashboard has zero static-file plumbing and works behind any tunnel.
"""

from __future__ import annotations

_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Ouroboros — Live Agents</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3;
    --muted: #8b949e; --pending: #6e7681; --executing: #d29922;
    --completed: #2ea043; --failed: #f85149;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
  header { padding: 14px 20px; border-bottom: 1px solid var(--border);
    display: flex; gap: 18px; align-items: center; flex-wrap: wrap; }
  header h1 { font-size: 15px; margin: 0; letter-spacing: .5px; }
  .meta { color: var(--muted); font-size: 12px; }
  .dot { display:inline-block; width:7px; height:7px; border-radius:50%;
    margin-right:5px; vertical-align:middle; }
  .live { color: var(--completed); }
  #legend { margin-left:auto; display:flex; gap:10px; flex-wrap:wrap; }
  .legend-item { font-size:11px; color:var(--muted); }
  .legend-swatch { display:inline-block; width:9px; height:9px; border-radius:2px;
    margin-right:4px; vertical-align:middle; }
  #board { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px;
    padding: 18px; align-items: start; }
  .col { background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; min-height: 120px; display:flex; flex-direction:column; }
  .col-head { padding: 10px 12px; font-size: 12px; text-transform: uppercase;
    letter-spacing: .6px; border-bottom: 1px solid var(--border);
    display:flex; justify-content:space-between; align-items:center; }
  .col-head .count { color: var(--muted); }
  .col-body { padding: 10px; display:flex; flex-direction:column; gap:9px; }
  .card { background:#1c2230; border:1px solid var(--border); border-left-width:3px;
    border-radius:6px; padding:9px 10px; }
  .card .title { font-size: 12.5px; }
  .card .sub { margin-top:6px; display:flex; gap:8px; align-items:center;
    flex-wrap:wrap; font-size:11px; color: var(--muted); }
  .badge { font-size:10px; padding:1px 6px; border-radius:10px; color:#fff;
    font-weight:600; letter-spacing:.3px; }
  .ac { color: var(--muted); }
  .tool { color: var(--executing); }
  .tok { color: var(--muted); }
  #m-frugality { color: var(--muted); }
  .empty { color: var(--muted); font-size: 11px; padding: 8px 12px; }
  .st-pending  .col-head { color: var(--pending); }
  .st-executing .col-head { color: var(--executing); }
  .st-completed .col-head { color: var(--completed); }
  .st-failed    .col-head { color: var(--failed); }
  .card.st-pending  { border-left-color: var(--pending); }
  .card.st-executing { border-left-color: var(--executing); }
  .card.st-completed { border-left-color: var(--completed); }
  .card.st-failed    { border-left-color: var(--failed); }
</style>
</head>
<body>
<header>
  <h1>OUROBOROS · LIVE AGENTS</h1>
  <span class="meta" id="m-status"><span class="dot" style="background:var(--muted)"></span>connecting…</span>
  <span class="meta" id="m-progress"></span>
  <span class="meta" id="m-phase"></span>
  <span class="meta" id="m-tokens"></span>
  <span class="meta" id="m-frugality"></span>
  <span class="meta" id="m-frugality-retro"></span>
  <div id="legend"></div>
</header>
<div id="board"></div>
<script>
const COLS = [
  ["pending", "To Do"], ["executing", "In Progress"],
  ["completed", "Done"], ["failed", "Failed"],
];
const PALETTE = ["#3b82f6","#e3963e","#a371f7","#2ea043","#f85149","#26a8a8","#db61a2"];
const providerColor = {};
function colorFor(p) {
  if (!p) return "#6e7681";
  if (!(p in providerColor))
    providerColor[p] = PALETTE[Object.keys(providerColor).length % PALETTE.length];
  return providerColor[p];
}
function esc(s){ return (s==null?"":String(s)).replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }
// Model-tier badge glyphs/colors. Unknown tiers fall back to a neutral dot.
const TIER_SYMBOL = { frugal: "⚡", standard: "•", frontier: "▲" };
const TIER_COLOR  = { frugal: "#2ea043", standard: "#6e7681", frontier: "#d29922" };
function fmtTokens(n) {
  if (n == null) return "";
  const v = Number(n);
  if (!isFinite(v) || v <= 0) return "";
  if (v >= 1000) return (v / 1000).toFixed(1).replace(/\\.0$/, "") + "k tok";
  return Math.round(v) + " tok";
}
function fmtEvidenceTokens(n) {
  if (n == null) return "0 tok";
  const v = Number(n);
  if (!isFinite(v) || v < 0) return "0 tok";
  if (v >= 1000) return (v / 1000).toFixed(1).replace(/\\.0$/, "") + "k tok";
  return Math.round(v) + " tok";
}

function render(board) {
  const { meta, columns, providers } = board;
  document.getElementById("m-progress").textContent =
    (meta.total ? `${meta.completed}/${meta.total} ACs` : "");
  document.getElementById("m-phase").textContent =
    [meta.phase, meta.activity].filter(Boolean).join(" · ");
  document.getElementById("m-tokens").textContent = fmtTokens(meta.total_tokens);
  // textContent auto-escapes — set raw strings, like m-phase above.
  const fr = meta.frugality;
  document.getElementById("m-frugality").textContent = fr && fr.status
    ? "Frugality: " + fr.status
        + (fr.token_reduction_pct != null ? ` (${Number(fr.token_reduction_pct).toFixed(1)}% ↓)` : "")
        + (fr.reason ? " — " + String(fr.reason).slice(0, 80) : "")
    : "";
  const retro = meta.frugality_retrospective;
  const cov = retro && retro.coverage;
  document.getElementById("m-frugality-retro").textContent = retro && cov
    ? "Evidence: retry " + fmtEvidenceTokens(retro.retry_associated_spend)
        + " · unaccepted " + fmtEvidenceTokens(retro.unaccepted_spend)
        + ` · ${cov.measured_attempts} measured/${cov.unknown_attempts} unknown/${cov.invalid_attempts} invalid`
    : "";
  // legend
  const legend = document.getElementById("legend");
  legend.innerHTML = (providers||[]).map(p =>
    `<span class="legend-item"><span class="legend-swatch" style="background:${colorFor(p)}"></span>${esc(p)}</span>`
  ).join("");

  const board_el = document.getElementById("board");
  board_el.innerHTML = COLS.map(([key, label]) => {
    const cards = (columns[key] || []);
    const body = cards.length
      ? cards.map(c => cardHtml(c)).join("")
      : `<div class="empty">—</div>`;
    return `<div class="col st-${key}">
      <div class="col-head"><span>${label}</span><span class="count">${cards.length}</span></div>
      <div class="col-body">${body}</div></div>`;
  }).join("");
}
function cardHtml(c) {
  const prov = c.provider
    ? `<span class="badge" style="background:${colorFor(c.provider)}">${esc(c.provider)}</span>` : "";
  const tier = c.model_tier
    ? `<span class="badge" style="background:${TIER_COLOR[c.model_tier]||"#6e7681"}">${TIER_SYMBOL[c.model_tier]||"•"} ${esc(c.model_tier)}</span>`
    : "";
  const ac = (c.ac_index!=null) ? `<span class="ac">AC ${esc(c.ac_index)}</span>` : "";
  const tok = fmtTokens(c.tokens);
  const tokHtml = tok ? `<span class="tok">${tok}</span>` : "";
  const tool = c.tool ? `<span class="tool">⚙ ${esc(c.tool)}</span>` : "";
  const indent = c.depth ? `style="margin-left:${Math.min(c.depth,4)*10}px"` : "";
  return `<div class="card st-${esc(c.status)}" ${indent}>
    <div class="title">${esc(c.title).slice(0,160)}</div>
    <div class="sub">${prov}${tier}${ac}${tokHtml}${tool}</div></div>`;
}

__BOOTSTRAP__
</script>
</body>
</html>"""

# Live bootstrap: open an SSE stream and re-render on every pushed snapshot.
#
# The daemon's base URL is published to the user BEFORE any run exists (the auto
# flow links it while interview/seed are still running), so the page must never
# resolve "no run yet" into a dead end: it polls /api/runs until a run appears,
# then attaches. The poll is interval-gated (setTimeout) so an idle page never
# spins hot against the shared SQLite file.
_LIVE_BOOTSTRAP = """
const WAIT_POLL_MS = 3000;
function connect(runId) {
  const src = new EventSource("/events?run=" + encodeURIComponent(runId));
  const st = document.getElementById("m-status");
  src.onopen = () => st.innerHTML = '<span class="dot" style="background:var(--completed)"></span><span class="live">live</span> · ' + esc(runId);
  src.onmessage = (e) => { try { render(JSON.parse(e.data)); } catch (_) {} };
  src.onerror = () => st.innerHTML = '<span class="dot" style="background:var(--failed)"></span>reconnecting…';
}
async function pickRun() {
  // One daemon serves every run; an explicit ?run= wins, else the latest run.
  const explicit = new URLSearchParams(location.search).get("run");
  if (explicit) return explicit;
  try {
    const runs = (await (await fetch("/api/runs")).json()).runs || [];
    if (runs.length) return runs[0].execution_id;
  } catch (_) {}
  return null;
}
async function start() {
  const st = document.getElementById("m-status");
  // Poll until a run exists — the base URL can be opened before one is created.
  let runId = await pickRun();
  while (!runId) {
    st.innerHTML = '<span class="dot" style="background:var(--muted)"></span>waiting for run… (ooo run / ooo auto)';
    await new Promise(r => setTimeout(r, WAIT_POLL_MS));
    runId = await pickRun();
  }
  connect(runId);
}
start();
"""

INDEX_HTML = _PAGE_TEMPLATE.replace("__BOOTSTRAP__", _LIVE_BOOTSTRAP)


def static_html(board: dict, *, run_id: str | None = None) -> str:
    """Render a FROZEN, self-contained snapshot of one board (no SSE).

    The board JSON is inlined and rendered immediately, so the page reaches
    network-idle at once — shareable as a single file and friendly to headless
    screenshot capture (which a live SSE page never settles for).
    """
    import html as _html
    import json as _json

    # Escape ``</`` so the inlined JSON can't terminate the <script> element.
    board_json = _json.dumps(board, default=str).replace("</", "<\\/")
    # The run id is caller-controlled and lands in innerHTML via an inline JS
    # string, so it needs BOTH treatments: HTML-escape the label (innerHTML sink),
    # then embed the whole status line as a JSON string literal (valid JS string,
    # no quote/backslash breakout). ``</`` is escaped like the board JSON above so
    # the payload can never terminate the surrounding <script> element.
    label = _html.escape(run_id or "")
    status_html = '<span class="dot" style="background:var(--muted)"></span>snapshot'
    if label:
        status_html += f" · {label}"
    status_js = _json.dumps(status_html).replace("</", "<\\/")
    bootstrap = (
        f'document.getElementById("m-status").innerHTML = {status_js};\nrender({board_json});\n'
    )
    return _PAGE_TEMPLATE.replace("__BOOTSTRAP__", bootstrap)


__all__ = ["INDEX_HTML", "static_html"]
