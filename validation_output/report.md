# Reconstruction Validation Report

Market: `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`

Interval: `44853800` through `44856100`, inclusive.

## Numerical results

- Event source: `envio_hypersync`
- Envio/RPC parity status: `pass`
- Envio event count: `598`
- RPC event count: `598`
- Events missing from Envio: `0`
- Events missing from RPC: `0`
- Checkpoint active orders: `265`
- Checkpoint matched levels: `244`
- Checkpoint missing levels: `0`
- Checkpoint extra levels: `0`
- Validated blocks: `9`
- Maximum midpoint error: `0`
- Maximum price-level error: `0.0` (`0` raw)
- Maximum aggregate-size error: `0.0` (`0` raw)
- Queue-invariant checks: `599`
- Queue-invariant failures: `0`
- Queue-invariant status: `pass`

Envio parity is `unavailable` when `ENVIO_API_TOKEN` is not set. This state is not a parity pass.

## Simulated passive order

- Side: `buy`
- Raw price: `2294300`
- Raw size: `2000000000000`
- Orders ahead: `1`
- Raw size ahead: `116701747809`
- Raw filled size: `2000000000000`
- Placement transaction: `0x3208b1a7f481a47d026531a2f532ad18b6781854f8a2f3ccc1f7507e41cdbe3b`
- Final cash: `-4.5886000000000005`
- Final inventory: `200.0`
- Final marked portfolio value: `-0.017000000000000348`
- Final cumulative P&L: `-0.017000000000000348`
- Gross P&L at the combined contract midpoint: `-0.017000000000000348`

Fees and gas are not included.

## Artifacts

- Price validation chart: `reconstructed_prices.png`
- Error chart: `reconstruction_error.png`
- Final summary: `summary.json`
- Deterministic replay timeline: `timeline.jsonl`
- Validation rows: `validation_rows.json`
- Artifact manifest: `manifest.json`

No OHLC candles were generated. If candles are added later, their open, high, low, and close values must come only from `Trade` events.

