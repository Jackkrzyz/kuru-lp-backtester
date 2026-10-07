"use strict";

const { PositionViewer } = require("@kuru-labs/kuru-sdk");

const SDK_PACKAGE = "@kuru-labs/kuru-sdk";
const SDK_VERSION = "0.0.95";
const SDK_GIT_HEAD = "c6a1e9eced12d634fa879ec46c52baff8a50f3c3";

function decimalToRaw(value, decimals) {
  const text = String(value).trim();
  if (!/^\d+(?:\.\d+)?$/.test(text)) throw new Error(`Invalid decimal value: ${value}`);
  const [whole, fraction = ""] = text.split(".");
  if (fraction.length > decimals && /[1-9]/.test(fraction.slice(decimals))) {
    throw new Error(`Value has more than ${decimals} decimal places.`);
  }
  return BigInt(whole) * 10n ** BigInt(decimals)
    + BigInt((fraction + "0".repeat(decimals)).slice(0, decimals) || "0");
}

function rawToDecimal(rawValue, decimals, minimumFractionDigits = 0) {
  const raw = BigInt(rawValue);
  const scale = 10n ** BigInt(decimals);
  const whole = raw / scale;
  let fraction = (raw % scale).toString().padStart(decimals, "0").replace(/0+$/, "");
  if (fraction.length < minimumFractionDigits) fraction = fraction.padEnd(minimumFractionDigits, "0");
  return fraction ? `${whole}.${fraction}` : `${whole}${minimumFractionDigits ? `.${"0".repeat(minimumFractionDigits)}` : ""}`;
}

// The installed SDK uses 100 pps per basis point. One percent is 10,000 pps.
function percentToPps(percentValue) {
  return decimalToRaw(percentValue, 4);
}

function ppsToPercent(ppsValue) {
  return rawToDecimal(BigInt(ppsValue), 4, 2);
}

function ceilDiv(value, divisor) {
  return value / divisor + (value % divisor === 0n ? 0n : 1n);
}

function percentagePriceBounds(referencePriceRaw, lowerPercent, upperPercent, tickSize) {
  const reference = BigInt(referencePriceRaw);
  const tick = BigInt(tickSize);
  const percentScale = 100n * 10n ** 6n;
  const lower = decimalToRaw(lowerPercent, 6);
  const upper = decimalToRaw(upperPercent, 6);
  if (lower >= percentScale) throw new Error("The lower range percentage must be less than 100%.");
  if (tick <= 0n) throw new Error("The market tick size must be positive.");
  const minimumTicks = reference * (percentScale - lower) / (percentScale * tick);
  const maximumTicks = ceilDiv(reference * (percentScale + upper), percentScale * tick);
  return {
    minimumPriceRaw: (minimumTicks > 0n ? minimumTicks : 1n) * tick,
    maximumPriceRaw: (maximumTicks > 0n ? maximumTicks : 1n) * tick,
  };
}

function absolutePriceBounds(minimumPrice, maximumPrice, tickSize) {
  const tick = BigInt(tickSize);
  const minimum = decimalToRaw(minimumPrice, 8);
  const maximum = decimalToRaw(maximumPrice, 8);
  if (tick <= 0n) throw new Error("The market tick size must be positive.");
  return {
    minimumPriceRaw: (minimum / tick || 1n) * tick,
    maximumPriceRaw: ceilDiv(maximum, tick) * tick,
  };
}

function requiredAmounts(orders, config) {
  const requiredMonRaw = orders
    .filter((order) => order.side === "sell")
    .reduce((sum, order) => sum + order.liquidity * 10n ** config.baseAssetDecimals / config.sizePrecision, 0n);
  const requiredUsdcRaw = orders
    .filter((order) => order.side === "buy")
    .reduce((sum, order) => sum + order.price * order.liquidity * 10n ** config.quoteAssetDecimals
      / (config.pricePrecision * config.sizePrecision), 0n);
  return { requiredMonRaw, requiredUsdcRaw };
}

