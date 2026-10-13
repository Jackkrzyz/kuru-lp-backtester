"use strict";

const app = {
  mode: "kuru_lp",
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
  normalizedBounds: null,
  running: false,
  amountDriver: "both",
  previewRevision: 0,
  requestGate: RunRequestGate.create(),
  orderState: new Map(),
  orderStateFrame: -1,
  currentJob: null,
  requestController: null,
  frontendTimings: null,
  chartCache: new Map(),
  priceSelection: null,
  priceSelectionKey: null,
  pricePreviewTimer: null,
  workflow: null,
};

const $ = (id) => document.getElementById(id);
const money = (value, digits = 4) => `${Number(value).toFixed(digits)} USDC`;
const quantity = (value, digits = 2) => `${Number(value).toFixed(digits)} MON`;
const price = (value) => (value == null ? "—" : Number(value).toFixed(6));
const percent = (value) => `${Number(value).toFixed(2)}%`;

function currentMetadata() { return app.rangeMetadata; }

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
  const lowerOffset = $("lower-range-percent").value;
  const upperOffset = $("upper-range-percent").value;
  const absoluteMode = $("absolute-range-mode").checked || Number(lowerOffset) > 0 || Number(upperOffset) < 0;
  const bounds = absoluteMode
    ? KuruSdkAdapter.absolutePriceBounds($("minimum-price").value, $("maximum-price").value, metadata.tick_size)
    : KuruSdkAdapter.percentagePriceBounds(metadata.reference_price_raw, lowerOffset.replace(/^-/, ""), upperOffset.replace(/^\+/, ""), metadata.tick_size);
  const minimumPrice = KuruSdkAdapter.rawToDecimal(bounds.minimumPriceRaw, 8);
  const maximumPrice = KuruSdkAdapter.rawToDecimal(bounds.maximumPriceRaw, 8);
  app.normalizedBounds = {
    rangeMode: absoluteMode ? "absolute" : "percentage",
    lowerRangePercent: absoluteMode ? null : lowerOffset.replace(/^-/, ""),
    upperRangePercent: absoluteMode ? null : upperOffset.replace(/^\+/, ""),
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
  $("computed-range").textContent = `Period start ${formatRaw(metadata.reference_price_raw, 8, 8)} USDC · Tick-rounded range ${minimumPrice}–${maximumPrice}`;
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
  const height = Math.max(40, rect.height);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return { context, width, height };
}

function syncPriceSelection() {
  const metadata = currentMetadata();
  const bounds = app.normalizedBounds;
  const key = `${metadata.from_block}:${metadata.to_block}:${metadata.reference_price_raw}:${metadata.tick_size}`;
  if (!app.priceSelection || app.priceSelectionKey !== key) {
    app.priceSelection = new PriceRange.Selection(metadata.reference_price_raw, metadata.tick_size, bounds.minimumPriceRaw, bounds.maximumPriceRaw);
    app.priceSelectionKey = key;
  } else app.priceSelection.setBounds(bounds.minimumPriceRaw, bounds.maximumPriceRaw);
  if (bounds.rangeMode === "absolute") {
    $("lower-range-percent").value = (Number(bounds.minimumPriceRaw - bounds.referencePriceRaw) / Number(bounds.referencePriceRaw) * 100).toFixed(6);
    $("upper-range-percent").value = (Number(bounds.maximumPriceRaw - bounds.referencePriceRaw) / Number(bounds.referencePriceRaw) * 100).toFixed(6);
  }
  renderPriceHandles();
}

function renderPriceHandles() {
  const selection = app.priceSelection;
  if (!selection) return;
  const span = selection.domainMaximum - selection.domainMinimum;
  [$("price-min-slider"), $("price-max-slider")].forEach((slider) => {
    slider.min = "0";
    slider.max = String(span);
    slider.step = String(selection.tick);
  });
  $("price-min-slider").value = String(selection.minimum - selection.domainMinimum);
  $("price-max-slider").value = String(selection.maximum - selection.domainMinimum);
  const tracks = $("price-min-slider").parentElement;
  tracks.style.setProperty("--range-start-percent", `${selection.fraction(selection.minimum) * 100}%`);
  tracks.style.setProperty("--range-end-percent", `${selection.fraction(selection.maximum) * 100}%`);
  $("range-center-price").value = KuruSdkAdapter.rawToDecimal(selection.center, 8);
  const marker = $("price-center-marker");
  marker.style.left = `${selection.fraction(selection.center) * 100}%`;
  marker.setAttribute("aria-valuemin", KuruSdkAdapter.rawToDecimal(selection.domainMinimum, 8));
  marker.setAttribute("aria-valuemax", KuruSdkAdapter.rawToDecimal(selection.domainMaximum, 8));
  marker.setAttribute("aria-valuenow", KuruSdkAdapter.rawToDecimal(selection.center, 8));
  marker.setAttribute("aria-valuetext", `Range center ${KuruSdkAdapter.rawToDecimal(selection.center, 8)} USDC. Period start ${KuruSdkAdapter.rawToDecimal(selection.reference, 8)} USDC.`);
  $("reset-price").disabled = app.running;
}

