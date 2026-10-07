"use strict";

const assert = require("assert");
const fixture = require("./fixtures/concentrated_liquidity_v0_0_95.json");
const adapter = require("./browser_adapter");

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
        order.price * order.liquidity * 10n ** grid.quoteAssetDecimals
          / (grid.pricePrecision * grid.sizePrecision),
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
      grid.orders.filter((item) => item.side === "buy").reduce((sum, item) => sum + item.quoteNotionalRaw, 0n),
    );
  }

  const quoteDriven = await adapter.generateOrderGrid({ ...input, shape: "spot", driver: "quote" });
  const baseDriven = await adapter.generateOrderGrid({ ...input, shape: "spot", driver: "base" });
  assert(quoteDriven.requiredMonRaw > 0n);
  assert(baseDriven.requiredUsdcRaw > 0n);

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
