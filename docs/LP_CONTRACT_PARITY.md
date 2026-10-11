# Kuru LP contract checks

Date: 2026-10-08. Current engine: `kuru-lp-contract-v18`.

## Scope

The product now has one mode: Kuru LP. The Advanced MM demo and all Advanced Simulation controls have been removed. The old MM research scripts and recorded raw data remain in place. They are not a fallback for Run Backtest.

The browser submits its exact SDK grid. The backend regenerates that grid and checks it before it uses local validated history. It does not download events during a run.

The historical reconstruction code, source events, checkpoints, and validation results are unchanged. Corrected LP accounting uses a new engine identity. Old LP results are not reused.

## Reference and contract evidence

- Official SDK: `@kuru-labs/kuru-sdk` version `0.0.95`.
- SDK npm gitHead: `c6a1e9eced12d634fa879ec46c52baff8a50f3c3`.
- Public contract source inspected: `Kuru-Labs/Kuru-contracts-dex-public`, commit `2060bb2736080c175d80d568bfdb6226bb5abd04`.
- Historical MON-USDC implementation: `0x5e3446c600524be453bbcefd46a9e4c9be8899a0`.
- Implementation runtime SHA-256: `7c7a6e72320d71112aaf58ad3604224f15bdf0734f5790cf79d2b04194c80d42`.
- All six local checkpoint reads returned this implementation, zero maker rebate, zero taker fee, and no active vault size.
- Historical market minimum: `2000000000000` size units (200 MON). Maximum: `2000000000000000000` size units. Price precision: `100000000`. Size precision: `10000000000`. Tick: `100`. MON decimals: 18. USDC decimals: 6.

`prepare_lp_contract_rules.py` reads the six existing checkpoint blocks through archive RPC. It saves market parameters, vault state, checkpoint hashes, proxy code hashes, implementation hashes, and the actual implementation runtime. It does not backfill events. The local cache is gitignored.

The local EVM tests execute the unmodified historical implementation runtime when this cache is present. The tests deploy an isolated market and a test margin ledger. They do not rewrite the order-book bytecode. The EVM contract-size limit is raised for the test because the Monad implementation is larger than Ethereum's limit. The ledger uses token-address placeholders. Wallet transfers, native-token deposits, gas, and Monad scheduling are not tested.

## Corrections

1. The reference price comes from the state before the selected first block. No event from that first block is used to choose the initial grid. A regression checks the differing midpoint at block 109100000.
2. Initial orders enter at the first selected block, after their one-block external inclusion step. They join their price-level queue tails.
3. Initial provisioning uses the SDK's `provisionOrRevert=false` behavior. Crossing orders are skipped, not inserted. Their reservations are released.
4. Initial orders must meet tick, price, minimum-size, maximum-size, and flip-direction checks. Internal partial-fill flips can be below the initial minimum. The contract permits this. The simulator does not discard these assets.
5. A partial fill creates or increases its paired order. Active paired orders keep their queue priority. A new pair joins the tail. Sell-to-buy size conversion uses integer floor division. Repeated cycles compound order size.
6. A contract-created flip can trade after its creation, including a later call in the same transaction. No extra block delay is added.
7. A historical `FlipOrderUpdated` size increase now increases simulated queue-ahead size. Reconstruction itself is unchanged.
8. Funding rounds up at both quote-conversion steps. Withdrawable quote value rounds down at both steps. Contract tests check funding and cancellation refunds for all three SDK shape fixtures. The historical implementation differs from the public source in its final funding conversion.
9. Positive contract `makerFeeBps` is a rebate. Buy makers receive quote rebates. Sell makers receive base rebates. Rebates are not charged as a USDC cost.
10. Earned flip spread counts completed cycles, not both legs of the same cycle. It is a separate diagnostic, not an extra cash credit. For MON-denominated cycle growth, its USDC equivalent uses the causal completion price. Net P&L still comes from the complete marked portfolio.

## Product assumptions

| Former control | Current rule |
|---|---|
| Participation cap | Removed. No artificial volume cap. A historical event's executable volume cannot be reused. |
| Inclusion latency | Fixed initial inclusion at the first selected block. Internal flips enter after their causal fill. |
| Maker fee | Read from the historical contract. It is zero in this indexed range. |
| Fixed action cost | Removed. Gas is excluded and stated in the form and normalized result. |
| Impact threshold | Not a fill rule. A fixed 25 percent transaction share triggers a warning. |

An impact warning does not make a position invalid. It means unchanged historical trade behavior is a weak assumption for that result. No arbitrary cap is used to make this warning disappear.

Current UI update, 2026-10-11: enter MON and USDC manually. The UI shows the
official SDK side minimums and the exact grid's funding requirements. Auto-fill
and minimum-setting buttons were removed. Their research helpers and tests
remain available internally. The backend returns `grid_deposit_shortage` if the
exact grid cannot be funded. Historical browser checks below describe the
earlier buttons, not the current product controls.

## Limits of an exact historical replay

SDK and contract mechanics can be tested exactly. Real counterfactual trading cannot be proved from the fixed event history. Simulated orders do not change later historical trades, cancellations, or prices. Historical logs do not prove the taker's full original limit, quote budget, calldata, or response to a different book. The app reports transaction-level impact. It does not claim exact deployed profit or fills.

Current continuous validated L3 coverage is blocks `109092028–109142027`. The LP service rejects an untested implementation hash or active vault liquidity. Future coverage needs corresponding contract and vault checks. Existing historical L2 checks do not prove every possible flip-event or vault-event case.

