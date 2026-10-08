"use strict";

const DATA_ROOT = "public/data";
const regimeLabels = {
  relatively_active: "Relatively active",
  relatively_quiet: "Relatively quiet",
  directional: "Directional move",
};
const scenarioLabels = {
  uncapped: "Uncapped",
  max_25_percent: "25% participation cap",
  max_10_percent: "10% participation cap",
  max_5_percent: "5% participation cap",
};

const app = {
  mode: "kuru_lp",
  mmCatalog: null,
  intervalMetadata: null,
  rangeMetadata: null,
  tradeChart: null,
  rangeRequestRevision: 0,
  rangeRequestController: null,
  rangeRefreshTimer: null,
  marketAddress: null,
  replay: null,
  frameIndex: 0,
  playing: false,
  speed: 1,
  animationId: null,
  lastTick: 0,
  cache: new Map(),
  generatedGrid: null,
  gridValidation: null,
  minimumViablePosition: null,
  normalizedBounds: null,
  running: false,
  amountDriver: "both",
  lastEditedToken: "quote",
  previewRevision: 0,
  requestGate: RunRequestGate.create(),
  orderState: new Map(),
  orderStateFrame: -1,
  currentJob: null,
  requestController: null,
  frontendTimings: null,
  chartCache: new Map(),
};

const $ = (id) => document.getElementById(id);
const money = (value, digits = 4) => `${Number(value).toFixed(digits)} USDC`;
const quantity = (value, digits = 2) => `${Number(value).toFixed(digits)} MON`;
const price = (value) => (value == null ? "—" : Number(value).toFixed(6));
const percent = (value) => `${Number(value).toFixed(2)}%`;

function currentMetadata() {
  if (app.tradeChart) return app.rangeMetadata;
  return app.intervalMetadata && app.intervalMetadata[$("regime").value];
}

function signedMoney(value) {
  const numeric = Number(value);
  return `${numeric >= 0 ? "+" : "−"}${Math.abs(numeric).toFixed(4)} USDC`;
}

function setSignedMetric(id, value) {
  const node = $(id);
  node.textContent = signedMoney(value);
  node.classList.toggle("positive", Number(value) >= 0);
  node.classList.toggle("negative", Number(value) < 0);
}

function sdkInput() {
  const metadata = currentMetadata();
  if (!metadata) throw new Error("The selected block range metadata is not ready.");
  const absoluteMode = $("absolute-range-mode").checked;
  const bounds = absoluteMode
    ? KuruSdkAdapter.absolutePriceBounds($("minimum-price").value, $("maximum-price").value, metadata.tick_size)
    : KuruSdkAdapter.percentagePriceBounds(
      metadata.reference_price_raw,
      $("lower-range-percent").value,
      $("upper-range-percent").value,
      metadata.tick_size,
    );
  const minimumPrice = KuruSdkAdapter.rawToDecimal(bounds.minimumPriceRaw, 8);
  const maximumPrice = KuruSdkAdapter.rawToDecimal(bounds.maximumPriceRaw, 8);
  app.normalizedBounds = {
    rangeMode: absoluteMode ? "absolute" : "percentage",
    lowerRangePercent: absoluteMode ? null : $("lower-range-percent").value,
    upperRangePercent: absoluteMode ? null : $("upper-range-percent").value,
    referencePriceRaw: BigInt(metadata.reference_price_raw),
    minimumPriceRaw: bounds.minimumPriceRaw,
    maximumPriceRaw: bounds.maximumPriceRaw,
    minimumPrice,
    maximumPrice,
  };
  if (!absoluteMode) {
    $("minimum-price").value = minimumPrice;
    $("maximum-price").value = maximumPrice;
  }
  $("computed-range").textContent = `Reference ${formatRaw(metadata.reference_price_raw, 8, 8)} USDC · Final range ${minimumPrice}–${maximumPrice} USDC`;
  return {
    shape: $("liquidity-shape").value,
    minimumPrice,
    maximumPrice,
    feePercent: $("flip-spread").value,
    monAmount: $("mon-amount").value,
    usdcAmount: $("usdc-amount").value,
    driver: app.amountDriver,
    bestAskPriceRaw: String(metadata.best_ask_price_raw),
    pricePrecision: String(metadata.price_precision),
    sizePrecision: String(metadata.size_precision),
    quoteAssetDecimals: 6,
    baseAssetDecimals: 18,
    tickSize: String(metadata.tick_size),
    minSize: String(metadata.min_size),
  };
}

function formatRaw(raw, decimals, digits = 6) {
  const exact = KuruSdkAdapter.rawToDecimal(raw, decimals);
  const number = Number(exact);
  return Number.isFinite(number) ? number.toLocaleString(undefined, { maximumFractionDigits: digits }) : exact;
}

function formatSizeRaw(raw) {
  const metadata = currentMetadata();
  if (!metadata) return String(raw);
  const value = Number(raw) / Number(metadata.size_precision);
  return Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 12 }) : String(raw);
}

async function generateDistribution() {
  return KuruSdkAdapter.generateOrderGrid(sdkInput());
}

function prepareCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(260, rect.width);
  const height = Math.max(100, rect.height);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return { context, width, height };
}

function renderAmountStatus(grid) {
  const enteredMon = KuruSdkAdapter.decimalToRaw($("mon-amount").value, 18);
  const enteredUsdc = KuruSdkAdapter.decimalToRaw($("usdc-amount").value, 6);
  const monShortage = grid.requiredMonRaw > enteredMon ? grid.requiredMonRaw - enteredMon : 0n;
  const usdcShortage = grid.requiredUsdcRaw > enteredUsdc ? grid.requiredUsdcRaw - enteredUsdc : 0n;
  $("mon-requirement").textContent = `Required ${formatRaw(grid.requiredMonRaw, 18)} · Entered ${formatRaw(enteredMon, 18)} · Shortage ${formatRaw(monShortage, 18)} MON`;
  $("usdc-requirement").textContent = `Required ${formatRaw(grid.requiredUsdcRaw, 6)} · Entered ${formatRaw(enteredUsdc, 6)} · Shortage ${formatRaw(usdcShortage, 6)} USDC`;
  $("mon-requirement").classList.toggle("shortage", monShortage > 0n);
  $("usdc-requirement").classList.toggle("shortage", usdcShortage > 0n);
  $("mon-driver").classList.toggle("active", app.amountDriver === "base");
  $("usdc-driver").classList.toggle("active", app.amountDriver === "quote");
  return { monShortage, usdcShortage };
}

