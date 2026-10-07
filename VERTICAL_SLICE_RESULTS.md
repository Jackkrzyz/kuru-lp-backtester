# L3 Vertical Slice Results

Run date: 2026-10-06

Market:

```text
0x065c9d28e428a0db40191a54d33d5b7c71a9c394
```

## Selected range

```text
44,853,800 through 44,856,100, inclusive
```

The range has 2,301 blocks and 598 decoded market events:

| Event | Count |
|---|---:|
| OrderCreated | 234 |
| OrderCanceled | 234 |
| OrdersCanceled | 117 |
| Trade | 7 |
| FlippedOrderCreated | 2 |
| FlipOrderUpdated | 2 |
| VaultParamsUpdated | 2 |

`OrdersCanceled` is a batch summary. The replay changes queues only for the 234 individual `OrderCanceled` events.

## Commands and results

Find the interval:

```bash
python discover_interval.py --from-block 44853800 --to-block 44856100 --chunk-size 2301 --batch-size 1
```

Live RPC validation while the Envio token is not present:

```bash
python l3_vertical_slice.py --rpc-only
```

Result:

```text
Checkpoint 44853799: 265 orders, 82 bid levels, 162 ask levels; invariants PASS.
L2 validations: PASS [(44853800, 82, 162), (44853836, 82, 162),
(44853995, 82, 162), (44854039, 82, 162), (44855470, 81, 161),
(44855818, 81, 161), (44855908, 80, 162), (44856040, 80, 164),
(44856100, 80, 164)]
Replay cursor: PASS index=598 position=(44856099, 5, 20)
```

Passive-order result:

```text
side=buy
price_raw=2294300
size_raw=2000000000000
queue_ahead_orders=1
queue_ahead_size_raw=116701747809
filled_size_raw=2000000000000
transaction=0x3208b1a7f481a47d026531a2f532ad18b6781854f8a2f3ccc1f7507e41cdbe3b
cash=-4.588600000000
inventory=200.000000000000
final_mark=0.022858000000
gross_pnl=-0.017000000000
```

The fill is queue-aware. The simulated buy order is behind one real order. The historical taker transaction clears that real size and continues to a worse bid. The overlay gives the simulated order priority before that worse price. The P&L excludes fees and gas.

Run all tests:

```bash
python -m unittest discover -s tests -v
```

Result: 19 tests passed.

## Envio status

The Envio-enabled run passed on 2026-10-06:

```text
event source: envio_hypersync
Envio events: 598
RPC events: 598
missing from Envio: 0
missing from RPC: 0
parity status: pass
```

All nine `getL2Book()` checks still had zero midpoint, price-level, and aggregate-size error. The queue-invariant status also stayed `pass`.

The default command uses Envio events for replay and uses RPC events and contract calls only for independent verification.

The recorded Envio-backed artifacts are under `validation_output/`. Their SHA-256 values are in `validation_output/manifest.json`.
