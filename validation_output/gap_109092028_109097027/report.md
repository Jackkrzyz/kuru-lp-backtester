# Reconstruction Validation Report

Market: `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`

Interval: `109092028` through `109097027`, inclusive.

Named interval: `None`

Discovered executed volume: `None` MON (`None` raw)

## Numerical results

- Event source: `envio_hypersync_staged_verified_by_rpc`
- Envio/RPC parity status: `pass`
- Envio event count: `49175`
- RPC event count: `49175`
- Events missing from Envio: `0`
- Events missing from RPC: `0`
- Checkpoint active orders: `44`
- Checkpoint matched levels: `39`
- Checkpoint missing levels: `0`
- Checkpoint extra levels: `0`
- Validated blocks: `7`
- Validated block numbers: `[109092028, 109092861, 109093694, 109094527, 109095360, 109096193, 109097027]`
- Maximum midpoint error: `0`
- Maximum price-level error: `0.0` (`0` raw)
- Maximum aggregate-size error: `0.0` (`0` raw)
- Queue-invariant checks: `49176`
- Queue-invariant failures: `0`
- Queue-invariant status: `pass`
- Cancellation-summary status: `pass`
- OrdersCanceled summary events: `11601`
- Summary nonzero order IDs: `18591`
- Individual OrderCanceled events: `18591`
- Summary order IDs applied as cancellations: `0`

Envio parity is `unavailable` when `ENVIO_API_TOKEN` is not set. This state is not a parity pass.

## Simulated passive order

- Side: `sell`
- Raw price: `2710500`
- Raw size: `2000000000000`
- Orders ahead: `1`
- Raw size ahead: `2000000000000`
- Raw filled size: `2000000000000`
- Placement transaction: `0x4b3b148d29854b560c694e59c647cf3dfc222248f76d6311be0c5d9f8ea42945`
- Final cash: `5.421`
- Final inventory: `-200.0`
- Final marked portfolio value: `-0.0012999999999996348`
- Final cumulative P&L: `-0.0012999999999996348`
- Gross P&L at the combined contract midpoint: `-0.0012999999999996348`

Fees and gas are not included.

## Artifacts

- Price validation chart: `reconstructed_prices.png`
- Error chart: `reconstruction_error.png`
- Final summary: `summary.json`
- Deterministic replay timeline: `timeline.jsonl`
- Validation rows: `validation_rows.json`
- Artifact manifest: `manifest.json`

No OHLC candles were generated. If candles are added later, their open, high, low, and close values must come only from `Trade` events.
