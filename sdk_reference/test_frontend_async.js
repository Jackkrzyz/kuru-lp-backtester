"use strict";

// Execute the actual frontend handlers with controlled asynchronous replies.
// This tests job ownership and stale responses, not a separate mock handler.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const gateFactory = require("../frontend/request_gate");
const source = fs.readFileSync(require("node:path").join(__dirname, "../frontend/app.js"), "utf8");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function context() {
  const elements = new Map();
  return {
    app: { mode: "kuru_lp", currentJob: null, requestGate: gateFactory.create() },
    $: (id) => {
      if (!elements.has(id)) elements.set(id, { textContent: "", hidden: false, querySelector: () => ({}) });
      return elements.get(id);
    },
    performance: { now: () => 0 },
    cancelCurrentJob: async () => {}, stopPlayback: () => {},
    clearResult: () => {}, setRunButtonState: () => {},
    renderDistributionPreview: async () => {}, buildKuruRequest: () => ({ shape: "spot" }),
    pollBacktestJob: async () => {}, applyKuruResponse: () => {},
    jobHeaders: (job) => ({ "X-Backtest-Owner": job.ownerToken }),
  };
}

function loadRun(ctx) {
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf("async function runKuruBacktest()"),
    source.indexOf("function updateRunHeader()")), ctx);
}

async function main() {
  {
    const ctx = context();
    const preview = deferred();
    const previewStarted = deferred();
    let posts = 0;
    ctx.renderDistributionPreview = () => { previewStarted.resolve(); return preview.promise; };
    ctx.fetch = async () => { posts += 1; throw new Error("must not post"); };
    loadRun(ctx);
    const run = ctx.runKuruBacktest();
    await previewStarted.promise;
    ctx.app.requestGate.change();
    preview.resolve();
    await run;
    assert.equal(posts, 0, "an edited configuration must not submit the old preview");
  }
  {
    const ctx = context();
    const posted = deferred();
    const reply = deferred();
    const deletes = [];
    ctx.fetch = async (url, options) => {
      if (options.method === "DELETE") { deletes.push(url); return {}; }
      posted.resolve();
      return reply.promise;
    };
    loadRun(ctx);
    const run = ctx.runKuruBacktest();
    await posted.promise;
    ctx.app.requestGate.change();
    const latestJob = { runId: "job-new", ownerToken: "new-owner" };
    ctx.app.currentJob = latestJob;
    reply.resolve({ ok: true, json: async () => ({ run_id: "job-old", owner_token: "old-owner" }) });
    await run;
    assert.deepEqual(deletes, ["/api/backtests/job-old"]);
    assert.equal(ctx.app.currentJob, latestJob, "an old POST reply must not replace the new job");
  }
  {
    const ctx = context();
    const reply = deferred();
    const received = deferred();
    ctx.fetch = async () => { received.resolve(); return reply.promise; };
    vm.createContext(ctx);
    vm.runInContext(source.slice(source.indexOf("async function pollBacktestJob("),
      source.indexOf("function buildKuruRequest()")), ctx);
    const job = { runId: "job-old", ownerToken: "owner" };
    ctx.app.currentJob = job;
    const token = ctx.app.requestGate.begin();
    const poll = ctx.pollBacktestJob(job, token);
    await received.promise;
    ctx.app.requestGate.change();
    ctx.$("status").textContent = "new state";
    reply.resolve({ ok: true, json: async () => ({ status: "running", stage: "old state" }) });
    await assert.rejects(poll, /superseded/);
    assert.equal(ctx.$("status").textContent, "new state");
  }
  {
    const ctx = context();
    const balancing = deferred();
    const started = deferred();
    let handler;
    ctx.app.lastEditedToken = "quote";
    ctx.app.generatedGrid = {};
    ctx.$("auto-fill-amounts").addEventListener = (_name, callback) => { handler = callback; };
    ctx.markConfigurationChanged = () => ctx.app.requestGate.change();
    ctx.sdkInput = () => ({});
    ctx.KuruSdkAdapter = {
      balanceQuoteDeposit: () => { started.resolve(); return balancing.promise; },
      rawToDecimal: String,
    };
    vm.createContext(ctx);
    vm.runInContext(source.slice(source.indexOf('$("auto-fill-amounts").addEventListener'),
      source.indexOf('$("use-minimum-amounts").addEventListener')), ctx);
    const filling = handler();
    await started.promise;
    ctx.app.requestGate.change();
    ctx.$("mon-amount").value = "new user amount";
    balancing.resolve({ monAmountRaw: 3000n });
    await filling;
    assert.equal(ctx.$("mon-amount").value, "new user amount", "stale Auto-fill must not replace an edited amount");
  }
  process.stdout.write("Frontend asynchronous ownership tests passed.\n");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
