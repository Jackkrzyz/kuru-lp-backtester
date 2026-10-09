# Kuru SDK Reference

Inspection date: 2026-10-07

## Pinned source

```text
package: @kuru-labs/kuru-sdk
version: 0.0.95
npm gitHead: c6a1e9eced12d634fa879ec46c52baff8a50f3c3
npm tarball SHA-1: 8e9456729fb54967927812580f537e1f4ab1d885
GitHub main at inspection: 636509c2eafd63479d3f399703354e0d09f51e18
```

The implementation reference is the published `0.0.95` package. The current GitHub main commit is not used for fixture parity.

The LP replay also uses the official public OrderBook contract for flip behavior:

```text
repository: Kuru-Labs/Kuru-contracts-dex-public
commit: 2060bb2736080c175d80d568bfdb6226bb5abd04
function: OrderBook._handleFlipOrderUpdate
```

## Inspected code

- `src/concentratedLiquidity/positionViewer.ts`
- `src/concentratedLiquidity/provision.ts`
- `src/concentratedLiquidity/lpSummary.ts`
- `examples/v2/spotConcentratedLiquidity.ts`
- `examples/v2/curveConcentratedLiquidity.ts`
- `examples/v2/bidAskConcentratedLiquidity.ts`

`PositionViewer` supplies the Spot, Curve, and Bid-Ask integer formulas. It uses a fee denominator of 1,000,000 and explicit tick alignment. `PositionProvider` sends each generated `price`, `flipPrice`, `liquidity`, and `isBuy` value to `batchProvisionLiquidity`.

The SDK calls the fee unit percentage-per-point (`pps`). Its source states that 100 pps equals one basis point. The browser adapter is the only code that converts the user percentage to pps. The checked conversions are:

| User value | SDK value |
|---:|---:|
| 0.01% | 100 pps |
| 0.30% | 3,000 pps |
| 1.00% | 10,000 pps |

The browser adapter calls `PositionViewer` directly. It combines the returned bids and asks, then sorts all levels by numeric price in ascending order. The SDK returns bids in descending order. The former preview did not sort the combined list, which made the bid distribution look reversed. The SDK formula was not the cause.

For each grid, required MON is the sum of the tick-rounded ask sizes in token raw units. Required USDC follows the historical implementation's two-stage calculation for each bid: `ceil(ceil(price * size / sizePrecision) * 10**quoteDecimals / pricePrecision)`. Withdrawable bid value and executed notional use floor instead of ceil. This differs from the published public source's final conversion. The contract tests use the actual historical implementation when its local bytecode cache is present. The preview uses the same order object and displays quote notional as its common bar unit. No SDK price, size, or flip price is changed by this token-accounting conversion.

The contract requires each initial flip order to be at least `minSize`. The internal `_handleFlipOrderUpdate` path does not repeat this check. It creates the paired opposite order on the first partial fill, even when that internal order is below `minSize`. A later partial fill adds size to the same paired order. For a filled sell order, the contract uses integer floor division to convert the paired bid size by `source price / flip price`. A new paired order joins the tail of its price level. The simulator marks this internal exemption and does not drop or split small fill proceeds.

The adapter also owns these product checks:

- percentage ranges use the reconstructed midpoint before the selected first block;
- the lower bound rounds down to a tick and the upper bound rounds up;
- every final SDK order is compared with `minSize` after all integer rounding;
- the minimum viable position uses a doubling search and raw-integer binary search;
- the final passing grid is regenerated, and one driver raw unit less must fail.

No local distribution formula is used without parity against SDK output.

## Reproduce fixtures

```bash
cd sdk_reference
npm install --ignore-scripts
npm run fixtures
npm run build:browser
npm run test:adapter
```

The generated fixture is `sdk_reference/fixtures/concentrated_liquidity_v0_0_95.json`.
