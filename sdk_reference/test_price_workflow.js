"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const sdk = require("./browser_adapter");
const range = require("../frontend/price_range");
const workflow = require("../frontend/workflow");
const source = fs.readFileSync(path.join(__dirname, "../frontend/app.js"), "utf8");

function rangeTests() {
  const reference = 2711150n;
  const tick = 100n;
  const expected = sdk.percentagePriceBounds(reference, "3", "3", tick);
  assert.deepEqual(range.offsetBounds(reference, "-3", "3", tick, sdk.decimalToRaw), expected);
  const selection = new range.Selection(reference, tick, expected.minimumPriceRaw, expected.maximumPriceRaw);
  const width = selection.maximum - selection.minimum;
  selection.moveCenter(selection.center + 3700n);
  assert.equal(selection.maximum - selection.minimum, width);
  assert.equal(selection.minimum % tick, 0n);
  selection.reset();
  assert.equal(selection.center, reference, "reset uses the period start, not the last Trade or range center");
  assert.deepEqual(selection.bounds(), expected);
  selection.moveEdge("minimum", selection.maximum + 1000n);
  assert.equal(selection.minimum, selection.maximum - tick);
  selection.moveEdge("maximum", selection.minimum - 1000n);
  assert.equal(selection.maximum, selection.minimum + tick);
  assert.throws(() => range.offsetBounds(reference, "-100", "3", tick, sdk.decimalToRaw));
  assert.throws(() => range.offsetBounds(reference, "4", "3", tick, sdk.decimalToRaw));
  const away = range.offsetBounds(reference, "1.01", "5.23", tick, sdk.decimalToRaw);
  assert.equal(away.minimumPriceRaw % tick, 0n);
  assert.equal(away.maximumPriceRaw % tick, 0n);
  const otherPeriod = new range.Selection(3000050n, tick, 2900000n, 3100100n);
  otherPeriod.moveCenter(3020050n);
  otherPeriod.reset();
  assert.equal(otherPeriod.center, 3000050n);
}

async function wiringTests() {
  const metadata = { from_block: 109097028, to_block: 109107027,
    reference_price_raw: "2711150", best_ask_price_raw: "2711300",
    tick_size: "100", price_precision: "100000000", size_precision: "1000000000000", min_size: "100000000000000" };
  const values = { "absolute-range-mode": false, "lower-range-percent": "-3", "upper-range-percent": "3",
    "minimum-price": "0.0263", "maximum-price": "0.02795", "liquidity-shape": "spot", "flip-spread": "0.30",
    "mon-amount": "20000", "usdc-amount": "600", "market": "MON / USDC", "from-block": metadata.from_block,
    "to-block": metadata.to_block };
  const elements = Object.fromEntries(Object.entries(values).map(([key, value]) => [key, { value, checked: Boolean(value === true) }]));
  elements["computed-range"] = {};
  const canvases = new Map();
  const ctx = { app: { amountDriver: "base", rangeMetadata: metadata, marketAddress: "0x065c9d28e428a0db40191a54d33d5b7c71a9c394" },
    $: (id) => elements[id] || { id }, currentMetadata: () => metadata, KuruSdkAdapter: sdk, PriceRange: range,
    formatRaw: (raw) => sdk.rawToDecimal(raw, 8),
    prepareCanvas: (canvas) => {
      const bars = [];
      canvases.set(canvas.id, bars);
      return { width: 700, height: 180, context: { fillRect: (...args) => bars.push(args),
        setLineDash() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fillText() {} } };
    } };
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf("function sdkInput()"), source.indexOf("function formatRaw(")), ctx);
  vm.runInContext(source.slice(source.indexOf("function drawDistribution("), source.indexOf("function queuePricePreview(")), ctx);
  vm.runInContext(source.slice(source.indexOf("function buildKuruRequest()"), source.indexOf("function applyKuruResponse(")), ctx);
  const grids = [];
  for (const shape of ["spot", "curve", "bid_ask"]) {
    elements["liquidity-shape"].value = shape;
    const input = ctx.sdkInput();
    const grid = await sdk.generateOrderGrid(input);
    const validation = sdk.validateOrderGrid(grid);
    ctx.app.generatedGrid = grid;
    ctx.app.gridValidation = { deployable: true };
    ctx.app.priceSelection = new range.Selection(metadata.reference_price_raw, metadata.tick_size, grid.minimumPriceRaw, grid.maximumPriceRaw);
    const request = ctx.buildKuruRequest();
    assert.equal(request.lower_range_percent, "3", "signed UI offsets normalize to the existing backend magnitude");
    assert.equal(request.reference_price_raw, metadata.reference_price_raw);
    assert.equal(request.shape, shape);
    assert.deepEqual(JSON.parse(JSON.stringify(request.order_grid)), JSON.parse(JSON.stringify(sdk.serializeOrderGrid(grid))));
    ctx.drawDistribution({ ...grid, orders: [...grid.orders].reverse() }, validation);
    for (const bars of canvases.values()) {
      const orderBars = bars.slice(1);
      assert.equal(orderBars.length, grid.orders.length);
      for (let index = 1; index < orderBars.length; index += 1) assert.ok(orderBars[index][0] >= orderBars[index - 1][0], "prices render ascending across both sides");
      const peak = Math.max(...grid.orders.map((order) => Number(order.quoteNotionalRaw)));
      grid.orders.forEach((order, index) => assert.equal(orderBars[index][3], Math.max(1, Number(order.quoteNotionalRaw) / peak * 146), "bar height uses exact quote notional"));
    }
    grids.push(JSON.stringify(request.order_grid));
  }
  assert.equal(new Set(grids).size, 3);
  elements["absolute-range-mode"].checked = true;
  elements["minimum-price"].value = "0.02630009";
  elements["maximum-price"].value = "0.02795009";
  ctx.sdkInput();
  assert.equal(ctx.app.normalizedBounds.minimumPrice, "0.0263");
  assert.equal(ctx.app.normalizedBounds.maximumPrice, "0.027951");
  assert.equal(ctx.app.normalizedBounds.referencePriceRaw.toString(), metadata.reference_price_raw);
}

