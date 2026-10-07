"use strict";

const fs = require("fs");
const path = require("path");
const { PositionViewer } = require("@kuru-labs/kuru-sdk");

const SDK_VERSION = "0.0.95";
const SDK_GIT_HEAD = "c6a1e9eced12d634fa879ec46c52baff8a50f3c3";
const inputs = {
  minFeesPps: 3000n,
  startPrice: 2630000n,
  endPrice: 2795000n,
  bestAskPrice: 2713900n,
  pricePrecision: 100000000n,
  sizePrecision: 10000000000n,
  quoteAssetDecimals: 6n,
  baseAssetDecimals: 18n,
  tickSize: 100n,
  minSize: 2000000000000n,
  quoteLiquidity: 60000000n,
  baseLiquidity: 2000000000000000000000n,
};

function stringify(value) {
  if (typeof value === "bigint") return value.toString();
  if (Array.isArray(value)) return value.map(stringify);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, stringify(item)]));
  }
  return value;
}

async function generate() {
  const common = [
    inputs.minFeesPps,
    inputs.startPrice,
    inputs.endPrice,
    inputs.bestAskPrice,
    inputs.pricePrecision,
    inputs.sizePrecision,
    inputs.quoteAssetDecimals,
    inputs.baseAssetDecimals,
    inputs.tickSize,
    inputs.minSize,
  ];
  const spot = await PositionViewer.getSpotBatchLPDetails(
    ...common,
    inputs.quoteLiquidity,
    inputs.baseLiquidity,
  );
  const curve = await PositionViewer.getCurveBatchLPDetails(
    ...common,
    inputs.quoteLiquidity,
  );
  const bidAsk = await PositionViewer.getBidAskBatchLPDetails(
    ...common,
    inputs.quoteLiquidity,
  );
  const fixture = stringify({
    schema_version: "1.0",
    sdk: {
      package: "@kuru-labs/kuru-sdk",
      version: SDK_VERSION,
      npm_git_head: SDK_GIT_HEAD,
    },
    inputs,
    shapes: {
      spot: spot.batchLPDetails,
      curve: curve.batchLPDetails,
      bid_ask: bidAsk.batchLPDetails,
    },
  });
  const target = path.join(__dirname, "fixtures", "concentrated_liquidity_v0_0_95.json");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, `${JSON.stringify(fixture, null, 2)}\n`);
  process.stdout.write(`${target}\n`);
}

generate().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
