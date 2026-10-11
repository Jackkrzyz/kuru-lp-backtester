"use strict";

const assert = require("assert");
const fixture = require("./fixtures/concentrated_liquidity_v0_0_95.json");
const adapter = require("./browser_adapter");
const { PositionViewer } = require("@kuru-labs/kuru-sdk");

const input = {
  minimumPrice: "0.0263",
  maximumPrice: "0.02795",
  feePercent: "0.30",
  monAmount: "2000",
  usdcAmount: "60",
  driver: "both",
  bestAskPriceRaw: fixture.inputs.bestAskPrice,
  pricePrecision: fixture.inputs.pricePrecision,
  sizePrecision: fixture.inputs.sizePrecision,
  quoteAssetDecimals: Number(fixture.inputs.quoteAssetDecimals),
  baseAssetDecimals: Number(fixture.inputs.baseAssetDecimals),
  tickSize: fixture.inputs.tickSize,
  minSize: fixture.inputs.minSize,
};

function expectedOrders(shape) {
  const value = fixture.shapes[shape];
  return [
    ...value.bids.map((item) => ({ side: "buy", ...item })),
    ...value.asks.map((item) => ({ side: "sell", ...item })),
  ].sort((left, right) => BigInt(left.price) < BigInt(right.price) ? -1 : 1);
}

