# Strategy Engine Causality and Accounting Audit

> Historical development record. Some settings, commands, and results describe earlier engines. See the [current project guide](../../README.md) and [LP contract checks](../LP_CONTRACT_PARITY.md) before using this document.

Run date: 2026-10-06

## Scope

This audit changes only the strategy engine and its tests. It does not change the verified market reconstruction in `l3_vertical_slice.py`.

The run uses the verified MON-USDC interval:

```text
checkpoint block: 44,853,799
first event block: 44,853,800
last event block: 44,856,100
Envio events: 598
RPC events: 598
missing events: 0
maximum price-level error: 0
maximum aggregate-size error: 0
```

## Commands

Run all tests:

```bash
python -m unittest discover -s tests -v
```

Run the strategy:

```bash
python strategy_vertical_slice.py
```

Set a different block inclusion delay:

```bash
python strategy_vertical_slice.py --inclusion-delay-blocks 2
```

## Causality rules

- A strategy decision occurs after all market events in its decision block.
- An order cannot fill in its decision block.
- The default inclusion delay is one block.
- An order becomes active at the first processed event block at or after its eligible block.
- Queue position is set when the order becomes active. The order joins the tail of its price level.
- A decision uses only the reconstructed book that exists after processed events.

## Queue rules

- The engine stores the historical order IDs and size in front of each simulated order.
- A partial historical fill reduces the size in front.
- A cancellation in front removes that size from the queue position.
- An order behind the simulated order does not enter the queue-ahead list.
- A canceled or replaced simulated order is removed. It cannot fill later.
- Each Trade event can fill only the remaining simulated size. Multiple Trade events cannot fill more than the order size.

## Capital and accounting rules

- A bid reserves its complete quote notional and all positive maker fees.
- An ask reserves its complete MON size.
- A new order can use only unreserved cash and inventory.
- A replacement releases the old reservation before it checks the new order.
- A positive maker fee is a USDC cost. A negative maker fee is a USDC rebate.
- A fixed action fee is charged one time for each placement and cancellation.
- Turnover contains executed notional only.
- The selected mark is the reconstructed midpoint.
- Marked value is cash plus inventory multiplied by the selected mark.
- Net P&L is final marked value minus initial marked value. Because fees are charged to cash, this value is also gross P&L before costs minus signed maker fees and action fees.
- Maximum drawdown uses the marked-equity high-water mark.

## Counterfactual assumption

The engine assumes that simulated orders do not change historical taker behavior, historical order actions, or later market events. This assumption can fail when a simulated fill uses a material part of the historical Trade event.

Each fill retains this diagnostic value:

```text
simulated fill size / historical Trade-event filled_size * 100
```

This per-event value does not flag the run. The run-level value groups records by transaction hash:

```text
total simulated fill size in the transaction
/
total filled_size across all historical Trade events in the transaction
* 100
```

The default material-impact threshold is 25 percent. A transaction at or above this value flags the run. The allocator assigns historical Trade volume once. It fails if simulated fills reuse more volume than the transaction contains.

## Test result

```text
Ran 84 tests in 0.055s
OK
```

The strategy test file has explicit tests for all causality, queue, capital, accounting, counterfactual, and deterministic-output rules in this report.

## Verified interval result

The default one-block inclusion delay did not change the prior three fill records or P&L.

| Metric | Previous result | Hardened result | Change |
|---|---:|---:|---:|
| Fill records | 3 | 3 | 0 |
| Filled size | 400 MON | 400 MON | 0 |
| Turnover | 9.1816 USDC | 9.1816 USDC | 0 |
| Final cash | 990.8184 USDC | 990.8184 USDC | 0 |
| Final inventory | 1,400 MON | 1,400 MON | 0 |
| Final marked value | 1,022.8196 USDC | 1,022.8196 USDC | 0 |
| Net P&L | -0.2234 USDC | -0.2234 USDC | 0 |
| Maximum drawdown | 0.2662 USDC | 0.2662 USDC | 0 |

The three per-event diagnostic percentages are 100 percent, 24.9996507392 percent, and 7.1159887907 percent. They do not set the run-level flag.

The first transaction contains two Trade events. Its historical aggressor size is 765 MON. It fills 200 simulated MON and displaces 200 MON of historical downstream maker volume. Its transaction-level impact is 26.1437908497 percent. It is above the 25 percent threshold.

The second transaction contains two Trade events. Its historical aggressor size is 2,810.5745021744 MON. It fills 200 simulated MON and displaces 200 MON of historical downstream maker volume. Its transaction-level impact is 7.1159828656 percent.

The run remains flagged because one transaction is above the threshold. The old 100 percent per-event value is no longer the headline.

The end state has 4.567 USDC reserved for the active bid and 200 MON reserved for the active ask. These reservations are not costs. They remain part of cash and inventory.

## Deterministic artifacts

Two consecutive runs produced the same hashes:

```text
summary SHA-256: 634793c6f3ee7c474c2eb4196fff3f19b9f193c06392e45dbc0e1d91ce403dd1
timeline SHA-256: 7a05a11d1cf076b785e3705ad9315492a5164b04cae3021dff108b4876d0c73a
timeline records: 744
```

The outputs are:

```text
strategy_output/summary.json
strategy_output/timeline.jsonl
strategy_output/manifest.json
```

Each timeline record includes the market state, active simulated orders, activations, fills, cancellations, placements, cash, inventory, reservations, available capital, marked value, and cumulative P&L. The backend completes the replay immediately. It does not add playback delay.

## Limits

- This is a counterfactual replay. It is not a matching-engine simulation that changes the historical event path.
- The material-impact flag is a warning. It does not correct the historical path.
- The engine supports one bid and one ask.
- The run uses only the verified interval. It does not expand the historical range.
- There is no frontend and no live execution.