function setRunButtonState() {
  const button = $("backtest-form").querySelector(".run-button");
  const canRun = app.mode !== "kuru_lp" || Boolean(app.gridValidation?.deployable);
  button.disabled = app.running || !canRun;
  $("run-button-label").textContent = app.running ? "Running backtest…" : "Run backtest";
}

function renderGridValidation(validation, shortages) {
  const smallest = formatSizeRaw(validation.smallestOrderSizeRaw);
  const minimum = formatSizeRaw(validation.marketMinSizeRaw);
  const hasShortage = shortages.monShortage > 0n || shortages.usdcShortage > 0n;
  const deployable = validation.deployable && !hasShortage;
  app.gridValidation = { ...validation, deployable };
  $("smallest-order").textContent = `${smallest} MON`;
  $("market-minimum-order").textContent = `${minimum} MON`;
  $("invalid-order-count").textContent = String(validation.invalidOrderCount);
  $("invalid-order-count").classList.toggle("invalid", validation.invalidOrderCount > 0);
  $("offending-order").textContent = validation.offendingOrder
    ? `${validation.offendingOrder.side} @ ${formatRaw(validation.offendingOrder.priceRaw, 8, 8)}`
    : "None";
  $("offending-order").classList.toggle("invalid", Boolean(validation.offendingOrder));
  $("deployable-status").textContent = deployable ? "Yes" : (hasShortage ? "No · deposit shortage" : "No");
  $("deployable-status").classList.toggle("invalid", !deployable);
  setRunButtonState();
}

async function renderDistributionPreview({ searchMinimum = true } = {}) {
  const revision = ++app.previewRevision;
  try {
    const input = sdkInput();
    const distribution = await KuruSdkAdapter.generateOrderGrid(input);
    if (revision !== app.previewRevision) return;
    app.generatedGrid = distribution;
    const validation = KuruSdkAdapter.validateOrderGrid(distribution, input.minSize);
    const rows = distribution.orders;
    const canvas = $("distribution-chart");
    const { context, width, height } = prepareCanvas(canvas);
    const maximum = Math.max(1, ...rows.map((item) => Number(item.quoteNotionalRaw)));
    const start = Number(distribution.minimumPriceRaw);
    const end = Number(distribution.maximumPriceRaw);
    const xForPrice = (raw) => 8 + (Number(raw) - start) / Math.max(1, end - start) * (width - 16);
    const barWidth = Math.max(2, (width - 16) / rows.length * 0.62);
    const invalidKeys = new Set(validation.invalidOrders.map((item) => `${item.side}:${item.priceRaw}:${item.sizeRaw}`));
    rows.forEach((item) => {
      const x = xForPrice(item.price) - barWidth / 2;
      const barHeight = Math.max(2, Number(item.quoteNotionalRaw) / maximum * (height - 31));
      context.fillStyle = invalidKeys.has(`${item.side}:${item.price}:${item.liquidity}`)
        ? "#f2b04f"
        : (item.side === "buy" ? "#3978d4" : "#d05a61");
      context.fillRect(x, height - 18 - barHeight, barWidth, barHeight);
    });
    const marketX = xForPrice(app.normalizedBounds.referencePriceRaw);
    context.strokeStyle = "#a8d04a";
    context.setLineDash([3, 3]);
    context.beginPath(); context.moveTo(marketX, 7); context.lineTo(marketX, height - 17); context.stroke();
    context.setLineDash([]);
    context.fillStyle = "#82908d";
    context.font = "9px Inter, system-ui, sans-serif";
    context.fillText(`${Number(distribution.minimumPriceRaw) / 1e8}`, 7, height - 4);
    const endText = `${Number(distribution.maximumPriceRaw) / 1e8}`;
    context.fillText(endText, width - context.measureText(endText).width - 7, height - 4);
    const bidCount = rows.filter((item) => item.side === "buy").length;
    const askCount = rows.length - bidCount;
    $("preview-count").textContent = `${bidCount} bids · ${askCount} asks`;
    $("preview-detail").textContent = `SDK ${distribution.source.version} · ${distribution.feePercent}% · bar height is quote notional · green marker is first-block midpoint${validation.invalidOrderCount ? " · amber bars are below minSize" : ""}`;
    $("preview-detail").classList.toggle("warning", validation.invalidOrderCount > 0);
    const shortages = renderAmountStatus(distribution);
    renderGridValidation(validation, shortages);
    if (searchMinimum) {
      $("minimum-viable-position").textContent = "Calculating minimum viable position…";
      $("use-minimum-amounts").disabled = true;
      const minimum = await KuruSdkAdapter.findMinimumViablePosition(input, app.lastEditedToken);
      if (revision !== app.previewRevision) return;
      app.minimumViablePosition = minimum;
      $("minimum-viable-position").textContent = `Minimum viable MON ${formatRaw(minimum.minimumMonAmountRaw, 18, 12)} · Corresponding USDC ${formatRaw(minimum.minimumUsdcAmountRaw, 6, 6)}`;
      $("use-minimum-amounts").disabled = false;
    }
  } catch (error) {
    if (revision !== app.previewRevision) return;
    app.generatedGrid = null;
    app.gridValidation = null;
    app.minimumViablePosition = null;
    $("preview-count").textContent = "Invalid setup";
    $("preview-detail").textContent = error.message;
    $("preview-detail").classList.add("warning");
    $("deployable-status").textContent = "No";
    $("deployable-status").classList.add("invalid");
    $("minimum-viable-position").textContent = "Minimum viable position unavailable";
    $("use-minimum-amounts").disabled = true;
    setRunButtonState();
  }
}