async function workflowTests() {
  const panel = () => ({ hidden: true, classList: { add() {}, remove() {} } });
  const panels = { timeframe: panel(), strategy: panel(), results: panel() };
  panels.timeframe.hidden = false;
  const entered = [];
  const pending = [];
  const controller = workflow.create({ panels, steps: [], wait: () => new Promise((resolve) => pending.push(resolve)),
    reducedMotion: () => false, onEnter: (name) => entered.push(name) });
  const stale = controller.show("strategy");
  const latest = controller.show("results");
  pending[1]();
  assert.equal(await latest, true);
  pending[0]();
  assert.equal(await stale, false);
  assert.equal(controller.current(), "results");
  assert.equal(panels.strategy.hidden, true);
  assert.equal(panels.results.hidden, false);
  assert.deepEqual(entered, ["results"]);
  const canceled = controller.show("strategy");
  controller.invalidate();
  pending[2]();
  assert.equal(await canceled, false, "an input edit cancels a pending results transition");
  assert.equal(controller.current(), "results");
  const reduced = workflow.create({ panels, steps: [], wait: () => { throw new Error("must not animate"); },
    reducedMotion: () => true, onEnter() {} });
  await reduced.show("strategy");
  assert.equal(panels.strategy.hidden, false);
}

async function main() {
  rangeTests();
  await wiringTests();
  await workflowTests();
  const nodes = Object.fromEntries(["from-block", "to-block", "period-start-price", "period-end-price", "period-high-price", "period-low-price"].map((key) => [key, {}]));
  nodes["from-block"].value = "100";
  nodes["to-block"].value = "200";
  const summaryContext = { $: (key) => nodes[key], KuruSdkAdapter: sdk,
    app: { rangeMetadata: { from_block: 100, to_block: 200, reference_price_raw: "2711150",
      trade_price_statistics: { ending_price: "0.0274", highest_price: "0.028", lowest_price: "0.026" } } } };
  vm.createContext(summaryContext);
  vm.runInContext(source.slice(source.indexOf("function renderPeriodSummary()"), source.indexOf("function setRangeInputs(")), summaryContext);
  summaryContext.renderPeriodSummary();
  assert.equal(nodes["period-start-price"].textContent, "0.0271115 USDC", "the starting price also accepts exact raw reference metadata");
  assert.equal(nodes["period-end-price"].textContent, "0.0274 USDC");
  assert.equal(nodes["period-high-price"].textContent, "0.028 USDC");
  nodes["from-block"].value = "101";
  summaryContext.renderPeriodSummary();
  assert.equal(nodes["period-start-price"].textContent, "Loading…", "an edited block range must not keep stale prices");
  assert.equal(nodes["period-end-price"].textContent, "—");
  console.log("Price range, SDK preview/request parity, and workflow tests passed");
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
