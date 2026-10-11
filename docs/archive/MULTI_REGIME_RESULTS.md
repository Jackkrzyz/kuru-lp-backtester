# Multi-Regime Evaluation Results

> Historical development record. Some settings, commands, and results describe earlier engines. See the [current project guide](../../README.md) and [LP contract checks](../LP_CONTRACT_PARITY.md) before using this document.

Run date: 2026-10-06

## Scope

This evaluation does not change market reconstruction or the existing uncapped fill decisions. It uses the same default strategy parameters for all runs:

```text
starting cash: 1,000 USDC
starting inventory: 1,000 MON
bid offset: 10 bps
ask offset: 10 bps
order size: 200 MON
inventory limits: 0 to 2,000 MON
replace threshold: 1 bp
inclusion delay: 1 block
maker fee: 0
fixed action fee: 0
```

Only the transaction participation cap changes. The scenarios are uncapped, 25 percent, 10 percent, and 5 percent.

## Commands

```bash
python reconstruction_validation_report.py \
  --interval relatively_quiet \
  --validation-points 7 \
  --output-dir validation_output/relatively_quiet

python reconstruction_validation_report.py \
  --interval directional \
  --validation-points 7 \
  --output-dir validation_output/directional

python multi_regime_evaluation.py \
  --validation-root validation_output \
  --output-root multi_regime_output

python -m unittest discover -s tests -v
```

## Reconstruction gates

| Interval | Blocks | Checkpoint | Envio events | RPC events | Checks | Price error | Size error | Midpoint error | Queue failures | Cancellation audit |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Relatively quiet | 109132028-109142027 | 109132027 | 69,672 | 69,672 | 7 | 0 | 0 | 0 | 0 | pass |
| Relatively active | 109097028-109107027 | 109097027 | 98,178 | 98,178 | 7 | 0 | 0 | 0 | 0 | pass |
| Directional | 109117028-109127027 | 109117027 | 96,656 | 96,656 | 7 | 0 | 0 | 0 | 0 | pass |

The quiet check blocks are 109132028, 109133694, 109135361, 109137027, 109138694, 109140360, and 109142027.

The active check blocks are 109097028, 109098694, 109100361, 109102027, 109103694, 109105360, and 109107027.

The directional check blocks are 109117028, 109118694, 109120361, 109122027, 109123694, 109125360, and 109127027.

The cancellation audit did not apply `OrdersCanceled` summary events as individual cancellations. The number of summary order IDs applied was zero in all three intervals. The duplicate individual cancellation count was also zero.

## Strategy and baseline results

All monetary values in this table are USDC. Filled size and final inventory are MON.

| Interval | Scenario | Fills | Filled size | Turnover | Final inventory | Strategy P&L | Hold P&L | Excess P&L | Drawdown | Maximum impact |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Quiet | uncapped | 18 | 2983.3308279535 | 81.1722854488 | 1928.4216276655 | -0.4720106954 | -0.2435000000 | -0.2285106954 | 0.8657303548 | 100% |
| Quiet | 25% | 20 | 2725.2349439006 | 74.1303479359 | 1885.6065918822 | -0.4577261947 | -0.2435000000 | -0.2142261947 | 0.8223584567 | 25% |
| Quiet | 10% | 20 | 2633.3018357802 | 71.6254033897 | 1837.3575160354 | -0.4484185774 | -0.2435000000 | -0.2049185774 | 0.8064277922 | 10% |
| Quiet | 5% | 21 | 2545.1554414129 | 69.2085976158 | 1963.2855567173 | -0.4620669095 | -0.2435000000 | -0.2185669095 | 0.8043763416 | 5% |
| Active | uncapped | 15 | 1690.2421648709 | 46.0293779019 | 1838.2742947639 | 0.9145041619 | 0.5070000000 | 0.4075041619 | 0.4194581017 | 100% |
| Active | 25% | 18 | 1763.7907982637 | 48.0138847774 | 1911.8229281567 | 0.9613002178 | 0.5070000000 | 0.4543002178 | 0.4360065442 | 25% |
| Active | 10% | 18 | 1694.0421976490 | 46.1120896584 | 1842.0743275420 | 0.9367436107 | 0.5070000000 | 0.4297436107 | 0.4203131091 | 10% |
| Active | 5% | 18 | 1670.7926641107 | 45.4781579521 | 1818.8247940037 | 0.9285580750 | 0.5070000000 | 0.4215580750 | 0.4150819640 | 5% |
| Directional | uncapped | 67 | 9537.0330707912 | 258.2010094793 | 1972.8332039196 | -0.0419042480 | -0.6335000000 | 0.5915957520 | 1.1547535703 | 100% |
| Directional | 25% | 73 | 8808.4892556197 | 238.4558711253 | 1813.3509778651 | -0.1396983449 | -0.6335000000 | 0.4938016551 | 1.1443516823 | 25% |
| Directional | 10% | 77 | 8369.3662200404 | 226.5910229405 | 1898.9231886470 | -0.2212767547 | -0.6335000000 | 0.4122232453 | 1.1602782082 | 10% |
| Directional | 5% | 79 | 8421.8176002157 | 227.9654504011 | 1969.0592945267 | -0.2281191446 | -0.6335000000 | 0.4053808554 | 1.1162617630 | 5% |

