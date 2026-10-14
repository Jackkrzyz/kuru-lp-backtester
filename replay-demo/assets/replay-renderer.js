/* Copied from frontend/app.js by tools/export_replay_demo.py. */
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
  renderReplayFills();
  const chartStarted = performance.now();
  drawPriceChart();
  drawEquityChart();
  app.frontendTimings = app.frontendTimings || {};
  app.frontendTimings.last_chart_render_milliseconds = performance.now() - chartStarted;
}


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

// Keep executions separate from the historical quote path. Counterfactual LP
// executions can have a different price from the unchanged historical quotes.
function replayFillsThrough(index) {
  return app.replay.frames.slice(0, index + 1).flatMap(frame => (frame.fills || []).map(fill => ({
    ...fill, timestamp: fill.timestamp || frame.timestamp, block: fill.block ?? frame.block,
  })));
}

function renderReplayFills() {
  const target = $("replay-fills");
  if (!target) return;
  const fills = replayFillsThrough(app.frameIndex);
  target.replaceChildren();
  const count = document.createElement("p");
  count.textContent = `${fills.length} executions through this frame · Latest 12 shown · Recorded execution prices, not historical quotes`;
  target.append(count);
  for (const fill of fills.slice(-12).reverse()) {
    const row = document.createElement("div");
    row.className = "replay-fill-row";
    // textContent keeps imported recording values out of HTML markup.
    row.textContent = `${fill.timestamp} · Block ${fill.block} · ${fill.side} · ${fill.size} MON @ ${fill.price} USDC · ${fill.order_id}`;
    target.append(row);
  }
}

function drawPriceChart() {
  const { context, width, height } = prepareCanvas($("price-chart"));
  const frames = app.replay.frames;
  const padding = { left: 58, right: 14, top: 12, bottom: 58 };
  const layer = chartLayer("price", width, height, (layerContext) => {
    const prices = frames.flatMap(frame => [frame.bid, frame.ask]);
    const scale = replayTimeScale(frames, chartScale(prices.map(value => value == null ? NaN : Number(value)), width, height, padding));
    drawLine(layerContext, frames, "bid", scale, "#8894ff", 1.2);
    drawLine(layerContext, frames, "ask", scale, "#f2aa79", 1.2);
    return { scale };
  });
  const scale = layer.scale;
  drawGrid(context, width, height, padding, scale, (value) => value.toFixed(5));
  drawLayerPrefix(context, layer, width, height, app.frameIndex, frames.length);
  context.font = "10px Inter, system-ui, sans-serif";
  for (const [label, y, color] of [["Buy", height - 40, "#8894ff"], ["Sell", height - 25, "#ed8d8d"]]) {
    context.fillStyle = color; context.fillText(label, 8, y + 3);
  }
  for (const fill of replayFillsThrough(app.frameIndex)) {
    const stamp = Date.parse(fill.timestamp);
    if (!Number.isFinite(stamp)) continue;
    const buy = ["buy", "bid"].includes(fill.side);
    const y = height - (buy ? 40 : 25), x = scale.timeX(stamp);
    context.beginPath(); context.strokeStyle = buy ? "#8894ff" : "#ed8d8d";
    context.lineWidth = 2; context.moveTo(x, y - 4); context.lineTo(x, y + 4); context.stroke();
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