async function generateOrderGrid(input) {
  const config = {
    minFeesPps: percentToPps(input.feePercent),
    startPrice: decimalToRaw(input.minimumPrice, 8),
    endPrice: decimalToRaw(input.maximumPrice, 8),
    bestAskPrice: BigInt(input.bestAskPriceRaw),
    pricePrecision: BigInt(input.pricePrecision),
    sizePrecision: BigInt(input.sizePrecision),
    quoteAssetDecimals: BigInt(input.quoteAssetDecimals),
    baseAssetDecimals: BigInt(input.baseAssetDecimals),
    tickSize: BigInt(input.tickSize),
    minSize: BigInt(input.minSize),
  };
  if (config.startPrice >= config.endPrice) throw new Error("The minimum price must be less than the maximum price.");
  const driver = ["base", "quote"].includes(input.driver) ? input.driver : "both";
  const quoteLiquidity = driver !== "base" ? decimalToRaw(input.usdcAmount, Number(config.quoteAssetDecimals)) : undefined;
  const baseLiquidity = driver !== "quote" ? decimalToRaw(input.monAmount, Number(config.baseAssetDecimals)) : undefined;
  const common = [
    config.minFeesPps, config.startPrice, config.endPrice, config.bestAskPrice,
    config.pricePrecision, config.sizePrecision, config.quoteAssetDecimals,
    config.baseAssetDecimals, config.tickSize, config.minSize,
  ];
  const method = {
    spot: "getSpotBatchLPDetails",
    curve: "getCurveBatchLPDetails",
    bid_ask: "getBidAskBatchLPDetails",
  }[input.shape];
  if (!method) throw new Error(`Unsupported liquidity shape: ${input.shape}`);
  const result = await PositionViewer[method](...common, quoteLiquidity, baseLiquidity);
  const batch = result.batchLPDetails;
  const orders = [
    ...batch.bids.map((item) => ({ side: "buy", ...item })),
    ...batch.asks.map((item) => ({ side: "sell", ...item })),
  ].map((item) => ({
    side: item.side,
    price: BigInt(item.price),
    flipPrice: BigInt(item.flipPrice),
    liquidity: BigInt(item.liquidity),
    quoteNotionalRaw: BigInt(item.price) * BigInt(item.liquidity) * 10n ** config.quoteAssetDecimals
      / (config.pricePrecision * config.sizePrecision),
  })).sort((left, right) => left.price < right.price ? -1 : left.price > right.price ? 1 : 0);
  if (!orders.some((item) => item.side === "buy") || !orders.some((item) => item.side === "sell")) {
    throw new Error("The range must contain at least one bid and one ask.");
  }
  const required = requiredAmounts(orders, config);
  return {
    schemaVersion: "1.0",
    source: { package: SDK_PACKAGE, version: SDK_VERSION, npmGitHead: SDK_GIT_HEAD },
    shape: input.shape,
    driver,
    feePercent: ppsToPercent(config.minFeesPps),
    feePps: config.minFeesPps,
    currentMarketPriceRaw: config.bestAskPrice,
    pricePrecision: config.pricePrecision,
    sizePrecision: config.sizePrecision,
    quoteAssetDecimals: config.quoteAssetDecimals,
    baseAssetDecimals: config.baseAssetDecimals,
    minimumPriceRaw: config.startPrice,
    maximumPriceRaw: config.endPrice,
    marketMinSizeRaw: config.minSize,
    minSizeError: Boolean(batch.minSizeError),
    orders,
    ...required,
  };
}

function validateOrderGrid(grid, minSizeRaw = grid.marketMinSizeRaw) {
  const minimum = BigInt(minSizeRaw);
  const invalidOrders = grid.orders.filter((order) => order.liquidity < minimum).map((order) => ({
    side: order.side,
    priceRaw: order.price,
    sizeRaw: order.liquidity,
  }));
  const smallestOrderSizeRaw = grid.orders.reduce(
    (smallest, order) => smallest === null || order.liquidity < smallest ? order.liquidity : smallest,
    null,
  );
  return {
    deployable: grid.orders.length > 0 && invalidOrders.length === 0,
    smallestOrderSizeRaw: smallestOrderSizeRaw || 0n,
    marketMinSizeRaw: minimum,
    invalidOrderCount: invalidOrders.length,
    offendingOrder: invalidOrders[0] || null,
    invalidOrders,
  };
}