The active uncapped result is unchanged at +0.9145041619 USDC. A cap can increase the number of fill records. This is expected. A partial fill leaves the order active, so later historical transactions can fill more of it.

## Result decomposition

The spread-capture value uses the reconstructed midpoint immediately before the Trade event. It does not use a later price. Inventory mark-to-market is the signed filled inventory times the change from that fill-time midpoint to the final midpoint.

For every run, this identity holds:

```text
strategy excess P&L
= fill-time spread capture
+ inventory mark-to-market contribution
- maker fees
- action fees
```

| Interval | Scenario | Buy fills / MON | Sell fills / MON | Buy notional | Sell notional | Inventory change | Spread capture | Inventory mark-to-market |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Quiet | uncapped | 11 / 1955.8762278095 | 7 / 1027.4546001440 | 53.2512658457 | 27.9210196031 | 928.4216276655 | 0.0698323098 | -0.2983430052 |
| Quiet | 25% | 13 / 1805.4207678914 | 7 / 919.8141760092 | 49.1443597777 | 24.9859881582 | 885.6065918822 | 0.0644941642 | -0.2787203589 |
| Quiet | 10% | 13 / 1735.3296759078 | 7 / 897.9721598724 | 47.2349785641 | 24.3904248257 | 837.3575160354 | 0.0623184446 | -0.2672370220 |
| Quiet | 5% | 14 / 1754.2204990651 | 7 / 790.9349423478 | 47.7357580611 | 21.4728395547 | 963.2855567173 | 0.0596327829 | -0.2781996924 |
| Active | uncapped | 11 / 1264.2582298174 | 4 / 425.9839350535 | 34.3868761750 | 11.6425017270 | 838.2742947639 | 0.0348877568 | 0.3726164051 |
| Active | 25% | 14 / 1337.8068632102 | 4 / 425.9839350535 | 36.3713830504 | 11.6425017270 | 911.8229281567 | 0.0353997392 | 0.4189004787 |
| Active | 10% | 14 / 1268.0582625955 | 4 / 425.9839350535 | 34.4695879314 | 11.6425017270 | 842.0743275420 | 0.0335261316 | 0.3962174791 |
| Active | 5% | 14 / 1244.8087290572 | 4 / 425.9839350535 | 33.8356562251 | 11.6425017270 | 818.8247940037 | 0.0329015958 | 0.3886564793 |
| Directional | uncapped | 37 / 5254.9331373554 | 30 / 4282.0999334358 | 141.9734635285 | 116.2275459508 | 972.8332039196 | 0.2453220291 | 0.3462737229 |
| Directional | 25% | 37 / 4810.9201167424 | 36 / 3997.5691388773 | 129.9909602470 | 108.4649108784 | 813.3509778651 | 0.2326153862 | 0.2611862689 |
| Directional | 10% | 37 / 4634.1447043437 | 40 / 3735.2215156967 | 125.2576735907 | 101.3333493498 | 898.9231886470 | 0.2171844619 | 0.1950387834 |
| Directional | 5% | 38 / 4695.4384473712 | 41 / 3726.3791528445 | 126.8977059132 | 101.0677444879 | 969.0592945267 | 0.2222632594 | 0.1831175960 |

