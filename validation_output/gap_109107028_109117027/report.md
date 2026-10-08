# Reconstruction Validation Report

Market: `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`

Interval: `109107028` through `109117027`, inclusive.

Named interval: `None`

Discovered executed volume: `None` MON (`None` raw)

## Numerical results

- Event source: `envio_hypersync_staged_verified_by_rpc`
- Envio/RPC parity status: `pass`
- Envio event count: `76871`
- RPC event count: `76871`
- Events missing from Envio: `0`
- Events missing from RPC: `0`
- Checkpoint active orders: `44`
- Checkpoint matched levels: `39`
- Checkpoint missing levels: `0`
- Checkpoint extra levels: `0`
- Validated blocks: `7`
- Validated block numbers: `[109107028, 109108694, 109110361, 109112027, 109113694, 109115360, 109117027]`
- Maximum midpoint error: `0`
- Maximum price-level error: `0.0` (`0` raw)
- Maximum aggregate-size error: `0.0` (`0` raw)
- Queue-invariant checks: `76872`
- Queue-invariant failures: `0`
- Queue-invariant status: `pass`
- Cancellation-summary status: `pass`
- OrdersCanceled summary events: `18686`
- Summary nonzero order IDs: `28907`
- Individual OrderCanceled events: `28907`
- Summary order IDs applied as cancellations: `0`

Envio parity is `unavailable` when `ENVIO_API_TOKEN` is not set. This state is not a parity pass.

## Simulated passive order

- Side: `sell`
- Raw price: `2789200`
- Raw size: `2000000000000`
- Orders ahead: `1`
- Raw size ahead: `89680000000000`
- Raw filled size: `2000000000000`
- Placement transaction: `0xa68e8b9916d152f830d00dca3da69b2358db0378b9c44433df40b5304d42604b`
- Final cash: `5.5784`
- Final inventory: `-200.0`
- Final marked portfolio value: `0.03709999999999969`
- Final cumulative P&L: `0.03709999999999969`
- Gross P&L at the combined contract midpoint: `0.03710000000000058`

Fees and gas are not included.

## Artifacts

- Price validation chart: `reconstructed_prices.png`
- Error chart: `reconstruction_error.png`
- Final summary: `summary.json`
- Deterministic replay timeline: `timeline.jsonl`
- Validation rows: `validation_rows.json`
- Artifact manifest: `manifest.json`

No OHLC candles were generated. If candles are added later, their open, high, low, and close values must come only from `Trade` events.