function mmArtifact(interval, scenario) {
  return app.mmCatalog.artifacts.find((item) => item.interval === interval && item.scenario === scenario);
}

async function fetchReplay(path) {
  if (!app.cache.has(path)) {
    const response = await fetch(`${DATA_ROOT}/${path}`);
    if (!response.ok) throw new Error(`Replay request failed with HTTP ${response.status}.`);
    app.cache.set(path, await response.json());
  }
  return app.cache.get(path);
}

function updateMMConfig(config) {
  $("starting-cash").value = `${config.starting_cash} USDC`;
  $("starting-inventory").value = `${config.starting_inventory} MON`;
  $("bid-offset").value = `${config.bid_offset_bps} bps`;
  $("ask-offset").value = `${config.ask_offset_bps} bps`;
  $("order-size").value = `${config.order_size} MON`;
  $("minimum-inventory").value = `${config.minimum_inventory} MON`;
  $("maximum-inventory").value = `${config.maximum_inventory} MON`;
  $("maker-fee").value = `${config.maker_fee_bps} bps`;
  $("cancel-replace").value = config.cancel_replace ? "Enabled" : "Disabled";
  $("inclusion-latency").value = `${config.inclusion_delay_blocks} block`;
  $("action-fee").value = `${config.fixed_action_fee} USDC`;
  $("impact-threshold").value = `${config.material_impact_threshold_percent}%`;
}

function participationPercent() {
  return {
    uncapped: null,
    max_25_percent: 25,
    max_10_percent: 10,
    max_5_percent: 5,
  }[$("scenario").value];
}

function clearResult(message = "No run yet", stale = false) {
  stopPlayback();
  app.replay = null;
  app.frameIndex = 0;
  app.orderState = new Map();
  app.orderStateFrame = -1;
  $("scrubber").max = "0";
  $("scrubber").value = "0";
  $("current-frame").textContent = "0";
  $("total-frames").textContent = "—";
  $("frame-time").textContent = "—";
  $("block-label").textContent = "Block —";
  $("frame-actions").textContent = "—";
  $("orders-list").innerHTML = '<div class="empty-orders">Run a backtest to view simulated orders</div>';
  ["strategy-pnl", "hold-pnl", "excess-pnl", "drawdown", "turnover", "ending-inventory", "fill-counts", "order-actions", "extra-one", "extra-two", "extra-three", "break-even-cost"].forEach((id) => {
    $(id).textContent = "—";
    $(id).classList.remove("positive", "negative");
  });
  $("run-identity").hidden = true;
  $("result-state").textContent = stale ? "Configuration changed" : "No result";
  $("status").textContent = message;
  $("impact-warning").hidden = true;
  if (app.mode === "kuru_lp") {
    const shape = $("liquidity-shape").value.replace("bid_ask", "Bid-Ask").replace(/^./, (value) => value.toUpperCase());
    $("engine-label").textContent = "Kuru LP engine";
    $("run-title").textContent = `MON / USDC · blocks ${$("from-block").value}–${$("to-block").value}`;
    $("scenario-badge").textContent = `${shape} · ${scenarioLabels[$("scenario").value]}`;
    $("scenario-badge").classList.toggle("caution", $("scenario").value === "uncapped");
  }
  ["price-chart", "equity-chart"].forEach((id) => {
    const canvas = $(id);
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
  });
}

function markConfigurationChanged() {
  app.requestGate.change();
  cancelCurrentJob().catch(() => {});
  app.running = false;
  app.generatedGrid = null;
  app.gridValidation = null;
  app.minimumViablePosition = null;
  if (app.mode === "kuru_lp") clearResult("Configuration changed. Run a new backtest.", true);
  setRunButtonState();
}

function jobHeaders(job) {
  return { "X-Backtest-Owner": job.ownerToken };
}

async function cancelCurrentJob() {
  const job = app.currentJob;
  if (!job) return;
  app.currentJob = null;
  app.requestController?.abort();
  app.requestController = null;
  await fetch(`/api/backtests/${job.runId}`, { method: "DELETE", headers: jobHeaders(job) }).catch(() => {});
  $("cancel-backtest").hidden = true;
}

