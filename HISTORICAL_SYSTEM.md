# General historical system

## Scope

The product interface uses a market address, a first block, a last block, and a Kuru LP configuration. A named regime is not required. The three old regime names remain as golden presets and regression fixtures only.

An interactive backtest reads local files only. It does not call Envio or RPC.

## Architecture

`historical_system.py` contains the reusable historical layer:

1. `HistoricalEventStore` verifies partition hashes and validation reports. It streams canonical events for one continuous block range.
2. `CheckpointStore` finds the latest verified checkpoint at or before the state block that is required for a run.
3. `ReconstructionEngine` copies that checkpoint and replays local events to the block before `from_block`.
4. `ReplayCursor` opens a new chronological stream for each engine pass. It does not keep the complete event range in memory.
5. `KuruLpBacktestEngine` adapts the cursor to the unchanged `kuru_lp.run_lp()` engine and selects a bounded market-frame sample rate.
6. `ValidationLayer` checks content hashes, Envio/RPC parity status, L2 error status, queue-invariant status, and checkpoint identity before the data is used.

`backtest_service.py` composes these objects with the official Kuru SDK grid adapter. A block-range request has this minimum identity:

```json
{
  "engine": "kuru_lp",
  "market": "0x065c9d28e428a0db40191a54d33d5b7c71a9c394",
  "from_block": 109100000,
  "to_block": 109102000,
  "shape": "spot"
}
```

The request also contains the LP range, fee, token amounts, simulation limits, and the exact SDK grid. The normalized result records the final absolute prices and the exact block range.

## Local index manifest

`historical_index_manifest.json` is the authority for interactive coverage. It records:

- the market and deployment block;
- event partition bounds, counts, paths, and SHA-256 values;
- validation report paths and SHA-256 values;
- checkpoint blocks, timestamps, paths, and SHA-256 values;
- the separate Trade-only chart scope.

A Trade-only region is not an L3 event partition. It cannot be submitted to the backtester until it has complete order-event history, a usable checkpoint, and a passing validation report.

The current complete validated L3 partitions are:

| From block | To block | Events | Golden fixture |
|---:|---:|---:|---|
| 109092028 | 109097027 | 49,175 | — |
| 109097028 | 109107027 | 98,178 | relatively_active |
| 109107028 | 109117027 | 76,871 | — |
| 109117028 | 109127027 | 96,656 | directional |
| 109127028 | 109132027 | 41,973 | — |
| 109132028 | 109142027 | 69,672 | relatively_quiet |

Together, these partitions give continuous validated L3 coverage from block 109092028 through block 109142027. Any interval inside these inclusive bounds is backtestable. The local Trade chart has the same current bounds.

The three added partitions passed event-for-event Envio/RPC parity, seven distributed L2 checks, queue invariants, and batch-cancellation checks. Their maximum price-level, aggregate-size, and midpoint errors are zero.

## Progressive indexing

Use a bounded, resumable staging request:

```bash
export ENVIO_API_TOKEN="your-token"
python index_history.py \
  --market 0x065c9d28e428a0db40191a54d33d5b7c71a9c394 \
  --from-block 109107028 \
  --to-block 109117027
```

The command uses one forward HyperSync cursor. It requests the market address, all required event topics, transaction index, log index, related block number, and timestamp. It saves the cursor before each request and rate-limit wait. It rejects duplicate positions and foreign market logs. After the event download, it captures the exact RPC checkpoint at `from_block - 1` and records its SHA-256 in the staging state.

The output stays under `.cache/historical_index_staging/` with status `downloaded_unvalidated`. This status is intentional. Do not add the range to `historical_index_manifest.json` until the existing validation runner has proved:

- event-for-event Envio/RPC parity;
- exact L2 matches at distributed blocks;
- zero price, aggregate-size, and midpoint error;
- queue invariants;
- correct batch-cancellation handling;
- the checkpoint content hash.

After those gates pass, promote the exact files:

```bash
python promote_historical_partition.py \
  --events path/to/source_events.jsonl \
  --validation-summary path/to/summary.json \
  --checkpoint path/to/checkpoint.json
```

The promotion command runs the saved validation gates again. It rejects a market mismatch, count mismatch, overlapping range, or checkpoint that is not `from_block - 1`. It then stores all content hashes in the manifest.

The command has a default 100,000-block safety limit. Use several adjacent bounded ranges for progressive coverage. Do not use `--allow-large-range` for an unreviewed lifetime request.

## Trade history chart

Generate the local chart artifact with:

```bash
python generate_trade_history_chart.py
```

The generator reads Trade events only. It keeps deterministic first, low, high, and last points in block buckets. It writes at most 1,000 points by default. The browser draws validated L3 segments in green and Trade-only regions in amber. The selected range is blue.

## Golden parity

Run:

```bash
python golden_regression.py
```

The script runs each golden range through both the old named wrapper and the generalized block-range path. It compares financial results, fill content, order counts, and the semantic timeline hash. The current report is `golden_regression_report.json`; all three fixtures pass with no differences.

## Lifetime size estimate

The known range from deployment block 43,660,999 through local chart block 109,142,027 contains 65,481,029 blocks. The three validated samples contain 264,506 events and 180,774,574 bytes of canonical JSONL across 30,000 blocks.

If that observed density continued, indexing through block 109,142,027 would require approximately:

- 577 million events and 395 GB at the three-sample mean;
- 456 million to 643 million events across the observed quiet-to-active density;
- 310 GB to 440 GB of uncompressed canonical JSONL across the observed size range.

This is a capacity estimate, not a download plan. Activity changes over time. Checkpoints, metadata, validation artifacts, temporary files, and indexes need more space. Columnar compression can reduce final storage. Blocks after 109,142,027 add to these values. Do not start the complete lifetime backfill from this estimate alone.