function drawDistribution(grid, validation) {
  if (!app.priceSelection) return;
  const selection = app.priceSelection;
  // Render the canonical SDK grid, sorted across both sides. Height is quote
  // notional for both MON asks and USDC bids; never a separate shape formula.
  const rows = [...grid.orders].sort((a, b) => a.price < b.price ? -1 : a.price > b.price ? 1 : 0);
  const maxHeight = Math.max(1, ...rows.map((row) => Number(row.quoteNotionalRaw)));
  const invalid = new Set(validation.invalidOrders.map((row) => `${row.side}:${row.priceRaw}`));
  ["distribution-chart", "range-depth-chart"].forEach((id) => {
    const { context, width, height } = prepareCanvas($(id));
    const overview = id === "range-depth-chart";
    const baseline = height - 22;
    const plotHeight = baseline - 12;
    const x = (raw) => selection.fraction(raw) * width;
    const distinct = [...new Set(rows.map((row) => String(row.price)))];
    const gaps = distinct.slice(1).map((raw, index) => x(raw) - x(distinct[index])).filter((gap) => gap > 0);
    const barWidth = Math.max(1, Math.min(18, gaps.length ? Math.min(...gaps) * 0.86 : 12));
    context.fillStyle = overview ? "rgba(255,135,74,.045)" : "rgba(255,255,255,.025)";
    context.fillRect(x(selection.minimum), 0, x(selection.maximum) - x(selection.minimum), baseline);
    rows.forEach((row) => {
      const barHeight = Number(row.quoteNotionalRaw) / maxHeight * plotHeight;
      context.fillStyle = invalid.has(`${row.side}:${row.price}`) ? "#ed8d8d" : overview ? "rgba(230,218,205,.18)" : row.side === "buy" ? "#8894ff" : "#f2aa79";
      context.fillRect(x(row.price) - barWidth / 2, baseline - barHeight, barWidth, Math.max(1, barHeight));
    });
    context.strokeStyle = "rgba(240,226,213,.35)";
    context.setLineDash([3, 4]);
    context.beginPath(); context.moveTo(x(selection.reference), 0); context.lineTo(x(selection.reference), baseline); context.stroke();
    context.setLineDash([]);
    context.fillStyle = "#a3978d";
    context.font = "9px Inter, system-ui, sans-serif";
    const tickCount = Math.max(2, Math.min(6, Math.floor(width / 95)));
    for (let index = 0; index <= tickCount; index += 1) {
      const raw = selection.domainMinimum + (selection.domainMaximum - selection.domainMinimum) * BigInt(index) / BigInt(tickCount);
      context.textAlign = index === 0 ? "left" : index === tickCount ? "right" : "center";
      context.fillText(KuruSdkAdapter.rawToDecimal(raw, 8), width * index / tickCount, height - 5);
    }
  });
}

function queuePricePreview({ final = false } = {}) {
  clearTimeout(app.pricePreviewTimer);
  if (final) renderDistributionPreview();
  else app.pricePreviewTimer = setTimeout(() => renderDistributionPreview(), 120);
}