## Reproduce

From the project directory in WSL:

```bash
source .venv/bin/activate
npm install --ignore-scripts --prefix sdk_reference
python -m pip install -e '.[dev,reports,contract-tests]'
python -m kuru_backtester.tools.prepare_lp_contract_rules
npm run test:adapter --prefix sdk_reference
npm run build:browser --prefix sdk_reference
python -m pytest -q
python -m kuru_backtester.tools.golden_regression
python -m kuru_backtester.server.backtest_server --host 0.0.0.0 --port 8000
```

The contract-rule preparation uses archive RPC. All test backtests use local history. The server prints the complete browser URL. Restart an existing server to load the corrections.

`data/reports/lp_contract_regression_report.json` records the current named-wrapper versus block-range comparison. Both paths use the corrected engine. This does not claim that corrected results equal the former engine's results. The archived `data/reports/golden_regression_report.json` is retained.

The comparison profile is Spot, a 3 percent lower and upper range, 0.30 percent spread, 6,000 MON, and 200 USDC, with MON as the size driver. The 200 USDC deposit funds all three exact grids. The former 180 USDC budget does not fund these MON-driven grids after rounding. This is a funding check, not a P&L-based parameter choice.

## Final results

At the contract-correction milestone, the complete suite passed: **175 tests and 12 subtests**. This included ten tests against unmodified contract bytecode. The official SDK adapter tests passed for all three shapes, unit conversions, minimum-size searches, and funded Auto-fill positions. See [maintenance results](MAINTENANCE.md) for the current test count.

All three named-wrapper versus block-range comparisons passed, with no differences in the compared financial fields, fill files, compact timelines, or semantic timeline hashes.

| Interval | Blocks | Fills | Net P&L, USDC | Generalized run ID |
|---|---|---:|---:|---|
| Quiet | 109132028–109142027 | 3 | -1.7249738019380478 | `lp-3dcbac7069fbb06a80b8` |
| Active | 109097028–109107027 | 11 | 2.292635767594539 | `lp-b9c3bae81918c7e45797` |
| Directional | 109117028–109127027 | 14 | -5.088984279593537 | `lp-10bf3000c74990119972` |

The full deterministic hashes are in `data/reports/lp_contract_regression_report.json`. No new RPC event-parity or L2 comparison run was needed: the source data and reconstruction did not change. These LP runs consume the existing validated partitions.

### Integration configurations on the active interval

Both use the exact submitted token amounts and `amount_driver=both`. The integration test repeats configuration A and requires identical run ID, summary hash, timeline hash, and compact frames. It also checks fee-only, deposit-only, and shape-only grid changes.

| Field | A | B |
|---|---|---|
| Shape | Spot | Bid-Ask |
| Absolute range, USDC | 0.02710–0.02718 | 0.025–0.030 |
| Flip spread | 0.01% | 1.00% |
| MON | 6,000 | 16,000 |
| USDC | 180 | 450 |
| Initial bids / asks | 20 / 20 | 9 / 10 |
| Fills | 355 | 5 |
| Net P&L, USDC | 0.518375 | 8.225090183399153 |
| Run ID | `lp-4c3083377cb06b94108c` | `lp-2d41d3f8be336bf585c6` |

Configuration A:

```text
summary  4007fbc0b386a7c2f25aeecdeb4aff251957190dacdc43cc550fab912f61a003
timeline 20b4cf8b8553dc681886ec657a9ce6c955d0a2fe4f24b266f3dd05e3831b13d5
```

Configuration B:

```text
summary  c6c953050b81f3c4bff5e29d492a4b4bb7f0d148888fd07cfa808eb4fbd8f567
timeline 316b25a73c2d2603d6ecf216ffde564966704423948bed010d5c4e45098e4466
```

These are replay results, not deployment profit estimates. Gas is excluded. Transaction-impact warnings must be read with each result.

### Browser check

A separate local server used port 8001. The form had no MM mode or Advanced Simulation controls. An invalid position disabled Run Backtest. Use minimum valid amounts produced a deployable exact grid. Run Backtest showed its running state, received HTTP 202, then loaded its new HTTP 200 result. The run ID, normalized configuration, hashes, metrics, warning, and replay appeared in the screen. The timeline scrubber reached the last retained state.

A second check kept an edited 80 USDC deposit unchanged. Auto-fill derived `2640.826931282752487448` MON through the SDK search. The regenerated grid required `79.999997` USDC and had no undersized orders or deposit shortage.

That configuration ran through the form with engine v18 and displayed run `lp-5bbe278c429cfd9cab9a`: 10 fills, 21 initial orders, 9 new flip orders, and net P&L of about 1.0091 USDC. Its transaction-impact warning was displayed. Summary hash: `18a6b5a8ca66c041691a7f352794f0471ca5c4376b21e49289ccc6be2762e9f0`. Timeline hash: `da5e74c860a61f7cda916b3094dfd5a1058f9a38507df9cdf04ae95e5bbb5fab`.

The verification skill guided the form-to-result check. It confirmed the displayed configuration, API responses, result identity, metrics, and replay. The temporary test server was separate from the existing server on port 8000.

## Official documentation

- [How liquidity provisioning works](https://docs.kuru.io/liquidity/how-liquidity-provisioning-works)
- [Flip orders](https://docs.kuru.io/concepts/flip-orders)
- [Liquidity shapes](https://docs.kuru.io/liquidity/liquidity-shapes-explained)
- [How fees work](https://docs.kuru.io/liquidity/how-fees-work)