async function main() {
  for (const [percent, pps] of [["0.01", 100n], ["0.30", 3000n], ["1.00", 10000n]]) {
    assert.equal(adapter.percentToPps(percent), pps);
    assert.equal(adapter.ppsToPercent(pps), percent);
  }

  const bounds = adapter.percentagePriceBounds("2711150", "3", "3", "100");
  assert.equal(bounds.minimumPriceRaw, 2629800n);
  assert.equal(bounds.maximumPriceRaw, 2792500n);
  assert.equal(bounds.minimumPriceRaw % 100n, 0n);
  assert.equal(bounds.maximumPriceRaw % 100n, 0n);

  for (const shape of ["spot", "curve", "bid_ask"]) {
    const grid = await adapter.generateOrderGrid({ ...input, shape });
    const method = { spot: "getSpotBatchLPDetails", curve: "getCurveBatchLPDetails", bid_ask: "getBidAskBatchLPDetails" }[shape];
    const official = await PositionViewer[method](
      adapter.percentToPps(input.feePercent), adapter.decimalToRaw(input.minimumPrice, 8), adapter.decimalToRaw(input.maximumPrice, 8),
      BigInt(input.bestAskPriceRaw), BigInt(input.pricePrecision), BigInt(input.sizePrecision), BigInt(input.quoteAssetDecimals),
      BigInt(input.baseAssetDecimals), BigInt(input.tickSize), BigInt(input.minSize), adapter.decimalToRaw(input.usdcAmount, 6), adapter.decimalToRaw(input.monAmount, 18),
    );
    assert.equal(grid.sdkMinimumMonRaw, official.lpSummary.baseLiquidity * 10n ** 18n / BigInt(input.sizePrecision), `${shape} official MON minimum and token units`);
    assert.equal(grid.sdkMinimumUsdcRaw, official.lpSummary.quoteLiquidity, `${shape} official USDC minimum`);
    if (shape === "spot") assert.equal(grid.sdkMinimumMonRaw, BigInt(grid.orders.filter((order) => order.side === "sell").length) * BigInt(input.minSize) * 10n ** 18n / BigInt(input.sizePrecision));
    const expected = expectedOrders(shape);
    assert.equal(grid.orders.length, expected.length, `${shape} order count`);
    grid.orders.forEach((order, index) => {
      assert.equal(order.side, expected[index].side, `${shape} side ${index}`);
      assert.equal(order.price, BigInt(expected[index].price), `${shape} price ${index}`);
      assert.equal(order.flipPrice, BigInt(expected[index].flipPrice), `${shape} flip price ${index}`);
      assert.equal(order.liquidity, BigInt(expected[index].liquidity), `${shape} size ${index}`);
      if (index) assert(grid.orders[index - 1].price <= order.price, `${shape} ascending price`);
      assert.equal(
        order.quoteNotionalRaw,
        (order.price * order.liquidity / grid.sizePrecision) * 10n ** grid.quoteAssetDecimals
          / grid.pricePrecision,
        `${shape} quote notional ${index}`,
      );
    });
    assert.equal(
      grid.requiredMonRaw,
      grid.orders.filter((item) => item.side === "sell")
        .reduce((sum, item) => sum + item.liquidity * 10n ** grid.baseAssetDecimals / grid.sizePrecision, 0n),
    );
    assert.equal(
      grid.requiredUsdcRaw,
      grid.orders.filter((item) => item.side === "buy").reduce((sum, item) => sum
        + (((item.price * item.liquidity + grid.sizePrecision - 1n) / grid.sizePrecision)
          * 10n ** grid.quoteAssetDecimals + grid.pricePrecision - 1n) / grid.pricePrecision, 0n),
    );
  }

  const quoteDriven = await adapter.generateOrderGrid({ ...input, shape: "spot", driver: "quote" });
  const screenshot = await adapter.generateOrderGrid({ ...input, shape: "spot", minimumPrice: "0.024926", maximumPrice: "0.025047",
    bestAskPriceRaw: "2498300", feePercent: "0.01", monAmount: "4203.09373367544", usdcAmount: "244.610955" });
  assert.equal(screenshot.orders.filter((order) => order.side === "sell").length, 32);
  assert.equal(screenshot.sdkMinimumMonRaw, adapter.decimalToRaw("6400", 18), "32 Spot asks require the SDK minimum of 6,400 MON");
  assert.equal(adapter.validateOrderGrid(screenshot).deployable, false, "entered 4,203 MON must not pass a 32-ask grid");
  for (const shape of ["spot", "curve", "bid_ask"]) {
    const zeroInput = { ...input, shape, monAmount: "0", usdcAmount: "0" };
    const empty = await adapter.generateOrderGrid(zeroInput);
    const before = adapter.serializeOrderGrid(empty);
    const minimum = await adapter.minimumDepositSummary(zeroInput, empty);
    assert(minimum.minimumMonRaw > 0n && minimum.minimumUsdcRaw > 0n, `${shape} zero deposits still show minimums`);
    assert.deepEqual(adapter.serializeOrderGrid(empty), before, "minimum display must not resize orders");
    assert.equal(zeroInput.monAmount, "0");
    assert.equal(zeroInput.usdcAmount, "0");
  }
  const baseDriven = await adapter.generateOrderGrid({ ...input, shape: "spot", driver: "base" });
  assert(quoteDriven.requiredMonRaw > 0n);
  assert(baseDriven.requiredUsdcRaw > 0n);
  for (const shape of ["spot", "curve", "bid_ask"]) {
    const balanced = await adapter.balanceQuoteDeposit({ ...input, shape });
    assert(balanced.grid.requiredUsdcRaw <= adapter.decimalToRaw(input.usdcAmount, 6), `${shape} locked USDC funding`);
    const repeated = await adapter.generateOrderGrid({ ...input, shape, driver: "base",
      monAmount: adapter.rawToDecimal(balanced.monAmountRaw, 18) });
    assert.deepEqual(adapter.serializeOrderGrid(balanced.grid), adapter.serializeOrderGrid(repeated));
    const minimum = await adapter.findMinimumViablePosition({ ...input, shape }, "base");
    const regenerated = await adapter.generateOrderGrid({ ...input, shape, driver: "base",
      monAmount: adapter.rawToDecimal(minimum.minimumMonAmountRaw, 18),
      usdcAmount: adapter.rawToDecimal(minimum.minimumUsdcAmountRaw, 6) });
    assert(regenerated.requiredMonRaw <= minimum.minimumMonAmountRaw);
    assert(regenerated.requiredUsdcRaw <= minimum.minimumUsdcAmountRaw);
    assert(adapter.validateOrderGrid(regenerated, input.minSize).deployable);
  }

  for (const shape of ["spot", "curve", "bid_ask"]) {
    const undersized = await adapter.generateOrderGrid({
      ...input, shape, driver: "quote", usdcAmount: "0.000001", monAmount: "0.000000000000000001",
    });
    assert.equal(adapter.validateOrderGrid(undersized, input.minSize).deployable, false, `${shape} undersized`);
    const minimum = await adapter.findMinimumViablePosition({ ...input, shape }, "quote");
    assert.equal(minimum.validation.deployable, true, `${shape} minimum passes`);
    assert.equal(minimum.oneRawUnitBelowPasses, false, `${shape} one raw unit below fails`);
    const below = await adapter.generateOrderGrid({
      ...input,
      shape,
      driver: "quote",
      usdcAmount: adapter.rawToDecimal(minimum.driverAmountRaw - 1n, Number(input.quoteAssetDecimals)),
    });
    assert.equal(adapter.validateOrderGrid(below, input.minSize).deployable, false, `${shape} exact lower bound`);
    const balancedMinimum = await adapter.generateOrderGrid({
      ...input,
      shape,
      driver: minimum.driver,
      monAmount: adapter.rawToDecimal(minimum.minimumMonAmountRaw, Number(input.baseAssetDecimals)),
      usdcAmount: adapter.rawToDecimal(minimum.minimumUsdcAmountRaw, Number(input.quoteAssetDecimals)),
    });
    assert.equal(adapter.validateOrderGrid(balancedMinimum, input.minSize).deployable, true, `${shape} locked-driver minimum`);
  }
  process.stdout.write("Kuru SDK browser adapter tests passed.\n");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
