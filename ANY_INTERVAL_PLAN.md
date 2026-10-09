# Plan: historical interval selection and on-demand L3 preparation

Status: proposed work, not an implemented download service. No lifetime scan was started during this review.

## Decision

Use hourly OHLC candles as a compact navigation chart. Build every candle from Trade events only. Download dense order-event history only when a user selects a range that is not already validated locally.

The first implementation remains scoped to MON-USDC. It does not add another strategy mode, another market, or wallet execution.

Do not run the LP engine on candles. Candles do not contain individual orders, queue priority, cancellations, partial fills, or flip-order links. An hour without trades does not prove that the order book was unchanged.

“Any interval” means any interval after market deployment for which complete events, historical state, and tested contract rules are available. A recent unfinalized interval, an unsupported contract version, unavailable archive state, or an empty reference book can still prevent a valid run. The UI must give the reason.

## Current state

- The interface already accepts market address, `from_block`, `to_block`, and LP configuration. Named ranges are optional presets.
- Validated L3 coverage is continuous from `109092028` through `109142027`.
- Six canonical partitions contain 432,525 events and 295,560,286 bytes of uncompressed event files, about 281.9 MiB. This excludes caches, checkpoints, results, and duplicate staging files.
- The local Trade chart has 3,291 trades and 494 downsampled points. It is not a lifetime chart or an OHLC series.
- `index_history.py` already has bounded downloads, cursor resume, market/topic filtering, rate-limit handling, and staging output.
- Validation and promotion exist as separate commands. There is no automatic preparation coordinator.
- Current LP checks support one tested historical implementation and reject active vault liquidity. Earlier or later contract versions need their own evidence before they can be promoted for LP runs.

## Data flow

```text
Trade-only history -> hourly candle catalog -> user selects UTC range
                                               |
                                     resolve exact block limits
                                               |
                                     check validated coverage
                                      /                  \
                                complete                missing
                                   |                       |
                                   |             bounded preparation job
                                   |              checkpoint + events
                                   |                  validation
                                   |               atomic promotion
                                   |                       |
                                   +---- exact start book--+
                                               |
                                   exact SDK preview + funding checks
                                               |
                                      local-only LP backtest
                                               |
                                   summary + bounded client replay
```

The network preparation job is separate from `BacktestService.run()`. The strategy worker continues to read only immutable local data. No server needs to record the live market continuously.

## Step 1: define coverage and preparation states

Add a read-only coverage catalog and a resumable preparation-job record. Use explicit states: missing, downloading, downloaded-unvalidated, validating, validated, failed, and canceled.

Keep Trade coverage separate from validated L3 coverage and LP-compatible coverage. A passed reconstruction check is not proof of compatibility with every historical LP contract version.

Use exact inclusive block limits in user requests. Convert the end to an exclusive `to_block + 1` for HyperSync. Save chain ID, market address, topic set, field set, schema version, block hashes, implementation hash, ABI version, and provider provenance. Record empty scanned ranges as covered only after a complete cursor pass.

## Step 2: build the Trade candle catalog

