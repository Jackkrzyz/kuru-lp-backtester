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
    refreshSavedBacktests: async () => {},
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
    const handlers = {};
    for (const id of ["mon-amount", "usdc-amount"]) ctx.$(id).addEventListener = (_name, callback) => { handlers[id] = callback; };
    ctx.app.amountDriver = "both";
    ctx.markConfigurationChanged = () => ctx.app.requestGate.change();
    vm.createContext(ctx);
    vm.runInContext(source.slice(source.indexOf('$("mon-amount").addEventListener'),
      source.indexOf('[$("range-start-slider"), $("range-end-slider")]')), ctx);
    ctx.$("mon-amount").value = "6400";
    ctx.$("usdc-amount").value = "244.610955";
    handlers["mon-amount"]();
    handlers["usdc-amount"]();
    assert.equal(ctx.$("mon-amount").value, "6400", "preview must not change entered MON");
    assert.equal(ctx.$("usdc-amount").value, "244.610955", "preview must not change entered USDC");
    assert.equal(ctx.app.amountDriver, "both");
  }
  {
    const ctx=context(),pending=deferred();let applied=false;
    ctx.$('saved-run-select').value='lp-old';
    ctx.fetch=async()=>pending.promise;
    ctx.app.workflow={show:async()=>{}};
    ctx.applyKuruResponse=()=>{applied=true;};
    vm.createContext(ctx);
    vm.runInContext(source.slice(source.indexOf('async function loadSavedBacktest()'),source.indexOf('async function runKuruBacktest()')),ctx);
    const loading=ctx.loadSavedBacktest();await new Promise(resolve=>setImmediate(resolve));
    ctx.app.requestGate.change();
    pending.resolve({ok:true,json:async()=>({normalized_configuration:{},compact_timeline:[]})});
    await loading;
    assert.equal(applied,false,'An old saved-result response cannot replace an edited configuration');
  }
  {
    const ctx=context(),requests=[];
    const result={run_id:'lp-saved',interval:{from_block:10,to_block:20},normalized_configuration:{market:'market',
      amount_driver:'both',shape:'curve',minimum_price:'1',maximum_price:'2',fee_percent:'0.3',mon_amount:'2000',usdc_amount:'60'}};
    ctx.$('saved-run-select').value='lp-saved';
    ctx.fetch=async(path,options)=>{requests.push([path,options]);return {ok:true,json:async()=>result};};
    ctx.setRangeInputs=(first,last)=>{ctx.$('from-block').value=first;ctx.$('to-block').value=last;};
    ctx.refreshRangeMetadata=async()=>{};
    ctx.app.workflow={show:async()=>{}};
    let applied;ctx.applyKuruResponse=value=>{applied=value;};
    vm.createContext(ctx);
    vm.runInContext(source.slice(source.indexOf('async function loadSavedBacktest()'),source.indexOf('async function runKuruBacktest()')),ctx);
    await ctx.loadSavedBacktest();
    assert.equal(applied,result);assert.equal(ctx.$('liquidity-shape').value,'curve');
    assert.equal(ctx.$('from-block').value,10);assert.equal(ctx.$('mon-amount').value,'2000');
    assert.equal(requests.length,1);assert.ok(requests[0][0].startsWith('/api/saved-backtests/'));
    assert.notEqual(requests[0][1].method,'POST','Loading cannot start simulation');
  }
  {
    const ctx=context();vm.createContext(ctx);
    vm.runInContext(source.slice(source.indexOf('function syncReplayVisibility()'),source.indexOf('function clearResult(')),ctx);
    ctx.app.replay=null;ctx.syncReplayVisibility();
    assert.equal(ctx.$('backtest-replay').hidden,true,'No run: the chart itself is hidden');
    ctx.app.replay={frames:[]};ctx.syncReplayVisibility();
    assert.equal(ctx.$('backtest-replay').hidden,true,'An empty replay cannot reveal the chart');
    ctx.app.replay={frames:[{}]};ctx.syncReplayVisibility();
    assert.equal(ctx.$('backtest-replay').hidden,false,'A completed or loaded replay reveals the chart');
    ctx.app.replay=null;ctx.syncReplayVisibility();
    assert.equal(ctx.$('step-results').hidden,true,'Clearing a result hides its entire panel');
  }
  process.stdout.write("Frontend asynchronous ownership tests passed.\n");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
