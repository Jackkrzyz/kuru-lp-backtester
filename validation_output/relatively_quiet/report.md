# Reconstruction Validation Report

Market: `0x065c9d28e428a0db40191a54d33d5b7c71a9c394`

Interval: `109132028` through `109142027`, inclusive.

Named interval: `relatively_quiet`

Discovered executed volume: `1087143.8640440903` MON (`10871438640440903` raw)

## Numerical results

- Event source: `envio_hypersync_cache_verified_by_rpc`
- Envio/RPC parity status: `pass`
- Envio event count: `69672`
- RPC event count: `69672`
- Events missing from Envio: `0`
- Events missing from RPC: `0`
- Checkpoint active orders: `43`
- Checkpoint matched levels: `38`
- Checkpoint missing levels: `0`
- Checkpoint extra levels: `0`
- Validated blocks: `7`
- Validated block numbers: `[109132028, 109133694, 109135361, 109137027, 109138694, 109140360, 109142027]`
- Maximum midpoint error: `0`
- Maximum price-level error: `0.0` (`0` raw)
- Maximum aggregate-size error: `0.0` (`0` raw)
- Queue-invariant checks: `69673`
- Queue-invariant failures: `0`
- Queue-invariant status: `pass`
- Cancellation-summary status: `pass`
- OrdersCanceled summary events: `17873`
- Summary nonzero order IDs: `25724`
- Individual OrderCanceled events: `25724`
- Summary order IDs applied as cancellations: `0`

Envio parity is `unavailable` when `ENVIO_API_TOKEN` is not set. This state is not a parity pass.

## Simulated passive order

- Side: `sell`
- Raw price: `2725900`
- Raw size: `2000000000000`
- Orders ahead: `1`
- Raw size ahead: `69967597915060`
- Raw filled size: `2000000000000`
- Placement transaction: `0x0fbba1cc7db7022a3dfd1438754beb90c69736d887e81db2b5b50d238dc4a6cd`
- Final cash: `5.4517999999999995`
- Final inventory: `-200.0`
- Final marked portfolio value: `0.04439999999999955`
- Final cumulative P&L: `0.04439999999999955`
- Gross P&L at the combined contract midpoint: `0.04439999999999955`

Fees and gas are not included.

## Artifacts

- Price validation chart: `reconstructed_prices.png`
- Error chart: `reconstruction_error.png`
- Final summary: `summary.json`
- Deterministic replay timeline: `timeline.jsonl`
- Validation rows: `validation_rows.json`
- Artifact manifest: `manifest.json`

No OHLC candles were generated. If candles are added later, their open, high, low, and close values must come only from `Trade` events.