function useSelectionBounds({ final = false } = {}) {
  const selection = app.priceSelection;
  if (!selection) return;
  $("absolute-range-mode").checked = true;
  $("minimum-price").value = KuruSdkAdapter.rawToDecimal(selection.minimum, 8);
  $("maximum-price").value = KuruSdkAdapter.rawToDecimal(selection.maximum, 8);
  $("lower-range-percent").value = ((Number(selection.minimum - selection.reference) / Number(selection.reference)) * 100).toFixed(6);
  $("upper-range-percent").value = ((Number(selection.maximum - selection.reference) / Number(selection.reference)) * 100).toFixed(6);
  markConfigurationChanged();
  renderPriceHandles();
  queuePricePreview({ final });
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
  $("run-button-label").textContent = app.loadingSaved ? "Loading results…" : app.running ? "Running backtest…" : "Run backtest";
  if ($("load-saved-run")) $("load-saved-run").disabled = app.running || !$("saved-run-select").value;
  const ready = Boolean(app.rangeMetadata)
    && Number(app.rangeMetadata.from_block) === Number($("from-block").value)
    && Number(app.rangeMetadata.to_block) === Number($("to-block").value);
  $("timeframe-next").disabled = !ready || app.running;
  document.querySelectorAll('[data-step="strategy"]').forEach((item) => { item.disabled = !ready || app.running; });
  document.querySelectorAll('[data-step="results"]').forEach((item) => { item.disabled = !app.replay || app.running; });
  document.querySelectorAll('[data-navigate="timeframe"]').forEach((item) => { item.disabled = app.running; });
  $("reset-price").disabled = app.running || !app.priceSelection;
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

async function renderDistributionPreview() {
  const revision = ++app.previewRevision;
  try {
    const input = sdkInput();
    const distribution = await KuruSdkAdapter.generateOrderGrid(input);
    if (revision !== app.previewRevision) return;
    app.generatedGrid = distribution;
    const validation = KuruSdkAdapter.validateOrderGrid(distribution, input.minSize);
    syncPriceSelection();
    drawDistribution(distribution, validation);
    const rows = distribution.orders;
    const bidCount = rows.filter((item) => item.side === "buy").length;
    const askCount = rows.length - bidCount;
    $("preview-count").textContent = `${bidCount} bids · ${askCount} asks`;
    $("position-order-count").textContent = rows.length.toLocaleString();
    $("preview-detail").textContent = `${bidCount} bids · ${askCount} asks · bar height in USDC · faint line marks the period start${validation.invalidOrderCount ? ` · ${validation.invalidOrderCount} undersized orders in red` : ""}`;
    $("preview-detail").classList.toggle("warning", validation.invalidOrderCount > 0);
    const shortages = renderAmountStatus(distribution);
    renderGridValidation(validation, shortages);
    const minimum = await KuruSdkAdapter.minimumDepositSummary(input, distribution);
    if (revision !== app.previewRevision) return;
    const enteredMon = KuruSdkAdapter.decimalToRaw(input.monAmount, 18);
    const enteredUsdc = KuruSdkAdapter.decimalToRaw(input.usdcAmount, 6);
    const monGap = minimum.minimumMonRaw > enteredMon ? minimum.minimumMonRaw - enteredMon : 0n;
    const usdcGap = minimum.minimumUsdcRaw > enteredUsdc ? minimum.minimumUsdcRaw - enteredUsdc : 0n;
    $("minimum-mon").textContent = `SDK minimum ${formatRaw(minimum.minimumMonRaw, 18, 12)} MON${monGap ? ` · Add at least ${formatRaw(monGap, 18, 12)} MON` : ""}`;
    $("minimum-usdc").textContent = `SDK minimum ${formatRaw(minimum.minimumUsdcRaw, 6, 6)} USDC${usdcGap ? ` · Add at least ${formatRaw(usdcGap, 6, 6)} USDC` : ""}`;
    $("minimum-viable-position").textContent = "Official SDK minimums. Tick rounding can require more. The exact order-grid checks determine whether this position can run.";
  } catch (error) {
    if (revision !== app.previewRevision) return;
    app.generatedGrid = null;
    app.gridValidation = null;
    ["distribution-chart", "range-depth-chart"].forEach((id) => prepareCanvas($(id)));
    $("preview-count").textContent = "Invalid setup";
    $("position-order-count").textContent = "—";
    $("preview-detail").textContent = error.message;
    $("preview-detail").classList.add("warning");
    $("deployable-status").textContent = "No";
    $("deployable-status").classList.add("invalid");
    $("minimum-viable-position").textContent = "Minimum viable position unavailable";
    $("minimum-mon").textContent = "Minimum MON —";
    $("minimum-usdc").textContent = "Minimum USDC —";
    setRunButtonState();
  }
}


function syncReplayVisibility() {
  const available = Boolean(app.replay?.frames?.length);
  $("step-results").hidden = !available;
  $("backtest-replay").hidden = !available;
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
  $("sharpe-ratio").textContent = "—";
  $("sharpe-detail").textContent = "";
  $("status").textContent = message;
  $("impact-warning").hidden = true;
  if (app.mode === "kuru_lp") {
    const shape = $("liquidity-shape").value.replace("bid_ask", "Bid-Ask").replace(/^./, (value) => value.toUpperCase());
    $("engine-label").textContent = "Kuru LP engine";
    $("run-title").textContent = `MON / USDC · blocks ${$("from-block").value}–${$("to-block").value}`;
    $("scenario-badge").textContent = `${shape} · Kuru LP`;
    $("scenario-badge").classList.remove("caution");
  }
  window.LabChart?.clear();
  syncReplayVisibility();
}

function markConfigurationChanged() {
  app.requestGate.change();
  app.workflow?.invalidate();
  app.previewRevision += 1;
  cancelCurrentJob().catch(() => {});
  app.running = false;
  app.loadingSaved = false;
  app.generatedGrid = null;
  app.gridValidation = null;
  $("minimum-mon").textContent = "Minimum MON —";
  $("minimum-usdc").textContent = "Minimum USDC —";
  $("job-progress").hidden = true;
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
  if (!app.currentJob) $("cancel-backtest").hidden = true;
}

async function pollBacktestJob(job, token) {
  while (app.currentJob === job && app.requestGate.accepts(token)) {
    const response = await fetch(`/api/backtests/${job.runId}`, { headers: jobHeaders(job) });
    const status = await response.json().catch(() => ({}));
    if (app.currentJob !== job || !app.requestGate.accepts(token)) {
      throw new Error("Backtest request was superseded.");
    }
    if (!response.ok) throw new Error(status.error || `Job status failed with HTTP ${response.status}.`);
    window.LabJobs?.backtest(job.runId, status);
    const processed = Number(status.processed_events || 0).toLocaleString();
    const total = status.total_events == null ? "?" : Number(status.total_events).toLocaleString();
    const percentComplete = Number(status.percent_complete || 0).toFixed(1);
    $("status").textContent = `${status.stage} · ${processed}/${total} events · ${percentComplete}%`;
    $("job-progress").style.setProperty("--job-progress", `${Math.min(100, Math.max(0, Number(status.percent_complete || 0)))}%`);
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
    order_grid: KuruSdkAdapter.serializeOrderGrid(app.generatedGrid),
  };
}

function applyKuruResponse(response) {
  if (!Array.isArray(response.compact_timeline) || !response.compact_timeline.length) {
    throw new Error("This backtest has no replay frames.");
  }
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
  syncReplayVisibility();
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

async function refreshSavedBacktests() {
  const response = await fetch("/api/saved-backtests", { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Cannot read saved backtests.");
  const select = $("saved-run-select"), previous = select.value;
  select.replaceChildren();
  const blank = document.createElement("option");
  blank.value = ""; blank.textContent = payload.runs.length ? "Choose a completed run" : "No completed runs saved";
  select.append(blank);
  for (const run of payload.runs) {
    const option = document.createElement("option"), config = run.configuration;
    option.value = run.run_id;
    option.textContent = `${config.shape} · ${config.from_block ?? run.interval?.from_block ?? config.interval}–${config.to_block ?? run.interval?.to_block ?? ""} · ${signedMoney(run.results.strategy_pnl)} · ${run.run_id}`;
    select.append(option);
  }
  select.value = payload.runs.some(run => run.run_id === previous) ? previous : "";
  $("saved-run-status").textContent = `${payload.runs.length} saved runs${payload.skipped_invalid ? ` · ${payload.skipped_invalid} incomplete or invalid runs hidden` : ""}`;
  setRunButtonState();
}

async function loadSavedBacktest() {
  const runId = $("saved-run-select").value;
  if (!runId || app.running) return;
  await cancelCurrentJob();
  stopPlayback();
  const token = app.requestGate.begin();
  clearResult("Loading saved results…");
  app.running = true; app.loadingSaved = true; setRunButtonState();
  try {
    const response = await fetch(`/api/saved-backtests/${encodeURIComponent(runId)}`, { cache: "no-store" });
    const result = await response.json();
    if (!app.requestGate.accepts(token)) return;
    if (!response.ok) throw new Error(result.error || "Cannot load this saved run.");
    const config = result.normalized_configuration;
    app.marketAddress = config.market; app.amountDriver = config.amount_driver || "both";
    setRangeInputs(config.from_block ?? result.interval.from_block, config.to_block ?? result.interval.to_block);
    for (const [id, key] of [["liquidity-shape", "shape"], ["minimum-price", "minimum_price"],
      ["maximum-price", "maximum_price"], ["flip-spread", "fee_percent"], ["mon-amount", "mon_amount"], ["usdc-amount", "usdc_amount"]]) $(id).value = config[key];
    $("absolute-range-mode").checked = true;
    await refreshRangeMetadata();
    if (!app.requestGate.accepts(token)) return;
    applyKuruResponse(result);
    $("saved-run-status").textContent = `Loaded ${result.run_id}. No simulation was started.`;
    $("status").textContent = `Saved results · ${result.run_id}`;
    await app.workflow.show("results");
  } catch (error) {
    if (app.requestGate.accepts(token)) {
      clearResult(error.message); $("saved-run-status").textContent = error.message;
    }
  } finally {
    if (app.requestGate.accepts(token)) {
      app.running = false; app.loadingSaved = false; setRunButtonState();
    }
  }
}

async function runKuruBacktest() {
  await cancelCurrentJob();
  stopPlayback();
  const button = $("backtest-form").querySelector(".run-button");
  const token = app.requestGate.begin();
  clearResult("Running backtest…");
  app.running = true;
  $("job-progress").hidden = false;
  setRunButtonState();
  try {
    await renderDistributionPreview();
    if (!app.requestGate.accepts(token)) return;
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
    if (!app.requestGate.accepts(token)) {
      // Cancel only the stale request's returned job. Never overwrite or
      // cancel a newer job which is already attached to the form.
      await fetch(`/api/backtests/${job.runId}`, { method: "DELETE", headers: jobHeaders(job) }).catch(() => {});
      return;
    }
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
    refreshSavedBacktests().catch(error => { $("saved-run-status").textContent = error.message; });
    app.frontendTimings = {
      total_request_seconds: (parsedAt - requestStarted) / 1000,
      result_fetch_seconds: (fetchedAt - resultStarted) / 1000,
      timeline_parse_seconds: (parsedAt - parseStarted) / 1000,
    };
    app.currentJob = null;
    $("cancel-backtest").hidden = true;
    $("status").textContent = "New backtest ready";
    if (app.workflow) await app.workflow.show("results");
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
      $("job-progress").hidden = true;
    }
  }
}

function updateRunHeader() {
  const replay = app.replay;
  $("engine-label").textContent = "Kuru LP engine";
  const config = replay.config || {};
  $("run-title").textContent = config.from_block != null
    ? `MON / USDC · blocks ${config.from_block}–${config.to_block}`
    : `MON / USDC · ${replay.interval}`;
  const badge = $("scenario-badge");
  const shape = replay.config.shape ? replay.config.shape.replace("bid_ask", "Bid-Ask").replace(/^./, (value) => value.toUpperCase()) : "";
  badge.textContent = `${shape} · Kuru LP`;
  badge.classList.toggle("caution", replay.results.transaction_impact_warning);
  const warning = $("impact-warning");
  if (replay.results.transaction_impact_warning) {
    warning.hidden = false;
    warning.textContent = `Historical-data limitation: this position filled ${Number(replay.results.maximum_transaction_impact_percent).toFixed(2)}% of a historical transaction. Real trades could change with this added liquidity.`;
  } else {
    warning.hidden = true;
    warning.textContent = "";
  }
}

function updateResults() {
  const result = app.replay.results;
  const sharpe = ReplayAnalytics.sharpe(app.replay.frames);
  $("sharpe-ratio").textContent = sharpe.value == null ? "—" : sharpe.value.toFixed(3);
  $("sharpe-detail").textContent = `${sharpe.sample_count} one-minute returns · not annualized`;
  $("sharpe-ratio").title = sharpe.reason;
  setSignedMetric("strategy-pnl", result.strategy_pnl);
  setSignedMetric("hold-pnl", result.hold_pnl);
  setSignedMetric("excess-pnl", result.excess_pnl);
  $("drawdown").textContent = money(result.maximum_drawdown);
  $("turnover").textContent = money(result.turnover, 2);
  $("inventory-label").textContent = "Inventory composition";
  $("ending-inventory").textContent = `${Number(result.ending_mon_percent).toFixed(1)}% MON / ${Number(result.ending_usdc_percent).toFixed(1)}% USDC`;
  $("fill-counts").textContent = `${result.buy_fill_count} / ${result.sell_fill_count}`;
  $("order-actions").textContent = Number(result.total_order_actions).toLocaleString();
  $("actions-label").textContent = "Initial + flip orders";
  $("extra-one-label").textContent = "Earned flip spread";
  $("extra-one").textContent = money(result.earned_flip_spread, 6);
  $("extra-two-label").textContent = "Time in range";
  $("extra-two").textContent = percent(result.time_in_range_percent);
  $("extra-three-label").textContent = "Capital utilization";
  $("extra-three").textContent = percent(result.capital_utilization_percent);
  $("break-even-label").textContent = "Required MON / USDC ratio";
  $("break-even-cost").textContent = `${Number(result.required_mon_usdc_ratio).toFixed(4)} MON per USDC`;
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
  $("frame-time").textContent = `${new Date(frame.timestamp).toISOString().replace("T", " ")} · Block ${Number(frame.block).toLocaleString()} · Mid ${price(frame.midpoint)}`;
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

// Restore the original cached Canvas replay renderer. The history chart is
// independent; its candle period must not collapse the replay into one point.
function chartScale(values, width, height, padding) {
  const valid = values.filter(Number.isFinite);
  if (!valid.length) valid.push(0, 1);
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

function replayTimeScale(frames, scale) {
  const start = Date.parse(frames[0].timestamp), end = Date.parse(frames.at(-1).timestamp);
  if (!Number.isFinite(start) || !Number.isFinite(end)) throw new Error("Replay timestamps are missing.");
  const width = scale.x(frames.length - 1, frames.length) - scale.x(0, frames.length);
  const left = scale.x(0, frames.length);
  return {...scale, timeX: stamp => left + (stamp - start) / Math.max(1, end - start) * width,
    x: index => left + (Date.parse(frames[index].timestamp) - start) / Math.max(1, end - start) * width};
}

function drawGrid(context, width, height, padding, scale, formatter) {
  context.strokeStyle = "rgba(255,135,74,.07)";
  context.fillStyle = "#a3978d";
  context.font = "10px Inter, system-ui, sans-serif";
  for (let row = 0; row <= 4; row += 1) {
    const y = padding.top + row / 4 * (height - padding.top - padding.bottom);
    context.beginPath(); context.moveTo(padding.left, y); context.lineTo(width - padding.right, y); context.stroke();
    context.fillText(formatter(scale.maximum - row / 4 * (scale.maximum - scale.minimum)), 2, y + 3);
  }
}

function drawReplayDates(context, frames, scale, height) {
  const first=Date.parse(frames[0].timestamp),last=Date.parse(frames.at(-1).timestamp);
  context.fillStyle="#88929a";context.font="9px Inter, system-ui, sans-serif";
  for(let i=0;i<3;i++){
    const stamp=first+(last-first)*i/2;
    context.textAlign=i===0?"left":i===2?"right":"center";
    context.fillText(new Date(stamp).toISOString().slice(5,16).replace("T"," ")+" UTC",scale.timeX(stamp),height-4);
  }
  context.textAlign="left";
}

function drawLine(context, frames, key, scale, color, lineWidth = 1.5) {
  context.beginPath(); context.strokeStyle = color; context.lineWidth = lineWidth;
  let started = false;
  frames.forEach((frame, index) => {
    if (frame[key] == null) { started = false; return; }
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
    const prices = frames.flatMap(frame => [frame.bid, frame.ask, ...(frame.fills || []).map(fill => fill.price)]);
    const scale = replayTimeScale(frames, chartScale(prices.map(value => value == null ? NaN : Number(value)), width, height, padding));
    drawLine(layerContext, frames, "bid", scale, "#8894ff", 1.2);
    drawLine(layerContext, frames, "ask", scale, "#f2aa79", 1.2);
    return { scale };
  });
  const scale = layer.scale;
  drawGrid(context, width, height, padding, scale, (value) => value.toFixed(5));
  drawLayerPrefix(context, layer, width, height, app.frameIndex, frames.length);
  const now = Date.parse(frames[app.frameIndex].timestamp);
  for (const frame of frames.slice(0, app.frameIndex + 1)) for (const fill of frame.fills || []) {
    const stamp = Date.parse(fill.timestamp || frame.timestamp);
    if (!Number.isFinite(stamp) || stamp > now) continue;
    context.beginPath(); context.fillStyle = ["buy", "bid"].includes(fill.side) ? "#8894ff" : "#ed8d8d";
    context.arc(scale.timeX(stamp), scale.y(Number(fill.price)), 3.5, 0, Math.PI * 2); context.fill();
  }
  drawReplayDates(context, frames, scale, height);
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
    const scale = replayTimeScale(frames, chartScale(values, width, height, padding));
    const equityFrames = frames.map((frame) => ({
      timestamp: frame.timestamp,
      marked_value: frame.marked_value,
      hold_value: initialValue + Number(frame.hold_baseline_pnl),
    }));
    drawLine(layerContext, equityFrames, "marked_value", scale, "#ff874a", 2);
    drawLine(layerContext, equityFrames, "hold_value", scale, "#9aa5a2", 1.4);
    return { scale };
  });
  const scale = layer.scale;
  drawGrid(context, width, height, padding, scale, (value) => value.toFixed(2));
  drawLayerPrefix(context, layer, width, height, app.frameIndex, frames.length);
  drawReplayDates(context, frames, scale, height);
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
  $("form-help").textContent = "Run a local Kuru LP simulation with this exact SDK-generated position.";
    await renderDistributionPreview();
    clearResult("No Kuru LP run yet");
  setRunButtonState();
}

function drawHistoryRangeChart() {
  window.LabChart?.select(Number($("from-block").value), Number($("to-block").value));
}

function renderPeriodSummary() {
  const matches = app.rangeMetadata
    && Number(app.rangeMetadata.from_block) === Number($("from-block").value)
    && Number(app.rangeMetadata.to_block) === Number($("to-block").value);
  const metadata = matches ? app.rangeMetadata : null;
  const startingPrice = metadata?.reference_price ?? (metadata?.reference_price_raw != null ? KuruSdkAdapter.rawToDecimal(metadata.reference_price_raw, 8) : null);
  $("period-start-price").textContent = startingPrice != null ? `${startingPrice} USDC` : "Loading…";
  const statistics = metadata?.trade_price_statistics;
  for (const [id, key] of [["period-end-price", "ending_price"], ["period-high-price", "highest_price"], ["period-low-price", "lowest_price"]]) {
    $(id).textContent = statistics?.[key] != null ? `${statistics[key]} USDC` : statistics ? "No Trades" : metadata ? "Unavailable" : "—";
  }
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
  renderPeriodSummary();
  // Trade chart dates are approximate, not block-header timestamps.
  const nearest = (block) => app.tradeChart.points.reduce((best, point) => !best || Math.abs(point.block - block) < Math.abs(best.block - block) ? point : best, null);
  const dateText = (point) => point ? `≈ ${new Date(Number(point.timestamp) * 1000).toLocaleString("en-GB", { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} UTC` : "—";
  $("period-start-time").textContent = dateText(nearest(start));
  $("period-end-time").textContent = dateText(nearest(end));
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
  renderPeriodSummary();
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
    $("status").textContent = message || "This range has no continuous validated L3 coverage.";
    $("period-start-price").textContent = "Unavailable";
    app.generatedGrid = null;
    app.gridValidation = null;
    setRunButtonState();
    return;
  }
  app.rangeMetadata = payload;
  renderPeriodSummary();
  $("range-coverage-status").textContent = "Validated L3";
  $("range-coverage-status").className = "available";
  setRunButtonState();
  if (app.mode === "kuru_lp") await renderDistributionPreview();
}

async function initialize() {
  try {
    const response = await fetch("/api/intervals");
    if (!response.ok) throw new Error("Cannot load local market coverage.");
    const metadata = await response.json();
    app.marketAddress = metadata.market;
    app.intervalMetadata = metadata;
    const coverage = metadata.validated_l3_coverage;
    if (!coverage.length) throw new Error("No backtest-ready local range is available.");
    const first = coverage[0].from_block, last = coverage.at(-1).to_block;
    app.tradeChart = {trade_only_coverage:{from_block:first,to_block:last},points:[]};
    window.LabBacktestCoverage = coverage;
    setRangeInputs(first, last);
    await refreshRangeMetadata();
    await setMode("kuru_lp");
    PageLoading.ready("backtest");
  } catch(error) { PageLoading.error(error.message); $("status").textContent=error.message; }
}
window.addEventListener("chart-range-selected", event => {
  if(app.running || !app.tradeChart) return;
  app.rangeMetadata=null;
  // Chart selection is allowed outside backtest coverage; the backend decides
  // eligibility. Never clamp a selected missing range into a valid one.
  const {from_block,to_block}=event.detail;
  app.tradeChart.trade_only_coverage.from_block=Math.min(app.tradeChart.trade_only_coverage.from_block,from_block);
  app.tradeChart.trade_only_coverage.to_block=Math.max(app.tradeChart.trade_only_coverage.to_block,to_block);
  setRangeInputs(from_block,to_block);
  markConfigurationChanged();
  refreshRangeMetadata().catch(error=>clearResult(error.message));
});
window.addEventListener("market-candles", event => {
  if(!app.tradeChart)return;
  app.tradeChart.points=event.detail.candles.map(b=>({block:b.from_block,timestamp:b.time,price:b.close}));
  for(const range of event.detail.indexed_ranges||[]){
    app.tradeChart.trade_only_coverage.from_block=Math.min(app.tradeChart.trade_only_coverage.from_block,range.from_block);
    app.tradeChart.trade_only_coverage.to_block=Math.max(app.tradeChart.trade_only_coverage.to_block,range.to_block);
  }
  setRangeInputs($("from-block").value,$("to-block").value);
  if(app.replay)renderFrame(app.frameIndex);
});

$("backtest-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await runKuruBacktest();
});
$("cancel-backtest").addEventListener("click", async () => {
  await cancelCurrentJob();
  app.running = false;
  clearResult("Backtest canceled.");
  setRunButtonState();
});
["liquidity-shape", "flip-spread"].forEach((id) => $(id).addEventListener("input", () => {
  markConfigurationChanged(); queuePricePreview({ final: true });
}));
["minimum-price", "maximum-price"].forEach((id) => $(id).addEventListener("input", () => {
  $("absolute-range-mode").checked = true;
  markConfigurationChanged(); queuePricePreview({ final: true });
}));
["lower-range-percent", "upper-range-percent"].forEach((id) => $(id).addEventListener("input", () => {
  markConfigurationChanged();
  try {
    const metadata = currentMetadata();
    const bounds = PriceRange.offsetBounds(metadata.reference_price_raw, $("lower-range-percent").value, $("upper-range-percent").value, metadata.tick_size, KuruSdkAdapter.decimalToRaw);
    $("minimum-price").value = KuruSdkAdapter.rawToDecimal(bounds.minimumPriceRaw, 8);
    $("maximum-price").value = KuruSdkAdapter.rawToDecimal(bounds.maximumPriceRaw, 8);
    $("absolute-range-mode").checked = Number($("lower-range-percent").value) > 0 || Number($("upper-range-percent").value) < 0;
    queuePricePreview({ final: true });
  } catch (error) {
    ["distribution-chart", "range-depth-chart"].forEach((canvasId) => prepareCanvas($(canvasId)));
    $("preview-detail").textContent = error.message;
    $("preview-detail").classList.add("warning");
  }
}));
[$("price-min-slider"), $("price-max-slider")].forEach((slider, index) => {
  slider.addEventListener("input", () => {
    if (!app.priceSelection || app.running) return;
    app.priceSelection.moveEdge(index === 0 ? "minimum" : "maximum", app.priceSelection.domainMinimum + BigInt(slider.value));
    useSelectionBounds();
  });
  slider.addEventListener("change", () => queuePricePreview({ final: true }));
});
const priceMarker = $("price-center-marker");
let centerDrag = null;
priceMarker.addEventListener("pointerdown", (event) => {
  if (!app.priceSelection || app.running) return;
  event.preventDefault();
  centerDrag = { pointerId: event.pointerId, rect: $("distribution-chart").getBoundingClientRect() };
  priceMarker.setPointerCapture(event.pointerId);
});
priceMarker.addEventListener("pointermove", (event) => {
  if (!centerDrag || event.pointerId !== centerDrag.pointerId) return;
  const { rect } = centerDrag;
  app.priceSelection.moveCenter(app.priceSelection.priceAt((event.clientX - rect.left) / rect.width));
  useSelectionBounds();
});
const finishCenterDrag = (event) => {
  if (!centerDrag || event.pointerId !== centerDrag.pointerId) return;
  centerDrag = null;
  queuePricePreview({ final: true });
};
priceMarker.addEventListener("pointerup", finishCenterDrag);
priceMarker.addEventListener("pointercancel", finishCenterDrag);
priceMarker.addEventListener("keydown", (event) => {
  if (!app.priceSelection || app.running || !["ArrowLeft", "ArrowRight", "Home"].includes(event.key)) return;
  event.preventDefault();
  try {
    if (event.key === "Home") app.priceSelection.reset();
    else app.priceSelection.moveCenter(app.priceSelection.center + app.priceSelection.tick * BigInt(event.key === "ArrowLeft" ? -1 : 1) * BigInt(event.shiftKey ? 10 : 1));
    useSelectionBounds({ final: true });
  } catch (error) { $("status").textContent = error.message; }
});
$("range-center-price").addEventListener("change", () => {
  if (!app.priceSelection || app.running) return;
  try {
    app.priceSelection.moveCenter(KuruSdkAdapter.decimalToRaw($("range-center-price").value, 8));
    useSelectionBounds({ final: true });
  } catch (error) { $("status").textContent = error.message; }
});
$("reset-price").addEventListener("click", () => {
  if (!app.priceSelection || app.running) return;
  try { app.priceSelection.reset(); useSelectionBounds({ final: true }); }
  catch (error) { $("status").textContent = error.message; }
});
$("mon-amount").addEventListener("input", () => {
  markConfigurationChanged(); renderDistributionPreview();
});
$("usdc-amount").addEventListener("input", () => {
  markConfigurationChanged(); renderDistributionPreview();
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
  renderPeriodSummary();
  clearTimeout(app.rangeRefreshTimer);
  app.rangeRefreshTimer = setTimeout(() => {
    setRangeInputs($("from-block").value, $("to-block").value);
    refreshRangeMetadata().catch((error) => clearResult(error.message));
  }, 400);
}));
$("play-button").addEventListener("click", togglePlayback);
$("scrubber").addEventListener("input", (event) => { stopPlayback(); renderFrame(Number(event.target.value)); });
$("saved-run-select").addEventListener("change", setRunButtonState);
$("load-saved-run").addEventListener("click", loadSavedBacktest);
document.querySelectorAll(".speed-button").forEach((button) => button.addEventListener("click", () => {
  app.speed = Number(button.dataset.speed);
  document.querySelectorAll(".speed-button").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
}));
window.addEventListener("resize", () => {
  drawHistoryRangeChart();
  if (app.generatedGrid && app.gridValidation) drawDistribution(app.generatedGrid, app.gridValidation);
  if (app.replay) renderFrame(app.frameIndex);
});
window.addEventListener("page-ready", () => requestAnimationFrame(() => {
  if (app.generatedGrid && app.gridValidation) drawDistribution(app.generatedGrid, app.gridValidation);
}));

app.workflow = BacktestWorkflow.create({
  panels: { timeframe: $("step-timeframe"), strategy: $("step-strategy"), results: $("step-results") },
  steps: [...document.querySelectorAll(".step-button")],
  persistent: true,
  wait: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  reducedMotion: () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  onEnter: (step) => {
    if (step !== "results") stopPlayback();
    syncReplayVisibility();
    document.querySelector(".app-shell").dataset.stage = step;
    if (step === "timeframe") drawHistoryRangeChart();
    if (step === "strategy" && app.generatedGrid && app.gridValidation) drawDistribution(app.generatedGrid, app.gridValidation);
    if (step === "results" && app.replay) renderFrame(app.frameIndex);
    $(step === "results" ? "run-title" : `${step}-title`)?.focus({ preventScroll: true });
  },
});
document.querySelectorAll("[data-navigate]").forEach((button) => button.addEventListener("click", () => {
  if (app.running || button.disabled) return;
  app.workflow.show(button.dataset.navigate);
}));
$("timeframe-next").addEventListener("click", () => app.workflow.show("strategy"));
initialize();
refreshSavedBacktests().catch(error => { $("saved-run-status").textContent = error.message; });
