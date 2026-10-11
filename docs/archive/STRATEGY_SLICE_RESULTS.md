# Parameterized Strategy Vertical Slice Results

> Historical development record. Some settings, commands, and results describe earlier engines. See the [current project guide](../../README.md) and [LP contract checks](../LP_CONTRACT_PARITY.md) before using this document.

Run date: 2026-10-06

The strategy used the verified MON-USDC interval:

```text
44,853,800 through 44,856,100, inclusive
```

The validation gate required:

```text
Envio/RPC parity: pass
Envio events: 598
RPC events: 598
maximum price-level error: 0
maximum aggregate-size error: 0
```

## Command

```bash
python strategy_vertical_slice.py
```

## Parameters

```text
starting cash: 1000 USDC
starting inventory: 1000 MON
bid offset: 10 bps
ask offset: 10 bps
order size: 200 MON
minimum inventory: 0 MON
maximum inventory: 2000 MON
cancel/replace: enabled
replace threshold: 1 bps
maker fee: 0 bps
fixed order-action fee: 0 USDC
block inclusion delay: 1 block
material-impact threshold: 25 percent
```

The strategy places one bid and one ask. It makes one decision after each event block. It cancels and replaces an order when the new target price moves by at least the configured threshold. A new order becomes active no earlier than the next block. It joins the queue tail at activation. The engine reserves capital for all active and pending orders.

## Results

```text
placements: 130
cancellations: 126
fill records: 3
filled size: 400 MON
turnover: 9.1816 USDC
initial portfolio value: 1023.043 USDC
final cash: 990.8184 USDC
final inventory: 1400 MON
final midpoint: 0.022858 USDC per MON
final portfolio value: 1022.8196 USDC
net P&L: -0.2234 USDC
return: -0.021836814288353473 percent
maximum drawdown: 0.2662 USDC
minimum inventory: 1000 MON
maximum inventory: 1400 MON
queue-invariant checks: 599
ending active orders: 2
ending reserved cash: 4.567 USDC
ending reserved inventory: 200 MON
material-impact transactions: 1
maximum transaction impact: 26.1437908497 percent
```

Fees were zero for this recorded run. The engine supports a maker fee in basis points and a fixed fee for each placement or cancellation.

## Deterministic outputs

```text
summary SHA-256: 634793c6f3ee7c474c2eb4196fff3f19b9f193c06392e45dbc0e1d91ce403dd1
timeline SHA-256: 7a05a11d1cf076b785e3705ad9315492a5164b04cae3021dff108b4876d0c73a
timeline records: 744
market-event records: 598
strategy-decision records: 143
strategy-fill records: 3
```

Two consecutive runs produced the same summary and timeline hashes. Every timeline record contains the timestamp, market bid, ask and midpoint, active simulated orders, fills, cancellations, placements, cash, inventory, reservations, marked portfolio value, cumulative P&L and cumulative fees.

Artifacts are under `strategy_output/`:

```text
strategy_output/
├── summary.json
├── timeline.jsonl
└── manifest.json
```

## Current limits

- The strategy supports one bid and one ask.
- Decisions occur after each event block.
- Fill inference uses visible historical Trade events and the verified explicit L3 queue.
- The run models a configurable block inclusion delay. It does not model within-block ordering for simulated transactions, gas, failed transactions, vault quoting, or contract execution.
- The replay assumes that simulated orders do not change historical taker behavior or later market events. Per-event percentages are diagnostic only. The maximum transaction-level impact is 26.1437908497 percent, so the recorded run has a material counterfactual-impact flag.
- The interval does not extend beyond the verified 2,301 blocks.
- There is no frontend or live execution.

See [the causality and accounting audit](STRATEGY_ENGINE_AUDIT.md) for the full assumptions, tests, and comparison with the prior run.
