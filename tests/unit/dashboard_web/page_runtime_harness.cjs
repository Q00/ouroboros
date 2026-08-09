"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const liveScript = fs.readFileSync(0, "utf8").replace(/\nstart\(\);\s*$/, "\n");
assert.notEqual(liveScript.length, 0, "generated dashboard script was empty");

let retryHandler = null;
const runList = {
  hidden: false,
  innerHTML: "",
  querySelector(selector) {
    assert.equal(selector, ".retry-picker");
    return {
      addEventListener(event, handler) {
        assert.equal(event, "click");
        retryHandler = handler;
      },
    };
  },
};
const elements = new Map([["run-list", runList]]);
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, { hidden: false, innerHTML: "", textContent: "" });
  }
  return elements.get(id);
}

const location = { search: "" };
const scheduledDelays = [];
const context = {
  URLSearchParams,
  console,
  document: { getElementById: element },
  fetch: async () => {
    throw new Error("fetch response was not configured");
  },
  location,
  setTimeout(callback, delay) {
    scheduledDelays.push(delay);
    location.search = "?run=stop-after-one-poll";
    callback();
    return 1;
  },
  window: {},
};
vm.createContext(context);
vm.runInContext(
  `${liveScript}\n` +
    "globalThis.__dashboardTestHooks = {" +
    "fetchRuns, refreshRunList, startList, waitPollMs: WAIT_POLL_MS};",
  context,
  { filename: "generated-dashboard-page.js" },
);

const hooks = context.__dashboardTestHooks;
assert.ok(hooks, "dashboard test hooks were not exported from the generated script");

function response(status, payload, { malformed = false } = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    async json() {
      if (malformed) throw new SyntaxError("malformed JSON");
      return payload;
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

async function assertRequestError(responseFactory, label) {
  context.fetch = async () => responseFactory();
  const result = await hooks.fetchRuns();
  assert.equal(result.state, "network-error", `${label} classification`);
  await hooks.refreshRunList();
  assert.match(runList.innerHTML, /Run list request failed/, `${label} visible error`);
  assert.doesNotMatch(runList.innerHTML, /waiting for run/, `${label} is not empty success`);
  assert.doesNotMatch(
    runList.innerHTML,
    /Run picker temporarily unavailable/,
    `${label} is not the exact picker contract`,
  );
}

async function main() {
  context.fetch = async () =>
    response(503, { runs: [], error: "picker_index_contract_unavailable" });
  const unavailable = await hooks.fetchRuns();
  assert.equal(unavailable.state, "picker-unavailable");
  assert.equal(unavailable.runs.length, 0);
  await hooks.refreshRunList();
  assert.match(runList.innerHTML, /Run picker temporarily unavailable/);
  assert.match(runList.innerHTML, /role="alert"/);
  assert.doesNotMatch(runList.innerHTML, /waiting for run/);
  assert.equal(typeof retryHandler, "function", "picker alert must bind manual retry");

  context.fetch = async () => response(200, { runs: [] });
  const empty = await hooks.fetchRuns();
  assert.equal(empty.state, "ready");
  assert.equal(empty.runs.length, 0);
  await retryHandler();
  assert.match(runList.innerHTML, /waiting for run/);
  assert.doesNotMatch(runList.innerHTML, /temporarily unavailable/);

  await assertRequestError(() => response(500, { error: "internal" }), "generic HTTP 500");
  await assertRequestError(
    () => response(503, null, { malformed: true }),
    "malformed JSON 503",
  );
  await assertRequestError(
    () => response(200, null, { malformed: true }),
    "malformed JSON 200",
  );
  assert.equal(typeof retryHandler, "function", "generic alert must bind manual retry");
  context.fetch = async () => response(200, { runs: [] });
  await retryHandler();
  assert.match(runList.innerHTML, /waiting for run/);

  const olderUnavailable = deferred();
  const newerReady = deferred();
  let requestCount = 0;
  context.fetch = () => (requestCount++ === 0 ? olderUnavailable.promise : newerReady.promise);
  const oldRefresh = hooks.refreshRunList();
  const newRefresh = hooks.refreshRunList();
  newerReady.resolve(response(200, { runs: [] }));
  await newRefresh;
  assert.match(runList.innerHTML, /waiting for run/);
  const newestReadyHtml = runList.innerHTML;
  olderUnavailable.resolve(
    response(503, { runs: [], error: "picker_index_contract_unavailable" }),
  );
  await oldRefresh;
  assert.equal(runList.innerHTML, newestReadyHtml, "stale 503 overwrote newer empty success");

  const olderReady = deferred();
  const newerUnavailable = deferred();
  requestCount = 0;
  context.fetch = () => (requestCount++ === 0 ? olderReady.promise : newerUnavailable.promise);
  const oldReadyRefresh = hooks.refreshRunList();
  const newUnavailableRefresh = hooks.refreshRunList();
  newerUnavailable.resolve(
    response(503, { runs: [], error: "picker_index_contract_unavailable" }),
  );
  await newUnavailableRefresh;
  assert.match(runList.innerHTML, /Run picker temporarily unavailable/);
  const newestUnavailableHtml = runList.innerHTML;
  olderReady.resolve(response(200, { runs: [] }));
  await oldReadyRefresh;
  assert.equal(
    runList.innerHTML,
    newestUnavailableHtml,
    "stale empty success overwrote newer picker unavailability",
  );

  location.search = "";
  scheduledDelays.length = 0;
  context.fetch = async () => response(200, { runs: [] });
  await hooks.startList();
  assert.equal(hooks.waitPollMs, 3000);
  assert.deepEqual(scheduledDelays, [3000], "list polling must stay interval-gated at 3s");
}

main()
  .then(() => process.stdout.write("dashboard browser state machine: PASS\n"))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