Maker fees and action fees are zero in all runs because the unchanged defaults set both fees to zero.

## Operational costs

| Interval | Scenario | Placements | Cancellations | Actions | Gross P&L/action | Excess P&L/action | Break-even fixed cost/action |
|---|---|---:|---:|---:|---:|---:|---:|
| Quiet | uncapped | 1431 | 1419 | 2850 | -0.0001656178 | -0.0000801792 | -0.0000801792 |
| Quiet | 25% | 1496 | 1483 | 2979 | -0.0001536510 | -0.0000719121 | -0.0000719121 |
| Quiet | 10% | 1495 | 1483 | 2978 | -0.0001505771 | -0.0000688108 | -0.0000688108 |
| Quiet | 5% | 1587 | 1576 | 3163 | -0.0001460850 | -0.0000691011 | -0.0000691011 |
| Active | uncapped | 2012 | 2004 | 4016 | 0.0002277152 | 0.0001014702 | 0.0001014702 |
| Active | 25% | 2021 | 2013 | 4034 | 0.0002382995 | 0.0001126178 | 0.0001126178 |
| Active | 10% | 2021 | 2013 | 4034 | 0.0002322121 | 0.0001065304 | 0.0001065304 |
| Active | 5% | 2021 | 2013 | 4034 | 0.0002301830 | 0.0001045013 | 0.0001045013 |
| Directional | uncapped | 2670 | 2626 | 5296 | -0.0000079124 | 0.0001117061 | 0.0001117061 |
| Directional | 25% | 2510 | 2471 | 4981 | -0.0000280462 | 0.0000991371 | 0.0000991371 |
| Directional | 10% | 2578 | 2539 | 5117 | -0.0000432435 | 0.0000805596 | 0.0000805596 |
| Directional | 5% | 2482 | 2442 | 4924 | -0.0000463280 | 0.0000823275 | 0.0000823275 |

A negative break-even value means that the strategy already trails the hold baseline before a positive fixed action cost. A positive value is the maximum fixed action cost that can be added before excess P&L becomes zero, with all other results unchanged.

## Participation rule and limits

Each historical transaction is atomic. Before its logs replay, the engine calculates the permitted simulated volume from that transaction's total historical Trade volume. Replay consumes the budget in deterministic log order. It never applies a post-run P&L subtraction. The unfilled order size stays active with its normal queue state.

The capped runs did not exceed their caps. The 10 percent active result is 9.99999999997048 percent and the 5 percent active result is 4.999999999962933 percent because raw integer size uses floor rounding.

The counterfactual assumption is still present. Simulated orders do not change historical taker actions or later market events.

## Deterministic artifacts

```text
comparison SHA-256: a3f3c2dc087c3b78ce17c6dd9a84b29f25d09bea03698a67de11be1f7300e7f5
manifest SHA-256:   ce636a070b0c2dc373f3df135a18564cd93b84be54aa973187b09f61e6ec4513
```

The comparison artifact is `multi_regime_output/comparison.json`. The manifest is `multi_regime_output/manifest.json`. The manifest records the full summary and timeline SHA-256 hash for each of the 12 runs.

Raw timelines are separate:

```text
multi_regime_output/timelines/<interval>/<scenario>/timeline.jsonl
```

The total generated output size is 1,731,071,324 bytes. No frontend or live execution was built.

## Test result

```text
Ran 90 tests in 0.056s
OK
```

The hash verification recalculated all 12 summary and timeline hashes. It found zero mismatches.