Use Envio HyperSync to scan target-market Trade logs in bounded, resumable chunks. Do not fetch dense order topics for this broad chart scan. Keep address validation and explicit related-block timestamps. Use the response `next_block` cursor, not one request for each overlapping window. Envio documents these filtering and pagination rules in its [query reference](https://docs.envio.dev/docs/HyperSync/hypersync-query).

Store the canonical Trade records, then build UTC hour buckets locally:

- Open is the first Trade price in chain order.
- High and low are Trade price extrema.
- Close is the final Trade price in chain order.
- Volume is executed base size, with raw values retained. Token-unit conversion must use the correct historical market parameters.
- Keep Trade count, first/last Trade blocks, and source hashes.

Keep raw integer prices and sizes until display conversion. Order records by block, transaction index, and log index. Trades with the same timestamp still have a deterministic order.

Show no-trade hours as gaps or explicit zero-volume records with null OHLC. Do not insert midpoint values or pretend that a carried price was a Trade. Mark missing download coverage separately from a known no-trade hour.

Derive daily candles from hourly candles for a wide view. Fetch hourly chart pages as the user zooms. Do not send a full lifetime Trade timeline to the browser. Keep raw Trade data so finer candles can be added later without another broad download.

First prove this on the existing 50,000-block data. An hourly-only chart is too coarse for its short validated intervals; retain exact block entry and a Trade-level zoom for those checks.

## Step 3: map selected times to exact blocks

The slider selects UTC start/end times. Resolve these to exact block boundaries through a local sparse block-time index. A candle's first or last Trade block is not the start or end of the hour.

If header data is missing, the preparation job can use narrow block-header lookups or a binary search. Save these results. Do not issue network requests for each slider movement. Define the selection as a half-open time range, with explicit handling of multiple blocks sharing a timestamp.

Freeze the target range at job creation. Resolve the provider's available and finalized end once. Do not keep moving the target as new blocks arrive. Display both UTC times and final inclusive block limits.

## Step 4: plan only missing L3 work

Subtract validated local coverage from the requested range. Reuse existing partitions. Merge overlapping pending requests so two jobs do not download the same market blocks twice.

The starting state is essential. Fetching only events inside the selected range is not sufficient: orders may have been placed before it.

Use one of these paths:

1. A verified checkpoint at `from_block - 1`: replay the selected range directly.
2. An earlier verified checkpoint: fetch any missing warm-up events from checkpoint + 1 through `from_block - 1`, then replay the selected range.
3. No usable local checkpoint: use archive RPC to capture the complete L3 state at `from_block - 1` with the existing contract readers, then validate it.

If archive RPC cannot supply a complete checkpoint, offer a bounded reconstruction from an earlier supported checkpoint or deployment. Show the larger work range and obtain confirmation before starting it. Do not assume that unknown pre-existing orders can be omitted. Do not use a checkpoint after the start or use an L2 snapshot as an L3 checkpoint.

## Step 5: run bounded preparation

Wrap the existing indexing, validation, and promotion components. Keep one download worker initially to reduce 429 responses and disk pressure.

Show stages, durable cursor, downloaded event count, completed chunks, validation progress, and the current failure reason. Honor Retry-After/reset headers and retain the current bounded backoff. Cancellation leaves reusable staging data but adds no validated coverage.

Use limits for blocks, bytes, elapsed time, and checkpoint order count. Estimate work from local density when possible, and label estimates as uncertain. This 50,000-block sample is not a reliable lifetime storage forecast.

A page must be committed atomically. The current append-only staging file needs an explicit committed byte offset or atomic page files before robust power-loss recovery can be claimed. Detect incomplete trailing records and conflicting duplicates. Do not silently delete malformed raw records.

## Step 6: validate before promotion

Reuse the current gates: Envio/RPC event parity, checkpoint integrity, queue invariants, batch-cancellation checks, L2 comparisons, midpoint error, and content hashes. Select distributed checks including both bounds; a short range may have fewer than seven distinct blocks.

Capture market parameters and implementation code at the actual checkpoint and required version boundaries. Check market-state, fee, upgrade, and vault changes. Filtered known topics alone cannot prove that no new state-changing event exists. Add a bounded topic census or a reviewed unknown-topic policy during preparation.

Require price-level, aggregate-size, and midpoint errors to be zero. An unsupported implementation or active vault path stays unavailable until corresponding tests pass. Never apply the current implementation's rules silently to an older deployment.

Promote immutable partitions and checkpoints, then write coverage metadata last. Use a manifest revision or lock so concurrent preparation cannot lose another job's promoted range. Save block hashes and invalidate affected coverage after a reorganization.

RPC verification may be the slowest stage. Measure it before changing the gates. A download is not a passed validation.

## Step 7: connect the UI without a false preview

For a missing interval, show “Prepare historical data” with a bounded work estimate. Keep Run Backtest disabled until preparation and LP compatibility checks pass.

Once the start book is available, use its pre-interval midpoint to rebuild the exact SDK preview and required deposits. An hourly Trade close must not substitute for that reference. Recheck the user's amounts and show any shortage. Do not silently change deposits or range settings.

The first version should use Prepare, then Run Backtest. A later combined button can coordinate both stages, but it must still show the final exact preview and require valid funding before simulation.

On a repeated request, show a local-data cache hit and skip network preparation. Strategy-result reuse still requires the complete configuration, data hashes, contract rules, SDK grid, and engine version to match.

## Step 8: make longer local runs efficient

Keep canonical event partitions authoritative. Add a derived, hash-keyed block-offset index or columnar row-group index so a small interval does not scan a whole JSONL partition from the start. Store counts and timestamps with that index. Prove event-for-event equality before using it.

Reuse warm SDK workers and verified checkpoints. Profile repeated event passes, transaction-budget calculations, and timestamp loading. Preserve the existing streaming cursor, result-cache identity, and bounded compact replay. Do not create a full per-event UI timeline for a month-long run.

Time-in-range and capital utilization currently use activity samples, not elapsed-time weighting. Correct or relabel these metrics in a separate reporting change before comparing long intervals with different event rates. This must not change fills or cash/inventory accounting.

## Acceptance gates and order of work

1. Hourly candle tests on existing local Trades: OHLC, raw volume, ties, gaps, and aggregation.
2. Time-to-block tests: exact boundaries, repeated timestamps, finalized limits, and missing headers.
3. Preparation tests: local hit, missing gap, overlapping requests, resume, 429, cancellation, partial writes, and bounded failure.
4. Checkpoint tests: exact start, earlier warm-up, missing archive state, and empty/one-sided reference book.
5. Validation tests: bad parity, failed L2 checks, unknown contract version, active vault, reorganization, and no promotion on failure.
6. End-to-end test: select one small missing interval, prepare it, show its exact SDK grid, run locally, then repeat with no network access.
7. Require unchanged LP results and hashes for all three current fixtures after each infrastructure change.

Start with the candle schema and local generator. Then add the preparation coordinator for one bounded missing interval. Expand Trade chart coverage only through explicit bounded jobs. Do not begin a complete lifetime L3 backfill as part of this plan.
