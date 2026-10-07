# Reconstruction Validation Report

Market: `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`

Interval: `109097028` through `109107027`, inclusive.

Named interval: `relatively_active`

Discovered executed volume: `2997622.9559281976` MON (`29976229559281976` raw)

## Numerical results

- Event source: `envio_hypersync_cache_verified_by_rpc`
- Envio/RPC parity status: `pass`
- Envio event count: `98178`
- RPC event count: `98178`
- Events missing from Envio: `0`
- Events missing from RPC: `0`
- Checkpoint active orders: `43`
- Checkpoint matched levels: `37`
- Checkpoint missing levels: `0`
- Checkpoint extra levels: `0`
- Validated blocks: `7`
- Validated block numbers: `[109097028, 109098694, 109100361, 109102027, 109103694, 109105360, 109107027]`
- Maximum midpoint error: `0`
- Maximum price-level error: `0.0` (`0` raw)
- Maximum aggregate-size error: `0.0` (`0` raw)
- Queue-invariant checks: `98179`
- Queue-invariant failures: `0`
- Queue-invariant status: `pass`
- Cancellation-summary status: `pass`
- OrdersCanceled summary events: `23166`
- Summary nonzero order IDs: `36849`
- Individual OrderCanceled events: `36849`
- Summary order IDs applied as cancellations: `0`

Envio parity is `unavailable` when `ENVIO_API_TOKEN` is not set. This state is not a parity pass.

## Simulated passive order

- Side: `buy`
- Raw price: `2708500`
- Raw size: `2000000000000`
- Orders ahead: `1`
- Raw size ahead: `70108489704932`
- Raw filled size: `2000000000000`
- Placement transaction: `0xcda03bf8a0550119a64c1291f88c19c2f4acaac2e258742d09a16d557567d4fe`
- Final cash: `-5.417000000000001`
- Final inventory: `200.0`
- Final marked portfolio value: `0.10669999999999913`
- Final cumulative P&L: `0.10669999999999913`
- Gross P&L at the combined contract midpoint: `0.10669999999999913`

Fees and gas are not included.

## Artifacts

- Price validation chart: `reconstructed_prices.png`
- Error chart: `reconstruction_error.png`
- Final summary: `summary.json`
- Deterministic replay timeline: `timeline.jsonl`
- Validation rows: `validation_rows.json`
- Artifact manifest: `manifest.json`

No OHLC candles were generated. If candles are added later, their open, high, low, and close values must come only from `Trade` events.