async function pollBacktestJob(job, token) {
  while (app.currentJob === job && app.requestGate.accepts(token)) {
    const response = await fetch(`/api/backtests/${job.runId}`, { headers: jobHeaders(job) });
    const status = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(status.error || `Job status failed with HTTP ${response.status}.`);
    const processed = Number(status.processed_events || 0).toLocaleString();
    const total = status.total_events == null ? "?" : Number(status.total_events).toLocaleString();
    const percentComplete = Number(status.percent_complete || 0).toFixed(1);
    $("status").textContent = `${status.stage} · ${processed}/${total} events · ${percentComplete}%`;
    if (status.status === "completed") return status;
    if (["failed", "canceled", "stalled"].includes(status.status)) {
      const failure = status.failure;
      const message = failure && typeof failure === "object" ? failure.message : status.failure_reason;
      throw new Error(message || `Backtest ${status.status}.`);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("Backtest request was superseded.");
}

function buildKuruRequest() {
  if (!app.generatedGrid) throw new Error("The SDK order grid is not valid.");
  if (!app.gridValidation?.deployable) throw new Error("The generated position is not deployable.");
  if (!app.normalizedBounds) throw new Error("The normalized range is not ready.");
  if (
    !app.rangeMetadata
    || Number(app.rangeMetadata.from_block) !== Number($("from-block").value)
    || Number(app.rangeMetadata.to_block) !== Number($("to-block").value)
  ) throw new Error("The exact block range has not passed local coverage validation.");
  return {
    engine: "kuru_lp",
    market: app.marketAddress,
    market_symbol: $("market").value,
    from_block: Number($("from-block").value),
    to_block: Number($("to-block").value),
    shape: $("liquidity-shape").value,
    range_mode: app.normalizedBounds.rangeMode,
    lower_range_percent: app.normalizedBounds.lowerRangePercent,
    upper_range_percent: app.normalizedBounds.upperRangePercent,
    reference_price: KuruSdkAdapter.rawToDecimal(app.normalizedBounds.referencePriceRaw, 8),
    reference_price_raw: app.normalizedBounds.referencePriceRaw.toString(),
    minimum_price: app.normalizedBounds.minimumPrice,
    maximum_price: app.normalizedBounds.maximumPrice,
    fee_percent: $("flip-spread").value,
    mon_amount: $("mon-amount").value,
    usdc_amount: $("usdc-amount").value,
    amount_driver: app.amountDriver,
    inclusion_delay_blocks: 1,
    max_transaction_participation_percent: participationPercent(),
    maker_fee_bps: 0,
    fixed_action_fee: 0,
    material_impact_threshold_percent: 25,
    order_grid: KuruSdkAdapter.serializeOrderGrid(app.generatedGrid),
  };
}

function applyKuruResponse(response) {
  app.replay = {
    engine: response.engine,
    interval: response.normalized_configuration.interval,
    scenario: `${response.normalized_configuration.shape}_custom`,
    uncapped: response.normalized_configuration.max_transaction_participation_percent === null,
    config: response.normalized_configuration,
    results: response.results,
    frames: response.compact_timeline,
    run_id: response.run_id,
    summary_sha256: response.summary_sha256,
    timeline_sha256: response.timeline_sha256,
  };
  app.frameIndex = 0;
  app.chartCache.clear();
  updateRunHeader();
  updateResults();
  $("run-id").textContent = response.run_id;
  $("normalized-config").textContent = JSON.stringify(response.normalized_configuration);
  $("result-hashes").textContent = `summary ${response.summary_sha256} · timeline ${response.timeline_sha256}`;
  $("run-identity").hidden = false;
  $("scrubber").max = String(app.replay.frames.length - 1);
  $("scrubber").value = "0";
  $("total-frames").textContent = app.replay.frames.length.toLocaleString();
  renderFrame(0);
}

async function runKuruBacktest() {
  await cancelCurrentJob();
  stopPlayback();
  const button = $("backtest-form").querySelector(".run-button");
  const token = app.requestGate.begin();
  clearResult("Running backtest…");
  app.running = true;
  setRunButtonState();
  try {
    await renderDistributionPreview();
    const request = buildKuruRequest();
    const requestStarted = performance.now();
    const response = await fetch("/api/backtests", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = payload.error && typeof payload.error === "object" ? payload.error.message : payload.error;
      throw new Error(message || `Backtest request failed with HTTP ${response.status}.`);
    }
    const job = { runId: payload.run_id, ownerToken: payload.owner_token };
    app.currentJob = job;
    $("cancel-backtest").hidden = false;
    await pollBacktestJob(job, token);
    if (!app.requestGate.accepts(token) || app.mode !== "kuru_lp") return;
    const resultStarted = performance.now();
    const resultResponse = await fetch(`/api/backtests/${job.runId}/result`, { headers: jobHeaders(job) });
    const resultText = await resultResponse.text();
    const fetchedAt = performance.now();
    let result = {};
    const parseStarted = performance.now();
    try { result = JSON.parse(resultText); } catch (_) { result = {}; }
    if (!resultResponse.ok) throw new Error(result.error || `Backtest result failed with HTTP ${resultResponse.status}.`);
    const parsedAt = performance.now();
    if (!app.requestGate.accepts(token) || app.mode !== "kuru_lp") return;
    applyKuruResponse(result);
    app.frontendTimings = {
      total_request_seconds: (parsedAt - requestStarted) / 1000,
      result_fetch_seconds: (fetchedAt - resultStarted) / 1000,
      timeline_parse_seconds: (parsedAt - parseStarted) / 1000,
    };
    app.currentJob = null;
    $("cancel-backtest").hidden = true;
    $("status").textContent = "New backtest ready";
  } catch (error) {
    if (app.requestGate.accepts(token)) {
      clearResult(error.message);
      $("impact-warning").hidden = false;
      $("impact-warning").textContent = `Backtest failed: ${error.message}`;
    }
  } finally {
    if (app.requestGate.accepts(token)) {
      app.running = false;
      setRunButtonState();
      $("cancel-backtest").hidden = true;
    }
  }
}

async function loadDemoReplay() {
  stopPlayback();
  $("status").textContent = "Loading labeled demo replay…";
  const artifact = mmArtifact($("regime").value, $("scenario").value);
  if (!artifact) throw new Error("The selected deterministic bot demo is not available.");
  app.replay = await fetchReplay(artifact.path);
  app.chartCache.clear();
  updateMMConfig(app.replay.config);
  app.frameIndex = 0;
  updateRunHeader();
  updateResults();
  $("run-identity").hidden = true;
  $("result-state").textContent = "Saved demo replay";
  $("scrubber").max = String(app.replay.frames.length - 1);
  $("scrubber").value = "0";
  $("total-frames").textContent = app.replay.frames.length.toLocaleString();
  renderFrame(0);
  $("status").textContent = "Demo replay ready";
}

function updateRunHeader() {
  const replay = app.replay;
  const engine = replay.engine === "kuru_lp" ? "Kuru LP engine" : "Advanced MM Bot engine";
  $("engine-label").textContent = engine;
  const config = replay.config || {};
  $("run-title").textContent = config.from_block != null
    ? `MON / USDC · blocks ${config.from_block}–${config.to_block}`
    : `MON / USDC · ${regimeLabels[replay.interval] || replay.interval}`;
  const badge = $("scenario-badge");
  const shape = replay.config.shape ? replay.config.shape.replace("bid_ask", "Bid-Ask").replace(/^./, (value) => value.toUpperCase()) : "";
  badge.textContent = `${replay.engine === "kuru_lp" ? `${shape} · ` : ""}${scenarioLabels[$("scenario").value]}`;
  badge.classList.toggle("caution", replay.uncapped);
  const warning = $("impact-warning");
  if (replay.uncapped || replay.results.transaction_impact_warning) {
    warning.hidden = false;
    warning.textContent = `${replay.uncapped ? "Uncapped replay" : "Transaction-impact warning"}. Maximum participation was ${Number(replay.results.maximum_transaction_impact_percent).toFixed(2)}%.`;
  } else {
    warning.hidden = true;
    warning.textContent = "";
  }
}

function updateResults() {
  const result = app.replay.results;
  const isLP = app.replay.engine === "kuru_lp";
  setSignedMetric("strategy-pnl", result.strategy_pnl);
  setSignedMetric("hold-pnl", result.hold_pnl);
  setSignedMetric("excess-pnl", result.excess_pnl);
  $("drawdown").textContent = money(result.maximum_drawdown);
  $("turnover").textContent = money(result.turnover, 2);
  $("inventory-label").textContent = isLP ? "Inventory composition" : "Ending inventory";
  $("ending-inventory").textContent = isLP
    ? `${Number(result.ending_mon_percent).toFixed(1)}% MON / ${Number(result.ending_usdc_percent).toFixed(1)}% USDC`
    : quantity(result.ending_inventory, 2);
  $("fill-counts").textContent = `${result.buy_fill_count} / ${result.sell_fill_count}`;
  $("order-actions").textContent = Number(result.total_order_actions).toLocaleString();
  $("actions-label").textContent = isLP ? "Initial + flip orders" : "Order actions";
  $("extra-one-label").textContent = isLP ? "Earned flip spread" : "Engine";
  $("extra-one").textContent = isLP ? money(result.earned_flip_spread, 6) : "Cancel / replace";
  $("extra-two-label").textContent = isLP ? "Time in range" : "Participation";
  $("extra-two").textContent = isLP ? percent(result.time_in_range_percent) : scenarioLabels[$("scenario").value];
  $("extra-three-label").textContent = isLP ? "Capital utilization" : "Mode";
  $("extra-three").textContent = isLP ? percent(result.capital_utilization_percent) : "Advanced";
  $("break-even-label").textContent = isLP ? "Required MON / USDC ratio" : "Break-even action cost";
  $("break-even-cost").textContent = isLP ? `${Number(result.required_mon_usdc_ratio).toFixed(4)} MON per USDC` : money(result.break_even_action_cost, 6);
  $("result-state").textContent = result.transaction_impact_warning ? "Impact warning" : "Impact check passed";
}

function activeOrdersAt(frameIndex) {
  if (frameIndex < app.orderStateFrame) {
    app.orderState = new Map();
    app.orderStateFrame = -1;
  }
  for (let index = app.orderStateFrame + 1; index <= frameIndex; index += 1) {
    const frame = app.replay.frames[index];
    const legacySnapshot = frame.active_orders?.length && !frame.active_order_deltas?.length;
    if (frame.order_snapshot_complete || legacySnapshot) {
      app.orderState = new Map((frame.active_orders || []).map((order) => [order.order_id, order]));
    }
    (frame.active_order_deltas || []).forEach((order) => {
      if (["pending", "active"].includes(order.status)) app.orderState.set(order.order_id, order);
      else app.orderState.delete(order.order_id);
    });
    app.orderStateFrame = index;
  }
  return [...app.orderState.values()];
}

function renderOrders(frame, frameIndex) {
  const list = $("orders-list");
  const activeOrders = activeOrdersAt(frameIndex);
  if (!activeOrders.length) {
    list.innerHTML = '<div class="empty-orders">No active simulated orders</div>';
    return;
  }
  list.innerHTML = activeOrders.slice(0, 12).map((order) => `
    <div class="order-row">
      <span class="order-side ${order.side}">${order.side}</span>
      <div class="order-detail"><strong>${price(order.price)} USDC</strong><span>${order.status} · queue ahead ${Number(order.queue_ahead_size).toFixed(2)} MON</span></div>
      <div class="order-size">${Number(order.remaining_size).toFixed(2)} MON</div>
    </div>`).join("") + (activeOrders.length > 12 ? `<div class="more-orders">+ ${activeOrders.length - 12} more orders</div>` : "");
}

function renderFrame(index) {
  if (!app.replay) return;
  app.frameIndex = Math.max(0, Math.min(index, app.replay.frames.length - 1));
  const frame = app.replay.frames[app.frameIndex];
  $("scrubber").value = String(app.frameIndex);
  $("current-frame").textContent = (app.frameIndex + 1).toLocaleString();
  $("frame-time").textContent = `${new Date(frame.timestamp).toLocaleString()} · Block ${Number(frame.block).toLocaleString()} · Mid ${price(frame.midpoint)}`;
  $("block-label").textContent = `Block ${Number(frame.block).toLocaleString()}`;
  const actionText = [frame.placements.length ? `${frame.placements.length} placed` : "", frame.activations.length ? `${frame.activations.length} active` : "", frame.cancellations.length ? `${frame.cancellations.length} canceled` : "", frame.fills.length ? `${frame.fills.length} filled` : ""].filter(Boolean).join(" · ");
  $("frame-actions").textContent = actionText || "Market update only";
  renderOrders(frame, app.frameIndex);
  const chartStarted = performance.now();
  drawPriceChart();
  drawEquityChart();
  app.frontendTimings = app.frontendTimings || {};
  app.frontendTimings.last_chart_render_milliseconds = performance.now() - chartStarted;
}

function chartScale(values, width, height, padding) {
  const valid = values.filter(Number.isFinite);
  let minimum = Math.min(...valid);
  let maximum = Math.max(...valid);
  const span = Math.max(maximum - minimum, Math.abs(maximum) * 0.002, 0.000001);
  minimum -= span * 0.12;
  maximum += span * 0.12;
  return {
    x: (position, count) => padding.left + position / Math.max(1, count - 1) * (width - padding.left - padding.right),
    y: (value) => padding.top + (maximum - value) / (maximum - minimum) * (height - padding.top - padding.bottom),
    minimum, maximum,
  };
}

function drawGrid(context, width, height, padding, scale, formatter) {
  context.strokeStyle = "#edf1ee";
  context.fillStyle = "#76817f";
  context.font = "10px Inter, system-ui, sans-serif";
  for (let row = 0; row <= 4; row += 1) {
    const y = padding.top + row / 4 * (height - padding.top - padding.bottom);
    context.beginPath(); context.moveTo(padding.left, y); context.lineTo(width - padding.right, y); context.stroke();
    context.fillText(formatter(scale.maximum - row / 4 * (scale.maximum - scale.minimum)), 2, y + 3);
  }
}

function drawLine(context, frames, key, scale, color, lineWidth = 1.5) {
  context.beginPath(); context.strokeStyle = color; context.lineWidth = lineWidth;
  let started = false;
  frames.forEach((frame, index) => {
    const value = Number(frame[key]);
    if (!Number.isFinite(value)) return;
    const x = scale.x(index, frames.length); const y = scale.y(value);
    if (!started) { context.moveTo(x, y); started = true; } else context.lineTo(x, y);
  });
  context.stroke();
}

function chartLayer(name, width, height, build) {
  const ratio = window.devicePixelRatio || 1;
  const identity = app.replay.timeline_sha256 || app.replay.run_id || `${app.replay.interval}-${app.replay.frames.length}`;
  const key = `${name}:${identity}:${Math.round(width)}:${Math.round(height)}:${ratio}`;
  if (app.chartCache.has(key)) return app.chartCache.get(key);
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  const value = build(context, canvas, ratio);
  const layer = { canvas, ratio, ...value };
  app.chartCache.set(key, layer);
  return layer;
}

function drawLayerPrefix(context, layer, width, height, frameIndex, frameCount) {
  const cutoff = Math.min(width, layer.scale.x(frameIndex, frameCount) + 5);
  if (cutoff <= 0) return;
  context.drawImage(
    layer.canvas,
    0, 0, Math.round(cutoff * layer.ratio), layer.canvas.height,
    0, 0, cutoff, height,
  );
}

function drawPriceChart() {
  const { context, width, height } = prepareCanvas($("price-chart"));
  const frames = app.replay.frames;
  const padding = { left: 58, right: 14, top: 12, bottom: 22 };
  const layer = chartLayer("price", width, height, (layerContext) => {
    const scale = chartScale(frames.flatMap((frame) => [Number(frame.bid), Number(frame.ask), Number(frame.midpoint)]), width, height, padding);
    drawLine(layerContext, frames, "bid", scale, "#3978d4", 1.2);
    drawLine(layerContext, frames, "midpoint", scale, "#0d786d", 2.1);
    drawLine(layerContext, frames, "ask", scale, "#d47a35", 1.2);
    frames.forEach((frame, index) => frame.fills.forEach((fill) => {
      layerContext.beginPath(); layerContext.fillStyle = fill.side === "buy" ? "#3978d4" : "#c94c52";
      layerContext.arc(scale.x(index, frames.length), scale.y(Number(fill.price)), 3.5, 0, Math.PI * 2); layerContext.fill();
    }));
    return { scale };
  });
  const scale = layer.scale;
  drawGrid(context, width, height, padding, scale, (value) => value.toFixed(5));
  drawLayerPrefix(context, layer, width, height, app.frameIndex, frames.length);
  activeOrdersAt(app.frameIndex).forEach((order) => {
    const y = scale.y(Number(order.price));
    context.setLineDash([5, 4]); context.strokeStyle = order.side === "buy" ? "#3978d4" : "#c94c52";
    context.beginPath(); context.moveTo(width * 0.76, y); context.lineTo(width - padding.right, y); context.stroke(); context.setLineDash([]);
  });
}

function drawEquityChart() {
  const { context, width, height } = prepareCanvas($("equity-chart"));
  const initialValue = Number(app.replay.frames[0].marked_value);
  const frames = app.replay.frames;
  const padding = { left: 58, right: 14, top: 9, bottom: 17 };
  const layer = chartLayer("equity", width, height, (layerContext) => {
    const values = frames.flatMap((frame) => [
      Number(frame.marked_value),
      initialValue + Number(frame.hold_baseline_pnl),
    ]);
    const scale = chartScale(values, width, height, padding);
    const equityFrames = frames.map((frame) => ({
      marked_value: frame.marked_value,
      hold_value: initialValue + Number(frame.hold_baseline_pnl),
    }));
    drawLine(layerContext, equityFrames, "marked_value", scale, "#0d786d", 2);
    drawLine(layerContext, equityFrames, "hold_value", scale, "#9aa5a2", 1.4);
    return { scale };
  });
  const scale = layer.scale;
  drawGrid(context, width, height, padding, scale, (value) => value.toFixed(2));
  drawLayerPrefix(context, layer, width, height, app.frameIndex, frames.length);
}

function stopPlayback() {
  app.playing = false;
  if (app.animationId) cancelAnimationFrame(app.animationId);
  app.animationId = null; app.lastTick = 0;
  if ($("play-button")) $("play-button").textContent = "▶";
}

function playbackRate() { return { 1: 5, 10: 20, 50: 55, 100: 100 }[app.speed]; }
function playbackLoop(timestamp) {
  if (!app.playing) return;
  if (!app.lastTick || timestamp - app.lastTick >= 1000 / playbackRate()) {
    app.lastTick = timestamp;
    if (app.frameIndex + 1 >= app.replay.frames.length) { stopPlayback(); return; }
    renderFrame(app.frameIndex + 1);
  }
  app.animationId = requestAnimationFrame(playbackLoop);
}
function togglePlayback() {
  if (!app.replay) return;
  if (app.playing) { stopPlayback(); return; }
  if (app.frameIndex >= app.replay.frames.length - 1) renderFrame(0);
  app.playing = true; $("play-button").textContent = "Ⅱ";
  app.animationId = requestAnimationFrame(playbackLoop);
}

async function setMode(mode) {
  stopPlayback();
  app.requestGate.invalidate();
  app.mode = mode;
  document.querySelectorAll(".mode-button").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  $("lp-controls").hidden = mode !== "kuru_lp";
  $("mm-controls").hidden = mode !== "advanced_mm_bot";
  $("form-help").textContent = mode === "kuru_lp"
    ? "Run a new local Kuru LP simulation with this exact SDK-generated position."
    : "This is an explicitly labeled saved demo of the preserved cancel/replace market-making engine.";
  if (mode === "kuru_lp") {
    $("regime").disabled = false;
    await renderDistributionPreview();
    clearResult("No Kuru LP run yet");
  } else {
    $("regime").disabled = false;
    try { await loadDemoReplay(); } catch (error) {
      clearResult(error.message);
      $("impact-warning").hidden = false;
      $("impact-warning").textContent = error.message;
    }
  }
  setRunButtonState();
}

function drawHistoryRangeChart() {
  if (!app.tradeChart) return;
  const points = app.tradeChart.points;
  const canvas = $("history-range-chart");
  const { context, width, height } = prepareCanvas(canvas);
  if (!points.length) return;
  const first = app.tradeChart.trade_only_coverage.from_block;
  const last = app.tradeChart.trade_only_coverage.to_block;
  const prices = points.map((item) => Number(item.price));
  const low = Math.min(...prices);
  const high = Math.max(...prices);
  const x = (block) => 5 + (Number(block) - first) / Math.max(1, last - first) * (width - 10);
  const y = (value) => 5 + (high - Number(value)) / Math.max(1e-12, high - low) * (height - 18);
  context.fillStyle = "rgba(242,176,79,.08)";
  context.fillRect(0, 0, width, height);
  app.tradeChart.validated_l3_coverage.forEach((segment) => {
    context.fillStyle = "rgba(168,208,74,.17)";
    context.fillRect(x(segment.from_block), 0, Math.max(1, x(segment.to_block) - x(segment.from_block)), height);
  });
  context.strokeStyle = "#4fc1b2";
  context.lineWidth = 1.2;
  context.beginPath();
  points.forEach((item, index) => {
    const px = x(item.block); const py = y(item.price);
    if (index === 0) context.moveTo(px, py); else context.lineTo(px, py);
  });
  context.stroke();
  const selectedFrom = Number($("from-block").value || first);
  const selectedTo = Number($("to-block").value || last);
  context.fillStyle = "rgba(57,120,212,.13)";
  context.fillRect(x(selectedFrom), 0, Math.max(1, x(selectedTo) - x(selectedFrom)), height);
}

function setRangeInputs(fromBlock, toBlock) {
  const scope = app.tradeChart.trade_only_coverage;
  const start = Math.max(scope.from_block, Math.min(Number(fromBlock), Number(toBlock)));
  const end = Math.min(scope.to_block, Math.max(Number(fromBlock), Number(toBlock)));
  $("from-block").value = String(start);
  $("to-block").value = String(end);
  // Native range controls can lose unit precision near block 109 million.
  // Store small offsets in the sliders and keep absolute blocks in number inputs.
  $("range-start-slider").value = String(start - scope.from_block);
  $("range-end-slider").value = String(end - scope.from_block);
  const span = Math.max(1, scope.to_block - scope.from_block);
  const startPercent = (start - scope.from_block) / span * 100;
  const endPercent = (end - scope.from_block) / span * 100;
  $("range-start-slider").parentElement.style.setProperty("--range-start-percent", `${startPercent}%`);
  $("range-start-slider").parentElement.style.setProperty("--range-end-percent", `${endPercent}%`);
  drawHistoryRangeChart();
}

async function refreshRangeMetadata() {
  const fromBlock = Number($("from-block").value);
  const toBlock = Number($("to-block").value);
  const revision = ++app.rangeRequestRevision;
  app.rangeRequestController?.abort();
  const controller = new AbortController();
  app.rangeRequestController = controller;
  app.rangeMetadata = null;
  $("range-coverage-status").textContent = "Checking L3 coverage";
  $("range-coverage-status").className = "";
  setRunButtonState();
  const query = new URLSearchParams({
    market: app.marketAddress,
    from_block: String(fromBlock),
    to_block: String(toBlock),
  });
  let response;
  try {
    response = await fetch(`/api/range-metadata?${query}`, { signal: controller.signal });
  } catch (error) {
    if (error.name === "AbortError") return;
    throw error;
  }
  const payload = await response.json().catch(() => ({}));
  if (
    revision !== app.rangeRequestRevision
    || Number($("from-block").value) !== fromBlock
    || Number($("to-block").value) !== toBlock
  ) return;
  if (!response.ok) {
    const error = payload.error;
    const message = error && typeof error === "object" ? error.message : error;
    $("range-coverage-status").textContent = "Not backtestable";
    $("range-coverage-status").className = "unavailable";
    $("history-scope-note").textContent = message || "This range has no continuous validated L3 coverage.";
    app.generatedGrid = null;
    app.gridValidation = null;
    setRunButtonState();
    return;
  }
  app.rangeMetadata = payload;
  $("range-coverage-status").textContent = "Validated L3";
  $("range-coverage-status").className = "available";
  $("history-scope-note").textContent = `${Number(payload.event_count).toLocaleString()} local events · checkpoint ${Number(payload.checkpoint_block).toLocaleString()} · no network query`;
  if (app.mode === "kuru_lp") await renderDistributionPreview();
}

async function selectGoldenPreset() {
  const preset = app.intervalMetadata[$("regime").value];
  setRangeInputs(preset.from_block, preset.to_block);
  markConfigurationChanged();
  await refreshRangeMetadata();
}

async function initialize() {
  try {
    const [mmResponse, intervalResponse, chartResponse] = await Promise.all([
      fetch(`${DATA_ROOT}/catalog.json`), fetch("/api/intervals"), fetch(`${DATA_ROOT}/trade_history_chart.json`),
    ]);
    if (!mmResponse.ok) throw new Error("The Advanced MM demo catalog could not be loaded.");
    if (!intervalResponse.ok) throw new Error("The local backtest API could not load interval metadata.");
    if (!chartResponse.ok) throw new Error("The local Trade history chart could not be loaded.");
    app.mmCatalog = await mmResponse.json();
    const metadata = await intervalResponse.json();
    app.marketAddress = metadata.market;
    app.intervalMetadata = Object.fromEntries(metadata.intervals.map((item) => [item.name, item]));
    app.tradeChart = await chartResponse.json();
    const scope = app.tradeChart.trade_only_coverage;
    [$("range-start-slider"), $("range-end-slider")].forEach((slider) => {
      slider.min = "0";
      slider.max = String(scope.to_block - scope.from_block);
      slider.step = "1";
    });
    const initial = app.intervalMetadata[$("regime").value];
    setRangeInputs(initial.from_block, initial.to_block);
    await refreshRangeMetadata();
    await setMode("kuru_lp");
  } catch (error) {
    $("status").textContent = error.message;
    $("impact-warning").hidden = false;
    $("impact-warning").textContent = "Start a local HTTP server from the project root. Direct file access cannot load replay data.";
  }
}

$("backtest-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (app.mode === "kuru_lp") {
    await runKuruBacktest();
  } else {
    try { await loadDemoReplay(); } catch (error) {
      clearResult(error.message);
      $("impact-warning").hidden = false;
      $("impact-warning").textContent = error.message;
    }
  }
});
$("cancel-backtest").addEventListener("click", async () => {
  await cancelCurrentJob();
  app.running = false;
  clearResult("Backtest canceled.");
  setRunButtonState();
});
document.querySelectorAll(".mode-button").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
["liquidity-shape", "lower-range-percent", "upper-range-percent", "minimum-price", "maximum-price", "flip-spread"].forEach((id) => $(id).addEventListener("input", () => {
  markConfigurationChanged(); renderDistributionPreview();
}));
$("absolute-range-mode").addEventListener("change", () => {
  $("absolute-range-fields").hidden = !$("absolute-range-mode").checked;
  $("lower-range-percent").disabled = $("absolute-range-mode").checked;
  $("upper-range-percent").disabled = $("absolute-range-mode").checked;
  markConfigurationChanged();
  renderDistributionPreview();
});
$("mon-amount").addEventListener("input", () => {
  app.amountDriver = "base"; app.lastEditedToken = "base"; markConfigurationChanged(); renderDistributionPreview();
});
$("usdc-amount").addEventListener("input", () => {
  app.amountDriver = "quote"; app.lastEditedToken = "quote"; markConfigurationChanged(); renderDistributionPreview();
});
$("regime").addEventListener("change", () => {
  if (app.mode === "kuru_lp") selectGoldenPreset().catch((error) => clearResult(error.message));
  else loadDemoReplay().catch((error) => clearResult(error.message));
});
[$("range-start-slider"), $("range-end-slider")].forEach((slider) => {
  slider.addEventListener("input", () => {
    const first = app.tradeChart.trade_only_coverage.from_block;
    setRangeInputs(
      first + Number($("range-start-slider").value),
      first + Number($("range-end-slider").value),
    );
    markConfigurationChanged();
  });
  slider.addEventListener("change", () => refreshRangeMetadata().catch((error) => clearResult(error.message)));
});
[$("from-block"), $("to-block")].forEach((input) => input.addEventListener("input", () => {
  app.rangeMetadata = null;
  markConfigurationChanged();
  clearTimeout(app.rangeRefreshTimer);
  app.rangeRefreshTimer = setTimeout(() => {
    setRangeInputs($("from-block").value, $("to-block").value);
    refreshRangeMetadata().catch((error) => clearResult(error.message));
  }, 400);
}));
$("scenario").addEventListener("change", () => {
  if (app.mode === "kuru_lp") markConfigurationChanged();
  else loadDemoReplay().catch((error) => clearResult(error.message));
});
$("auto-fill-amounts").addEventListener("click", async () => {
  markConfigurationChanged();
  app.amountDriver = app.lastEditedToken;
  await renderDistributionPreview();
  if (!app.generatedGrid) return;
  if (app.amountDriver === "base") {
    $("usdc-amount").value = KuruSdkAdapter.rawToDecimal(app.generatedGrid.requiredUsdcRaw, 6);
  } else {
    $("mon-amount").value = KuruSdkAdapter.rawToDecimal(app.generatedGrid.requiredMonRaw, 18);
  }
  await renderDistributionPreview();
});
$("use-minimum-amounts").addEventListener("click", async () => {
  if (!app.minimumViablePosition) return;
  const minimum = app.minimumViablePosition;
  markConfigurationChanged();
  $("mon-amount").value = KuruSdkAdapter.rawToDecimal(minimum.minimumMonAmountRaw, 18);
  $("usdc-amount").value = KuruSdkAdapter.rawToDecimal(minimum.minimumUsdcAmountRaw, 6);
  app.amountDriver = minimum.driver;
  app.lastEditedToken = minimum.driver;
  await renderDistributionPreview();
});
$("play-button").addEventListener("click", togglePlayback);
$("scrubber").addEventListener("input", (event) => { stopPlayback(); renderFrame(Number(event.target.value)); });
document.querySelectorAll(".speed-button").forEach((button) => button.addEventListener("click", () => {
  app.speed = Number(button.dataset.speed);
  document.querySelectorAll(".speed-button").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
}));
window.addEventListener("resize", () => {
  drawHistoryRangeChart();
  renderDistributionPreview({ searchMinimum: false });
  if (app.replay) renderFrame(app.frameIndex);
});

initialize();
