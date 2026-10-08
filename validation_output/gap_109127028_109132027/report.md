# Reconstruction Validation Report

Market: `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`

Interval: `109127028` through `109132027`, inclusive.

Named interval: `None`

Discovered executed volume: `None` MON (`None` raw)

## Numerical results

- Event source: `envio_hypersync_staged_verified_by_rpc`
- Envio/RPC parity status: `pass`
- Envio event count: `41973`
- RPC event count: `41973`
- Events missing from Envio: `0`
- Events missing from RPC: `0`
- Checkpoint active orders: `43`
- Checkpoint matched levels: `38`
- Checkpoint missing levels: `0`
- Checkpoint extra levels: `0`
- Validated blocks: `7`
- Validated block numbers: `[109127028, 109127861, 109128694, 109129527, 109130360, 109131193, 109132027]`
- Maximum midpoint error: `0`
- Maximum price-level error: `0.0` (`0` raw)
- Maximum aggregate-size error: `0.0` (`0` raw)
- Queue-invariant checks: `41974`
- Queue-invariant failures: `0`
- Queue-invariant status: `pass`
- Cancellation-summary status: `pass`
- OrdersCanceled summary events: `9897`
- Summary nonzero order IDs: `15869`
- Individual OrderCanceled events: `15869`
- Summary order IDs applied as cancellations: `0`

Envio parity is `unavailable` when `ENVIO_API_TOKEN` is not set. This state is not a parity pass.

## Simulated passive order

- Side: `sell`
- Raw price: `2712300`
- Raw size: `2000000000000`
- Orders ahead: `1`
- Raw size ahead: `70137943262201`
- Raw filled size: `2000000000000`
- Placement transaction: `0xb9140271a291ac3a80af9b012ac83e71a04661e926207845d0486e06ec8d96fd`
- Final cash: `5.4246`
- Final inventory: `-200.0`
- Final marked portfolio value: `-0.031500000000000306`
- Final cumulative P&L: `-0.031500000000000306`
- Gross P&L at the combined contract midpoint: `-0.031500000000000306`

Fees and gas are not included.

## Artifacts

- Price validation chart: `reconstructed_prices.png`
- Error chart: `reconstruction_error.png`
- Final summary: `summary.json`
- Deterministic replay timeline: `timeline.jsonl`
- Validation rows: `validation_rows.json`
- Artifact manifest: `manifest.json`

No OHLC candles were generated. If candles are added later, their open, high, low, and close values must come only from `Trade` events.