async function findMinimumViablePosition(input, selectedDriver = input.driver) {
  const driver = selectedDriver === "base" ? "base" : "quote";
  const decimals = driver === "base" ? Number(input.baseAssetDecimals) : Number(input.quoteAssetDecimals);
  const field = driver === "base" ? "monAmount" : "usdcAmount";
  const withDriverAmount = (amountRaw) => ({
    ...input,
    driver,
    [field]: rawToDecimal(amountRaw, decimals),
  });
  const evaluate = async (amountRaw) => {
    const grid = await generateOrderGrid(withDriverAmount(amountRaw));
    return { grid, validation: validateOrderGrid(grid, input.minSize) };
  };

  let high = 1n;
  let passing = await evaluate(high);
  for (let step = 0; !passing.validation.deployable && step < 256; step += 1) {
    high *= 2n;
    passing = await evaluate(high);
  }
  if (!passing.validation.deployable) {
    throw new Error("A valid minimum position was not found inside the raw-integer search limit.");
  }
  let low = 0n;
  while (low + 1n < high) {
    const middle = (low + high) / 2n;
    const candidate = await evaluate(middle);
    if (candidate.validation.deployable) {
      high = middle;
      passing = candidate;
    } else {
      low = middle;
    }
  }
  const finalResult = await evaluate(high);
  if (!finalResult.validation.deployable) throw new Error("The final minimum position did not pass validation.");
  let oneRawUnitBelowPasses = false;
  if (high > 1n) oneRawUnitBelowPasses = (await evaluate(high - 1n)).validation.deployable;
  if (oneRawUnitBelowPasses) throw new Error("The raw-integer minimum search did not find the smallest passing amount.");
  return {
    driver,
    driverAmountRaw: high,
    minimumMonAmountRaw: driver === "base" ? high : finalResult.grid.requiredMonRaw,
    minimumUsdcAmountRaw: driver === "quote" ? high : finalResult.grid.requiredUsdcRaw,
    requiredMonRaw: finalResult.grid.requiredMonRaw,
    requiredUsdcRaw: finalResult.grid.requiredUsdcRaw,
    oneRawUnitBelowPasses,
    grid: finalResult.grid,
    validation: finalResult.validation,
  };
}

function serializeOrderGrid(grid) {
  return {
    schema_version: grid.schemaVersion,
    source: {
      package: grid.source.package,
      version: grid.source.version,
      npm_git_head: grid.source.npmGitHead,
    },
    shape: grid.shape,
    driver: grid.driver,
    fee_percent: grid.feePercent,
    fee_pps: grid.feePps.toString(),
    current_market_price_raw: grid.currentMarketPriceRaw.toString(),
    price_precision: grid.pricePrecision.toString(),
    size_precision: grid.sizePrecision.toString(),
    quote_asset_decimals: grid.quoteAssetDecimals.toString(),
    base_asset_decimals: grid.baseAssetDecimals.toString(),
    minimum_price_raw: grid.minimumPriceRaw.toString(),
    maximum_price_raw: grid.maximumPriceRaw.toString(),
    min_size_error: grid.minSizeError,
    required_mon_raw: grid.requiredMonRaw.toString(),
    required_usdc_raw: grid.requiredUsdcRaw.toString(),
    orders: grid.orders.map((order) => ({
      side: order.side,
      price_raw: order.price.toString(),
      flip_price_raw: order.flipPrice.toString(),
      size_raw: order.liquidity.toString(),
      quote_notional_raw: order.quoteNotionalRaw.toString(),
    })),
  };
}

module.exports = {
  SDK_PACKAGE,
  SDK_VERSION,
  SDK_GIT_HEAD,
  decimalToRaw,
  rawToDecimal,
  percentToPps,
  ppsToPercent,
  percentagePriceBounds,
  absolutePriceBounds,
  requiredAmounts,
  generateOrderGrid,
  validateOrderGrid,
  findMinimumViablePosition,
  serializeOrderGrid,
};
