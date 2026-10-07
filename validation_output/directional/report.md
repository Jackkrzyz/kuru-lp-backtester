# Reconstruction Validation Report

Market: `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`

Interval: `109117028` through `109127027`, inclusive.

Named interval: `directional`

Discovered executed volume: `4017891.3311869967` MON (`40178913311869967` raw)

## Numerical results

- Event source: `envio_hypersync_cache_verified_by_rpc`
- Envio/RPC parity status: `pass`
- Envio event count: `96656`
- RPC event count: `96656`
- Events missing from Envio: `0`
- Events missing from RPC: `0`
- Checkpoint active orders: `43`
- Checkpoint matched levels: `38`
- Checkpoint missing levels: `0`
- Checkpoint extra levels: `0`
- Validated blocks: `7`
- Validated block numbers: `[109117028, 109118694, 109120361, 109122027, 109123694, 109125360, 109127027]`
- Maximum midpoint error: `0`
- Maximum price-level error: `0.0` (`0` raw)
- Maximum aggregate-size error: `0.0` (`0` raw)
- Queue-invariant checks: `96657`
- Queue-invariant failures: `0`
- Queue-invariant status: `pass`
- Cancellation-summary status: `pass`
- OrdersCanceled summary events: `22711`
- Summary nonzero order IDs: `36423`
- Individual OrderCanceled events: `36423`
- Summary order IDs applied as cancellations: `0`

Envio parity is `unavailable` when `ENVIO_API_TOKEN` is not set. This state is not a parity pass.

## Simulated passive order

- Side: `buy`
- Raw price: `2742300`
- Raw size: `2000000000000`
- Orders ahead: `1`
- Raw size ahead: `69650614644553`
- Raw filled size: `2000000000000`
- Placement transaction: `0xdcbcd15f90905bbf89c4b8fa3e6bf628144f4de97ab63f27e2645aead4ebf083`
- Final cash: `-5.4845999999999995`
- Final inventory: `200.0`
- Final marked portfolio value: `-0.0699999999999994`
- Final cumulative P&L: `-0.0699999999999994`
- Gross P&L at the combined contract midpoint: `-0.0699999999999994`

Fees and gas are not included.

## Artifacts

- Price validation chart: `reconstructed_prices.png`
- Error chart: `reconstruction_error.png`
- Final summary: `summary.json`
- Deterministic replay timeline: `timeline.jsonl`
- Validation rows: `validation_rows.json`
- Artifact manifest: `manifest.json`

No OHLC candles were generated. If candles are added later, their open, high, low, and close values must come only from `Trade` events.

